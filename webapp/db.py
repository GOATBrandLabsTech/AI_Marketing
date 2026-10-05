import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

# Hardcoded, not Path.home()-derived: Path.home() reads USERPROFILE, which in
# some Jupyter kernels on this machine resolves to a different (stale/wrong)
# profile than the one these files actually live under - the same traditional
# hardcoded-FL-location convention every other script in this folder already
# uses, for exactly that reason.
PYTHON_SCRIPTS_DIR = r"C:\Users\Amit Singh\Documents\Python_Scripts"
DEFAULT_CRED_FILE = PYTHON_SCRIPTS_DIR + r"\Voylla_Cred.txt"
DEFAULT_ANTHROPIC_KEY_FILE = PYTHON_SCRIPTS_DIR + r"\Claude_api_key.txt"


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


def get_anthropic_config():
    """Reuses the same key already used by the batch-decision notebooks
    (Blinkit_actions_llm_marketing_Batch_Api.ipynb) - Claude_api_key.txt is
    two lines: model name, then the API key."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {
            "api_key": os.environ["ANTHROPIC_API_KEY"],
            "model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        }
    key_path = os.environ.get("ANTHROPIC_KEY_FILE", DEFAULT_ANTHROPIC_KEY_FILE)
    with open(key_path, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"{key_path} should have 2 lines: model, then API key")
    return {"model": lines[0], "api_key": lines[1]}


def get_engine():
    """SQLAlchemy engine for pandas-based aggregation (ondemand_engine.py) -
    same connection details as get_conn(), different driver interface."""
    from sqlalchemy import create_engine

    cfg = get_db_config()
    url = (
        f"postgresql+psycopg2://{cfg['user']}:{cfg['password']}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['dbname']}"
    )
    return create_engine(url)


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
