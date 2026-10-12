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
