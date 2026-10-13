"""Auto-mode tracker: how the campaigns set to Auto are doing since the day
Auto started, day by day, week by week and month by month, against the same
campaigns before it - and against the brand's other campaigns over the same
days, so a festive week that lifts everything is not credited to the AI.

Weeks and months are counted from the start date (7- and 30-day blocks), not
calendar weeks, so no block mixes days before and after the switch. ROAS is
always total sales / total spend for the block, never an average of daily
ROAS values.
"""
from collections import Counter
from datetime import timedelta

from db import get_cursor

SETTLING_DAYS = 3          # Blinkit keeps adding sales to the last ~3 report days
BEFORE_BLOCKS = {"day": 14, "week": 4, "month": 3}   # how many blocks before the start to show
BLOCK_DAYS = {"day": 1, "week": 7, "month": 30}
IST = timedelta(hours=5, minutes=30)


def fetch_auto_campaigns(brand):
    """Campaigns set to Auto for the brand and the IST date each was switched."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT campaign_id::text AS campaign_id, set_at FROM voylla.campaign_autonomy_mode
               WHERE brand = %(brand)s AND mode = 'auto' AND campaign_id::text ~ '^[0-9]+$'""",
            {"brand": brand},
        )
        rows = cur.fetchall()
    return {r["campaign_id"]: ((r["set_at"] + IST).date() if r["set_at"] else None) for r in rows}


def fetch_campaign_daily(brand, start):
    """One row per campaign per report day from `start`, every campaign of the brand."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT "Campaign ID"::text AS campaign_id, MAX("Campaign Name") AS campaign_name,
                      TO_TIMESTAMP("Date", 'YYYY-MM-DD HH24:MI:SS')::date AS d,
                      SUM("Estimated Budget Consumed") AS spend,
                      SUM("Direct Sales" + "Indirect Sales") AS sales,
                      SUM("Impressions") AS impressions
               FROM voylla."Blinkit_Ads_Report"
               WHERE "Brand" = %(brand)s
                 AND TO_TIMESTAMP("Date", 'YYYY-MM-DD HH24:MI:SS')::date >= %(start)s
               GROUP BY 1, 3""",
            {"brand": brand, "start": start},
        )
        return [dict(r, spend=float(r["spend"] or 0), sales=float(r["sales"] or 0),
                     impressions=int(r["impressions"] or 0)) for r in cur.fetchall()]


def default_start(auto_campaigns):
    """The date most Auto campaigns were switched on (latest one on a tie)."""
    dates = [d for d in auto_campaigns.values() if d]
    if not dates:
        return None
    counts = Counter(dates)
    return max(counts, key=lambda d: (counts[d], d))


def _agg(rows):
    spend = sum(r["spend"] for r in rows)
    sales = sum(r["sales"] for r in rows)
    return {"spend": spend, "sales": sales, "impressions": sum(r["impressions"] for r in rows),
            "roas": round(sales / spend, 2) if spend else None}


def _pct(new, old):
    return round((new / old - 1) * 100) if old and new is not None else None


def build_blocks(daily, start, last_day, granularity="day"):
    """Splits rows (dicts with d, spend, sales, impressions) into blocks around
    `start`: BEFORE_BLOCKS[granularity] blocks before it, then blocks up to
    `last_day` (the newest report day). Returns (blocks, baseline) where
    baseline is all the before-blocks together, per day."""
    size = BLOCK_DAYS[granularity]
    first = start - timedelta(days=size * BEFORE_BLOCKS[granularity])
    blocks = []
    b_start, n = first, -BEFORE_BLOCKS[granularity]
    while b_start <= last_day:
        b_end = b_start + timedelta(days=size - 1)
        rows = [r for r in daily if b_start <= r["d"] <= b_end]
        days_landed = (min(b_end, last_day) - b_start).days + 1
        agg = _agg(rows)
        k = n if n < 0 else n + 1  # -2, -1, then 1, 2 (no block 0)
        if granularity == "day":
            label = b_start.strftime("%d %b")
        else:
            unit = "Week" if granularity == "week" else "Month"
            label = f"{unit} {k:+d}" if k < 0 else f"{unit} {k}"
        blocks.append(dict(
            agg, label=label, start=b_start, end=b_end, after=b_start >= start,
            days=days_landed, partial=days_landed < size,
            settling=b_end > last_day - timedelta(days=SETTLING_DAYS),
            spend_per_day=agg["spend"] / days_landed, sales_per_day=agg["sales"] / days_landed,
        ))
        b_start, n = b_end + timedelta(days=1), n + 1

    before = [b for b in blocks if not b["after"]]
    before_days = sum(b["days"] for b in before)
    base = _agg([dict(spend=b["spend"], sales=b["sales"], impressions=b["impressions"]) for b in before])
    baseline = dict(base, days=before_days,
                    spend_per_day=base["spend"] / before_days if before_days else 0,
                    sales_per_day=base["sales"] / before_days if before_days else 0)
    for b in blocks:
        b["roas_vs_base"] = _pct(b["roas"], baseline["roas"]) if b["after"] else None
        b["sales_vs_base"] = _pct(b["sales_per_day"], baseline["sales_per_day"]) if b["after"] else None
        b["spend_vs_base"] = _pct(b["spend_per_day"], baseline["spend_per_day"]) if b["after"] else None
    return blocks, baseline


def campaign_rows(daily, auto_campaigns, start, last_day, before_days=14):
    """One row per Auto campaign: `before_days` before ITS OWN switch date vs
    every day since it, per day, so campaigns switched on different days are
    each judged from their own start."""
    names = {}
    for r in daily:
        names[r["campaign_id"]] = r.get("campaign_name") or names.get(r["campaign_id"])
    out = []
    for cid, own_start in auto_campaigns.items():
        s = own_start or start
        rows = [r for r in daily if r["campaign_id"] == cid]
        before = _agg([r for r in rows if s - timedelta(days=before_days) <= r["d"] < s])
        after_rows = [r for r in rows if s <= r["d"] <= last_day]
        after = _agg(after_rows)
        after_days = max((last_day - s).days + 1, 0)
        out.append({
            "campaign_id": cid, "campaign_name": names.get(cid) or cid, "start": s,
            "after_days": after_days,
            "roas_before": before["roas"], "roas_after": after["roas"],
            "roas_change": _pct(after["roas"], before["roas"]),
            "spend_before_pd": before["spend"] / before_days,
            "spend_after_pd": after["spend"] / after_days if after_days else None,
            "sales_before_pd": before["sales"] / before_days,
            "sales_after_pd": after["sales"] / after_days if after_days else None,
        })
    out.sort(key=lambda r: -(r["spend_after_pd"] or 0))
    return out


def tracker(brand, granularity="day", start=None, today=None):
    """Everything the page needs. `start` defaults to the day most Auto
    campaigns were switched on."""
    from datetime import date
    today = today or date.today()
    auto = fetch_auto_campaigns(brand)
    start = start or default_start(auto)
    if not auto or not start:
        return {"auto": auto, "start": None}
    size = BLOCK_DAYS[granularity]
    earliest = min([start - timedelta(days=size * BEFORE_BLOCKS[granularity])]
                   + [d - timedelta(days=14) for d in auto.values() if d])
    daily = fetch_campaign_daily(brand, earliest)
    if not daily:
        return {"auto": auto, "start": start, "no_data": True}
    last_day = min(max(r["d"] for r in daily), today - timedelta(days=1))

    def totals(rows):
        by_day = {}
        for r in rows:
            t = by_day.setdefault(r["d"], {"d": r["d"], "spend": 0.0, "sales": 0.0, "impressions": 0})
            t["spend"] += r["spend"]
            t["sales"] += r["sales"]
            t["impressions"] += r["impressions"]
        return sorted(by_day.values(), key=lambda t: t["d"])

    auto_daily = totals([r for r in daily if r["campaign_id"] in auto])
    other_daily = totals([r for r in daily if r["campaign_id"] not in auto])
    blocks, baseline = build_blocks(auto_daily, start, last_day, granularity)
    other_blocks, other_base = build_blocks(other_daily, start, last_day, granularity)
    for b, o in zip(blocks, other_blocks):
        b["other_roas"] = o["roas"]
        b["other_roas_vs_base"] = o["roas_vs_base"]

    def since(rows):
        after = [r for r in rows if start <= r["d"] <= last_day]
        days = max((last_day - start).days + 1, 0)
        a = _agg(after)
        return dict(a, days=days, spend_per_day=a["spend"] / days if days else 0,
                    sales_per_day=a["sales"] / days if days else 0)

    since_auto, since_other = since(auto_daily), since(other_daily)
    for s, base in ((since_auto, baseline), (since_other, other_base)):
        s["roas_vs_base"] = _pct(s["roas"], base["roas"])
        s["sales_vs_base"] = _pct(s["sales_per_day"], base["sales_per_day"])
        s["spend_vs_base"] = _pct(s["spend_per_day"], base["spend_per_day"])

    return {
        "auto": auto, "start": start, "last_day": last_day, "granularity": granularity,
        "blocks": blocks, "baseline": baseline, "other_baseline": other_base,
        "since": since_auto, "other_since": since_other,
        "campaigns": campaign_rows(daily, auto, start, last_day),
        "settling_from": last_day - timedelta(days=SETTLING_DAYS - 1),
        "start_options": sorted({d for d in auto.values() if d}, reverse=True),
    }
