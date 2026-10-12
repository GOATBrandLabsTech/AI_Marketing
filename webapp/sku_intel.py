"""
SKU intelligence - Phase 1 (read-only).

One row per Voylla Blinkit SKU that pulls the departments' data together:
sales (units 7d / 30d), stock (Blinkit warehouses), price (what we asked
Blinkit to run vs what is live), the brand-funded offer, profit per unit,
and which live campaigns advertise it. On top of that, plain flags that
answer "what is wrong with this SKU right now" - e.g. advertised while out of
stock. These are the department reports the boss agent will read in Phase 2.

Voylla only for now. Nothing here writes to Blinkit.
"""
import pandas as pd
from sqlalchemy import text

BRAND = "Voylla"

LOW_COVER_DAYS = 7          # advertised but runs out within a week
OVERSTOCK_DAYS = 90         # more than a quarter of stock at today's pace
PRICE_TOLERANCE = 0.02      # live price within 2% of what we asked = fine

SNAPSHOT_SQL = """
WITH m AS (
    SELECT "SKU" AS sku, "EAN"::text AS ean, "ITEM ID"::text AS item_id, "PID"::text AS pid,
           NULLIF("MRP", '')::numeric AS mrp_map, NULLIF("SP", '')::numeric AS sp_asked,
           NULLIF("LP", '')::numeric AS lp
    FROM voylla."Blinkit_Mapping" WHERE "Brand" = :brand
),
pr AS (
    SELECT DISTINCT ON (product_id::text) product_id::text AS pid, name, selling_price AS sp_live,
           mrp AS mrp_live, in_stock AS listing_status
    FROM "DataWarehouse".blinkit_pricing
    WHERE brand = lower(:brand)
      AND scraped_date = (SELECT MAX(scraped_date) FROM "DataWarehouse".blinkit_pricing WHERE brand = lower(:brand))
    ORDER BY product_id::text
),
st AS (
    SELECT item_id::text AS item_id,
           SUM(backend_inv_qty + frontend_inv_qty) AS stock,
           COUNT(*) FILTER (WHERE backend_inv_qty + frontend_inv_qty > 0) AS warehouses_with_stock
    FROM "DataWarehouse".blinkit_soh
    WHERE brand = lower(:brand)
      AND created_at = (SELECT MAX(created_at) FROM "DataWarehouse".blinkit_soh WHERE brand = lower(:brand))
    GROUP BY 1
),
sa AS (
    SELECT item_id::text AS item_id,
           SUM(qty_sold) FILTER (WHERE date >= CURRENT_DATE - 7) AS units_7d,
           SUM(qty_sold) AS units_30d
    FROM "DataWarehouse".blinkit_sales
    WHERE brand = lower(:brand) AND date >= CURRENT_DATE - 30 AND date < CURRENT_DATE
    GROUP BY 1
),
cp AS (
    SELECT product_id::text AS pid, COUNT(DISTINCT campaign_id) AS campaigns,
           STRING_AGG(DISTINCT campaign_id::text, ',') AS campaign_ids
    FROM (SELECT p.*, MAX(created_at) OVER (PARTITION BY campaign_id) AS newest
          FROM voylla."Blinkit_CampaignWise_ProductID" p WHERE brand_name = :brand) x
    WHERE created_at >= newest - INTERVAL '1 hour' AND status IN ('ACTIVE', 'ON_HOLD')
    GROUP BY 1
),
im AS (
    SELECT DISTINCT ON ("EAN") "EAN"::text AS ean, "Product Code" AS product_code,
           "Category Name" AS category, "Sub-Category" AS sub_category,
           NULLIF("Cost Price"::text, '')::numeric AS cost_price
    FROM voylla.voylla_in_unicommerce_itemmaster_dedup
    ORDER BY "EAN"
),
bf AS (
    SELECT product_id::text AS pid, SUM(qty_sold) AS offer_units_7d, SUM(total_brand_fund) AS brand_fund_7d
    FROM voylla.blinkit_brand_fund
    WHERE date >= (SELECT MAX(date) FROM voylla.blinkit_brand_fund) - 6
    GROUP BY 1
)
SELECT m.sku, pr.name, m.ean, m.item_id, m.pid, im.product_code, im.category, im.sub_category,
       COALESCE(pr.mrp_live, m.mrp_map) AS mrp, m.sp_asked, pr.sp_live, pr.listing_status,
       m.lp, im.cost_price,
       COALESCE(st.stock, 0) AS stock, COALESCE(st.warehouses_with_stock, 0) AS warehouses_with_stock,
       COALESCE(sa.units_7d, 0) AS units_7d, COALESCE(sa.units_30d, 0) AS units_30d,
       COALESCE(cp.campaigns, 0) AS campaigns, cp.campaign_ids,
       COALESCE(bf.offer_units_7d, 0) AS offer_units_7d, COALESCE(bf.brand_fund_7d, 0) AS brand_fund_7d
FROM m
LEFT JOIN pr ON pr.pid = m.pid
LEFT JOIN st ON st.item_id = m.item_id
LEFT JOIN sa ON sa.item_id = m.item_id
LEFT JOIN cp ON cp.pid = m.pid
LEFT JOIN im ON im.ean = m.ean
LEFT JOIN bf ON bf.pid = m.pid
"""

FLAG_LABELS = {
    "advertised_oos": "Advertised, out of stock",
    "advertised_low_cover": f"Advertised, runs out in < {LOW_COVER_DAYS} days",
    "price_below_asked": "Selling below the SP we asked",
    "price_above_asked": "Selling above the SP we asked",
    "below_cost": "Landing price below cost",
    "dead_stock": "Stock but no sale in 30 days",
    "overstock": f"Over {OVERSTOCK_DAYS} days of stock",
}


def days_of_cover(stock, units_30d):
    if not units_30d:
        return None
    return round(stock / (units_30d / 30.0), 1)


def sku_flags(r):
    """The 'what is wrong right now' list for one SKU row (a dict)."""
    flags = []
    stock, campaigns = r.get("stock") or 0, r.get("campaigns") or 0
    cover = days_of_cover(stock, r.get("units_30d") or 0)
    if campaigns and stock <= 0:
        flags.append("advertised_oos")
    elif campaigns and cover is not None and cover < LOW_COVER_DAYS:
        flags.append("advertised_low_cover")
    # an out-of-stock listing often shows MRP, so only in-stock listings are compared
    asked, live = r.get("sp_asked"), r.get("sp_live")
    if asked and live and str(r.get("listing_status") or "").lower() == "in stock":
        gap = (float(live) - float(asked)) / float(asked)
        if gap < -PRICE_TOLERANCE:
            flags.append("price_below_asked")
        elif gap > PRICE_TOLERANCE:
            flags.append("price_above_asked")
    lp, cost = r.get("lp"), r.get("cost_price")
    if lp and cost and float(lp) < float(cost):
        flags.append("below_cost")
    if stock > 0 and not (r.get("units_30d") or 0):
        flags.append("dead_stock")
    elif cover is not None and cover > OVERSTOCK_DAYS:
        flags.append("overstock")
    return flags


def sku_snapshot(engine, brand=BRAND):
    df = pd.read_sql(text(SNAPSHOT_SQL), engine, params={"brand": brand})
    rows = []
    for r in df.to_dict(orient="records"):
        r = {k: (None if (isinstance(v, float) and pd.isna(v)) else v) for k, v in r.items()}
        r["days_of_cover"] = days_of_cover(r["stock"], r["units_30d"])
        lp, cost = r.get("lp"), r.get("cost_price")
        r["profit_per_unit"] = round(float(lp) - float(cost), 2) if lp and cost else None
        r["flags"] = sku_flags(r)
        rows.append(r)
    return rows


def flag_counts(rows):
    return {f: sum(1 for r in rows if f in r["flags"]) for f in FLAG_LABELS}


# ── offers file loader ───────────────────────────────────────────────────────
OFFER_COLUMNS = ["date", "city", "product_id", "item_id", "offer_type", "item_mrp",
                 "brandfund_absolute_value", "brandfund_percentage_value", "product_name",
                 "p_type", "qty_sold", "total_brand_fund", "mrp_gmv"]


def load_brand_fund_csv(engine, path):
    """Loads a PartnersBiz 'Spends summary' export into voylla.blinkit_brand_fund.
    Re-loading the same days replaces them, so a newer export can simply be loaded again."""
    df = pd.read_csv(path)
    df = df[df["brand_name"].str.lower() == BRAND.lower()][OFFER_COLUMNS].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS voylla.blinkit_brand_fund (
                date date, city text, product_id text, item_id text, offer_type text, item_mrp numeric,
                brandfund_absolute_value numeric, brandfund_percentage_value numeric, product_name text,
                p_type text, qty_sold numeric, total_brand_fund numeric, mrp_gmv numeric,
                loaded_at timestamp DEFAULT NOW())"""))
        conn.execute(text("DELETE FROM voylla.blinkit_brand_fund WHERE date BETWEEN :a AND :b"),
                     {"a": df["date"].min(), "b": df["date"].max()})
    df["product_id"] = df["product_id"].astype(str)
    df["item_id"] = df["item_id"].astype(str)
    df.to_sql("blinkit_brand_fund", engine, schema="voylla", if_exists="append", index=False, method="multi", chunksize=1000)
    return len(df), df["date"].min(), df["date"].max()


# ── one SKU in depth (Phase 2: what the department agents read) ──────────────
# Ads are judged on a window that ends 3 days ago: Blinkit keeps adding sales to
# the last couple of days (~30% for yesterday), so fresher days look worse than
# they are.
ADS_WINDOW_END_DAYS_AGO = 3
ADS_WINDOW_DAYS = 7

DAILY_SALES_SQL = """
SELECT date, SUM(qty_sold) AS units
FROM "DataWarehouse".blinkit_sales
WHERE brand = lower(:brand) AND item_id::text = :item_id
  AND date >= CURRENT_DATE - 30 AND date < CURRENT_DATE
GROUP BY 1 ORDER BY 1
"""

CITY_SALES_SQL = """
SELECT COALESCE(city_name, city_id::text) AS city,
       SUM(qty_sold) FILTER (WHERE date >= CURRENT_DATE - 7) AS units_7d,
       SUM(qty_sold) AS units_30d
FROM "DataWarehouse".blinkit_sales
WHERE brand = lower(:brand) AND item_id::text = :item_id
  AND date >= CURRENT_DATE - 30 AND date < CURRENT_DATE
GROUP BY 1 ORDER BY units_30d DESC
"""

# Stock is summed per warehouse FIRST, then the cities each warehouse serves are
# attached. QC_City_Mappping has one row per (warehouse, city), so joining it
# before summing counted a warehouse's stock once per city it serves (13 Oct:
# VMJAI40866 showed 3,667 units across warehouses against 924 real).
STOCK_SQL = """
WITH st AS (
    SELECT backend_facility_name AS warehouse, SUM(backend_inv_qty + frontend_inv_qty) AS stock
    FROM "DataWarehouse".blinkit_soh
    WHERE brand = lower(:brand) AND item_id::text = :item_id
      AND created_at = (SELECT MAX(created_at) FROM "DataWarehouse".blinkit_soh WHERE brand = lower(:brand))
    GROUP BY 1
),
q AS (
    SELECT "Facility" AS warehouse, STRING_AGG(DISTINCT "City Name", ', ' ORDER BY "City Name") AS city,
           MAX("Cluster Name") AS cluster
    FROM voylla."QC_City_Mappping"
    WHERE POSITION('blinkit' IN LOWER("Channel")) > 0
    GROUP BY 1
)
SELECT st.warehouse, q.city, q.cluster, st.stock
FROM st LEFT JOIN q ON q.warehouse = st.warehouse
ORDER BY st.stock DESC
"""

PRICE_HISTORY_SQL = """
SELECT DISTINCT ON (scraped_date) scraped_date, selling_price, mrp, in_stock
FROM "DataWarehouse".blinkit_pricing
WHERE brand = lower(:brand) AND product_id::text = :pid AND scraped_date >= CURRENT_DATE - 30
ORDER BY scraped_date
"""

OFFER_HISTORY_SQL = """
SELECT date, SUM(qty_sold) AS units, SUM(total_brand_fund) AS brand_fund,
       STRING_AGG(DISTINCT offer_type, ', ') AS offer_types
FROM voylla.blinkit_brand_fund
WHERE product_id = :pid AND date >= (SELECT MAX(date) FROM voylla.blinkit_brand_fund) - 29
GROUP BY 1 ORDER BY 1
"""

SKU_CAMPAIGNS_SQL = """
WITH c AS (
    SELECT p.*, MAX(created_at) OVER (PARTITION BY campaign_id) AS newest
    FROM voylla."Blinkit_CampaignWise_ProductID" p WHERE brand_name = :brand
),
cur AS (SELECT * FROM c WHERE created_at >= newest - INTERVAL '1 hour' AND status IN ('ACTIVE', 'ON_HOLD')),
mine AS (SELECT DISTINCT campaign_id FROM cur WHERE product_id::text = :pid)
SELECT cur.campaign_id::text AS campaign_id, MAX(cur.status) AS status, MAX(cur.campaign_budget) AS budget,
       MAX(cur.region_type) AS region_type, MAX(TRANSLATE(cur.region_ids::text, '{}', '')) AS region_ids,
       COUNT(DISTINCT cur.product_id) AS n_products
FROM cur JOIN mine USING (campaign_id)
GROUP BY 1
"""

CAMPAIGN_NAMES_SQL = """
SELECT DISTINCT ON (campaign_id::text) campaign_id::text AS campaign_id, campaign_name
FROM voylla."Blinkit_Campaign_Runtime"
WHERE brand = :brand AND campaign_id::text = ANY(:ids)
ORDER BY campaign_id::text, log_date DESC
"""

CAMPAIGN_MODES_SQL = """
SELECT campaign_id::text AS campaign_id, mode FROM voylla.campaign_autonomy_mode
WHERE brand = :brand AND campaign_id::text = ANY(:ids)
"""

CAMPAIGN_ADS_SQL = """
SELECT "Campaign ID"::text AS campaign_id,
       SUM("Estimated Budget Consumed") AS spend,
       SUM("Direct Sales" + "Indirect Sales") AS sales,
       SUM("Direct Quantities Sold" + "Indirect Quantities Sold") AS units
FROM voylla."Blinkit_Ads_Report"
WHERE "Brand" = :brand AND "Campaign ID"::text = ANY(:ids)
  AND TO_TIMESTAMP("Date", 'YYYY-MM-DD HH24:MI:SS')::date
      BETWEEN CURRENT_DATE - :end_ago - :days + 1 AND CURRENT_DATE - :end_ago
GROUP BY 1
"""

CITY_MASTER_SQL = "SELECT city_id::text AS city_id, city_name FROM voylla.blinkit_city_master"


def _records(engine, sql, params, optional=False):
    """Rows as plain dicts (NaN -> None). optional=True: a missing table (e.g.
    the offers or city master not loaded yet) gives no rows instead of an error."""
    try:
        df = pd.read_sql(text(sql), engine, params=params)
    except Exception:
        if optional:
            return []
        raise
    return [{k: (None if (isinstance(v, float) and pd.isna(v)) else v) for k, v in r.items()}
            for r in df.to_dict(orient="records")]


def find_skus(rows, query):
    """Exact SKU match first, else every SKU whose code or name contains the text."""
    q = (query or "").strip().lower()
    if not q:
        return []
    exact = [r for r in rows if (r.get("sku") or "").lower() == q]
    return exact or [r for r in rows if q in (r.get("sku") or "").lower() or q in (r.get("name") or "").lower()]


def sku_detail(engine, row, brand=BRAND):
    """Everything the department agents need for one SKU (a row from sku_snapshot)."""
    item, pid = str(row.get("item_id") or ""), str(row.get("pid") or "")
    d = {
        "daily_sales": _records(engine, DAILY_SALES_SQL, {"brand": brand, "item_id": item}),
        "city_sales": _records(engine, CITY_SALES_SQL, {"brand": brand, "item_id": item}),
        "stock_by_warehouse": _records(engine, STOCK_SQL, {"brand": brand, "item_id": item}),
        "price_history": _records(engine, PRICE_HISTORY_SQL, {"brand": brand, "pid": pid}),
        "offer_history": _records(engine, OFFER_HISTORY_SQL, {"pid": pid}, optional=True),
        "city_master": {r["city_id"]: r["city_name"] for r in _records(engine, CITY_MASTER_SQL, {}, optional=True)},
    }
    campaigns = _records(engine, SKU_CAMPAIGNS_SQL, {"brand": brand, "pid": pid})
    ids = [c["campaign_id"] for c in campaigns]
    if ids:
        names = {r["campaign_id"]: r["campaign_name"]
                 for r in _records(engine, CAMPAIGN_NAMES_SQL, {"brand": brand, "ids": ids})}
        modes = {r["campaign_id"]: r["mode"]
                 for r in _records(engine, CAMPAIGN_MODES_SQL, {"brand": brand, "ids": ids}, optional=True)}
        ads = {r["campaign_id"]: r for r in _records(engine, CAMPAIGN_ADS_SQL, {
            "brand": brand, "ids": ids, "end_ago": ADS_WINDOW_END_DAYS_AGO, "days": ADS_WINDOW_DAYS})}
        for c in campaigns:
            a = ads.get(c["campaign_id"]) or {}
            c["campaign_name"] = names.get(c["campaign_id"])
            c["mode"] = modes.get(c["campaign_id"]) or "semi_auto"
            c["spend"], c["sales"], c["ad_units"] = a.get("spend") or 0, a.get("sales") or 0, a.get("units") or 0
    d["campaigns"] = campaigns
    return d


def campaign_names(engine, ids, brand=BRAND):
    """campaign_id -> latest name, for showing which campaigns a SKU is in."""
    if not ids:
        return {}
    return {r["campaign_id"]: r["campaign_name"]
            for r in _records(engine, CAMPAIGN_NAMES_SQL, {"brand": brand, "ids": list(ids)}, optional=True)}
