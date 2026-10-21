"""New keyword ideas for Blinkit campaigns.

Blinkit suggests keywords per product (voylla."Blinkit_keyword_suggestions"),
and voylla."Blinkit_keyword_suggestions_history" keeps one snapshot a day, so
we can see which searches are growing (Karwa Chauth, Diwali gifting ...).
Every day this:
  1. takes the newest snapshot, keywords with at least MIN_SEARCHES searches
     that NO campaign of the brand targets yet;
  2. drops other brands' names (Blinkit marks "chocolate", "giva", "casio" ...
     as brand keywords) unless they are our own brand's name;
  3. attaches each to the best live campaign that already advertises the
     product Blinkit suggested it for, and skips products with no stock;
  4. ranks by searches and growth, and asks the AI whether each keyword really
     fits that product (one cheap call per brand);
  5. saves the survivors as PENDING ideas. A person accepts or rejects each
     one (AI Agent page, New keywords tab); accepted ones are added to the
     campaign by the 2 pm push. Nothing is added without a person, Auto or not.
"""
import json
import math
from datetime import date

import pandas as pd
from sqlalchemy import text

MIN_SEARCHES = 500
AI_CANDIDATES_PER_BRAND = 60
MAX_PER_CAMPAIGN = 5
MAX_PER_BRAND = 25
START_CPM_FLOOR = 200          # the push floor
START_CPM_CAP = 400            # a new keyword starts cautious; the daily AI moves it after
REDECIDE_AFTER_DAYS = 30       # a rejected idea is not offered again for a month
OWN_BRAND_TERMS = {"voylla": ("voylla", "imara"), "chumbak": ("chumbak",), "petcrux": ("petcrux", "hipkoo")}
KEYWORD_MODEL = "claude-sonnet-5-5"   # relevance needs judgement; ~3 calls a day
BROAD_ONE_WORD_SEARCHES = 50_000      # "pink", "fish", "dry": too broad for a small daily budget

# Occasion keywords whose day has passed are skipped. Blinkit's `searches` is a
# rolling 30-day total, so it keeps rising for days after a festival ends
# ("kanjak" still climbing on 21 Oct, the day after Dussehra) and the trend
# cannot show it. Dates are the occasion's LAST day; update them each year.
OCCASIONS = [
    (("navratri", "garba", "dandiya", "kanjak", "kanya pujan", "durga puja", "dussehra", "ashtami"), date(2026, 10, 20)),
    (("karwa chauth", "karva chauth"), date(2026, 10, 29)),
    (("dhanteras", "diwali", "deepawali", "bhai dooj", "bhaidooj"), date(2026, 11, 11)),
    (("christmas", "xmas", "secret santa"), date(2026, 12, 25)),
]
OCCASION_GRACE_DAYS = 2               # a keyword needs a couple of days to be accepted and go live

CANDIDATES_SQL = """
WITH latest AS (
    SELECT MAX(snapshot_date) AS d FROM voylla."Blinkit_keyword_suggestions_history" WHERE brand_name = :brand
),
h AS (
    SELECT lower(trim(keyword)) AS kw, product_id::text AS pid, searches, is_brand_keyword,
           suggested_min, suggested_max, boost_min, exact_min, snapshot_date
    FROM voylla."Blinkit_keyword_suggestions_history" WHERE brand_name = :brand
),
cur AS (
    SELECT DISTINCT ON (kw) h.* FROM h, latest WHERE h.snapshot_date = latest.d ORDER BY kw, searches DESC
),
w1 AS (SELECT DISTINCT ON (kw) kw, searches FROM h, latest WHERE snapshot_date <= latest.d - 7
       ORDER BY kw, snapshot_date DESC),
w4 AS (SELECT DISTINCT ON (kw) kw, searches FROM h, latest WHERE snapshot_date <= latest.d - 28
       ORDER BY kw, snapshot_date DESC),
fs AS (SELECT kw, MIN(snapshot_date) AS first_seen FROM h GROUP BY kw)
SELECT cur.kw, cur.pid, cur.searches, cur.is_brand_keyword, cur.suggested_min, cur.suggested_max,
       cur.boost_min, cur.exact_min, w1.searches AS searches_7d_ago, w4.searches AS searches_28d_ago,
       fs.first_seen, cur.snapshot_date
FROM cur LEFT JOIN w1 USING (kw) LEFT JOIN w4 USING (kw) LEFT JOIN fs USING (kw)
WHERE cur.searches >= :min_searches
"""

TARGETED_SQL = """SELECT DISTINCT lower(replace(keyword, '_', ' ')) AS kw FROM voylla."Blinkit_CPM" WHERE "Brand" = :brand"""

CAMPAIGNS_SQL = """
WITH c AS (
    SELECT p.*, MAX(created_at) OVER (PARTITION BY campaign_id) AS newest
    FROM voylla."Blinkit_CampaignWise_ProductID" p WHERE brand_name = :brand
),
cur AS (SELECT DISTINCT campaign_id::text AS campaign_id, product_id::text AS pid, campaign_name
        FROM c WHERE created_at >= newest - INTERVAL '1 hour' AND status IN ('ACTIVE', 'ON_HOLD')),
rt AS (SELECT DISTINCT ON (campaign_id) campaign_id::text AS campaign_id, last_status
       FROM voylla."Blinkit_Campaign_Runtime" ORDER BY campaign_id, log_date DESC),
ads AS (SELECT "Campaign ID"::text AS campaign_id, SUM("Estimated Budget Consumed") AS spend,
               SUM("Direct Sales" + "Indirect Sales") AS sales
        FROM voylla."Blinkit_Ads_Report"
        WHERE "Brand" = :brand
          AND TO_TIMESTAMP("Date", 'YYYY-MM-DD HH24:MI:SS')::date BETWEEN CURRENT_DATE - 17 AND CURRENT_DATE - 3
        GROUP BY 1)
SELECT cur.campaign_id, cur.pid, cur.campaign_name, ads.spend, ads.sales
FROM cur LEFT JOIN rt USING (campaign_id) LEFT JOIN ads USING (campaign_id)
WHERE COALESCE(rt.last_status, '') <> 'STOPPED'
"""

STOCK_SQL = """
WITH m AS (SELECT DISTINCT ON ("PID"::text) "PID"::text AS pid, "ITEM ID"::text AS item_id
           FROM voylla."Blinkit_Mapping" WHERE lower("Brand") = lower(:brand) ORDER BY "PID"::text),
st AS (SELECT item_id::text AS item_id, SUM(backend_inv_qty + frontend_inv_qty) AS stock
       FROM "DataWarehouse".blinkit_soh
       WHERE brand = lower(:brand)
         AND created_at = (SELECT MAX(created_at) FROM "DataWarehouse".blinkit_soh WHERE brand = lower(:brand))
       GROUP BY 1)
SELECT m.pid, COALESCE(st.stock, 0) AS stock FROM m LEFT JOIN st USING (item_id)
"""

NAMES_SQL = """
SELECT DISTINCT ON (product_id::text) product_id::text AS pid, name
FROM "DataWarehouse".blinkit_pricing WHERE brand = lower(:brand)
ORDER BY product_id::text, scraped_date DESC
"""

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS voylla.blinkit_keyword_ideas (
    id            serial PRIMARY KEY,
    first_found   date NOT NULL,
    last_found    date NOT NULL,
    brand         text NOT NULL,
    campaign_id   text NOT NULL,
    campaign_name text,
    keyword       text NOT NULL,
    product_id    text,
    product_name  text,
    searches      integer,
    searches_7d_ago  integer,
    searches_28d_ago integer,
    growth_7d     numeric,
    growth_28d    numeric,
    first_seen    date,
    trend         text,
    suggested_cpm integer,
    blinkit_cpm_range text,
    score         numeric,
    ai_reason     text,
    status        text NOT NULL DEFAULT 'pending',   -- pending / accepted / rejected / done / failed
    decided_by    text,
    decided_at    timestamp,
    pushed_at     timestamp,
    push_note     text
)"""


# ── decisions (pure, tested) ────────────────────────────────────────────────

def _f(v):
    try:
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
    except (TypeError, ValueError):
        return None


def growth(now, before):
    now, before = _f(now), _f(before)
    if not now or not before:
        return None
    return round((now / before - 1) * 100, 1)


def trend_label(g7, g28, first_seen, snapshot):
    if first_seen and snapshot and (snapshot - first_seen).days <= 14:
        return "New"
    if g7 is not None and g7 >= 50:
        return "Rising fast"
    if g7 is not None and g7 <= -20:
        return "Falling - past its peak"
    if (g7 is not None and g7 >= 10) or (g28 is not None and g28 >= 50 and (g7 is None or g7 >= 0)):
        return "Rising"
    return "Steady"


def is_other_brand(keyword, is_brand_keyword, brand):
    """Blinkit's brand flag covers other companies' names; ours are kept."""
    if not is_brand_keyword:
        return False
    own = OWN_BRAND_TERMS.get(brand.lower(), (brand.lower(),))
    return not any(t in keyword for t in own)


def occasion_over(keyword, today):
    """True when the keyword belongs to an occasion that ends before the ad could run."""
    for terms, last_day in OCCASIONS:
        if any(t in keyword for t in terms):
            return (last_day - today).days < OCCASION_GRACE_DAYS
    return False


def too_broad(keyword, searches):
    return " " not in keyword.strip() and (searches or 0) > BROAD_ONE_WORD_SEARCHES


def start_cpm(row):
    """Bottom of Blinkit's suggested range (else its minimums), between the floor and a cautious cap."""
    base = _f(row.get("suggested_min")) or _f(row.get("boost_min")) or _f(row.get("exact_min")) or START_CPM_FLOOR
    return int(min(max(base, START_CPM_FLOOR), START_CPM_CAP))


def score(searches, g7):
    """Bigger searches and faster growth first; a fading keyword is pushed down, not dropped."""
    g = max(-50.0, min(200.0, g7 if g7 is not None else 0.0))
    return round(math.log10(max(float(searches or 0), 1.0)) * (1 + g / 100), 3)


def pick_campaign(campaigns):
    """Among the live campaigns that already advertise the product: best ROAS
    over the settled window, then the biggest spender."""
    def key(c):
        spend, sales = _f(c.get("spend")) or 0, _f(c.get("sales")) or 0
        return (sales / spend if spend else -1, spend)
    return max(campaigns, key=key) if campaigns else None


def build_candidates(rows, targeted, campaigns_by_pid, stock_by_pid, brand, names=None, today=None):
    """Rows from CANDIDATES_SQL -> ranked ideas, before the AI check."""
    names, today = names or {}, today or date.today()
    out = []
    for r in rows:
        kw = (r.get("kw") or "").strip()
        if not kw or kw in targeted or is_other_brand(kw, r.get("is_brand_keyword"), brand):
            continue
        if occasion_over(kw, today) or too_broad(kw, r.get("searches")):
            continue
        pid = str(r.get("pid") or "")
        stock = stock_by_pid.get(pid)
        if stock is not None and stock <= 0:
            continue                      # no point paying to show an out-of-stock product
        camp = pick_campaign(campaigns_by_pid.get(pid, []))
        if not camp:
            continue                      # the product is in no live campaign
        g7, g28 = growth(r.get("searches"), r.get("searches_7d_ago")), growth(r.get("searches"), r.get("searches_28d_ago"))
        lo, hi = _f(r.get("suggested_min")), _f(r.get("suggested_max"))
        out.append({
            "keyword": kw, "product_id": pid, "product_name": names.get(pid),
            "campaign_id": camp["campaign_id"], "campaign_name": camp.get("campaign_name"),
            "searches": int(r.get("searches") or 0),
            "searches_7d_ago": int(r["searches_7d_ago"]) if _f(r.get("searches_7d_ago")) else None,
            "searches_28d_ago": int(r["searches_28d_ago"]) if _f(r.get("searches_28d_ago")) else None,
            "growth_7d": g7, "growth_28d": g28, "first_seen": r.get("first_seen"),
            "trend": trend_label(g7, g28, r.get("first_seen"), r.get("snapshot_date")),
            "suggested_cpm": start_cpm(r),
            "blinkit_cpm_range": f"{int(lo)}-{int(hi)}" if lo and hi else None,
            "score": score(r.get("searches"), g7),
        })
    out.sort(key=lambda c: -c["score"])
    return out


def cap_per_campaign(ideas, per_campaign=MAX_PER_CAMPAIGN, per_brand=MAX_PER_BRAND):
    seen, kept = {}, []
    for i in ideas:
        if seen.get(i["campaign_id"], 0) >= per_campaign:
            continue
        seen[i["campaign_id"]] = seen.get(i["campaign_id"], 0) + 1
        kept.append(i)
        if len(kept) >= per_brand:
            break
    return kept


def parse_ai_verdicts(text_):
    """The AI answers a JSON array [{"i", "relevant", "reason"}]; anything else -> {}."""
    if not text_:
        return {}
    a, b = text_.find("["), text_.rfind("]")
    if a < 0 or b <= a:
        return {}
    try:
        items = json.loads(text_[a:b + 1])
    except ValueError:
        return {}
    return {int(x["i"]): x for x in items if isinstance(x, dict) and "i" in x}


# ── AI relevance check ──────────────────────────────────────────────────────

AI_PROMPT = """You check new search-ad keyword ideas for the brand {brand} on Blinkit (a quick-commerce app).
Each idea is a keyword a shopper types, and the {brand} product it would show.
Today is {today}; an accepted keyword starts running in a day or two.
A keyword is RELEVANT only if someone typing it could plausibly want to buy THAT product
(same kind of item, occasion or gift fit is fine) AND it is worth starting now. It is NOT
relevant if it is: another company's brand; an unrelated item (food, cosmetics, electronics ...);
a mismatched product type (e.g. "necklace" for earrings); a cut-off or misspelt fragment
(e.g. "oxid", "oxidi", "oxidised jew"); too broad to convert on a small budget (a bare colour,
"gift for men", "dry"); an occasion that is already over by today; or adult/offensive.
Upcoming occasions (e.g. an approaching festival) are good.
Answer ONLY a JSON array, one object per idea: {{"i": <number>, "relevant": true/false,
"reason": "<at most 12 words>"}}.

Ideas:
{ideas}"""


def ai_check(brand, ideas):
    """One call per brand on the shared (cheap) model. Returns ideas the AI
    called relevant, each with its reason; on any failure, returns []
    (nothing unchecked is ever offered)."""
    if not ideas:
        return []
    import anthropic
    from db import get_anthropic_config
    cfg = get_anthropic_config()
    lines = "\n".join(f'{n}. keyword "{i["keyword"]}" ({i["searches"]:,} searches, {i["trend"]}) -> product: '
                      f'{i.get("product_name") or "product " + i["product_id"]}' for n, i in enumerate(ideas))
    try:
        resp = anthropic.Anthropic(api_key=cfg["api_key"]).messages.create(
            model=KEYWORD_MODEL, max_tokens=4000,
            messages=[{"role": "user", "content": AI_PROMPT.format(brand=brand, ideas=lines,
                                                                     today=date.today().strftime("%d %b %Y"))}])
        verdicts = parse_ai_verdicts("".join(b.text for b in resp.content if b.type == "text"))
    except Exception as exc:
        print(f"[keywords] {brand}: AI check failed - {exc}")
        return []
    kept = []
    for n, i in enumerate(ideas):
        v = verdicts.get(n)
        if v and v.get("relevant") is True:
            kept.append(dict(i, ai_reason=str(v.get("reason") or "")[:200]))
    return kept


# ── storage ─────────────────────────────────────────────────────────────────

def ensure_table(engine):
    with engine.begin() as conn:
        conn.execute(text(TABLE_DDL))


def save_ideas(engine, brand, ideas, today):
    """An open (pending) idea for the same campaign + keyword is refreshed, not
    duplicated; one decided within REDECIDE_AFTER_DAYS is not offered again."""
    added = refreshed = 0
    cols = ("campaign_name", "product_id", "product_name", "searches", "searches_7d_ago", "searches_28d_ago",
            "growth_7d", "growth_28d", "first_seen", "trend", "suggested_cpm", "blinkit_cpm_range", "score", "ai_reason")
    with engine.begin() as conn:
        for i in ideas:
            key = {"b": brand, "c": i["campaign_id"], "k": i["keyword"]}
            row = conn.execute(text("""
                SELECT id, status, decided_at FROM voylla.blinkit_keyword_ideas
                WHERE brand = :b AND campaign_id = :c AND keyword = :k
                ORDER BY id DESC LIMIT 1"""), key).mappings().first()
            vals = {c: i.get(c) for c in cols}
            if row and row["status"] == "pending":
                conn.execute(text("UPDATE voylla.blinkit_keyword_ideas SET last_found = :d, "
                                  + ", ".join(f"{c} = :{c}" for c in cols) + " WHERE id = :id"),
                             dict(vals, d=today, id=row["id"]))
                refreshed += 1
                continue
            if row and row["status"] in ("accepted", "done"):
                continue
            if row and row["decided_at"] and (pd.Timestamp(today) - pd.Timestamp(row["decided_at"])).days < REDECIDE_AFTER_DAYS:
                continue
            conn.execute(text("INSERT INTO voylla.blinkit_keyword_ideas (first_found, last_found, brand, campaign_id, keyword, "
                              + ", ".join(cols) + ") VALUES (:d, :d, :b, :c, :k, "
                              + ", ".join(f":{c}" for c in cols) + ")"),
                         dict(vals, d=today, **key))
            added += 1
    return added, refreshed


def _rows(engine, sql, params):
    df = pd.read_sql(text(sql), engine, params=params)
    return [{k: (None if (isinstance(v, float) and pd.isna(v)) else v) for k, v in r.items()}
            for r in df.to_dict(orient="records")]


def find_for_brand(engine, brand, today=None, use_ai=True, save=True):
    today = today or date.today()
    rows = _rows(engine, CANDIDATES_SQL, {"brand": brand, "min_searches": MIN_SEARCHES})
    targeted = {r["kw"].strip() for r in _rows(engine, TARGETED_SQL, {"brand": brand}) if r["kw"]}
    by_pid = {}
    for c in _rows(engine, CAMPAIGNS_SQL, {"brand": brand}):
        by_pid.setdefault(c["pid"], []).append(c)
    try:
        stock = {r["pid"]: float(r["stock"]) for r in _rows(engine, STOCK_SQL, {"brand": brand})}
    except Exception:
        stock = {}
    try:
        names = {r["pid"]: r["name"] for r in _rows(engine, NAMES_SQL, {"brand": brand})}
    except Exception:
        names = {}
    ranked = build_candidates(rows, targeted, by_pid, stock, brand, names, today)[:AI_CANDIDATES_PER_BRAND]
    checked = ai_check(brand, ranked) if use_ai else ranked
    ideas = cap_per_campaign(checked)
    result = {"brand": brand, "candidates": len(ranked), "relevant": len(checked), "ideas": ideas}
    if save and ideas:
        ensure_table(engine)
        result["added"], result["refreshed"] = save_ideas(engine, brand, ideas, today)
    return result


def run(brands=("Voylla", "Chumbak", "Petcrux"), use_ai=True):
    """Daily: called at the end of the 1:15 pm perf-alerts run. Never raises."""
    from db import get_engine
    engine = get_engine()
    out = []
    for b in brands:
        try:
            r = find_for_brand(engine, b, use_ai=use_ai)
            print(f"[keywords] {b}: {r['candidates']} candidates, {r['relevant']} relevant, "
                  f"{r.get('added', 0)} new, {r.get('refreshed', 0)} refreshed")
            out.append(r)
        except Exception as exc:
            print(f"[keywords] {b}: failed - {exc}")
            out.append({"brand": b, "error": str(exc), "ideas": []})
    return out


# ── the page and the push ───────────────────────────────────────────────────

def list_ideas(engine, brand, days=7):
    try:
        return _rows(engine, """SELECT * FROM voylla.blinkit_keyword_ideas
                                WHERE brand = :b AND (status IN ('pending', 'accepted')
                                      OR COALESCE(decided_at, pushed_at) >= CURRENT_DATE - :d)
                                ORDER BY status = 'pending' DESC, score DESC NULLS LAST""",
                     {"b": brand, "d": days})
    except Exception:
        return []


def decide(engine, idea_id, accept, cpm=None, by=None):
    """Only a pending idea can be decided; an accepted bid is kept between the floor and the cap's double."""
    params = {"i": int(idea_id), "s": "accepted" if accept else "rejected", "by": by}
    sql = "UPDATE voylla.blinkit_keyword_ideas SET status = :s, decided_by = :by, decided_at = NOW()"
    if accept and cpm is not None:
        params["cpm"] = int(min(max(float(cpm), START_CPM_FLOOR), START_CPM_CAP * 2))
        sql += ", suggested_cpm = :cpm"
    with engine.begin() as conn:
        return conn.execute(text(sql + " WHERE id = :i AND status = 'pending'"), params).rowcount == 1


def campaigns_with_accepted(engine, brand):
    try:
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(text(
                "SELECT DISTINCT campaign_id FROM voylla.blinkit_keyword_ideas WHERE brand = :b AND status = 'accepted'"),
                {"b": brand})]
    except Exception:
        return []


def additions_to_apply(engine, brand, campaign_id, live_keywords):
    """Keywords to add on this push: accepted ones, plus ones pushed in the last
    3 days that Blinkit_CPM does not show yet (it syncs later; leaving them out
    would delete the keyword we just added)."""
    live = {str(k).replace("_", " ").strip().lower() for k in live_keywords}
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, keyword, suggested_cpm, status FROM voylla.blinkit_keyword_ideas
                WHERE brand = :b AND campaign_id = :c
                  AND (status = 'accepted' OR (status = 'done' AND pushed_at >= NOW() - INTERVAL '3 days'))"""),
                {"b": brand, "c": str(campaign_id)}).mappings().all()
    except Exception:
        return []
    return [dict(r) for r in rows if r["keyword"].strip().lower() not in live]


def mark_additions(engine, ids, status, note):
    if not ids:
        return
    with engine.begin() as conn:
        conn.execute(text("""UPDATE voylla.blinkit_keyword_ideas SET status = :s, pushed_at = NOW(), push_note = :n
                             WHERE id = ANY(:ids) AND status = 'accepted'"""),
                     {"s": status, "n": note, "ids": [int(i) for i in ids]})
