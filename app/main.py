import asyncio
import hashlib
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .config import load_config
from .errors import OCRException
from .extraction import validate_extraction
from .images import isThisImageAPrescription, read_path, validate_image
from .providers import Adapter
from .storage import Storage


class RequestLimits:
    """Bound the actual multipart body, even when Content-Length is absent or false."""
    def __init__(self, app, config):
        self.app = app
        self.config = config
        self.slots = asyncio.Semaphore(config['max_concurrent_requests'])

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] != 'POST':
            return await self.app(scope, receive, send)
        try:
            await asyncio.wait_for(self.slots.acquire(), self.config['queue_timeout_seconds'])
        except TimeoutError:
            return await JSONResponse(status_code=503, content={'status': 'error', 'error': {'code': 'busy', 'message': 'Request capacity exhausted.'}})(scope, receive, send)
        try:
            body = bytearray()
            while True:
                try:
                    message = await asyncio.wait_for(receive(), self.config['body_timeout_seconds'])
                except TimeoutError:
                    return await JSONResponse(status_code=408, content={'status': 'error', 'error': {'code': 'body_timeout', 'message': 'Request body timed out.'}})(scope, receive, send)
                if message['type'] == 'http.disconnect':
                    return
                body.extend(message.get('body', b''))
                if len(body) > self.config['max_request_bytes']:
                    return await JSONResponse(status_code=413, content={'status': 'error', 'error': {'code': 'request_too_large', 'message': 'Multipart request exceeds configured limit.'}})(scope, receive, send)
                if not message.get('more_body', False):
                    break
            delivered = False
            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()
            await self.app(scope, replay, send)
        finally:
            self.slots.release()


def create_app(config=None):
    config = config or load_config()
    server = config['server']

    @asynccontextmanager
    async def lifespan(app):
        storage = Storage(config['database'])
        app.state.storage = storage
        if storage.pool:
            await storage.pool.open(wait=False)
        async with httpx.AsyncClient() as client:
            app.state.providers = {name: Adapter(name, config, client) for name in ('openai', 'gemini')}
            try:
                yield
            finally:
                if storage.pool:
                    await storage.pool.close()

    app = FastAPI(title=server['title'], version=server['version'], docs_url=server['docs_url'],
                  redoc_url=server['redoc_url'], openapi_url=server['openapi_url'], lifespan=lifespan,root_path="/ocr-engine")
    app.add_middleware(RequestLimits, config=config['api'])

    @app.exception_handler(OCRException)
    async def ocr_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={'status': 'error', 'error': {'code': exc.code, 'message': exc.message}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo raw uploaded prescription bytes or submitted patient data in errors.
        return JSONResponse(status_code=422, content={'status': 'error', 'error': {'code': 'invalid_request', 'message': 'Supply multipart type=image with image, or type=path with path; provider must be gemini or openai.'}})

    response_schema = {'type': 'object', 'properties': {
        'status': {'type': 'string', 'enum': ['success']}, 'id': {'type': 'string', 'format': 'uuid'},
        'extraction': config['schema'], 'metadata': {'type': 'object'}}}

    @app.post(config['api']['endpoint'], summary='Extract and store a medical prescription',
              responses={200: {'content': {'application/json': {'schema': response_schema}}},
                         413: {'description': 'Image or request too large'}, 415: {'description': 'Unsupported image format'},
                         422: {'description': 'Invalid image or input combination'}, 502: {'description': 'Provider or output error'},
                         503: {'description': 'Database/provider unavailable or service busy'}, 504: {'description': 'Provider timeout'}},
              description='Image upload or server-local path, without authentication. Both fields may be supplied; type selects the source. Original image bytes are sent unchanged. Success means PostgreSQL committed the extraction. Confidence percentages are model estimates, not measured accuracy.')
    async def extract(request: Request,
                      type: Annotated[Literal['image', 'path'], Form(description='Select the image source.')],
                      image: Annotated[UploadFile | None, File(description='Original image file.')] = None,
                      path: Annotated[str | None, Form(description='Server-local image path within configured allowed roots.')] = None,
                      provider: Annotated[Literal['gemini', 'openai'] | None, Form()] = None):
        started = time.perf_counter()
        selected = provider or config['provider']['default']
        try:
            if type == 'image':
                if image is None:
                    raise OCRException(422, 'image_required', 'type=image requires an image upload.')
                data = bytearray()
                while chunk := await image.read(config['image']['read_chunk_bytes']):
                    data.extend(chunk)
                    if len(data) > config['image']['max_bytes']:
                        raise OCRException(413, 'image_too_large', 'Image exceeds configured byte limit.')
                data = bytes(data)
            else:
                if not path:
                    raise OCRException(422, 'path_required', 'type=path requires an image path.')
                data = await run_in_threadpool(read_path, path, config['image'])
            mime = await run_in_threadpool(validate_image, data, config['image'])
            if not isThisImageAPrescription(data):
                raise OCRException(422, 'not_prescription', 'Image is not a prescription.')
            # Fail before a paid provider call if PostgreSQL cannot be reached.
            await request.app.state.storage.check()
            result = await request.app.state.providers[selected].extract(data, mime)
            extraction = validate_extraction(result.extraction, config)
            record = {'id': str(uuid.uuid4()), 'source_type': type, 'image_sha256': hashlib.sha256(data).hexdigest(),
                      'image_mime': mime, 'provider': selected, 'model': config['provider'][selected]['model'],
                      'prompt_version': config['prompt']['version'], 'extraction': extraction,
                      'token_usage': result.token_usage, 'latency_ms': result.latency_ms, 'attempts': result.attempts}
            await request.app.state.storage.save(record)
            return {'status': 'success', 'id': record['id'], 'extraction': extraction,
                    'metadata': {'provider': selected, 'model': record['model'], 'prompt_version': record['prompt_version'],
                                 'token_usage': result.token_usage, 'provider_latency_ms': result.latency_ms,
                                 'total_latency_ms': (time.perf_counter() - started) * config['metrics']['milliseconds_per_second'], 'attempts': result.attempts,
                                 'storage_status': 'stored'}}
        finally:
            if image is not None:
                await image.close()

    return app


app = create_app()
