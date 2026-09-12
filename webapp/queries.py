"""
All SQL for the Blinkit dashboard lives here. The ROAS-impact and
campaign-status queries are ports of the two reference queries used to build
the old Power BI report / Power Apps scheduler - the write helpers below
reproduce the exact Patch() logic those two canvas apps used, so this
dashboard writes to the same columns the same way and the existing scheduled
notebooks (Blinkit_Campaign_Control.ipynb etc.) keep working unchanged.
"""
from datetime import date, datetime, time, timedelta

from db import get_cursor

ACTION_OPTIONS = ["NO_CHANGE", "INCREASE_CPM", "DECREASE_CPM", "PAUSE", "ZOMBIE_FLAG"]

ALL_BRANDS = "__ALL__"

# The Chumbak AI-bids-only experiment: a deliberately single-variable test of
# the bid agent (pacing schedule paused so the ROAS delta is attributable to
# the agent alone). Live from 2026-09-10 across these 12 campaigns; 4
# Teacher's Day campaigns were excluded on purpose since they were already
# stopped and the agent wanted to raise zero-ROAS bids on them.
CHUMBAK_SHOWCASE_CAMPAIGN_IDS = [
    354499, 354509, 359470, 359869, 379520, 429819,
    497432, 497437, 500151, 607078, 607168, 624587,
]
CHUMBAK_SHOWCASE_EXCLUDED_IDS = [640511, 640513, 640515, 640517]
CHUMBAK_SHOWCASE_GO_LIVE = date(2026, 9, 10)

ROAS_IMPACT_SQL = """
WITH actions AS (
    SELECT
        l.unique_key,
        l.campaign_id,
        l.campaign_name,
        l.targeting,
        l.action_date::date                       AS action_date,
        l.action,
        l.rule_action,
        l.quick_action,
        l.decision_step,
        l.confidence,
        l.bid_change,
        l.current_cpm,
        l.campaign_budget,
        l.explanation,
        l.alternative_keywords::TEXT              AS alternative_keywords,
        l.user_implemented,
        l.override_action,
        l.override_note,
        l.cpm_llm_override,
        l.cpm_change_user,
        l.implementation_date,
        l.update_status,
        l.campaign_suggestion,
        l.campaign_suggestion_acceptance,
        l."Brand",
        (l.rule_action IS NOT NULL
         AND l.rule_action <> ''
         AND l.rule_action <> l.action)           AS diverged,
        (l.update_status = 'done')                AS was_implemented,
        CASE
            WHEN l.user_implemented = 'false' AND l.override_action ILIKE 'INCREASE%%'
                THEN (l.current_cpm * (1 + COALESCE(l.cpm_change_user, 0) / 100.0))::INT
            WHEN l.user_implemented = 'false' AND l.override_action ILIKE 'DECREASE%%'
                THEN (l.current_cpm * (1 - COALESCE(l.cpm_change_user, 0) / 100.0))::INT
            WHEN l.user_implemented = 'true'  AND l.cpm_llm_override IS NOT NULL
                 AND l.action ILIKE 'INCREASE%%'
                THEN (l.current_cpm * (1 + l.cpm_llm_override / 100.0))::INT
            WHEN l.user_implemented = 'true'  AND l.cpm_llm_override IS NOT NULL
                 AND l.action ILIKE 'DECREASE%%'
                THEN (l.current_cpm * (1 - l.cpm_llm_override / 100.0))::INT
            WHEN l.user_implemented = 'true'  AND l.action ILIKE 'INCREASE%%'
                THEN (l.current_cpm + COALESCE(l.bid_change, 0))::INT
            WHEN l.user_implemented = 'true'  AND l.action ILIKE 'DECREASE%%'
                THEN (l.current_cpm - COALESCE(l.bid_change, 0))::INT
            ELSE NULL
        END                                       AS cpm_intended
    FROM voylla."Blinkit_actions_llm" l
    WHERE (%(brand)s = '__ALL__' OR l."Brand" = %(brand)s)
      AND l.action_date ~ '^\\d{4}-\\d{2}-\\d{2}'
      AND l.action_date::date >= %(since)s
),
performance AS (
    SELECT
        TO_TIMESTAMP("Date", 'YYYY-MM-DD HH24:MI:SS')::date AS report_date,
        "Campaign ID"::TEXT                        AS campaign_id,
        "Targeting Value"                          AS targeting,
        SUM("Estimated Budget Consumed")           AS spend,
        SUM("Direct Sales" + "Indirect Sales")     AS sales,
        SUM("Impressions")                         AS impressions
    FROM voylla."Blinkit_Ads_Report"
    WHERE (%(brand)s = '__ALL__' OR "Brand" = %(brand)s)
    GROUP BY 1, 2, 3
),
windows AS (
    SELECT
        a.unique_key,
        SUM(p.spend)       FILTER (WHERE p.report_date BETWEEN a.action_date - 7 AND a.action_date - 1) AS spend_before,
        SUM(p.sales)       FILTER (WHERE p.report_date BETWEEN a.action_date - 7 AND a.action_date - 1) AS sales_before,
        SUM(p.impressions) FILTER (WHERE p.report_date BETWEEN a.action_date - 7 AND a.action_date - 1) AS impressions_before,
        COUNT(*)           FILTER (WHERE p.report_date BETWEEN a.action_date - 7 AND a.action_date - 1) AS days_before,
        SUM(p.spend)       FILTER (WHERE p.report_date BETWEEN a.action_date + 1 AND a.action_date + 7) AS spend_after,
        SUM(p.sales)       FILTER (WHERE p.report_date BETWEEN a.action_date + 1 AND a.action_date + 7) AS sales_after,
        SUM(p.impressions) FILTER (WHERE p.report_date BETWEEN a.action_date + 1 AND a.action_date + 7) AS impressions_after,
        COUNT(*)           FILTER (WHERE p.report_date BETWEEN a.action_date + 1 AND a.action_date + 7) AS days_after
    FROM actions a
    LEFT JOIN performance p
           ON a.campaign_id = p.campaign_id
          AND lower(replace(a.targeting, '_', ' ')) = lower(p.targeting)
    GROUP BY a.unique_key
)
SELECT
    a.*,
    w.spend_before, w.sales_before, w.impressions_before, w.days_before,
    w.spend_after, w.sales_after, w.impressions_after, w.days_after,
    ROUND((w.sales_before / NULLIF(w.spend_before, 0))::NUMERIC, 2) AS roas_before,
    ROUND((w.sales_after  / NULLIF(w.spend_after,  0))::NUMERIC, 2) AS roas_after,
    ROUND(((w.sales_after / NULLIF(w.spend_after, 0))
         - (w.sales_before / NULLIF(w.spend_before, 0)))::NUMERIC, 2) AS roas_change,
    ROUND((w.spend_after - w.spend_before)::NUMERIC, 2)  AS spend_change,
    (w.impressions_after - w.impressions_before)         AS impressions_change,
    CASE
        WHEN a.update_status = 'done'
         AND w.spend_before > 0 AND w.spend_after > 0
        THEN CASE
                WHEN (w.sales_after / w.spend_after) > (w.sales_before / w.spend_before) THEN 'IMPROVED'
                WHEN (w.sales_after / w.spend_after) < (w.sales_before / w.spend_before) THEN 'WORSENED'
                ELSE 'FLAT'
             END
        ELSE NULL
    END AS verdict
FROM actions a
LEFT JOIN windows w ON w.unique_key = a.unique_key
ORDER BY a.action_date DESC, a.campaign_name, a.targeting
"""

CAMPAIGN_STATUS_SQL = """
WITH scoped AS (
    -- filter to the brand(s) we actually want FIRST - Blinkit_Campaign_Runtime
    -- accumulates one row per campaign per check, so this table can be large
    -- and a per-row correlated subquery over the unfiltered table was slow
    -- enough to make this page look like it hung.
    SELECT *
    FROM voylla."Blinkit_Campaign_Runtime"
    WHERE (%(brand)s = '__ALL__' OR brand = %(brand)s)
),
latest_per_brand AS (
    SELECT brand, MAX(log_date) AS max_log_date FROM scoped GROUP BY brand
)
SELECT DISTINCT ON (r.campaign_id)
       r.campaign_id::TEXT                AS campaign_id,
       r.campaign_name,
       r.brand,
       r.budget,
       r.last_status,
       COALESCE(e.window_count, 0)        AS window_count,
       e.next_start,
       e.last_end
FROM scoped r
JOIN latest_per_brand l ON l.brand = r.brand AND l.max_log_date = r.log_date
LEFT JOIN (
    SELECT campaign_id,
           COUNT(*)        AS window_count,
           MIN(start_date) AS next_start,
           MAX(end_date)   AS last_end
    FROM voylla."Blinkit_Campaign_Schedule_Entries"
    GROUP BY campaign_id
) e ON r.campaign_id::TEXT = e.campaign_id
ORDER BY r.campaign_id, r.last_checked DESC NULLS LAST
"""


def fetch_brands():
    with get_cursor() as cur:
        cur.execute(
            'SELECT DISTINCT brand FROM voylla."Blinkit_Campaign_Runtime" '
            "WHERE brand IS NOT NULL ORDER BY brand"
        )
        rows = [r["brand"] for r in cur.fetchall()]
    return rows or ["Voylla", "Chumbak", "Petcrux"]


def fetch_roas_impact(brand, since_days=30, verdict=None, search=None):
    since = date.today().fromordinal(date.today().toordinal() - since_days)
    with get_cursor() as cur:
        cur.execute(ROAS_IMPACT_SQL, {"brand": brand, "since": since})
        rows = cur.fetchall()
    if verdict:
        rows = [r for r in rows if r["verdict"] == verdict]
    if search:
        s = search.lower()
        rows = [
            r
            for r in rows
            if s in (r["campaign_name"] or "").lower() or s in (r["targeting"] or "").lower()
        ]
    return rows


def fetch_campaign_status(brand):
    with get_cursor() as cur:
        cur.execute(CAMPAIGN_STATUS_SQL, {"brand": brand})
        return cur.fetchall()


def fetch_channel_daily(brand, days=180, campaign_ids=None):
    """Whole-channel (or campaign-scoped) daily performance straight from
    Blinkit_Ads_Report, independent of any individual AI recommendation -
    this is the actual portfolio ROAS. Also pulls ATC and New Users Acquired
    and average keyword position, since those are the metrics the Power BI
    Overview/Daily Report pages already track and were otherwise unused here.
    """
    start = date.today() - timedelta(days=days)
    params = {"brand": brand, "start": start}
    campaign_filter = ""
    if campaign_ids:
        campaign_filter = 'AND "Campaign ID" = ANY(%(campaign_ids)s)'
        params["campaign_ids"] = campaign_ids

    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT
                TO_TIMESTAMP("Date",'YYYY-MM-DD HH24:MI:SS')::date AS report_date,
                SUM("Estimated Budget Consumed")                  AS spend,
                SUM("Direct Sales" + "Indirect Sales")            AS sales,
                SUM("Impressions")                                AS impressions,
                SUM("Direct ATC" + "Indirect ATC")                AS atc,
                SUM("New Users Acquired")                         AS new_users,
                AVG("Most Viewed Position")                       AS avg_position
            FROM voylla."Blinkit_Ads_Report"
            WHERE "Brand" = %(brand)s
              AND TO_TIMESTAMP("Date",'YYYY-MM-DD HH24:MI:SS')::date >= %(start)s
              {campaign_filter}
            GROUP BY 1
            ORDER BY 1
            """,
            params,
        )
        rows = cur.fetchall()

    out = []
    for r in rows:
        spend = float(r["spend"] or 0)
        sales = float(r["sales"] or 0)
        out.append({
            "date": r["report_date"],
            "spend": spend,
            "sales": sales,
            "roas": round(sales / spend, 2) if spend else None,
            "impressions": int(r["impressions"] or 0),
            "atc": int(r["atc"] or 0),
            "new_users": int(r["new_users"] or 0),
            "avg_position": round(float(r["avg_position"]), 1) if r["avg_position"] is not None else None,
        })
    return out


def bucket_channel_series(daily, granularity="day"):
    """Aggregates fetch_channel_daily's rows into week or month buckets -
    ROAS is always recomputed from summed spend/sales (spend-weighted), never
    averaged day-to-day, so a single huge day can't be diluted or a single
    zero day can't crater the bucket."""
    if granularity == "day":
        return [dict(d, label=str(d["date"])) for d in daily]

    buckets, order = {}, []
    for d in daily:
        dt = d["date"]
        if granularity == "week":
            iso = dt.isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
        else:
            key = dt.strftime("%Y-%m")
        if key not in buckets:
            buckets[key] = {
                "label": key, "spend": 0.0, "sales": 0.0, "impressions": 0,
                "atc": 0, "new_users": 0, "_pos_sum": 0.0, "_pos_n": 0,
            }
            order.append(key)
        b = buckets[key]
        b["spend"] += d["spend"]
        b["sales"] += d["sales"]
        b["impressions"] += d["impressions"]
        b["atc"] += d["atc"]
        b["new_users"] += d["new_users"]
        if d["avg_position"] is not None:
            b["_pos_sum"] += d["avg_position"]
            b["_pos_n"] += 1

    out = []
    for key in order:
        b = buckets[key]
        b["roas"] = round(b["sales"] / b["spend"], 2) if b["spend"] else None
        b["avg_position"] = round(b["_pos_sum"] / b["_pos_n"], 1) if b["_pos_n"] else None
        del b["_pos_sum"], b["_pos_n"]
        out.append(b)
    return out


def channel_before_after(daily, cutover_date, window_days=7):
    """Compares `window_days` days before the cutover to the days actually
    available on/after it - if fewer than `window_days` have landed yet,
    `after_is_partial` says so instead of silently padding with nothing, and
    the ROAS is still spend-weighted rather than an average of daily ratios.
    """
    before = [d for d in daily if cutover_date - timedelta(days=window_days) <= d["date"] < cutover_date]
    after = [d for d in daily if cutover_date <= d["date"] < cutover_date + timedelta(days=window_days)]

    def agg(rows):
        spend = sum(r["spend"] for r in rows)
        sales = sum(r["sales"] for r in rows)
        return {
            "spend": round(spend, 2),
            "sales": round(sales, 2),
            "roas": round(sales / spend, 2) if spend else None,
            "days": len(rows),
            "atc": sum(r["atc"] for r in rows),
            "new_users": sum(r["new_users"] for r in rows),
        }

    b, a = agg(before), agg(after)
    return {
        "before": b,
        "after": a,
        "window_days": window_days,
        "after_is_partial": a["days"] < window_days,
    }


def _norm_keyword(t):
    return (t or "").strip().lower().replace("_", " ")


def fetch_action_dates(brand, limit=30):
    """Distinct action_date values available for a brand, most recent first -
    powers the date picker on Pending Actions instead of only ever showing
    the single latest date."""
    with get_cursor() as cur:
        cur.execute(
            'SELECT DISTINCT action_date FROM voylla."Blinkit_actions_llm" '
            'WHERE "Brand" = %(brand)s AND action_date ~ %(pat)s '
            "ORDER BY action_date DESC LIMIT %(limit)s",
            {"brand": brand, "pat": r"^\d{4}-\d{2}-\d{2}", "limit": limit},
        )
        return [r["action_date"] for r in cur.fetchall()]


def fetch_before_after_matrix(brand, cutover_date, pre_days=7, min_spend=60, campaign_ids=None):
    """Per-keyword before/after comparison, portfolio-style: BEFORE is the
    daily average over `pre_days` days ending the day before cutover_date,
    AFTER is the daily average from cutover_date through the latest day
    Blinkit has reported. This is intentionally independent of whether the
    LLM actually recommended anything for a keyword - a keyword the agent
    left untouched can still move (or not) and that's signal too.

    Verdicts are reported two ways on purpose: a naive per-keyword count,
    and a spend-weighted view (% of the AFTER period's spend). A handful of
    near-zero-spend keywords flipping to WORSE can dominate the naive count
    while being financially meaningless - `min_spend` (Rs/day) is the cutoff
    used to flag those as "noise" rather than folding them into the verdict.
    """
    pre_start = cutover_date - timedelta(days=pre_days)
    params = {"brand": brand, "pre_start": pre_start}
    campaign_filter = ""
    if campaign_ids:
        campaign_filter = 'AND "Campaign ID" = ANY(%(campaign_ids)s)'
        params["campaign_ids"] = campaign_ids

    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT
                "Campaign ID"::TEXT AS campaign_id,
                "Targeting Value" AS targeting,
                TO_TIMESTAMP("Date",'YYYY-MM-DD HH24:MI:SS')::date AS report_date,
                SUM("Estimated Budget Consumed") AS spend,
                SUM("Direct Sales" + "Indirect Sales") AS sales
            FROM voylla."Blinkit_Ads_Report"
            WHERE "Brand" = %(brand)s
              AND TO_TIMESTAMP("Date",'YYYY-MM-DD HH24:MI:SS')::date >= %(pre_start)s
              {campaign_filter}
            GROUP BY 1, 2, 3
            """,
            params,
        )
        perf_rows = cur.fetchall()

        cur.execute(
            'SELECT campaign_id, targeting, action, override_action, user_implemented '
            'FROM voylla."Blinkit_actions_llm" '
            'WHERE "Brand" = %(brand)s AND action_date::date = %(cutover)s',
            {"brand": brand, "cutover": cutover_date},
        )
        action_rows = cur.fetchall()

        cur.execute(
            'SELECT DISTINCT campaign_id::TEXT AS campaign_id, campaign_name '
            'FROM voylla."Blinkit_Campaign_Runtime" WHERE brand = %(brand)s',
            {"brand": brand},
        )
        name_map = {r["campaign_id"]: r["campaign_name"] for r in cur.fetchall()}

    actions_idx = {}
    for a in action_rows:
        key = (str(a["campaign_id"]), _norm_keyword(a["targeting"]))
        actions_idx[key] = a

    agg = {}
    for r in perf_rows:
        key = (r["campaign_id"], _norm_keyword(r["targeting"]))
        b = agg.setdefault(
            key,
            {
                "campaign_id": r["campaign_id"],
                "campaign_name": name_map.get(r["campaign_id"]) or r["campaign_id"],
                "targeting": r["targeting"],
                "spend_before": 0.0, "sales_before": 0.0, "days_before": 0,
                "spend_after": 0.0, "sales_after": 0.0, "days_after": 0,
            },
        )
        if pre_start <= r["report_date"] < cutover_date:
            b["spend_before"] += float(r["spend"] or 0)
            b["sales_before"] += float(r["sales"] or 0)
            b["days_before"] += 1
        elif r["report_date"] >= cutover_date:
            b["spend_after"] += float(r["spend"] or 0)
            b["sales_after"] += float(r["sales"] or 0)
            b["days_after"] += 1

    rows_out = []
    for key, b in agg.items():
        if not b["days_after"]:
            continue  # keyword didn't run at all in the AFTER window - not comparable
        days_before = b["days_before"] or 1
        spend_before_avg = b["spend_before"] / days_before
        sales_before_avg = b["sales_before"] / days_before
        spend_after_avg = b["spend_after"] / b["days_after"]
        sales_after_avg = b["sales_after"] / b["days_after"]
        roas_before = round(sales_before_avg / spend_before_avg, 2) if spend_before_avg else None
        roas_after = round(sales_after_avg / spend_after_avg, 2) if spend_after_avg else 0.0

        if roas_before is None:
            verdict = "NEW"
        elif roas_after > roas_before + 0.05:
            verdict = "BETTER"
        elif roas_after < roas_before - 0.05:
            verdict = "WORSE"
        else:
            verdict = "SAME"

        a = actions_idx.get(key)
        if a:
            action_label = a["override_action"] or a["action"]
        else:
            action_label = "NOT TARGETED"

        rows_out.append({
            "campaign_id": b["campaign_id"],
            "campaign_name": b["campaign_name"],
            "targeting": b["targeting"],
            "action": action_label,
            "spend_before": round(spend_before_avg, 2),
            "spend_after": round(spend_after_avg, 2),
            "roas_before": roas_before,
            "roas_after": roas_after,
            "roas_delta": round(roas_after - roas_before, 2) if roas_before is not None else None,
            "verdict": verdict,
            "meaningful": spend_after_avg >= min_spend or spend_before_avg >= min_spend,
        })

    rows_out.sort(key=lambda r: r["spend_after"], reverse=True)

    total_spend_after = sum(r["spend_after"] for r in rows_out) or 1
    naive = {"BETTER": 0, "WORSE": 0, "SAME": 0, "NEW": 0}
    weighted_spend = {"BETTER": 0.0, "WORSE": 0.0, "SAME": 0.0, "NEW": 0.0}
    meaningful_naive = {"BETTER": 0, "WORSE": 0, "SAME": 0, "NEW": 0}
    for r in rows_out:
        naive[r["verdict"]] += 1
        weighted_spend[r["verdict"]] += r["spend_after"]
        if r["meaningful"]:
            meaningful_naive[r["verdict"]] += 1

    portfolio_spend_before = sum(r["spend_before"] for r in rows_out)
    portfolio_sales_before = sum(
        r["spend_before"] * r["roas_before"] for r in rows_out if r["roas_before"] is not None
    )
    portfolio_spend_after = total_spend_after
    portfolio_sales_after = sum(r["spend_after"] * r["roas_after"] for r in rows_out)

    summary = {
        "cutover": cutover_date,
        "pre_days": pre_days,
        "min_spend": min_spend,
        "keyword_count": len(rows_out),
        "naive_counts": naive,
        "meaningful_counts": meaningful_naive,
        "spend_weighted_pct": {
            k: round(v / total_spend_after * 100, 1) for k, v in weighted_spend.items()
        },
        "portfolio_roas_before": round(portfolio_sales_before / portfolio_spend_before, 2) if portfolio_spend_before else None,
        "portfolio_roas_after": round(portfolio_sales_after / portfolio_spend_after, 2) if portfolio_spend_after else None,
        "portfolio_spend_before": round(portfolio_spend_before, 2),
        "portfolio_spend_after": round(portfolio_spend_after, 2),
    }
    return rows_out, summary


def fetch_brand_summary(rows):
    """Groups already-fetched ROAS-impact rows by brand for the 'All Brands'
    overview - no extra query, just a reduction over what fetch_roas_impact
    already returned."""
    summary = {}
    for r in rows:
        b = r.get("Brand")
        bucket = summary.setdefault(
            b, {"brand": b, "total": 0, "improved": 0, "worsened": 0, "flat": 0, "changes": []}
        )
        bucket["total"] += 1
        if r["verdict"]:
            bucket[r["verdict"].lower()] += 1
            if r["roas_change"] is not None:
                bucket["changes"].append(float(r["roas_change"]))
    out = []
    for b in sorted(summary.keys(), key=lambda x: x or ""):
        bucket = summary[b]
        changes = bucket.pop("changes")
        bucket["avg_roas_change"] = round(sum(changes) / len(changes), 2) if changes else None
        out.append(bucket)
    return out


def fetch_latest_action_date(brand):
    with get_cursor() as cur:
        cur.execute(
            'SELECT MAX(action_date) AS d FROM voylla."Blinkit_actions_llm" '
            'WHERE "Brand" = %(brand)s AND action_date ~ %(pat)s',
            {"brand": brand, "pat": r"^\d{4}-\d{2}-\d{2}"},
        )
        row = cur.fetchone()
        return row["d"] if row else None


def fetch_pending_actions(brand, action_date):
    with get_cursor() as cur:
        cur.execute(
            'SELECT * FROM voylla."Blinkit_actions_llm" '
            'WHERE "Brand" = %(brand)s AND action_date = %(action_date)s '
            "ORDER BY campaign_name, targeting",
            {"brand": brand, "action_date": action_date},
        )
        return cur.fetchall()


def accept_action(unique_key, brand, accept, cpm_llm_override, note):
    """Mirrors the marketing app's LLM_Accept Submit1.OnSelect Patch()."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            'UPDATE voylla."Blinkit_actions_llm" '
            "SET user_implemented = %(accept)s, "
            "    cpm_llm_override = %(cpm)s, "
            "    override_note = %(note)s, "
            "    implementation_date = CURRENT_DATE "
            'WHERE unique_key = %(unique_key)s AND "Brand" = %(brand)s',
            {
                "accept": "true" if accept else "false",
                "cpm": cpm_llm_override,
                "note": note,
                "unique_key": unique_key,
                "brand": brand,
            },
        )
        return cur.rowcount


def override_action(unique_key, brand, override_action_value, cpm_change_user, note):
    """Mirrors the marketing app's Action_Override Submit OnSelect Patch()."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            'UPDATE voylla."Blinkit_actions_llm" '
            "SET user_implemented = 'false', "
            "    override_action = %(override_action)s, "
            "    cpm_change_user = %(cpm)s, "
            "    override_note = %(note)s, "
            "    implementation_date = CURRENT_DATE "
            'WHERE unique_key = %(unique_key)s AND "Brand" = %(brand)s',
            {
                "override_action": override_action_value,
                "cpm": cpm_change_user,
                "note": note,
                "unique_key": unique_key,
                "brand": brand,
            },
        )
        return cur.rowcount


def fetch_schedule_entries(campaign_id):
    with get_cursor() as cur:
        cur.execute(
            'SELECT * FROM voylla."Blinkit_Campaign_Schedule_Entries" '
            "WHERE campaign_id = %(campaign_id)s ORDER BY start_date",
            {"campaign_id": str(campaign_id)},
        )
        return cur.fetchall()


def add_schedule_window(campaign_id, campaign_name, brand):
    """Mirrors btnAddWindow: new PENDING window defaulted to today, 00:00-00:00."""
    import uuid

    entry_id = str(uuid.uuid4())
    with get_cursor(commit=True) as cur:
        cur.execute(
            'INSERT INTO voylla."Blinkit_Campaign_Schedule_Entries" '
            "(entry_id, campaign_id, campaign_name, brand, start_date, end_date, "
            " start_time, end_time, is_active, status, pause_on_gap, updated_at) "
            "VALUES (%(entry_id)s, %(campaign_id)s, %(campaign_name)s, %(brand)s, "
            "        CURRENT_DATE, CURRENT_DATE, '00:00:00', '00:00:00', "
            "        true, 'PENDING', false, NOW())",
            {
                "entry_id": entry_id,
                "campaign_id": str(campaign_id),
                "campaign_name": campaign_name,
                "brand": brand,
            },
        )
    return entry_id


class ScheduleConflict(Exception):
    pass


def save_schedule_window(entry_id, campaign_id, start_date, end_date, start_hour, end_hour, budget):
    """Mirrors btnSaveEntry: validates date/hour ordering and overlap with
    other windows for the same campaign before Patch()-ing."""
    if end_date < start_date:
        raise ScheduleConflict("'Date to' cannot be before 'Date from'.")
    if end_hour <= start_hour:
        raise ScheduleConflict("End hour must be after start hour.")

    start_time = time(start_hour, 0, 0)
    end_time = time(23, 59, 59) if end_hour == 24 else time(end_hour, 0, 0)

    with get_cursor(commit=True) as cur:
        cur.execute(
            'SELECT entry_id FROM voylla."Blinkit_Campaign_Schedule_Entries" '
            "WHERE campaign_id = %(campaign_id)s AND entry_id <> %(entry_id)s "
            "  AND start_date <= %(end_date)s AND end_date >= %(start_date)s "
            "  AND start_time < %(end_time)s AND end_time > %(start_time)s",
            {
                "campaign_id": str(campaign_id),
                "entry_id": entry_id,
                "start_date": start_date,
                "end_date": end_date,
                "start_time": start_time,
                "end_time": end_time,
            },
        )
        if cur.fetchone():
            raise ScheduleConflict(
                "Overlaps another window for this campaign - change the dates or the hours."
            )

        cur.execute(
            'UPDATE voylla."Blinkit_Campaign_Schedule_Entries" '
            "SET start_date = %(start_date)s, end_date = %(end_date)s, "
            "    start_time = %(start_time)s, end_time = %(end_time)s, "
            "    budget = %(budget)s, is_active = true, status = 'PENDING', "
            "    updated_at = NOW() "
            "WHERE entry_id = %(entry_id)s",
            {
                "start_date": start_date,
                "end_date": end_date,
                "start_time": start_time,
                "end_time": end_time,
                "budget": budget,
                "entry_id": entry_id,
            },
        )


def remove_schedule_window(entry_id):
    with get_cursor(commit=True) as cur:
        cur.execute(
            'DELETE FROM voylla."Blinkit_Campaign_Schedule_Entries" WHERE entry_id = %(entry_id)s',
            {"entry_id": entry_id},
        )
        return cur.rowcount
