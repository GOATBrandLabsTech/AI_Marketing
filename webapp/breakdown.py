"""Sales, stock and price broken down by product, category or city - the
"top 10 earrings in Kolkata" kind of question, for any brand.

Sources: units from "DataWarehouse".blinkit_sales (item x city x day); category
the way the team's channel queries do it: item_id -> Blinkit_Mapping EAN -> the
item master ("Category Name", "Sub-Category") and voylla."Ordering_Categories"
("New Category" = the ranking tier: A.Hero, B. Robin, ...); items with no
mapping fall back to the PartnersBiz spends feed's Blinkit category; stock from blinkit_soh, mapped to cities
through QC_City_Mappping; live price from blinkit_pricing through
Blinkit_Mapping (brands that have a mapping). Read-only.
"""
from collections import defaultdict

import pandas as pd
from sqlalchemy import text

BY_OPTIONS = ("product", "category", "product_type", "tier", "city")

SALES_SQL = """
SELECT item_id::text AS item_id, MAX(item_name) AS name, city_name AS city,
       -- blinkit_sales.mrp is already the row's total (qty x unit MRP: 42 pairs x 349 = 14,658),
       -- so it is summed as is; multiplying by qty again inflated it ~qty times (21 Oct)
       SUM(qty_sold) AS units, SUM(COALESCE(mrp, 0)) AS mrp_value
FROM "DataWarehouse".blinkit_sales
WHERE brand = lower(:brand) AND date >= CURRENT_DATE - :days AND date < CURRENT_DATE
GROUP BY 1, 3
"""

CATEGORY_SQL = """
SELECT DISTINCT ON (item_id::text) item_id::text AS item_id,
       l2_category_name AS category, p_type AS product_type
FROM "DataWarehouse".blinkit_spends_summary
WHERE brand = lower(:brand)
ORDER BY item_id::text, date DESC
"""

ITEM_MASTER_SQL = """
SELECT DISTINCT ON (m."ITEM ID"::text) m."ITEM ID"::text AS item_id, m."EAN"::text AS ean,
       im."Product Code" AS product_code, im."Category Name" AS category, im."Sub-Category" AS product_type,
       im."Look" AS look, im."Collection" AS collection, n."New Category" AS tier
FROM voylla."Blinkit_Mapping" m
JOIN voylla.voylla_in_unicommerce_itemmaster_dedup im ON im."EAN"::text = m."EAN"::text
LEFT JOIN voylla."Ordering_Categories" n ON n."EAN"::text = m."EAN"::text
WHERE lower(m."Brand") = lower(:brand)
ORDER BY m."ITEM ID"::text
"""

STOCK_SQL = """
WITH st AS (
    SELECT item_id::text AS item_id, backend_facility_name AS warehouse,
           SUM(backend_inv_qty + frontend_inv_qty) AS stock
    FROM "DataWarehouse".blinkit_soh
    WHERE brand = lower(:brand)
      AND created_at = (SELECT MAX(created_at) FROM "DataWarehouse".blinkit_soh WHERE brand = lower(:brand))
    GROUP BY 1, 2
)
SELECT st.item_id, st.warehouse, st.stock,
       STRING_AGG(DISTINCT lower(q."City Name"), '|') AS cities
FROM st
LEFT JOIN voylla."QC_City_Mappping" q
       ON q."Facility" = st.warehouse AND POSITION('blinkit' IN LOWER(q."Channel")) > 0
GROUP BY 1, 2, 3
"""

PRICE_SQL = """
WITH pr AS (
    SELECT DISTINCT ON (product_id::text) product_id::text AS pid, selling_price, mrp, in_stock
    FROM "DataWarehouse".blinkit_pricing
    WHERE brand = lower(:brand)
      AND scraped_date = (SELECT MAX(scraped_date) FROM "DataWarehouse".blinkit_pricing WHERE brand = lower(:brand))
    ORDER BY product_id::text
)
SELECT m."ITEM ID"::text AS item_id, pr.selling_price, pr.mrp, pr.in_stock
FROM voylla."Blinkit_Mapping" m JOIN pr ON pr.pid = m."PID"::text
WHERE lower(m."Brand") = lower(:brand)
"""


def _rows(engine, sql, params, optional=False):
    try:
        df = pd.read_sql(text(sql), engine, params=params)
    except Exception:
        if optional:
            return []
        raise
    return [{k: (None if (isinstance(v, float) and pd.isna(v)) else v) for k, v in r.items()}
            for r in df.to_dict(orient="records")]


def _matches(text_, query):
    """Loose category match: 'earrings' finds 'Earrings', 'earring' and 'Studs' under Earrings."""
    if not query:
        return True
    q = query.strip().lower()
    stem = q[:-1] if q.endswith("s") and len(q) > 3 else q
    return stem in (text_ or "").lower()


def aggregate(sales, categories, stock, prices, by="product", city=None, category=None, top=10):
    """Pure: the rows from the four queries -> the breakdown. Units and MRP value
    are summed per group; ROAS-style averages are never used. Stock for a city
    = stock in the warehouses that serve it (one warehouse can serve many cities,
    so city stock does not add up to the total)."""
    if by not in BY_OPTIONS:
        raise ValueError(f"by must be one of {BY_OPTIONS}")
    city_q = (city or "").strip().lower() or None
    cats = {c["item_id"]: c for c in categories}

    def cat_of(item_id):
        c = cats.get(item_id) or {}
        return (c.get("category") or "Unknown", c.get("product_type") or "Unknown",
                c.get("tier") or "No tier", c)

    picked = []
    for s in sales:
        if city_q and (s.get("city") or "").lower() != city_q:
            continue
        cat, ptype, tier, c = cat_of(s["item_id"])
        if category and not any(_matches(v, category) for v in (cat, ptype, tier, s.get("name"))):
            continue
        picked.append(dict(s, category=cat, product_type=ptype, tier=tier,
                           product_code=c.get("product_code"), look=c.get("look"), collection=c.get("collection")))

    stock_total, stock_city = defaultdict(float), defaultdict(float)
    for r in stock:
        stock_total[r["item_id"]] += float(r["stock"] or 0)
        for c in str(r.get("cities") or "").split("|"):
            if c:
                stock_city[(r["item_id"], c)] += float(r["stock"] or 0)
    price = {p["item_id"]: p for p in prices}

    key = {"product": lambda r: r["item_id"], "category": lambda r: r["category"],
           "product_type": lambda r: r["product_type"], "tier": lambda r: r["tier"],
           "city": lambda r: r.get("city") or "Unknown"}[by]
    groups = defaultdict(lambda: {"units": 0.0, "mrp_value": 0.0, "items": set(), "cities": set()})
    for r in picked:
        g = groups[key(r)]
        g["units"] += float(r["units"] or 0)
        g["mrp_value"] += float(r["mrp_value"] or 0)
        g["items"].add(r["item_id"])
        g["cities"].add(r.get("city"))
        g.setdefault("first", r)

    total_units = sum(g["units"] for g in groups.values()) or 0
    out = []
    for k, g in groups.items():
        row = {"units": int(g["units"]), "mrp_value": round(g["mrp_value"]),
               "share_pct": round(100 * g["units"] / total_units, 1) if total_units else None}
        if by == "product":
            f, p = g["first"], price.get(k) or {}
            stock_here = stock_city.get((k, city_q)) if city_q else None
            row.update(item_id=k, product_code=f.get("product_code"), name=f.get("name"), category=f["category"],
                       product_type=f["product_type"], tier=f["tier"], look=f.get("look"),
                       cities_sold=len(g["cities"]), stock_total=int(stock_total.get(k, 0)),
                       stock_in_city=int(stock_here) if stock_here is not None else None,
                       live_price=p.get("selling_price"), mrp=p.get("mrp"), listing=p.get("in_stock"))
        elif by == "city":
            row.update(city=k, products_sold=len(g["items"]),
                       stock_in_city=int(sum(stock_city.get((i, str(k).lower()), 0) for i in g["items"])))
        else:
            row.update(**{by: k}, products_sold=len(g["items"]),
                       stock_total=int(sum(stock_total.get(i, 0) for i in g["items"])))
        out.append(row)
    out.sort(key=lambda r: -r["units"])
    return {"rows": out[:top], "groups_total": len(out), "units_total": int(total_units),
            "mrp_value_total": round(sum(g["mrp_value"] for g in groups.values()))}


def breakdown(engine, brand, by="product", city=None, category=None, days=30, top=10):
    days = max(1, min(int(days or 30), 90))
    top = max(1, min(int(top or 10), 50))
    sales = _rows(engine, SALES_SQL, {"brand": brand, "days": days})
    # the item master (through Blinkit_Mapping) wins; the spends feed only fills items it lacks
    merged = {c["item_id"]: c for c in _rows(engine, CATEGORY_SQL, {"brand": brand}, optional=True)}
    merged.update({c["item_id"]: c for c in _rows(engine, ITEM_MASTER_SQL, {"brand": brand}, optional=True)})
    result = aggregate(sales, list(merged.values()),
                       _rows(engine, STOCK_SQL, {"brand": brand}, optional=True),
                       _rows(engine, PRICE_SQL, {"brand": brand}, optional=True),
                       by=by, city=city, category=category, top=top)
    result.update(brand=brand, by=by, city=city, category=category, days=days,
                  period=f"last {days} days to yesterday",
                  note=("Units = Blinkit sales (all orders, not only ad-driven). MRP value = units x MRP, not "
                        "what customers paid. City stock = stock in the warehouses that serve that city."))
    return result
