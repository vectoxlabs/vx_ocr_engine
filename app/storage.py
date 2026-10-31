from .errors import OCRException


class Storage:
    def __init__(self, config):
        self.config = config
        self.pool = None
        if config['dsn']:
            from psycopg_pool import AsyncConnectionPool
            self.pool = AsyncConnectionPool(
                config['dsn'], open=False, min_size=config['pool_min_size'], max_size=config['pool_max_size'],
                timeout=config['pool_timeout_seconds'],
                kwargs={'connect_timeout': config['connect_timeout_seconds'],
                        'options': f"-c statement_timeout={config['statement_timeout_ms']}"})

    async def check(self):
        if self.pool is None:
            raise OCRException(503, 'database_not_configured', 'DATABASE_URL is not configured.')
        try:
            async with self.pool.connection() as connection:
                await connection.execute(self.config['probe_sql'])
        except Exception as exc:
            raise OCRException(503, 'database_unavailable', 'PostgreSQL is unavailable.') from exc

    async def save(self, record):
        from psycopg.types.json import Jsonb
        try:
            async with self.pool.connection() as connection:
                await connection.execute(self.config['insert_sql'], (
                    record['id'], record['source_type'], record['image_sha256'], record['image_mime'],
                    record['provider'], record['model'], record['prompt_version'], Jsonb(record['extraction']),
                    Jsonb(record['token_usage']), record['latency_ms'], record['attempts']))
        except Exception as exc:
            raise OCRException(503, 'storage_failed', 'Extraction could not be committed to PostgreSQL; no success was returned.') from exc
