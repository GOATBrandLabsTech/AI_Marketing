"""
All SQL for the Blinkit dashboard lives here. The ROAS-impact and
campaign-status queries are ports of the two reference queries used to build
the old Power BI report / Power Apps scheduler - the write helpers below
reproduce the exact Patch() logic those two canvas apps used, so this
dashboard writes to the same columns the same way and the existing scheduled
notebooks (Blinkit_Campaign_Control.ipynb etc.) keep working unchanged.
"""
from datetime import date, datetime, time

from db import get_cursor

ACTION_OPTIONS = ["NO_CHANGE", "INCREASE_CPM", "DECREASE_CPM", "PAUSE", "ZOMBIE_FLAG"]

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
    WHERE l."Brand" = %(brand)s
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
    WHERE "Brand" = %(brand)s
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
SELECT DISTINCT ON (r.campaign_id)
       r.campaign_id::TEXT                AS campaign_id,
       r.campaign_name,
       r.brand,
       r.budget,
       r.last_status,
       COALESCE(e.window_count, 0)        AS window_count,
       e.next_start,
       e.last_end
FROM voylla."Blinkit_Campaign_Runtime" r
LEFT JOIN (
    SELECT campaign_id,
           COUNT(*)        AS window_count,
           MIN(start_date) AS next_start,
           MAX(end_date)   AS last_end
    FROM voylla."Blinkit_Campaign_Schedule_Entries"
    GROUP BY campaign_id
) e ON r.campaign_id::TEXT = e.campaign_id
WHERE r.log_date = (SELECT MAX(log_date) FROM voylla."Blinkit_Campaign_Runtime" WHERE brand = %(brand)s)
AND r.brand = %(brand)s
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
