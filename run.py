import uvicorn
from app.config import load_config

if __name__ == '__main__':
    server = load_config()['server']
    uvicorn.run('app.main:app', host=server['host'], port=server['port'], log_level=server['log_level'])
