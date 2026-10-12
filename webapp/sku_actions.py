"""
SKU actions a human asks for from the dashboard - for now one kind: take a
product out of a Blinkit campaign (e.g. it is advertised while out of stock).

The dashboard only QUEUES the request (voylla.sku_campaign_removals). The
dashboard server cannot log in to Blinkit; the push job on the office PC
(jobs/Blinkit_Ondemand_Bid_Push.ipynb) applies pending removals by sending
the campaign's full product list without the removed products, the same
UPDATE request it already sends for bids.

Rules that protect money:
- only a product that is in the campaign's newest load can be queued;
- a campaign is never left with no products (refused, not sent);
- once removed, later bid pushes for that campaign keep it out until a newer
  campaign load shows it back (someone re-added it on Blinkit).
"""
from sqlalchemy import text

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS voylla.sku_campaign_removals (
    id serial PRIMARY KEY, brand text, campaign_id text, product_id text, sku text,
    reason text, status text DEFAULT 'pending', requested_by text,
    requested_at timestamp DEFAULT NOW(), pushed_at timestamp, push_note text)
"""


def ensure_table(engine):
    with engine.begin() as conn:
        conn.execute(text(TABLE_SQL))


def valid_requests(items, rows):
    """Keeps only (campaign_id, product_id) pairs that the SKU snapshot says are
    live right now. items: [{"campaign_id", "pid"}]; rows: sku_intel.sku_snapshot."""
    live = {}
    for r in rows:
        for cid in str(r.get("campaign_ids") or "").split(","):
            if cid.strip():
                live[(cid.strip(), str(r.get("pid")))] = r.get("sku")
    ok, rejected = [], []
    for it in items:
        key = (str(it.get("campaign_id") or "").strip(), str(it.get("pid") or "").strip())
        (ok if key in live else rejected).append({"campaign_id": key[0], "pid": key[1], "sku": live.get(key)})
    return ok, rejected


def queue_removals(engine, brand, items, requested_by=None, reason=None):
    """Adds pending removals, skipping pairs that already have one pending."""
    ensure_table(engine)
    added = 0
    with engine.begin() as conn:
        for it in items:
            exists = conn.execute(text("""
                SELECT 1 FROM voylla.sku_campaign_removals
                WHERE brand = :b AND campaign_id = :c AND product_id = :p AND status = 'pending'"""),
                {"b": brand, "c": it["campaign_id"], "p": it["pid"]}).first()
            if exists:
                continue
            conn.execute(text("""
                INSERT INTO voylla.sku_campaign_removals (brand, campaign_id, product_id, sku, reason, requested_by)
                VALUES (:b, :c, :p, :s, :r, :u)"""),
                {"b": brand, "c": it["campaign_id"], "p": it["pid"], "s": it.get("sku"),
                 "r": reason, "u": requested_by})
            added += 1
    return added


def cancel_removal(engine, removal_id):
    with engine.begin() as conn:
        n = conn.execute(text("""UPDATE voylla.sku_campaign_removals SET status = 'cancelled'
                                 WHERE id = :i AND status = 'pending'"""), {"i": int(removal_id)}).rowcount
    return n == 1


def open_removals(engine, brand):
    """Pending ones, plus the last 7 days of done/failed, keyed (campaign_id, product_id)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT DISTINCT ON (campaign_id, product_id) id, campaign_id, product_id, status, push_note, requested_at
                FROM voylla.sku_campaign_removals
                WHERE brand = :b AND (status = 'pending' OR requested_at >= NOW() - INTERVAL '7 days')
                  AND status <> 'cancelled'
                ORDER BY campaign_id, product_id, requested_at DESC"""), {"b": brand}).mappings().all()
    except Exception:  # table not created yet
        return {}
    return {(r["campaign_id"], r["product_id"]): dict(r) for r in rows}


def products_after_removal(product_ids, remove_ids):
    """The product list to send. Returns (kept, error); error is set (and the
    campaign must not be sent) when nothing would be left."""
    remove = {str(p) for p in remove_ids}
    kept = [p for p in product_ids if str(p) not in remove]
    if product_ids and not kept:
        return [], "refused: removing these would leave the campaign with no products"
    return kept, None


def removals_to_apply(engine, brand, campaign_id, snapshot_time):
    """Product ids to leave out of this campaign's next push: pending removals,
    plus done ones pushed after the newest campaign load (that load still shows
    the product, so without this a bid push would quietly add it back)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, product_id, status FROM voylla.sku_campaign_removals
                WHERE brand = :b AND campaign_id = :c
                  AND (status = 'pending' OR (status = 'done' AND pushed_at > :t))"""),
                {"b": brand, "c": str(campaign_id), "t": snapshot_time}).mappings().all()
    except Exception:
        return []
    return [dict(r) for r in rows]


def campaigns_with_pending_removals(engine, brand):
    try:
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(text("""
                SELECT DISTINCT campaign_id FROM voylla.sku_campaign_removals
                WHERE brand = :b AND status = 'pending'"""), {"b": brand})]
    except Exception:
        return []


def mark_removals(engine, ids, status, note):
    if not ids:
        return
    with engine.begin() as conn:
        conn.execute(text("""UPDATE voylla.sku_campaign_removals
                             SET status = :s, pushed_at = NOW(), push_note = :n
                             WHERE id = ANY(:ids) AND status = 'pending'"""),
                     {"s": status, "n": note, "ids": [int(i) for i in ids]})
