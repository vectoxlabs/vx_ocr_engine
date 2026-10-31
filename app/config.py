import os
import re
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config.yaml'


def load_config():
    config = yaml.safe_load(CONFIG_PATH.read_text())
    def resolve(value):
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if isinstance(value, str):
            return re.sub(r'\$\{([A-Z_][A-Z_0-9]*)\}', lambda m: os.getenv(m[1], ''), value)
        return value
    config = resolve(config)
    Draft202012Validator.check_schema(config['schema'])
    p = config['provider']
    for key in ('request_timeout_seconds', 'total_timeout_seconds', 'max_output_tokens'):
        if p[key] <= 0:
            raise ValueError(f'provider.{key} must be positive')
    if p['retry_count'] < 0 or p['retry_delay_seconds'] < 0 or p['retry_backoff_multiplier'] < 1 or p['max_retry_delay_seconds'] < 0:
        raise ValueError('Invalid retry configuration')
    if p['default'] not in ('openai', 'gemini'):
        raise ValueError('Unknown default provider')
    return config
