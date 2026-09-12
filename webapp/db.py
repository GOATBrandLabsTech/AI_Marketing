import os
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras

DEFAULT_CRED_FILE = str(
    Path.home() / "Documents" / "Python_Scripts" / "Voylla_Cred.txt"
)


def _load_cred_file(path):
    with open(path, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    keys = ["host", "user", "password", "dbname", "port"]
    if len(lines) < len(keys):
        raise RuntimeError(f"Cred file {path} does not have {len(keys)} lines")
    return dict(zip(keys, lines))


def get_db_config():
    if os.environ.get("DB_HOST"):
        return {
            "host": os.environ["DB_HOST"],
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
            "dbname": os.environ.get("DB_NAME", "gbl_data_lake"),
            "port": os.environ.get("DB_PORT", "5432"),
        }
    cred_path = os.environ.get("WEBAPP_CRED_FILE", DEFAULT_CRED_FILE)
    return _load_cred_file(cred_path)


@contextmanager
def get_conn():
    cfg = get_db_config()
    conn = psycopg2.connect(
        host=cfg["host"],
        user=cfg["user"],
        password=cfg["password"],
        dbname=cfg["dbname"],
        port=int(cfg["port"]),
        connect_timeout=10,
    )
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_cursor(commit=False):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
