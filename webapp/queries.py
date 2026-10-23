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

ACTION_OPTIONS = ["NO_CHANGE", "INCREASE_CPM", "DECREASE_CPM", "PAUSE"]

ALL_BRANDS = "__ALL__"

AUTONOMY_MODES = ["auto", "semi_auto", "manual"]
AUTONOMY_MODE_LABELS = {
    "auto": "Auto - AI runs it",
    "semi_auto": "Semi-auto - human approves",
    "manual": "Manual - advisory only",
}

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

ONDEMAND_ROAS_IMPACT_SQL = """
WITH actions AS (
    SELECT
        l.unique_key,
        l.campaign_id,
        l.campaign_name,
        l.targeting,
        l.action_date                              AS action_date,
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
        l.push_status,
        l."Brand",
        (l.rule_action IS NOT NULL
         AND l.rule_action <> ''
         AND l.rule_action <> l.action)           AS diverged,
        (l.push_status = 'done')                  AS was_implemented,
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
    FROM voylla.blinkit_ondemand_actions l
    WHERE (%(brand)s = '__ALL__' OR l."Brand" = %(brand)s)
      AND l.action_date >= %(since)s
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
        WHEN a.was_implemented
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


# A pushed change is judged on 7 full days of results, and only once Blinkit
# has mostly finished adding late sales to the last of those days (3 more).
VERDICT_AFTER_DAYS = 7
VERDICT_SETTLE_DAYS = 3


def annotate_verdict_readiness(rows, today=None):
    """Adds, per row: status ('judged' | 'waiting' | 'not_pushed'),
    verdict_on (the date a waiting row gets its verdict), and changed_again_on
    (a later pushed change to the same keyword inside this row's 7-day
    'after' window - that verdict then mixes two changes). Clears the verdict
    of any row whose window isn't complete and settled yet: judging a change
    on 1-2 days of results, before late sales land, mostly reads noise."""
    today = today or date.today()
    pushed = {}
    for r in rows:
        if r.get("was_implemented"):
            pushed.setdefault((str(r["campaign_id"]), r["targeting"]), []).append(r["action_date"])
    for r in rows:
        d = r["action_date"]
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        r["verdict_on"] = d + timedelta(days=VERDICT_AFTER_DAYS + VERDICT_SETTLE_DAYS)
        later = sorted(
            x for x in pushed.get((str(r["campaign_id"]), r["targeting"]), [])
            if d < (date.fromisoformat(x[:10]) if isinstance(x, str) else x) <= d + timedelta(days=VERDICT_AFTER_DAYS)
        )
        r["changed_again_on"] = later[0] if later and r.get("was_implemented") else None
        if str(r.get("action") or "").upper() not in ("INCREASE_CPM", "DECREASE_CPM", "PAUSE"):
            r["status"] = "hold"   # no bid change was proposed, so nothing to judge
            r["verdict"] = None
        elif not r.get("was_implemented"):
            r["status"] = "not_pushed"
        elif today >= r["verdict_on"]:
            r["status"] = "judged"
        else:
            r["status"] = "waiting"
            r["verdict"] = None
    return rows


def fetch_ondemand_roas_impact(brand, since_date, verdict=None, search=None):
    """Same shape as fetch_roas_impact, sourced from voylla.blinkit_ondemand_actions
    instead of Blinkit_actions_llm - "was_implemented" here means the independent
    Blinkit_Ondemand_Bid_Push.ipynb actually pushed it live (push_status = 'done'),
    not just that someone accepted it. While that script stays in DRY_RUN, nothing
    is scoreable yet - which is correct: no real bid has gone live, so there is
    nothing to honestly attribute a ROAS change to."""
    with get_cursor() as cur:
        cur.execute(ONDEMAND_ROAS_IMPACT_SQL, {"brand": brand, "since": since_date})
        rows = cur.fetchall()
    annotate_verdict_readiness(rows)
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


def fetch_ondemand_action_dates(brand, limit=30):
    with get_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT action_date FROM voylla.blinkit_ondemand_actions "
            'WHERE "Brand" = %(brand)s ORDER BY action_date DESC LIMIT %(limit)s',
            {"brand": brand, "limit": limit},
        )
        return [str(r["action_date"]) for r in cur.fetchall()]


def fetch_latest_ondemand_action_date(brand):
    with get_cursor() as cur:
        cur.execute(
            'SELECT MAX(action_date) AS d FROM voylla.blinkit_ondemand_actions WHERE "Brand" = %(brand)s',
            {"brand": brand},
        )
        row = cur.fetchone()
        return row["d"] if row else None


def count_ondemand_decisions_on_date(brand, action_date):
    with get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM voylla.blinkit_ondemand_actions "
            'WHERE "Brand" = %(brand)s AND action_date = %(action_date)s '
            "AND user_implemented IS NOT NULL",
            {"brand": brand, "action_date": action_date},
        )
        return cur.fetchone()["cnt"]


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


def fetch_roas_impact(brand, since_date, verdict=None, search=None):
    since = since_date
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
        rows = cur.fetchall()
    settings = fetch_autonomy_settings(brand)
    for r in rows:
        s = settings.get(str(r["campaign_id"]))
        r["autonomy_mode"] = s["mode"] if s else "semi_auto"
        r["ondemand_managed"] = s["ondemand_managed"] if s else False
        r["bid_tolerance_pct"] = s["bid_tolerance_pct"] if s else 20
    return rows


def fetch_autonomy_settings(brand):
    """campaign_id -> {mode, ondemand_managed, bid_tolerance_pct} for every
    campaign that has an explicit row; anything absent defaults to
    semi_auto / not on-demand-managed / 20% tolerance, so this table only
    needs a row when someone changes a campaign away from the default."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT campaign_id, mode, ondemand_managed, bid_tolerance_pct "
            "FROM voylla.campaign_autonomy_mode WHERE brand = %(brand)s",
            {"brand": brand},
        )
        return {r["campaign_id"]: r for r in cur.fetchall()}


def fetch_autonomy_modes(brand):
    """Back-compat: campaign_id -> mode string only."""
    return {cid: s["mode"] for cid, s in fetch_autonomy_settings(brand).items()}


def brand_fully_delinked(brand):
    """True when every campaign that has ever appeared in Blinkit_actions_llm
    for this brand is on-demand-managed - i.e. Pending Actions can never
    show a row for this brand again, no matter what date is picked. Used to
    swap the page's empty-state for a clear "suggestions moved" message
    instead of a date-picker that looks like it's just missing data."""
    managed = fetch_ondemand_managed_ids(brand)
    if not managed:
        return False
    with get_cursor() as cur:
        cur.execute(
            'SELECT DISTINCT campaign_id::TEXT AS campaign_id FROM voylla."Blinkit_actions_llm" '
            'WHERE "Brand" = %(brand)s',
            {"brand": brand},
        )
        legacy_ids = {r["campaign_id"] for r in cur.fetchall()}
    return legacy_ids.issubset(managed)


def fetch_ondemand_managed_ids(brand):
    """campaign_ids (as strings) that are fully delinked from
    Blinkit_actions_llm and managed exclusively by the on-demand pipeline."""
    return {
        cid for cid, s in fetch_autonomy_settings(brand).items() if s["ondemand_managed"]
    }


def fetch_autonomy_setting(campaign_id, brand):
    """Single-campaign lookup - {mode, ondemand_managed, bid_tolerance_pct},
    defaulted, for the on-demand engine's auto-accept + tolerance clamp."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT mode, ondemand_managed, bid_tolerance_pct FROM voylla.campaign_autonomy_mode "
            "WHERE campaign_id = %(campaign_id)s AND brand = %(brand)s",
            {"campaign_id": str(campaign_id), "brand": brand},
        )
        row = cur.fetchone()
    if row:
        return row
    return {"mode": "semi_auto", "ondemand_managed": False, "bid_tolerance_pct": 20}


def set_autonomy_mode(campaign_id, brand, mode, set_by=None):
    if mode not in AUTONOMY_MODES:
        raise ValueError(f"mode must be one of {AUTONOMY_MODES}")
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO voylla.campaign_autonomy_mode (campaign_id, brand, mode, set_by, set_at)
            VALUES (%(campaign_id)s, %(brand)s, %(mode)s, %(set_by)s, NOW())
            ON CONFLICT (campaign_id, brand) DO UPDATE
                SET mode = EXCLUDED.mode, set_by = EXCLUDED.set_by, set_at = NOW()
            """,
            {"campaign_id": str(campaign_id), "brand": brand, "mode": mode, "set_by": set_by},
        )


def set_ondemand_managed_bulk(campaign_ids, brand, managed=True, bid_tolerance_pct=20, set_by=None):
    """Same as set_ondemand_managed but for many campaigns in one brand at
    once - used to delink a brand's whole catalog from Blinkit_actions_llm
    in a single call instead of one round-trip per campaign."""
    if not campaign_ids:
        return 0
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO voylla.campaign_autonomy_mode
                (campaign_id, brand, mode, ondemand_managed, bid_tolerance_pct, set_by, set_at)
            SELECT unnest(%(campaign_ids)s), %(brand)s, 'semi_auto', %(managed)s, %(tol)s, %(set_by)s, NOW()
            ON CONFLICT (campaign_id, brand) DO UPDATE
                SET ondemand_managed = EXCLUDED.ondemand_managed,
                    bid_tolerance_pct = EXCLUDED.bid_tolerance_pct,
                    set_by = EXCLUDED.set_by, set_at = NOW()
            """,
            {
                "campaign_ids": [str(c) for c in campaign_ids], "brand": brand,
                "managed": bool(managed), "tol": bid_tolerance_pct, "set_by": set_by,
            },
        )
        return cur.rowcount


def set_ondemand_managed(campaign_id, brand, managed, bid_tolerance_pct=20, set_by=None):
    """Turns full on-demand management on/off for one campaign. Does NOT
    touch `mode` - a caller that wants the "auto button" behaviour (auto
    generate + auto accept, no manual step) should also call
    set_autonomy_mode(campaign_id, brand, 'auto', ...)."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO voylla.campaign_autonomy_mode
                (campaign_id, brand, mode, ondemand_managed, bid_tolerance_pct, set_by, set_at)
            VALUES (%(campaign_id)s, %(brand)s, 'semi_auto', %(managed)s, %(tol)s, %(set_by)s, NOW())
            ON CONFLICT (campaign_id, brand) DO UPDATE
                SET ondemand_managed = EXCLUDED.ondemand_managed,
                    bid_tolerance_pct = EXCLUDED.bid_tolerance_pct,
                    set_by = EXCLUDED.set_by, set_at = NOW()
            """,
            {
                "campaign_id": str(campaign_id), "brand": brand,
                "managed": bool(managed), "tol": bid_tolerance_pct, "set_by": set_by,
            },
        )


def fetch_channel_daily(brand, days=180, campaign_ids=None, exclude_ids=None):
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
    if exclude_ids:
        # the brand's other campaigns: the comparison line for the AI's ones
        campaign_filter += ' AND NOT ("Campaign ID" = ANY(%(exclude_ids)s))'
        params["exclude_ids"] = exclude_ids

    with get_cursor() as cur:
        cur.execute(
            f"""
            SELECT
                TO_TIMESTAMP("Date",'YYYY-MM-DD HH24:MI:SS')::date AS report_date,
                SUM("Estimated Budget Consumed")                  AS spend,
                SUM("Direct Sales" + "Indirect Sales")            AS sales,
                SUM("Impressions")                                AS impressions,
                SUM("Direct ATC" + "Indirect ATC")                AS atc,
                SUM("Direct Quantities Sold" + "Indirect Quantities Sold") AS units,
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
            "units": int(r["units"] or 0),
            "new_users": int(r["new_users"] or 0),
            "avg_position": round(float(r["avg_position"]), 1) if r["avg_position"] is not None else None,
        })
    return out


def fetch_ai_managed_campaign_ids(brand):
    """Campaigns the AI runs: set to Auto in the panel, or with an AI decision
    that actually reached Blinkit. The channel ROAS panel scopes to these by
    default so its "After (AI)" line describes campaigns the AI really runs -
    the whole channel is ~35 campaigns and the AI may run only a few."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT campaign_id::text AS campaign_id FROM voylla.campaign_autonomy_mode
            WHERE brand = %(brand)s AND mode = 'auto' AND campaign_id::text ~ '^[0-9]+$'
            UNION
            SELECT DISTINCT campaign_id FROM voylla.blinkit_ondemand_actions
            WHERE "Brand" = %(brand)s AND push_status = 'done' AND campaign_id ~ '^[0-9]+$'
            """,
            {"brand": brand},
        )
        return sorted(int(r["campaign_id"]) for r in cur.fetchall())


def fetch_campaign_scoreboard(brand, campaign_ids, cutover_date, window_days=7, event_dates=None, days_mode="all"):
    """One row per AI campaign: spend and ROAS in the window_days before the
    cutover vs from the cutover on, and the bid changes the AI pushed in that
    after-window (raised / cut / paused). Biggest after-spend first."""
    if not campaign_ids:
        return []
    with get_cursor() as cur:
        cur.execute(
            """
            WITH perf AS (
                SELECT "Campaign ID"::text AS cid, MAX("Campaign Name") AS name,
                       SUM("Estimated Budget Consumed") FILTER (WHERE d >= %(b0)s AND d < %(cut)s) AS spend_before,
                       SUM("Direct Sales" + "Indirect Sales") FILTER (WHERE d >= %(b0)s AND d < %(cut)s) AS sales_before,
                       SUM("Estimated Budget Consumed") FILTER (WHERE d >= %(cut)s AND d < %(a1)s) AS spend_after,
                       SUM("Direct Sales" + "Indirect Sales") FILTER (WHERE d >= %(cut)s AND d < %(a1)s) AS sales_after,
                       COUNT(DISTINCT d) FILTER (WHERE d >= %(cut)s AND d < %(a1)s) AS days_after
                FROM (SELECT *, TO_TIMESTAMP("Date", 'YYYY-MM-DD HH24:MI:SS')::date AS d
                      FROM voylla."Blinkit_Ads_Report" WHERE "Brand" = %(brand)s) a
                WHERE "Campaign ID"::text = ANY(%(ids)s) AND d >= %(b0)s AND d < %(a1)s
                  -- BAU / event view: event days left out (bau) or kept alone (event)
                  AND (%(mode)s = 'all'
                       OR (%(mode)s = 'bau' AND NOT (d = ANY(%(ev)s::date[])))
                       OR (%(mode)s = 'event' AND d = ANY(%(ev)s::date[])))
                GROUP BY 1
            ),
            bids AS (
                SELECT campaign_id AS cid,
                       COUNT(*) FILTER (WHERE action = 'INCREASE_CPM') AS raised,
                       COUNT(*) FILTER (WHERE action = 'DECREASE_CPM') AS cut,
                       COUNT(*) FILTER (WHERE action = 'PAUSE') AS paused,
                       MAX(action_date) AS last_push
                FROM voylla.blinkit_ondemand_actions
                WHERE "Brand" = %(brand)s AND push_status = 'done' AND campaign_id = ANY(%(ids)s)
                  AND action_date >= %(cut)s AND action_date < %(a1)s
                GROUP BY 1
            ),
            mode AS (SELECT campaign_id::text AS cid, mode FROM voylla.campaign_autonomy_mode WHERE brand = %(brand)s)
            SELECT p.*, COALESCE(b.raised, 0) AS raised, COALESCE(b.cut, 0) AS cut,
                   COALESCE(b.paused, 0) AS paused, b.last_push, m.mode
            FROM perf p LEFT JOIN bids b USING (cid) LEFT JOIN mode m USING (cid)
            ORDER BY COALESCE(p.spend_after, 0) DESC
            """,
            {"brand": brand, "ids": [str(i) for i in campaign_ids], "cut": cutover_date,
             "mode": days_mode if days_mode in ("bau", "event") else "all",
             "ev": sorted(event_dates or []),
             "b0": cutover_date - timedelta(days=window_days), "a1": cutover_date + timedelta(days=window_days)},
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        sb, sa = float(r["spend_before"] or 0), float(r["spend_after"] or 0)
        rb = float(r["sales_before"] or 0) / sb if sb else None
        ra = float(r["sales_after"] or 0) / sa if sa else None
        out.append(dict(r, spend_before=sb, spend_after=sa,
                        roas_before=round(rb, 2) if rb is not None else None,
                        roas_after=round(ra, 2) if ra is not None else None,
                        change_pct=round((ra / rb - 1) * 100) if rb and ra is not None else None))
    return out


def fetch_ai_campaign_options(brand, campaign_ids):
    """Name, Auto mode and Blinkit status for each AI-managed campaign - the
    list the ROAS Impact campaign picker shows. Auto campaigns first."""
    if not campaign_ids:
        return []
    ids = [str(i) for i in campaign_ids]
    with get_cursor() as cur:
        cur.execute(
            """
            WITH n AS (
                SELECT DISTINCT ON (campaign_id::text) campaign_id::text AS cid, campaign_name, last_status
                FROM voylla."Blinkit_Campaign_Runtime"
                WHERE brand = %(brand)s AND campaign_id::text = ANY(%(ids)s)
                ORDER BY campaign_id::text, log_date DESC
            ),
            m AS (SELECT campaign_id::text AS cid, mode FROM voylla.campaign_autonomy_mode
                  WHERE brand = %(brand)s AND campaign_id::text = ANY(%(ids)s))
            SELECT i.cid, n.campaign_name, n.last_status, m.mode
            FROM UNNEST(%(ids)s::text[]) AS i(cid)
            LEFT JOIN n USING (cid) LEFT JOIN m USING (cid)
            """,
            {"brand": brand, "ids": ids},
        )
        rows = cur.fetchall()
    out = [{"cid": r["cid"], "name": r["campaign_name"] or r["cid"], "status": r["last_status"],
            "auto": r["mode"] == "auto"} for r in rows]
    return sorted(out, key=lambda c: (not c["auto"], str(c["name"]).lower()))


def pick_campaigns(arg, ai_ids):
    """The campaigns ticked in the picker (`campaigns=1,2,3`), limited to the
    AI-managed ones. Nothing valid ticked = all of them."""
    allowed = {int(i) for i in ai_ids}
    chosen = {int(p) for p in str(arg or "").split(",") if p.strip().isdigit() and int(p) in allowed}
    return sorted(chosen or allowed)


def fetch_push_counts(brand, campaign_ids, start):
    """Bid changes the AI actually pushed to Blinkit, per day, for the chosen
    campaigns: the markers on the ROAS trend."""
    if not campaign_ids:
        return []
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT action_date::date AS d,
                   COUNT(*) FILTER (WHERE action = 'INCREASE_CPM') AS raised,
                   COUNT(*) FILTER (WHERE action = 'DECREASE_CPM') AS cut,
                   COUNT(*) FILTER (WHERE action = 'PAUSE') AS paused
            FROM voylla.blinkit_ondemand_actions
            WHERE "Brand" = %(brand)s AND push_status = 'done'
              AND campaign_id = ANY(%(ids)s) AND action_date::date >= %(start)s
            GROUP BY 1 ORDER BY 1
            """,
            {"brand": brand, "ids": [str(i) for i in campaign_ids], "start": start},
        )
        return [{"date": r["d"], "raised": int(r["raised"]), "cut": int(r["cut"]), "paused": int(r["paused"])}
                for r in cur.fetchall()]


def fetch_campaign_roas_daily(brand, campaign_ids, days=30):
    """campaign id -> [roas per day] for the last `days` days (None = no spend),
    for the sparkline next to each campaign in the scoreboard."""
    if not campaign_ids:
        return {}
    start = date.today() - timedelta(days=days)
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT "Campaign ID"::text AS cid, TO_TIMESTAMP("Date", 'YYYY-MM-DD HH24:MI:SS')::date AS d,
                   SUM("Estimated Budget Consumed") AS spend, SUM("Direct Sales" + "Indirect Sales") AS sales
            FROM voylla."Blinkit_Ads_Report"
            WHERE "Brand" = %(brand)s AND "Campaign ID"::text = ANY(%(ids)s)
              AND TO_TIMESTAMP("Date", 'YYYY-MM-DD HH24:MI:SS')::date >= %(start)s
            GROUP BY 1, 2
            """,
            {"brand": brand, "ids": [str(i) for i in campaign_ids], "start": start},
        )
        rows = cur.fetchall()
    by_cid = {}
    for r in rows:
        spend = float(r["spend"] or 0)
        by_cid.setdefault(r["cid"], {})[r["d"]] = round(float(r["sales"] or 0) / spend, 2) if spend else None
    days_list = [start + timedelta(days=i) for i in range(days)]
    return {cid: [vals.get(d) for d in days_list] for cid, vals in by_cid.items()}


# When the AI started, per brand - offered first in the cutover picker. The
# Chumbak trial ran in the old pipeline (Blinkit_actions_llm), so 10 Sep is not
# among the on-demand action dates; Voylla's pilot was switched on 18 Sep.
AI_MILESTONES = {
    "Chumbak": [(date(2026, 9, 10), "AI trial start (12 campaigns)"), (date(2026, 10, 12), "Daily Auto start")],
    "Voylla": [(date(2026, 9, 18), "AI pilot start (Mumbai campaign)")],
}

RANGE_PRESETS = {"7": "Last 7 days", "15": "Last 15 days", "30": "Last 30 days", "90": "Last 90 days",
                 "this_month": "This month", "last_month": "Last month", "custom": "Custom"}


def resolve_range(preset, start_arg, end_arg, today):
    """(preset, start, end) for the trend's date range, inclusive. Unknown or
    incomplete input falls back to the last 30 days."""
    yesterday = today - timedelta(days=1)
    if preset == "custom":
        try:
            start, end = date.fromisoformat(start_arg), date.fromisoformat(end_arg)
            if start > end:
                start, end = end, start
            return "custom", start, min(end, today)
        except (TypeError, ValueError):
            preset = "30"
    if preset == "this_month":
        return preset, today.replace(day=1), today
    if preset == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return preset, end.replace(day=1), end
    if preset not in ("7", "15", "30", "90"):
        preset = "30"
    return preset, yesterday - timedelta(days=int(preset) - 1), yesterday


def period_totals(daily, start, end):
    """Totals for start..end (inclusive) of fetch_channel_daily rows, with the
    ratios recomputed from the totals (never averaged)."""
    rows = [d for d in daily if start <= d["date"] <= end]
    t = {k: sum(r.get(k) or 0 for r in rows) for k in ("spend", "sales", "impressions", "atc", "units")}
    t["roas"] = round(t["sales"] / t["spend"], 2) if t["spend"] else None
    t["ctr"] = round(t["atc"] / t["impressions"] * 100, 2) if t["impressions"] else None
    t["cpm"] = round(t["spend"] / t["impressions"] * 1000, 2) if t["impressions"] else None
    t["days"] = len(rows)
    return t


def compare_totals(cur, prev):
    """% change per metric vs the previous period; None when there is no base."""
    out = {}
    for k in ("spend", "sales", "impressions", "atc", "units", "roas", "ctr", "cpm"):
        a, b = cur.get(k), prev.get(k)
        out[k] = round((a / b - 1) * 100, 1) if (a is not None and b) else None
    return out


def bucket_label(d, granularity):
    if granularity == "week":
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if granularity == "month":
        return d.strftime("%Y-%m")
    return str(d)


def attach_comparison(trend, others_trend, push_counts, granularity):
    """Adds to each trend point the other campaigns' ROAS for the same day /
    week / month and the AI bid pushes that landed in it."""
    others = {o["label"]: o for o in others_trend}
    pushes = {}
    for p in push_counts:
        b = pushes.setdefault(bucket_label(p["date"], granularity), {"raised": 0, "cut": 0, "paused": 0})
        for k in ("raised", "cut", "paused"):
            b[k] += p[k]
    for t in trend:
        o = others.get(t["label"])
        t["others_roas"] = o.get("roas") if o else None
        t["others"] = ({k: o.get(k) for k in ("spend", "sales", "impressions", "atc", "units")} if o else None)
        t["pushes"] = pushes.get(t["label"], {"raised": 0, "cut": 0, "paused": 0})
        t["push_total"] = sum(t["pushes"].values())
    return trend


def sparkline_points(values, width=96, height=24, pad=2):
    """SVG polyline points for a small ROAS sparkline; days with no spend are
    skipped. "" when there are fewer than two points to draw."""
    pts = [(i, v) for i, v in enumerate(values or []) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1.0
    step = (width - 2 * pad) / (len(values) - 1)
    return " ".join(f"{pad + i * step:.1f},{pad + (height - 2 * pad) * (1 - (v - lo) / span):.1f}" for i, v in pts)


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
                "atc": 0, "units": 0, "new_users": 0, "_pos_sum": 0.0, "_pos_n": 0,
            }
            order.append(key)
        b = buckets[key]
        b["spend"] += d["spend"]
        b["sales"] += d["sales"]
        b["impressions"] += d["impressions"]
        b["atc"] += d["atc"]
        b["units"] += d.get("units", 0)
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
        impressions = sum(r.get("impressions") or 0 for r in rows)
        return {
            "spend": round(spend, 2),
            "sales": round(sales, 2),
            "roas": round(sales / spend, 2) if spend else None,
            "days": len(rows),
            "atc": sum(r["atc"] for r in rows),
            "new_users": sum(r["new_users"] for r in rows),
            "impressions": impressions,
            # CTR = add to carts / impressions (Blinkit reports no clicks), as the bid engine uses
            "ctr": round(sum(r["atc"] for r in rows) / impressions * 100, 2) if impressions else None,
            "cpm": round(spend / impressions * 1000, 2) if impressions else None,
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


# Expectation tolerances by action type - the bar an action must clear to
# count as "met expectations". This is a stated, rules-based bar, not a
# predictive/ML forecast - each threshold is one sentence to defend, not a
# fitted model, and can be retuned here without touching anything else.
EXPECTATION_TOLERANCE = {
    "DECREASE": 0.95,   # cut a bid → efficiency should hold within 5% or improve
    "INCREASE": 0.80,   # raised a bid → up to 20% softer ROAS is an acceptable trade for volume
    "HOLD": 0.85,       # NO_CHANGE → should stay within 15% either way
}


def _expectation_verdict(action, roas_before, roas_after, verdict):
    action = (action or "").upper()
    if verdict == "PAUSED":
        return "MET", "spend stopped, as intended"
    if verdict in ("STOPPED", "NEW") or roas_before is None or roas_after is None:
        return "N/A", "no comparable baseline"
    if "ZOMBIE" in action or "INSUFFICIENT" in action:
        return "N/A", "monitoring flag, not a bid decision"
    if "DECREASE" in action:
        bar = round(roas_before * EXPECTATION_TOLERANCE["DECREASE"], 2)
        reason = f"cut a bid - expected ROAS >= {bar} (hold or improve)"
    elif "INCREASE" in action:
        bar = round(roas_before * EXPECTATION_TOLERANCE["INCREASE"], 2)
        reason = f"raised a bid - expected ROAS >= {bar} (some softening for volume is OK)"
    else:
        bar = round(roas_before * EXPECTATION_TOLERANCE["HOLD"], 2)
        reason = f"held the bid - expected ROAS >= {bar} (should stay roughly stable)"
    return ("MET" if roas_after >= bar else "MISSED"), reason


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
            "SELECT campaign_id, targeting, action, override_action, user_implemented "
            "FROM voylla.blinkit_ondemand_actions "
            'WHERE "Brand" = %(brand)s AND action_date = %(cutover)s',
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
        days_before = b["days_before"] or 1
        spend_before_avg = b["spend_before"] / days_before
        sales_before_avg = b["sales_before"] / days_before
        roas_before = round(sales_before_avg / spend_before_avg, 2) if spend_before_avg else None

        a = actions_idx.get(key)
        action_label = (a["override_action"] or a["action"]) if a else "NOT TARGETED"

        if not b["days_after"]:
            # No Blinkit_Ads_Report rows at all after the cutover - usually
            # because the keyword/campaign stopped serving, most often from
            # an explicit PAUSE. This used to be silently dropped, which
            # hid the AI's most decisive interventions (a successful pause
            # looks identical to "no data" otherwise) from the analysis.
            if spend_before_avg <= 0:
                continue  # never ran in either window - genuinely not relevant
            row_verdict = "PAUSED" if "PAUSE" in (action_label or "") else "STOPPED"
            expectation, expectation_reason = _expectation_verdict(action_label, roas_before, None, row_verdict)
            rows_out.append({
                "campaign_id": b["campaign_id"],
                "campaign_name": b["campaign_name"],
                "targeting": b["targeting"],
                "action": action_label,
                "spend_before": round(spend_before_avg, 2),
                "spend_after": 0.0,
                "roas_before": roas_before,
                "roas_after": None,
                "roas_delta": None,
                "verdict": row_verdict,
                "expectation": expectation,
                "expectation_reason": expectation_reason,
                "meaningful": spend_before_avg >= min_spend,
            })
            continue

        spend_after_avg = b["spend_after"] / b["days_after"]
        sales_after_avg = b["sales_after"] / b["days_after"]
        roas_after = round(sales_after_avg / spend_after_avg, 2) if spend_after_avg else 0.0

        if roas_before is None:
            verdict = "NEW"
        elif roas_after > roas_before + 0.05:
            verdict = "BETTER"
        elif roas_after < roas_before - 0.05:
            verdict = "WORSE"
        else:
            verdict = "SAME"

        expectation, expectation_reason = _expectation_verdict(action_label, roas_before, roas_after, verdict)

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
            "expectation": expectation,
            "expectation_reason": expectation_reason,
            "meaningful": spend_after_avg >= min_spend or spend_before_avg >= min_spend,
        })

    rows_out.sort(key=lambda r: max(r["spend_after"], r["spend_before"]), reverse=True)

    total_spend_after = sum(r["spend_after"] for r in rows_out) or 1
    verdict_keys = ["BETTER", "WORSE", "SAME", "NEW", "PAUSED", "STOPPED"]
    naive = {k: 0 for k in verdict_keys}
    weighted_spend = {k: 0.0 for k in verdict_keys}
    meaningful_naive = {k: 0 for k in verdict_keys}
    spend_saved = 0.0
    expectation_counts = {"MET": 0, "MISSED": 0, "N/A": 0}
    expectation_spend = {"MET": 0.0, "MISSED": 0.0}
    for r in rows_out:
        naive[r["verdict"]] += 1
        weighted_spend[r["verdict"]] += r["spend_after"]
        if r["meaningful"]:
            meaningful_naive[r["verdict"]] += 1
        if r["verdict"] == "PAUSED":
            spend_saved += r["spend_before"]
        if r["meaningful"]:
            expectation_counts[r["expectation"]] += 1
            if r["expectation"] in expectation_spend:
                expectation_spend[r["expectation"]] += max(r["spend_after"], r["spend_before"])

    scored_spend = expectation_spend["MET"] + expectation_spend["MISSED"]

    portfolio_spend_before = sum(r["spend_before"] for r in rows_out)
    portfolio_sales_before = sum(
        r["spend_before"] * r["roas_before"] for r in rows_out if r["roas_before"] is not None
    )
    portfolio_spend_after = total_spend_after
    portfolio_sales_after = sum(
        r["spend_after"] * r["roas_after"] for r in rows_out if r["roas_after"] is not None
    )

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
        "spend_saved": round(spend_saved, 2),
        "expectation_counts": expectation_counts,
        "expectation_met_pct_by_spend": round(expectation_spend["MET"] / scored_spend * 100, 1) if scored_spend else None,
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


def count_decisions_on_date(brand, action_date):
    """How many rows on this action_date were actually accepted or
    overridden - used to warn on ROAS Impact when a before/after split is
    drawn at a date where nothing was actually implemented, so organic
    channel movement doesn't get misread as the AI's impact."""
    with get_cursor() as cur:
        cur.execute(
            'SELECT COUNT(*) AS cnt FROM voylla."Blinkit_actions_llm" '
            'WHERE "Brand" = %(brand)s AND action_date = %(action_date)s '
            "AND user_implemented IS NOT NULL",
            {"brand": brand, "action_date": str(action_date)},
        )
        return cur.fetchone()["cnt"]


def fetch_pending_actions(brand, action_date):
    """Never returns a row for a campaign that's on-demand-managed - that
    campaign is fully delinked from Blinkit_actions_llm, so its suggestions
    only ever come from voylla.blinkit_ondemand_actions (see the On-Demand
    AI page) instead of showing up twice from two disagreeing systems."""
    managed = fetch_ondemand_managed_ids(brand)
    with get_cursor() as cur:
        cur.execute(
            'SELECT * FROM voylla."Blinkit_actions_llm" '
            'WHERE "Brand" = %(brand)s AND action_date = %(action_date)s '
            "ORDER BY campaign_name, targeting",
            {"brand": brand, "action_date": action_date},
        )
        rows = cur.fetchall()
    if not managed:
        return rows
    return [r for r in rows if str(r["campaign_id"]) not in managed]


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


def bulk_accept_actions(unique_keys, brand):
    """Accept-as-is for many rows at once (Pending Actions "select all")."""
    if not unique_keys:
        return 0
    with get_cursor(commit=True) as cur:
        cur.execute(
            'UPDATE voylla."Blinkit_actions_llm" '
            "SET user_implemented = 'true', "
            "    implementation_date = CURRENT_DATE "
            'WHERE unique_key = ANY(%(unique_keys)s) AND "Brand" = %(brand)s',
            {"unique_keys": list(unique_keys), "brand": brand},
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


def fetch_ondemand_actions(brand, campaign_id=None, limit=300):
    """Rows from the on-demand replica table - never Blinkit_actions_llm."""
    with get_cursor() as cur:
        if campaign_id:
            cur.execute(
                'SELECT * FROM voylla.blinkit_ondemand_actions '
                'WHERE "Brand" = %(brand)s AND campaign_id = %(campaign_id)s '
                "ORDER BY requested_at DESC, unique_key LIMIT %(limit)s",
                {"brand": brand, "campaign_id": str(campaign_id), "limit": limit},
            )
        else:
            cur.execute(
                'SELECT * FROM voylla.blinkit_ondemand_actions '
                'WHERE "Brand" = %(brand)s '
                "ORDER BY requested_at DESC, unique_key LIMIT %(limit)s",
                {"brand": brand, "limit": limit},
            )
        return cur.fetchall()


def accept_ondemand_action(unique_key, brand, accept, cpm_llm_override, note):
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE voylla.blinkit_ondemand_actions "
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


def accept_instamart_ondemand_action(unique_key, brand, accept, cpm_llm_override, note):
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE voylla.instamart_ondemand_actions "
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


def fetch_instamart_ondemand_action_dates(brand, limit=30):
    with get_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT action_date FROM voylla.instamart_ondemand_actions "
            'WHERE "Brand" = %(brand)s ORDER BY action_date DESC LIMIT %(limit)s',
            {"brand": brand, "limit": limit},
        )
        return [str(r["action_date"]) for r in cur.fetchall()]


INSTAMART_CAMPAIGN_STATUS_SQL = """
SELECT "CAMPAIGN_ID"::TEXT AS campaign_id,
       MAX("CAMPAIGN_NAME")   AS campaign_name,
       "Brand"                AS brand,
       MAX("TOTAL_BUDGET")    AS budget,
       MAX("CAMPAIGN_STATUS") AS last_status,
       0                      AS window_count,
       NULL::date             AS next_start,
       NULL::date             AS last_end
FROM voylla."Instamart_Campaign_Placement"
WHERE (%(brand)s = '__ALL__' OR "Brand" = %(brand)s)
  AND "Date"::date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY "CAMPAIGN_ID", "Brand"
ORDER BY campaign_name
"""


def fetch_instamart_campaign_status(brand):
    """Instamart has no Blinkit_Campaign_Runtime-style live status feed yet,
    so this is a simpler campaign list off the last 30 days of placement
    data - same output shape as fetch_campaign_status() so the dashboard
    templates don't need to know which channel they're rendering."""
    with get_cursor() as cur:
        cur.execute(INSTAMART_CAMPAIGN_STATUS_SQL, {"brand": brand})
        rows = cur.fetchall()
    settings = fetch_autonomy_settings(brand)
    for r in rows:
        s = settings.get(str(r["campaign_id"]))
        r["autonomy_mode"] = s["mode"] if s else "semi_auto"
        r["ondemand_managed"] = s["ondemand_managed"] if s else False
        r["bid_tolerance_pct"] = s["bid_tolerance_pct"] if s else 20
    return rows


def bulk_accept_instamart_ondemand_actions(unique_keys, brand):
    if not unique_keys:
        return 0
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE voylla.instamart_ondemand_actions "
            "SET user_implemented = 'true', "
            "    implementation_date = CURRENT_DATE "
            'WHERE unique_key = ANY(%(unique_keys)s) AND "Brand" = %(brand)s',
            {"unique_keys": list(unique_keys), "brand": brand},
        )
        return cur.rowcount


def override_instamart_ondemand_action(unique_key, brand, override_action_value, cpm_change_user, note):
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE voylla.instamart_ondemand_actions "
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


def fetch_instamart_ondemand_actions(brand, campaign_id=None, limit=300):
    """Rows from the Instamart on-demand replica table."""
    with get_cursor() as cur:
        if campaign_id:
            cur.execute(
                'SELECT * FROM voylla.instamart_ondemand_actions '
                'WHERE "Brand" = %(brand)s AND campaign_id = %(campaign_id)s '
                "ORDER BY requested_at DESC, unique_key LIMIT %(limit)s",
                {"brand": brand, "campaign_id": str(campaign_id), "limit": limit},
            )
        else:
            cur.execute(
                'SELECT * FROM voylla.instamart_ondemand_actions '
                'WHERE "Brand" = %(brand)s '
                "ORDER BY requested_at DESC, unique_key LIMIT %(limit)s",
                {"brand": brand, "limit": limit},
            )
        return cur.fetchall()


def accept_zepto_ondemand_action(unique_key, brand, accept, cpm_llm_override, note):
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE voylla.zepto_ondemand_actions "
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


def fetch_zepto_ondemand_action_dates(brand, limit=30):
    with get_cursor() as cur:
        cur.execute(
            "SELECT DISTINCT action_date FROM voylla.zepto_ondemand_actions "
            'WHERE "Brand" = %(brand)s ORDER BY action_date DESC LIMIT %(limit)s',
            {"brand": brand, "limit": limit},
        )
        return [str(r["action_date"]) for r in cur.fetchall()]


ZEPTO_CAMPAIGN_STATUS_SQL = """
SELECT "Campaign_id"::TEXT AS campaign_id,
       MAX("Campaign_name")  AS campaign_name,
       "Brand"               AS brand,
       NULL::numeric         AS budget,
       'UNKNOWN'             AS last_status,
       0                     AS window_count,
       NULL::date            AS next_start,
       NULL::date            AS last_end
FROM "DataWarehouse"."Keyword_Performance_Zepto"
WHERE (%(brand)s = '__ALL__' OR "Brand" = %(brand)s)
  AND TO_TIMESTAMP("Date", 'YYYY-MM-DD HH24:MI:SS') >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY "Campaign_id", "Brand"
ORDER BY campaign_name
"""


def fetch_zepto_campaign_status(brand):
    """Zepto has no live campaign-status feed yet, so this is a simple
    campaign list off the last 30 days of performance data - same output
    shape as fetch_campaign_status()/fetch_instamart_campaign_status() so
    the dashboard templates stay channel-agnostic."""
    with get_cursor() as cur:
        cur.execute(ZEPTO_CAMPAIGN_STATUS_SQL, {"brand": brand})
        rows = cur.fetchall()
    settings = fetch_autonomy_settings(brand)
    for r in rows:
        s = settings.get(str(r["campaign_id"]))
        r["autonomy_mode"] = s["mode"] if s else "semi_auto"
        r["ondemand_managed"] = s["ondemand_managed"] if s else False
        r["bid_tolerance_pct"] = s["bid_tolerance_pct"] if s else 20
    return rows


def bulk_accept_zepto_ondemand_actions(unique_keys, brand):
    if not unique_keys:
        return 0
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE voylla.zepto_ondemand_actions "
            "SET user_implemented = 'true', "
            "    implementation_date = CURRENT_DATE "
            'WHERE unique_key = ANY(%(unique_keys)s) AND "Brand" = %(brand)s',
            {"unique_keys": list(unique_keys), "brand": brand},
        )
        return cur.rowcount


def override_zepto_ondemand_action(unique_key, brand, override_action_value, cpm_change_user, note):
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE voylla.zepto_ondemand_actions "
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


def fetch_zepto_ondemand_actions(brand, campaign_id=None, limit=300):
    """Rows from the Zepto on-demand replica table."""
    with get_cursor() as cur:
        if campaign_id:
            cur.execute(
                'SELECT * FROM voylla.zepto_ondemand_actions '
                'WHERE "Brand" = %(brand)s AND campaign_id = %(campaign_id)s '
                "ORDER BY requested_at DESC, unique_key LIMIT %(limit)s",
                {"brand": brand, "campaign_id": str(campaign_id), "limit": limit},
            )
        else:
            cur.execute(
                'SELECT * FROM voylla.zepto_ondemand_actions '
                'WHERE "Brand" = %(brand)s '
                "ORDER BY requested_at DESC, unique_key LIMIT %(limit)s",
                {"brand": brand, "limit": limit},
            )
        return cur.fetchall()


AUTO_ACCEPT_ACTIONS = {"INCREASE_CPM", "DECREASE_CPM", "PAUSE"}


def auto_accept_pending_ondemand(campaign_id, brand, tolerance_pct=20):
    """Sweeps every currently-undecided, actionable on-demand suggestion for
    one campaign and accepts it as-is (INCREASE_CPM/DECREASE_CPM/PAUSE, never
    a REVIEW-flagged row) - the same eligibility rule the engine applies
    right after generation, but run against whatever is already sitting
    there. Used when a campaign is switched into full-automation mode, so
    turning Auto on doesn't leave a backlog of suggestions waiting on a
    manual click that will now never come. Returns how many were accepted."""
    rows = fetch_ondemand_actions(brand, campaign_id=campaign_id, limit=500)
    accepted = 0
    for r in rows:
        if r["user_implemented"] is not None or r["override_action"]:
            continue
        if r["action"] not in AUTO_ACCEPT_ACTIONS:
            continue
        if r["quick_action"] == "REVIEW":
            continue
        accept_ondemand_action(
            r["unique_key"], brand, True, None,
            f"auto-accepted - autonomy mode 'auto', tolerance {float(tolerance_pct):.0f}% (swept on enabling automation)",
        )
        accepted += 1
    return accepted


def bulk_accept_ondemand_actions(unique_keys, brand):
    """Accept-as-is for many on-demand rows at once (Pending Actions "select all")."""
    if not unique_keys:
        return 0
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE voylla.blinkit_ondemand_actions "
            "SET user_implemented = 'true', "
            "    implementation_date = CURRENT_DATE "
            'WHERE unique_key = ANY(%(unique_keys)s) AND "Brand" = %(brand)s',
            {"unique_keys": list(unique_keys), "brand": brand},
        )
        return cur.rowcount


NOTE_ENTITY_TYPES = ("campaign", "keyword", "brand", "general")


def file_note(brand, entity_type, text, entity_id=None, source="chat", created_by=None):
    """Writes one row to the shared notes 'wiki' - the only write path Agent
    Chat has. entity_type/entity_id say what the note is about: a specific
    campaign_id, a keyword (brand-wide, not tied to one campaign), the whole
    brand, or 'general' (entity_id ignored) for something with no natural
    entity at all.

    brand=None means the note is universal - visible to every brand's AI,
    not just the one it was filed from. Only valid for entity_type='general':
    a campaign literally belongs to one brand, and a 'brand'-type note with
    no brand is self-contradictory, so those always require a real brand -
    the same wall that already keeps Voylla/Chumbak/Petcrux from leaking
    into each other's decisions. Use sparingly - reserve it for something
    that's genuinely true company-wide (a platform-wide policy), not just
    something that happens to apply to whichever brand is in the chat."""
    if entity_type not in NOTE_ENTITY_TYPES:
        raise ValueError(f"entity_type must be one of {NOTE_ENTITY_TYPES}")
    if brand is None and entity_type != "general":
        raise ValueError("brand is required unless entity_type is 'general'")
    with get_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO voylla.notes (brand, entity_type, entity_id, text, source, created_by)
            VALUES (%(brand)s, %(entity_type)s, %(entity_id)s, %(text)s, %(source)s, %(created_by)s)
            RETURNING id
            """,
            {
                "brand": brand, "entity_type": entity_type,
                "entity_id": str(entity_id) if entity_id is not None else None,
                "text": text, "source": source, "created_by": created_by,
            },
        )
        return cur.fetchone()["id"]


def fetch_relevant_notes(brand, campaign_id=None, targeting=None, limit=20):
    """Notes that should inform a decision about this specific keyword: any
    still-relevant brand-wide/general note (plus any universal note filed
    with no brand at all - visible to every brand), plus anything filed
    against this exact campaign_id or this exact keyword. Used both by the
    suggestion engine (folded into the prompt) and reusable anywhere else
    that needs 'what do we know about this'."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, entity_type, entity_id, text, source, created_at, brand
            FROM voylla.notes
            WHERE still_relevant = TRUE
              AND (
                    brand IS NULL
                    OR (
                        brand = %(brand)s
                        AND (
                              entity_type IN ('brand', 'general')
                              OR (entity_type = 'campaign' AND entity_id = %(campaign_id)s)
                              OR (entity_type = 'keyword'  AND entity_id = %(targeting)s)
                        )
                    )
              )
            ORDER BY created_at DESC
            LIMIT %(limit)s
            """,
            {
                "brand": brand,
                "campaign_id": str(campaign_id) if campaign_id is not None else None,
                "targeting": targeting,
                "limit": limit,
            },
        )
        return cur.fetchall()


def fetch_notes(brand, include_stale=False, limit=100, include_universal=True):
    """All notes for a brand, most recent first - for Agent Chat to answer
    'what notes do we have' and as a manual lint view (old + still_relevant
    rows are the ones worth reviewing). Includes universal (brand IS NULL)
    notes by default, since those genuinely apply to this brand too - set
    include_universal=False for a per-brand lint pass that shouldn't
    re-review the same universal notes once per brand. Calling with
    brand=None (and include_universal=True, the default) returns only the
    universal notes - the dedicated lint pass for those."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, entity_type, entity_id, text, source, created_at, still_relevant, brand
            FROM voylla.notes
            WHERE ({brand_filter}) {relevance_filter}
            ORDER BY created_at DESC
            LIMIT %(limit)s
            """.format(
                brand_filter="brand IS NULL OR brand = %(brand)s" if include_universal else "brand = %(brand)s",
                relevance_filter="" if include_stale else "AND still_relevant = TRUE",
            ),
            {"brand": brand, "limit": limit},
        )
        return cur.fetchall()


def set_note_relevance(note_id, still_relevant):
    """Lint action: mark a note stale (still_relevant=False) once it no
    longer applies, so it stops being folded into future prompts."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE voylla.notes SET still_relevant = %(v)s WHERE id = %(id)s",
            {"v": still_relevant, "id": note_id},
        )
        return cur.rowcount


def override_ondemand_action(unique_key, brand, override_action_value, cpm_change_user, note):
    with get_cursor(commit=True) as cur:
        cur.execute(
            "UPDATE voylla.blinkit_ondemand_actions "
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
            "        true, 'PENDING', "
            # a new window takes the campaign's pause-on-gap setting (see
            # campaign_pauses_on_gap); a campaign's first window starts off
            "        COALESCE((SELECT bool_or(COALESCE(pause_on_gap, true)) "
            "                  FROM voylla.\"Blinkit_Campaign_Schedule_Entries\" "
            "                  WHERE campaign_id = %(campaign_id)s), false), NOW())",
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


def campaign_pauses_on_gap(entries):
    """The scheduler (Blinkit_Campaign_Control.ipynb) pauses a campaign in the
    hours no window covers when ANY of its windows has pause_on_gap on, and it
    reads a blank as on. Same rule here so the checkbox shows what it will do."""
    return any(e.get("pause_on_gap") is None or bool(e.get("pause_on_gap")) for e in entries)


def set_pause_on_gap(campaign_id, value):
    """Campaign-level switch: sets pause_on_gap on every window of the campaign."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            'UPDATE voylla."Blinkit_Campaign_Schedule_Entries" '
            "SET pause_on_gap = %(v)s, updated_at = NOW() WHERE campaign_id = %(campaign_id)s",
            {"v": bool(value), "campaign_id": str(campaign_id)},
        )
        return cur.rowcount


def remove_schedule_window(entry_id):
    with get_cursor(commit=True) as cur:
        cur.execute(
            'DELETE FROM voylla."Blinkit_Campaign_Schedule_Entries" WHERE entry_id = %(entry_id)s',
            {"entry_id": entry_id},
        )
        return cur.rowcount
