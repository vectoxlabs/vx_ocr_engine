import asyncio
import base64
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from .errors import OCRException


@dataclass
class ProviderResult:
    extraction: dict
    token_usage: dict
    latency_ms: float
    attempts: int


class Adapter:
    def __init__(self, name, config, client):
        self.name = name
        self.config = config
        self.settings = config['provider']
        self.provider = self.settings[name]
        self.client = client

    def request(self, data, mime):
        p, s = self.provider, self.settings
        encoded = base64.b64encode(data).decode('ascii')
        prompt, schema = self.config['prompt']['text'], self.config['schema']
        if self.name == 'openai':
            return p['endpoint'], {'Authorization': f"Bearer {p['api_key']}"}, {
                'model': p['model'], 'temperature': s['temperature'],
                'max_completion_tokens': s['max_output_tokens'],
                'messages': [{'role': 'user', 'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{encoded}', 'detail': p['image_detail']}}
                ]}],
                'response_format': {'type': 'json_schema', 'json_schema': {
                    'name': p['schema_name'], 'strict': True, 'schema': schema}}
            }
        return p['endpoint'].format(model=quote(p['model'], safe='')), {'x-goog-api-key': p['api_key']}, {
            'contents': [{'role': 'user', 'parts': [{'text': prompt}, {'inline_data': {'mime_type': mime, 'data': encoded}}]}],
            'generationConfig': {'temperature': s['temperature'], 'maxOutputTokens': s['max_output_tokens'],
                                 'responseMimeType': 'application/json', 'responseJsonSchema': schema}
        }

    def parse(self, body):
        if self.name == 'openai':
            candidate = body['choices'][0]
            if candidate['message'].get('refusal'):
                raise OCRException(502, 'provider_refusal', 'Provider declined extraction.')
            if candidate['finish_reason'] != self.provider['finish_reason']:
                raise OCRException(502, 'incomplete_output', 'Provider output was incomplete.')
            content = candidate['message']['content']
            usage = body.get('usage', {})
            tokens = {'input_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
                      'total_tokens': usage.get('total_tokens'), 'provider_usage': usage}
        else:
            candidate = body['candidates'][0]
            if candidate.get('finishReason') != self.provider['finish_reason']:
                raise OCRException(502, 'incomplete_output', 'Provider output was blocked or incomplete.')
            content = ''.join(part.get('text', '') for part in candidate['content']['parts'] if not part.get('thought'))
            usage = body.get('usageMetadata', {})
            tokens = {'input_tokens': usage.get('promptTokenCount'), 'output_tokens': usage.get('candidatesTokenCount'),
                      'total_tokens': usage.get('totalTokenCount'), 'provider_usage': usage}
        return json.loads(content), tokens

    async def extract(self, data, mime):
        if not self.provider['api_key']:
            raise OCRException(503, 'provider_not_configured', 'Provider API key is not configured.')
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.settings['total_timeout_seconds']):
                return await self._extract(data, mime, started)
        except TimeoutError as exc:
            raise OCRException(504, 'provider_timeout', 'Provider time budget exhausted.') from exc

    async def _extract(self, data, mime, started):
        url, headers, payload = self.request(data, mime)
        attempts = self.settings['retry_count'] + 1
        for attempt in range(attempts):
            retry_after = None
            try:
                async with asyncio.timeout(self.settings['request_timeout_seconds']):
                    response = await self.client.post(url, headers=headers, json=payload,
                                                      timeout=self.settings['request_timeout_seconds'])
                if response.is_success:
                    try:
                        extraction, usage = self.parse(response.json())
                    except (KeyError, IndexError, TypeError, ValueError) as exc:
                        raise OCRException(502, 'invalid_provider_response', 'Provider returned an invalid structured response.') from exc
                    return ProviderResult(extraction, usage, (time.perf_counter() - started) * self.config['metrics']['milliseconds_per_second'], attempt + 1)
                if response.status_code not in self.settings['retry_status_codes']:
                    raise OCRException(502, 'provider_error', f'Provider rejected the request (HTTP {response.status_code}).')
                raw_retry = response.headers.get('retry-after')
                if raw_retry:
                    try:
                        retry_after = float(raw_retry)
                    except ValueError:
                        try:
                            retry_after = (parsedate_to_datetime(raw_retry) - datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            pass
                failure = OCRException(502, 'provider_unavailable', f'Provider failed after retries (HTTP {response.status_code}).')
            except (httpx.TimeoutException, TimeoutError):
                failure = OCRException(504, 'provider_timeout', 'Provider request timed out after retries.')
            except httpx.RequestError:
                failure = OCRException(502, 'provider_connection_error', 'Unable to contact provider after retries.')
            if attempt + 1 == attempts:
                raise failure
            delay = self.settings['retry_delay_seconds'] * self.settings['retry_backoff_multiplier'] ** attempt
            delay = min(max(delay, retry_after or 0), self.settings['max_retry_delay_seconds'])
            await asyncio.sleep(delay)
