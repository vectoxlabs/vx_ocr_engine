"""Print configured PostgreSQL DDL; never connect to or modify a database."""
from app.config import load_config

if __name__ == '__main__':
    print(load_config()['database']['create_sql'])
