"""
Minimal connection helper around psycopg3. Kept intentionally thin —
swap for SQLAlchemy later if the project grows past a few tables.
"""
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from config import DB


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(DB.dsn, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(schema_path: str = "db/schema.sql") -> None:
    """
    Apply schema.sql to the database.
    """
    with open(schema_path, "r") as f:
        sql = f.read()

    with get_conn() as conn:
        conn.execute(sql)

    print(f"Schema applied from {schema_path}.")


if __name__ == "__main__":
    import sys
    schema = sys.argv[1] if len(sys.argv) > 1 else "db/schema.sql"
    init_schema(schema)
