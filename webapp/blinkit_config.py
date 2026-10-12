"""
Blinkit city master: city id -> city name, read from Blinkit's own campaign
config (GET /adservice/v3/campaigns/config). Campaigns store their targeting
as city ids (region_ids); this table turns them into names, which then join to
warehouses through voylla."QC_City_Mappping".

Called by the push job right after its Blinkit login. Read-only on Blinkit's
side. The login token is only sent as a request header - it is never stored.
"""
from sqlalchemy import text

CONFIG_URL = "https://brands.blinkit.com/adservice/v3/campaigns/config"
ID_KEYS = ("city_id", "id", "value")
NAME_KEYS = ("city_name", "name", "label", "display_name")


def parse_city_config(payload):
    """Finds every {id, name} item in a list that sits under a key containing
    'cit' (cities, city_list, cityOptions ...), wherever it is in the reply.
    Returns [(city_id, city_name)] without duplicates."""
    found = {}

    def is_city(d):
        return isinstance(d, dict) and any(k in d for k in ID_KEYS) and any(k in d for k in NAME_KEYS)

    def walk(node, under_city_key=False):
        if isinstance(node, dict):
            if under_city_key and is_city(node):
                cid = next(node[k] for k in ID_KEYS if k in node)
                name = next(node[k] for k in NAME_KEYS if k in node)
                if cid not in (None, "") and name:
                    found.setdefault(str(cid).strip(), str(name).strip())
            for k, v in node.items():
                walk(v, under_city_key or "cit" in str(k).lower())
        elif isinstance(node, list):
            for v in node:
                walk(v, under_city_key)

    walk(payload)
    return sorted(found.items(), key=lambda kv: kv[1])


def save_city_master(engine, cities):
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS voylla.blinkit_city_master (
                city_id text PRIMARY KEY, city_name text, fetched_at timestamp DEFAULT NOW())"""))
        for cid, name in cities:
            conn.execute(text("""
                INSERT INTO voylla.blinkit_city_master (city_id, city_name, fetched_at)
                VALUES (:i, :n, NOW())
                ON CONFLICT (city_id) DO UPDATE SET city_name = EXCLUDED.city_name, fetched_at = NOW()"""),
                {"i": cid, "n": name})
    return len(cities)


def fetch_and_store_city_master(engine, token, cookies, timeout=30):
    """One GET with the push job's live login; stores only the city list."""
    import requests
    headers = {"accept": "application/json, text/plain, */*", "origin": "https://brands.blinkit.com",
               "referer": "https://brands.blinkit.com/diy/campaigns", "user-agent": "Mozilla/5.0",
               "firebase_user_token": token}
    resp = requests.get(CONFIG_URL, headers=headers, cookies=cookies, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    cities = parse_city_config(payload)
    if not cities:
        keys = list(payload.keys())[:30] if isinstance(payload, dict) else type(payload).__name__
        raise RuntimeError(f"no cities found in the config reply; top-level keys: {keys}")
    return save_city_master(engine, cities)
