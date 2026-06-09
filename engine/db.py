"""
Database connection manager for MySQL, PostgreSQL, and SQLite.
"""

import os
import time
import sqlite3
import logging

logger = logging.getLogger("db")

_connection = None
_db_type = None


def get_db_type() -> str:
    return os.environ.get('DB_TYPE', 'sqlite')


def quote_identifier(name: str, db_type: str = None) -> str:
    """Quote a SQL identifier (column/table name) for safe embedding in SQL strings.

    MySQL uses backticks, PostgreSQL and SQLite use double quotes.
    Prevents identifier injection when column/table names come from mapping files.
    """
    if db_type is None:
        db_type = get_db_type()

    # Already quoted — don't double-quote
    stripped = name.strip()
    if stripped.startswith('`') and stripped.endswith('`'):
        return stripped
    if stripped.startswith('"') and stripped.endswith('"'):
        return stripped

    if db_type == 'mysql':
        return f'`{name}`'
    else:
        return f'"{name}"'


def get_connection():
    """Return a database connection based on the configured DB_TYPE."""
    global _connection, _db_type
    if _connection is not None:
        return _connection

    _db_type = get_db_type()

    if _db_type == 'sqlite':
        db_path = os.environ.get('DB_PATH', 'data/salary.db')

        # Open in read-only mode via URI to avoid WAL/journal file issues
        # on Docker Windows volume mounts. Retry up to 10 times if table missing.
        for attempt in range(10):
            try:
                # Use URI mode with readonly flag — avoids creating -wal/-shm files
                uri = f"file:{db_path}?mode=ro"
                _connection = sqlite3.connect(uri, uri=True)
                _connection.row_factory = sqlite3.Row
                # Verify schema is accessible
                _connection.execute("SELECT 1 FROM salary LIMIT 1")
                logger.info(f"Connected to SQLite (read-only): {db_path}")
                break
            except Exception as e:
                if _connection:
                    try:
                        _connection.close()
                    except Exception:
                        pass
                    _connection = None
                if attempt < 9:
                    logger.warning(f"SQLite connection attempt {attempt+1}/10: {e} — retrying in 2s...")
                    time.sleep(2)
                else:
                    raise RuntimeError(f"Failed to connect to SQLite after 10 attempts: {e}")

    elif _db_type == 'mysql':
        import mysql.connector
        _connection = mysql.connector.connect(
            host=os.environ.get('DB_HOST', 'localhost'),
            port=int(os.environ.get('DB_PORT', '3306')),
            database=os.environ.get('DB_NAME', 'talent'),
            user=os.environ.get('DB_USER', 'root'),
            password=os.environ.get('DB_PASSWORD', ''),
        )
        logger.info(f"Connected to MySQL: {os.environ.get('DB_HOST')}")

    elif _db_type == 'postgresql':
        import psycopg2
        import psycopg2.extras
        _connection = psycopg2.connect(
            host=os.environ.get('DB_HOST', 'localhost'),
            port=int(os.environ.get('DB_PORT', '5432')),
            dbname=os.environ.get('DB_NAME', 'overseas'),
            user=os.environ.get('DB_USER', 'postgres'),
            password=os.environ.get('DB_PASSWORD', ''),
        )
        # Use RealDictCursor for dict-like access
        _connection.cursor_factory = psycopg2.extras.RealDictCursor
        logger.info(f"Connected to PostgreSQL: {os.environ.get('DB_HOST')}")

    else:
        raise ValueError(f"Unsupported DB_TYPE: {_db_type}")

    return _connection


def execute_query(sql: str, params: tuple = ()):
    """Execute a query and return all results as a list of dicts."""
    conn = get_connection()
    db_type = get_db_type()

    if db_type == 'sqlite':
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]
    elif db_type == 'mysql':
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        return cur.fetchall()
    elif db_type == 'postgresql':
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [dict(row) for row in rows]


def execute_scalar(sql: str, params: tuple = ()):
    """Execute a query and return a single scalar value."""
    conn = get_connection()
    db_type = get_db_type()

    if db_type == 'sqlite':
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None
    elif db_type == 'mysql':
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None
    elif db_type == 'postgresql':
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        # RealDictCursor returns dict-like rows; get first value
        if row:
            return next(iter(row.values()), None)
        return None
