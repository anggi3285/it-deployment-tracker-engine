import os
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL")


@contextmanager
def get_conn():
    """Commit otomatis jika blok sukses, rollback otomatis jika ada exception."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn
