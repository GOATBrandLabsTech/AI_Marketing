"""
SKU department agents + boss - Phase 2 (read-only).

Five department agents read one SKU's data (sku_intel.sku_snapshot row +
sku_intel.sku_detail) and each writes a short report: Sales, Inventory,
Price & Offer, Marketing, Product. They are plain Python - no LLM, no cost.

The boss is ONE LLM call per question: it gets a goal for the SKU (increase
sales, protect profit, avoid stock-out, clear stock) plus the five reports,
and recommends one action with reasons. Python then checks the answer against
hard rules (e.g. never spend more on ads for a SKU with no stock) and replaces
a blocked action with "no_action", saying why.

Nothing here changes a bid, a price, a campaign or an offer. The boss's answer
is advice for a human; Phase 3 turns it into approvable actions.
"""
import json
import re
from statistics import median

import sku_intel as si

BOSS_MODEL = "claude-sonnet-5-5"
BOSS_MAX_TOKENS = 2000   # a cut-off reply cannot be read; the answer itself is ~300-600 tokens

GOALS = {
    "increase_sales": "Increase sales of this SKU",
    "protect_profit": "Protect profit on this SKU",
    "avoid_stockout": "Avoid this SKU going out of stock",
    "clear_stock": "Clear this SKU's stock",
}

ACTIONS = {
    "raise_bid": "Raise the ad bid on this SKU's campaigns",
    "cut_bid": "Cut the ad bid on this SKU's campaigns",
    "hold_bid": "Keep bids as they are",
    "add_to_campaign": "Add the SKU to a campaign",
    "remove_from_campaign": "Take the SKU out of its campaigns",
    "change_city_targeting": "Change which cities its campaigns target",
    "change_price": "Change the selling price we ask Blinkit to run",
    "change_offer": "Start, change or stop a brand-funded offer",
    "shift_channel": "Push this SKU on another channel instead",
    "restock": "Send more stock to Blinkit warehouses",
    "mark_dead": "Mark the SKU dead (stop spending on it)",
    "no_action": "Do nothing for now",
}

# actions that put more ad money behind the SKU
SPEND_MORE = {"raise_bid", "add_to_campaign"}

SALES_DROP = -0.30          # 7-day pace this far below the 30-day pace = falling
LOW_MARGIN = 0.10           # profit under 10% of landing price
LOW_ROAS = 1.0              # ad spend bigger than the sales it brought
HERO_TOP_SHARE = 0.20       # top 20% of its sub-category by units
CITY_ALIASES = {"bangalore": "bengaluru", "gurgaon": "gurugram", "bombay": "mumbai",
                "new delhi": "delhi", "calcutta": "kolkata", "madras": "chennai"}


def _f(v):
    return float(v) if v is not None else None


def _city_key(name):
    n = re.sub(r"\s+", " ", str(name or "").strip().lower())
    return CITY_ALIASES.get(n, n)


def _stocked_cities(detail):
    """Every city served by a warehouse that holds this SKU now. A warehouse's
    'city' is the comma-joined list of cities it serves (sku_intel.STOCK_SQL)."""
    return {_city_key(c) for w in (detail.get("stock_by_warehouse") or [])
            if w.get("city") and (_f(w.get("stock")) or 0) > 0
            for c in str(w["city"]).split(",") if c.strip()}


def _report(department, status, headline, findings, facts):
    return {"department": department, "status": status, "headline": headline,
            "findings": findings, "facts": facts}


# ── Sales ────────────────────────────────────────────────────────────────────
def sales_agent(row, detail):
    u7, u30 = _f(row.get("units_7d")) or 0, _f(row.get("units_30d")) or 0
    pace7, pace30 = u7 / 7, u30 / 30
    trend = (pace7 - pace30) / pace30 if pace30 else None
    daily = detail.get("daily_sales") or []
    days_with_sales = sum(1 for d in daily if (_f(d.get("units")) or 0) > 0)
    cities = [c for c in (detail.get("city_sales") or []) if (_f(c.get("units_30d")) or 0) > 0]
    top = cities[:3]
    top_share = (_f(cities[0]["units_30d"]) / u30) if cities and u30 else None

    findings = []
    if not u30:
        status, headline = "problem", "No sale in 30 days"
    elif trend is not None and trend <= SALES_DROP:
        status, headline = "watch", f"Falling: {pace7:.1f}/day this week vs {pace30:.1f}/day over 30 days"
    else:
        status, headline = "ok", f"Selling {pace30:.1f}/day (30d), {pace7:.1f}/day this week"
    if u30:
        findings.append(f"Sold on {days_with_sales} of the last 30 days.")
        if top:
            findings.append("Top cities (30d): " + ", ".join(f"{c['city']} {int(_f(c['units_30d']))}" for c in top) + ".")
        if top_share is not None and top_share >= 0.5:
            findings.append(f"{cities[0]['city']} alone is {top_share:.0%} of sales.")
    return _report("Sales", status, headline, findings, {
        "units_7d": u7, "units_30d": u30, "per_day_7d": round(pace7, 2), "per_day_30d": round(pace30, 2),
        "trend_pct": round(trend * 100) if trend is not None else None,
        "days_with_sales_30d": days_with_sales, "selling_cities": len(cities),
        "top_cities": [{"city": c["city"], "units_30d": _f(c["units_30d"])} for c in top],
    })


# ── Inventory ────────────────────────────────────────────────────────────────
def inventory_agent(row, detail):
    stock, u30 = _f(row.get("stock")) or 0, _f(row.get("units_30d")) or 0
    cover = si.days_of_cover(stock, u30)
    wh = detail.get("stock_by_warehouse") or []
    stocked_cities = _stocked_cities(detail)
    unmapped = sorted({w["warehouse"] for w in wh if not w.get("city")})
    # cities that sold it in the last 30 days but have no warehouse holding it now
    dry = [c["city"] for c in (detail.get("city_sales") or [])
           if (_f(c.get("units_30d")) or 0) > 0 and _city_key(c["city"]) not in stocked_cities]
    if unmapped:
        dry = []  # a warehouse we cannot place may be the one serving these cities - don't guess

    findings = []
    if stock <= 0:
        status = "problem" if u30 else "watch"
        headline = "Out of stock on Blinkit" + (" while it was selling" if u30 else "")
    elif cover is not None and cover < si.LOW_COVER_DAYS:
        status, headline = "problem", f"Runs out in about {cover:g} days"
    elif cover is None:
        status, headline = "watch", f"{int(stock)} units and no sale in 30 days"
    elif cover > si.OVERSTOCK_DAYS:
        status, headline = "watch", f"Overstocked: {cover:g} days of cover"
    else:
        status, headline = "ok", f"{int(stock)} units, {cover:g} days of cover"
    if stock > 0:
        findings.append(f"Stock sits in {int(row.get('warehouses_with_stock') or 0)} warehouse(s).")
    if dry:
        findings.append("Sold here in 30 days but no stock in that city now: " + ", ".join(dry[:8]) + ".")
    if unmapped:
        findings.append(f"{len(unmapped)} warehouse(s) not in QC_City_Mappping, so city-level stock is incomplete.")
    return _report("Inventory", status, headline, findings, {
        "stock": stock, "days_of_cover": cover, "warehouses_with_stock": row.get("warehouses_with_stock"),
        "cities_selling_without_stock": dry, "unmapped_warehouses": unmapped,
    })


# ── Price & Offer ────────────────────────────────────────────────────────────
def price_agent(row, detail):
    asked, live, mrp = _f(row.get("sp_asked")), _f(row.get("sp_live")), _f(row.get("mrp"))
    lp, cost = _f(row.get("lp")), _f(row.get("cost_price"))
    in_stock = str(row.get("listing_status") or "").lower() == "in stock"
    profit = round(lp - cost, 2) if lp and cost else None
    margin = profit / lp if profit is not None and lp else None
    offer_units = _f(row.get("offer_units_7d")) or 0
    fund = _f(row.get("brand_fund_7d")) or 0
    fund_per_unit = round(fund / offer_units, 2) if offer_units else None
    hist = [p for p in (detail.get("price_history") or []) if str(p.get("in_stock") or "").lower() == "in stock"]
    live_prices = sorted({round(_f(p["selling_price"])) for p in hist if p.get("selling_price") is not None})

    flags = row.get("flags") or []
    findings = []
    if "below_cost" in flags:
        status, headline = "problem", f"Landing price {lp:g} is below cost {cost:g}"
    elif "price_below_asked" in flags:
        status, headline = "problem", f"Blinkit sells at {live:g}, below the {asked:g} we asked"
    elif "price_above_asked" in flags:
        status, headline = "watch", f"Blinkit sells at {live:g}, above the {asked:g} we asked"
    elif margin is not None and margin < LOW_MARGIN:
        status, headline = "watch", f"Thin margin: {profit:g} per unit ({margin:.0%} of LP)"
    elif profit is None:
        status, headline = "watch", "Profit unknown (landing price or cost missing)"
    else:
        status, headline = "ok", f"Profit {profit:g} per unit ({margin:.0%} of LP)"
    if live and mrp:
        findings.append(f"Live {live:g} vs MRP {mrp:g} ({1 - live / mrp:.0%} off)" + ("" if in_stock else " - listing out of stock, price not judged") + ".")
    if len(live_prices) > 1:
        findings.append(f"In-stock price moved between {live_prices[0]} and {live_prices[-1]} in 30 days.")
    if fund:
        findings.append(f"Voylla-funded offer: Rs {fund:,.0f} over 7 days on {int(offer_units)} units"
                        + (f" (Rs {fund_per_unit:g}/unit)." if fund_per_unit else "."))
        if profit is not None and fund_per_unit:
            findings.append(f"If LP does not already net off the offer, profit after offer is about {profit - fund_per_unit:g}/unit.")
    return _report("Price & Offer", status, headline, findings, {
        "sp_asked": asked, "sp_live": live, "mrp": mrp, "listing_in_stock": in_stock, "lp": lp,
        "cost_price": cost, "profit_per_unit": profit, "margin_pct": round(margin * 100) if margin is not None else None,
        "brand_fund_7d": fund, "offer_units_7d": offer_units, "brand_fund_per_unit": fund_per_unit,
    })


# ── Marketing ────────────────────────────────────────────────────────────────
def marketing_agent(row, detail):
    camps = detail.get("campaigns") or []
    stock, u30 = _f(row.get("stock")) or 0, _f(row.get("units_30d")) or 0
    master = detail.get("city_master") or {}
    stocked = _stocked_cities(detail)
    out, findings, low_roas, no_stock_cities = [], [], [], set()
    for c in camps:
        spend, sales = _f(c.get("spend")) or 0, _f(c.get("sales")) or 0
        roas = round(sales / spend, 2) if spend else None
        ids = [i.strip() for i in str(c.get("region_ids") or "").split(",") if i.strip()]
        cities = [master[i] for i in ids if i in master]
        missing = [n for n in cities if stocked and _city_key(n) not in stocked]
        no_stock_cities.update(missing)
        if roas is not None and roas < LOW_ROAS:
            low_roas.append(c)
        out.append({"campaign_id": c["campaign_id"], "name": c.get("campaign_name"), "mode": c.get("mode"),
                    "status": c.get("status"), "region_type": c.get("region_type"),
                    "cities": cities or (["(city names not loaded)"] if ids and not master else []),
                    "products_in_campaign": c.get("n_products"), "spend": spend, "sales": sales, "roas": roas,
                    "targets_cities_without_stock": missing})

    if not camps:
        if stock > 0 and u30:
            status, headline = "watch", "Not advertised, but in stock and selling"
        else:
            status, headline = "ok", "Not advertised"
    elif stock <= 0:
        status, headline = "problem", f"In {len(camps)} campaign(s) while out of stock"
    elif low_roas:
        status, headline = "watch", f"{len(low_roas)} of {len(camps)} campaign(s) spent more than they sold"
    else:
        status, headline = "ok", f"In {len(camps)} campaign(s)"
    for c in out:
        r = f"ROAS {c['roas']:g}" if c["roas"] is not None else "no spend"
        findings.append(f"{c['name'] or c['campaign_id']} ({c['mode']}, {c['products_in_campaign']} products): "
                        f"Rs {c['spend']:,.0f} spend, {r}.")
    if camps:
        findings.append(f"Ad numbers are campaign-level for days {si.ADS_WINDOW_END_DAYS_AGO + si.ADS_WINDOW_DAYS - 1}"
                        f"-{si.ADS_WINDOW_END_DAYS_AGO} days ago (Blinkit has no SKU-level ads report; fresh days skipped).")
    if no_stock_cities:
        findings.append("Campaigns target cities where this SKU has no stock: " + ", ".join(sorted(no_stock_cities)[:8]) + ".")
    if camps and not master:
        findings.append("City names for campaign targeting are not loaded yet (voylla.blinkit_city_master).")
    return _report("Marketing", status, headline, findings, {
        "campaigns": out, "cities_targeted_without_stock": sorted(no_stock_cities),
    })


# ── Product ──────────────────────────────────────────────────────────────────
def product_agent(row, all_rows):
    group = row.get("sub_category") or row.get("category")
    key = "sub_category" if row.get("sub_category") else "category"
    peers = [r for r in all_rows if group and r.get(key) == group]
    units = sorted((_f(r.get("units_30d")) or 0 for r in peers), reverse=True)
    mine = _f(row.get("units_30d")) or 0
    rank = 1 + sum(1 for u in units if u > mine) if peers else None
    margins = [_f(r["profit_per_unit"]) / _f(r["lp"]) for r in peers
               if r.get("profit_per_unit") is not None and r.get("lp")]
    peer_margin = median(margins) if margins else None
    my_margin = (_f(row["profit_per_unit"]) / _f(row["lp"])) if row.get("profit_per_unit") is not None and row.get("lp") else None

    if not mine:
        status, role = ("problem" if (_f(row.get("stock")) or 0) > 0 else "watch"), "dead"
        headline = "No sale in 30 days" + (" with stock on hand" if status == "problem" else "")
    elif rank is not None and rank <= max(1, round(len(peers) * HERO_TOP_SHARE)):
        status, role, headline = "ok", "hero", f"Hero: #{rank} of {len(peers)} in {group}"
    elif rank is not None and rank > len(peers) / 2:
        status, role, headline = "watch", "tail", f"Tail: #{rank} of {len(peers)} in {group}"
    else:
        status, role = "ok", "middle"
        headline = f"#{rank} of {len(peers)} in {group}" if rank else "No category peers to compare"
    findings = []
    if peers:
        findings.append(f"Median {group} SKU sells {median(units):g} units in 30 days; this one {mine:g}.")
    if my_margin is not None and peer_margin is not None:
        findings.append(f"Margin {my_margin:.0%} vs {peer_margin:.0%} median in {group}.")
    return _report("Product", status, headline, findings, {
        "group": group, "peers": len(peers), "rank_by_units": rank, "role": role,
        "margin_pct": round(my_margin * 100) if my_margin is not None else None,
        "peer_median_margin_pct": round(peer_margin * 100) if peer_margin is not None else None,
    })


def department_reports(row, detail, all_rows):
    return [sales_agent(row, detail), inventory_agent(row, detail), price_agent(row, detail),
            marketing_agent(row, detail), product_agent(row, all_rows)]


# ── hard rules on the boss's answer ──────────────────────────────────────────
def blocked_actions(row, goal):
    """Actions the boss may not recommend for this SKU right now, with the
    reason. These protect money whatever the model says."""
    stock, u30 = _f(row.get("stock")) or 0, _f(row.get("units_30d")) or 0
    cover = si.days_of_cover(stock, u30)
    flags = row.get("flags") or []
    blocked = {}
    if stock <= 0:
        for a in SPEND_MORE:
            blocked[a] = "no stock on Blinkit - more ad spend cannot turn into sales"
    elif cover is not None and cover < si.LOW_COVER_DAYS:
        for a in SPEND_MORE:
            blocked[a] = f"only about {cover:g} days of stock left - more demand would run it out"
    if "below_cost" in flags and goal != "clear_stock":
        for a in SPEND_MORE:
            blocked.setdefault(a, "it sells below cost - every extra sale loses money")
    if u30:
        blocked["mark_dead"] = "it sold in the last 30 days"
    if stock <= 0 and not u30:
        blocked["restock"] = "no sale in 30 days - nothing shows demand for more stock"
    return blocked


def enforce(rec, row, goal):
    """Checks the boss's answer: unknown or blocked actions become no_action,
    with the reason kept so the human sees what was overruled."""
    rec = dict(rec or {})
    action = str(rec.get("action") or "").strip().lower()
    if action not in ACTIONS:
        rec["overruled"] = f"'{action or 'nothing'}' is not an allowed action"
        rec["action"] = "no_action"
        return rec
    why = blocked_actions(row, goal).get(action)
    if why:
        rec["overruled"] = f"{action} blocked: {why}"
        rec["action"] = "no_action"
    else:
        rec["action"] = action
    return rec


# ── the boss: one LLM call ───────────────────────────────────────────────────
BOSS_SYSTEM = """You are the boss of five department agents for one Voylla SKU sold on Blinkit \
(quick commerce). You get a goal and each department's short report. Recommend ONE action that best \
serves the goal, using only the numbers in the reports. If the data is too thin or conflicting, say \
no_action and what to check.

Allowed actions (use the key exactly): {actions}

Rules: never recommend more ad spend (raise_bid, add_to_campaign) for a SKU with no stock or under a \
week of stock, or one that sells below cost; never mark_dead a SKU that sold in 30 days. Ad numbers are \
campaign-level, shared by every product in the campaign - say so if it matters. You only advise: \
nothing you say is executed without a human.

Keep the answer short: one-sentence summary, at most 4 reasons \
(each with a number from the reports), at most 3 risks, at most 3 things to check first."""

# Structured outputs: the API only returns JSON that matches this schema, so the
# answer is always readable. (Claude Sonnet 5.5 refuses forced tool calls, which
# is why this is not a tool.) Every field required, no extra fields.
BOSS_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "summary": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "check_first": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["action", "summary", "reasons", "risks", "check_first", "confidence"],
    "additionalProperties": False,
}


def boss_prompt(goal, row, reports):
    sku = {k: row.get(k) for k in ("sku", "name", "category", "sub_category", "flags")}
    body = {"goal": GOALS[goal], "sku": sku,
            "reports": [{k: r[k] for k in ("department", "status", "headline", "findings", "facts")} for r in reports]}
    return json.dumps(body, default=str, separators=(",", ":"))


def _parse_json_object(text_):
    m = re.search(r"\{.*\}", text_ or "", re.S)
    if not m:
        raise ValueError("boss reply had no JSON object")
    return json.loads(m.group(0))


def ask_boss(row, reports, goal, client, model=BOSS_MODEL):
    """One LLM call. `client` is an anthropic.Anthropic (or a stand-in in tests).
    Returns the checked recommendation plus token usage."""
    if goal not in GOALS:
        raise ValueError(f"unknown goal {goal!r}; use one of {', '.join(GOALS)}")
    system = BOSS_SYSTEM.format(actions="; ".join(f"{k} = {v}" for k, v in ACTIONS.items()))
    resp = client.messages.create(model=model, max_tokens=BOSS_MAX_TOKENS, system=system,
                                  output_config={"format": {"type": "json_schema", "schema": BOSS_SCHEMA}},
                                  messages=[{"role": "user", "content": boss_prompt(goal, row, reports)}])
    stop = getattr(resp, "stop_reason", None)
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    if stop == "max_tokens":
        rec = {"action": "no_action", "summary": "The boss reply was cut off before it finished.",
               "error": f"hit the {BOSS_MAX_TOKENS}-token limit"}
    elif stop == "refusal":
        rec = {"action": "no_action", "summary": "The model declined to answer.",
               "error": f"refusal: {getattr(getattr(resp, 'stop_details', None), 'category', None)}"}
    else:
        try:
            rec = _parse_json_object(raw)
        except Exception as exc:
            rec = {"action": "no_action", "summary": "The boss reply could not be read.",
                   "error": f"{exc}; reply started: {raw[:200]!r}"}
    rec = enforce(rec, row, goal)
    rec["stop_reason"] = stop
    usage = getattr(resp, "usage", None)
    rec["model"] = model
    rec["input_tokens"] = getattr(usage, "input_tokens", None)
    rec["output_tokens"] = getattr(usage, "output_tokens", None)
    return rec


def save_recommendation(engine, row, goal, rec, requested_by=None):
    """Keeps every boss answer (Phase 3 will make these approvable). Writes only
    to its own log table - never to a bid, price or campaign table."""
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS voylla.sku_boss_recommendations (
                id serial PRIMARY KEY, created_at timestamp DEFAULT NOW(), brand text, sku text,
                pid text, goal text, action text, recommendation jsonb, model text,
                input_tokens int, output_tokens int, requested_by text)"""))
        conn.execute(text("""
            INSERT INTO voylla.sku_boss_recommendations
              (brand, sku, pid, goal, action, recommendation, model, input_tokens, output_tokens, requested_by)
            VALUES (:b, :s, :p, :g, :a, CAST(:r AS jsonb), :m, :i, :o, :u)"""),
            {"b": si.BRAND, "s": row.get("sku"), "p": str(row.get("pid") or ""), "g": goal,
             "a": rec.get("action"), "r": json.dumps(rec, default=str), "m": rec.get("model"),
             "i": rec.get("input_tokens"), "o": rec.get("output_tokens"), "u": requested_by})


# ── entry points used by the page and Agent Chat ─────────────────────────────
def sku_reports(engine, sku_query):
    """Find one SKU and run the five department agents. Returns
    {"row", "detail", "reports"} or {"matches": [...]} when the text is ambiguous."""
    rows = si.sku_snapshot(engine)
    found = si.find_skus(rows, sku_query)
    if len(found) != 1:
        return {"matches": [{"sku": r["sku"], "name": r.get("name")} for r in found[:15]], "total_matches": len(found)}
    row = found[0]
    detail = si.sku_detail(engine, row)
    return {"row": row, "detail": detail, "reports": department_reports(row, detail, rows)}


def recommend(engine, sku_query, goal, requested_by=None):
    import anthropic
    from db import get_anthropic_config

    found = sku_reports(engine, sku_query)
    if "row" not in found:
        return found
    cfg = get_anthropic_config()
    rec = ask_boss(found["row"], found["reports"], goal, anthropic.Anthropic(api_key=cfg["api_key"]))
    try:
        save_recommendation(engine, found["row"], goal, rec, requested_by)
    except Exception as exc:  # the answer still reaches the human
        rec["log_error"] = str(exc)
    found["recommendation"] = rec
    return found
