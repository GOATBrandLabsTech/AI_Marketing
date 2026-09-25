"""
Instamart on-demand AI suggestion engine - same shape as ondemand_engine.py
(Blinkit), adapted to Instamart's real data (Instamart_Campaign_Placement /
instamart_bid) and its own trend-based rule logic. Writes to its own table,
voylla.instamart_ondemand_actions - never touches the legacy
Instamart_actions_llm / instamart_agent_v2.py pipeline.

Action set: INCREASE_CPM / DECREASE_CPM / PAUSE / NO_CHANGE / INSUFFICIENT_DATA.
No ZOMBIE_FLAG - same reasoning as the Blinkit engine: a keyword with poor ROI
and near-zero traffic gets an actionable PAUSE, not a hard-coded parking lot
for human review. zombie_keyword_flag is still computed and shown to the LLM
as evidence, it just doesn't force anything by itself.
"""

import json
import math
import re
from datetime import datetime, date

import pandas as pd
from sqlalchemy import text

SPEND_THRESHOLD = 500.0

TIER_7D_MIN = 300
TIER_15D_MIN = 600
TIER_30D_MIN = 1200

CPM_FLOOR_FALLBACK = 200.0
ZOMBIE_IMPRESSION_MAX = 500
ZOMBIE_SPEND_MAX = 300

AUTO_ACCEPT_ACTIONS = {"INCREASE_CPM", "DECREASE_CPM", "PAUSE"}
AUTO_ACCEPT_HOLD_BACK_QUICK_ACTIONS = {"REVIEW"}


def _f(val, default=0.0):
    try:
        v = float(val)
        return default if math.isnan(v) else v
    except Exception:
        return default


def _safe_int(val, default=0):
    try:
        v = int(float(val))
        return default if math.isnan(v) else v
    except Exception:
        return default


def clean_nan(obj):
    if isinstance(obj, float) and math.isnan(obj):
        return None
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_nan(v) for v in obj]
    return obj


def extract_json(response_text):
    """Pull the JSON array out of an LLM response - same tolerant approach
    as the Blinkit engine (fenced code block, or first [...] found)."""
    text_ = response_text.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text_, re.DOTALL)
    if fence:
        text_ = fence.group(1)
    else:
        start = text_.find("[")
        end = text_.rfind("]")
        if start != -1 and end != -1 and end > start:
            text_ = text_[start:end + 1]
    try:
        return json.loads(text_)
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════
# HISTORY GUARDS - ported from instamart_agent_v2.py (already proven there)
# ══════════════════════════════════════════════════════════════════════════

def detect_oscillation(history_list):
    """Returns (is_loop: bool, description: str). Fires when the last 3
    actions alternate INCREASE <-> DECREASE."""
    if len(history_list) < 3:
        return False, ""
    recent = [h.get("action", "") for h in history_list[:3]]
    pairs = [(recent[0], recent[1]), (recent[1], recent[2])]
    osc = all(
        (a in ("INCREASE_CPM", "DECREASE_CPM")) and
        (b in ("INCREASE_CPM", "DECREASE_CPM")) and
        a != b
        for a, b in pairs
    )
    if osc:
        return True, f"{recent[2]}→{recent[1]}→{recent[0]}"
    return False, ""


def get_cooldown_flag(history_list):
    """Cooldown warning string if the last IMPLEMENTED action was an
    INCREASE or DECREASE - block the opposite move for one cycle unless
    ROAS is extreme."""
    if not history_list:
        return ""
    last = history_list[0]
    if last.get("implemented") != "IMPLEMENTED":
        return ""
    action = last.get("action", "")
    if action == "INCREASE_CPM":
        return "COOLDOWN: Last INCREASE_CPM. Block DECREASE unless 7d ROAS < 1.0."
    if action == "DECREASE_CPM":
        return "COOLDOWN: Last DECREASE_CPM. Block INCREASE unless 7d ROAS > 4.0."
    return ""


# ══════════════════════════════════════════════════════════════════════════
# RULE ENGINE - deterministic, computed BEFORE the LLM call. The LLM treats
# rule_action as a strong default (Blinkit's model), not the "ignore this
# entirely" framing the legacy Instamart prompt used.
# ══════════════════════════════════════════════════════════════════════════

def instamart_search_tier(searches):
    if searches is None:
        return "UNKNOWN"
    s = _safe_int(searches, -1)
    if s < 0:
        return "UNKNOWN"
    if s < 100:
        return "DEAD"
    if s < 400:
        return "LOW"
    if s < 2000:
        return "MEDIUM"
    return "HIGH"


def compute_instamart_tier(row):
    s7 = _f(row.get("spend_7d")) >= TIER_7D_MIN
    s15 = _f(row.get("spend_15d")) >= TIER_15D_MIN
    s30 = _f(row.get("spend_30d")) >= TIER_30D_MIN
    if s7 and s15 and s30:
        return 1
    if not s7 and not s15 and not s30:
        return 3
    return 2


def instamart_tier3_decision(row):
    """No window has enough spend to trust in isolation. Returns
    (action, cpm_change_pct, reason_tag)."""
    roi = _f(row.get("roas_30d")) or _f(row.get("roas_15d")) or _f(row.get("roas_7d"))
    stier = row.get("search_volume_tier", "UNKNOWN")

    if roi < 1.0 and stier == "DEAD":
        return ("PAUSE", 0, "TIER 3 · thin data, ROI<1.0, dead search volume -> PAUSE")

    # Thinner data gets a bigger, more exploratory bump.
    if stier == "DEAD":
        mult = 2.5
    elif stier == "LOW":
        mult = 2.0
    elif roi >= 1.0:
        mult = 1.5
    else:
        mult = 1.3
    pct = round(10 * mult)
    return ("INCREASE_CPM", pct, f"TIER 3 · thin data, ROI={roi:.2f}, search={stier} -> INCREASE {pct}% (exploratory)")


def instamart_trend_decision(row):
    """Tier 1/2 path - direction of ROAS across 7d/15d/30d windows is the
    primary signal (Instamart's real production logic), not a flat
    position/ROI matrix like Blinkit's. Returns (action, cpm_change_pct, tag)."""
    tier = row.get("tier", 2)
    r7 = _f(row.get("roas_7d"))
    r15 = _f(row.get("roas_15d"))
    r30 = _f(row.get("roas_30d"))

    if tier == 1 and r7 < 1.0 and r15 < 1.0 and r30 < 1.0:
        return ("PAUSE", 0, "TIER 1 · 7d/15d/30d ROAS all <1.0 -> PAUSE")

    declining = r7 < r15 < r30
    inclining = r7 > r15 > r30

    if declining:
        return ("DECREASE_CPM", 10, f"TREND · declining 7d({r7:.2f})<15d({r15:.2f})<30d({r30:.2f}) -> DECREASE 10%")

    if inclining:
        if r7 > 3.0 and r15 > 3.0 and r30 > 3.0:
            return ("INCREASE_CPM", 10, f"TREND · inclining, all windows >3.0 -> INCREASE 10%")
        return ("NO_CHANGE", 0, f"TREND · inclining 7d({r7:.2f})>15d({r15:.2f})>30d({r30:.2f}), not yet >3.0 across the board -> NO_CHANGE")

    return ("NO_CHANGE", 0, f"TREND · mixed direction 7d({r7:.2f})/15d({r15:.2f})/30d({r30:.2f}) -> NO_CHANGE")


def instamart_rule_decision(row):
    """Single entry point - routes to the tier-3 matrix or the trend
    analysis depending on data sufficiency. Returns (action, cpm_change_pct, reason_tag)."""
    tier = compute_instamart_tier(row)
    row["tier"] = tier
    if tier == 3:
        return instamart_tier3_decision(row)
    return instamart_trend_decision(row)


def inject_python_flags(row):
    """Compute tier, search-volume tier, zombie flag (advisory only) and the
    rule_action - mutates row in place, same convention as the Blinkit engine."""
    row["search_volume_tier"] = instamart_search_tier(row.get("keyword_searches"))
    action, cpm_change, tag = instamart_rule_decision(row)
    row["rule_action"] = action
    row["rule_cpm_change"] = cpm_change
    row["rule_tag"] = tag
    row["cpm_floor"] = _f(row.get("min_bid"), CPM_FLOOR_FALLBACK) or CPM_FLOOR_FALLBACK
    row["zombie_keyword_flag"] = bool(
        _f(row.get("current_cpm")) >= row["cpm_floor"] and
        _f(row.get("impressions_7d"), 9999) <= ZOMBIE_IMPRESSION_MAX and
        _f(row.get("spend_7d"), 9999) <= ZOMBIE_SPEND_MAX
    )
    return row


# ══════════════════════════════════════════════════════════════════════════
# DATA FETCH - Instamart_Campaign_Placement is keyword-grain; instamart_bid
# is the current-bid/min-bid state.
# ══════════════════════════════════════════════════════════════════════════

def _fetch_aggregated_campaign_data(engine, brand, campaign_id):
    query = text("""
        SELECT "Date" AS report_date, "CAMPAIGN_ID" AS campaign_id, "CAMPAIGN_NAME" AS campaign_name,
               "KEYWORD" AS targeting, "MATCH_TYPE" AS match_type,
               "TOTAL_BUDGET_BURNT" AS spend, "TOTAL_GMV" AS sales,
               "TOTAL_IMPRESSIONS" AS impressions, "TOTAL_CLICKS" AS clicks,
               "TOTAL_BUDGET" AS campaign_budget
        FROM voylla."Instamart_Campaign_Placement"
        WHERE "Brand" = :brand AND "CAMPAIGN_ID" = :campaign_id
          AND "Date"::date >= (CURRENT_DATE - INTERVAL '31 days')
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"brand": brand, "campaign_id": str(campaign_id)})

    if df.empty:
        return None

    df["report_date"] = pd.to_datetime(df["report_date"])
    df["spend"] = df["spend"].fillna(0)
    df["sales"] = df["sales"].fillna(0)
    campaign_name = df["campaign_name"].dropna().iloc[-1] if not df["campaign_name"].dropna().empty else ""
    campaign_budget = _f(df["campaign_budget"].dropna().iloc[-1]) if not df["campaign_budget"].dropna().empty else None

    today = pd.Timestamp.now().normalize()
    windows = {}
    for days in (7, 15, 30):
        cutoff = today - pd.Timedelta(days=days)
        sub = df[df["report_date"] >= cutoff]
        agg = sub.groupby(["targeting", "match_type"]).agg(
            spend=("spend", "sum"), sales=("sales", "sum"), impressions=("impressions", "sum"),
        ).reset_index()
        agg["roas"] = (agg["sales"] / agg["spend"]).replace([float("inf"), -float("inf")], 0).fillna(0).round(2)
        windows[days] = agg.rename(columns={
            "spend": f"spend_{days}d", "sales": f"sales_{days}d",
            "impressions": f"impressions_{days}d", "roas": f"roas_{days}d",
        })

    merged = windows[30][["targeting", "match_type"]].drop_duplicates()
    for days in (7, 15, 30):
        merged = merged.merge(windows[days], on=["targeting", "match_type"], how="left")
    for days in (7, 15, 30):
        for col in (f"spend_{days}d", f"sales_{days}d", f"impressions_{days}d", f"roas_{days}d"):
            merged[col] = merged[col].fillna(0)

    bid_query = text("""
        SELECT targeting, match_type, current_bid AS current_cpm,
               min_bid, search_count AS keyword_searches
        FROM voylla.instamart_bid
        WHERE "Brand" = :brand AND campaign_id = :campaign_id
    """)
    with engine.connect() as conn:
        bid_df = pd.read_sql(bid_query, conn, params={"brand": brand, "campaign_id": str(campaign_id)})

    merged = merged.merge(bid_df, on=["targeting", "match_type"], how="left")
    merged["current_cpm"] = merged["current_cpm"].fillna(0)

    campaign_spend_7d = merged["spend_7d"].sum()

    return {
        "aggregated_df": merged,
        "campaign_name": campaign_name,
        "campaign_budget": campaign_budget,
        "campaign_spend": campaign_spend_7d,
    }


def _fetch_ondemand_history(engine, brand, campaign_id, limit_per_keyword=5):
    query = text("""
        SELECT unique_key, action_date, campaign_id, targeting, action,
               user_implemented, override_note, explanation
        FROM voylla.instamart_ondemand_actions
        WHERE "Brand" = :brand AND campaign_id = :campaign_id
        ORDER BY action_date DESC
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"brand": brand, "campaign_id": str(campaign_id)})
    return df


def resolve_impl(val):
    if val is True or str(val).lower() == "true":
        return "IMPLEMENTED"
    if val is False or str(val).lower() == "false":
        return "REJECTED"
    return "UNDECIDED"


def build_previous_context(history_df, campaign_id, targeting_key):
    if history_df.empty:
        return {"previous_summary": "No prior history for this keyword.", "previous_history": []}

    filtered = history_df[
        history_df["targeting"].astype(str).str.strip().str.lower().str.replace(" ", "_")
        == targeting_key
    ].sort_values("action_date", ascending=False)

    if filtered.empty:
        return {"previous_summary": "No prior history for this keyword.", "previous_history": []}

    rec = filtered.iloc[0].to_dict()
    impl = resolve_impl(rec.get("user_implemented"))
    history_rows = [
        {"date": str(r.get("action_date", "")), "action": r.get("action", "UNKNOWN"), "implemented": resolve_impl(r.get("user_implemented"))}
        for r in filtered.head(5).to_dict(orient="records")
    ]
    loop, loop_desc = detect_oscillation(history_rows)
    cooldown = get_cooldown_flag(history_rows)

    summary = f"[{rec.get('action_date')}] {rec.get('action')} | {impl}"
    if loop:
        summary += f" | LOOP: {loop_desc}"
    if cooldown:
        summary += f" | {cooldown}"

    return {"previous_summary": summary, "previous_history": history_rows}


# ══════════════════════════════════════════════════════════════════════════
# ROW CLEANING / SAFETY GUARDS - runs on every LLM output row before it's
# allowed to be written. Same job as Blinkit's _build_clean_rows: floor
# clamp, PAUSE-validity guard, quick_action, unique_key.
# ══════════════════════════════════════════════════════════════════════════

def _quick_action(action, confidence):
    if action == "PAUSE":
        return "PAUSE_NOW"
    if action == "INSUFFICIENT_DATA":
        return "LOW_PRIORITY"
    if action in ("INCREASE_CPM", "DECREASE_CPM"):
        return "HIGH_PRIORITY" if confidence >= 0.85 else "PRIORITY"
    if action == "NO_CHANGE":
        return "STABLE"
    return "REVIEW"


def _build_clean_rows(action_obj, brand, tolerance_pct=20):
    if not action_obj:
        return []
    if isinstance(action_obj, dict):
        action_obj = [action_obj]

    clean_rows = []
    today_str = date.today().strftime("%Y-%m-%d")

    for a in action_obj:
        if not isinstance(a, dict):
            continue

        campaign_id = str(a.get("campaign_id", ""))
        campaign_name = a.get("campaign_name", "")
        targeting = str(a.get("targeting", "")).strip().lower().replace(" ", "_")
        match_type = a.get("match_type", "")
        action = str(a.get("action", "")).upper()
        explanation = a.get("explanation", "")
        rule_action = a.get("rule_action")

        confidence = a.get("confidence")
        try:
            confidence = float(str(confidence).strip())
            if not (0.0 < confidence <= 1.0):
                raise ValueError("out of range")
        except Exception:
            confidence = 0.75
            a["_confidence_defaulted"] = True

        try:
            cpm_change = int(a.get("cpm_change"))
        except Exception:
            cpm_change = None

        try:
            current_cpm = float(a.get("current_cpm"))
            if math.isnan(current_cpm):
                current_cpm = None
        except Exception:
            current_cpm = None

        cpm_floor = _f(a.get("cpm_floor"), CPM_FLOOR_FALLBACK) or CPM_FLOOR_FALLBACK

        # Floor enforcement: a cut that would breach the floor clamps to
        # exactly the floor rather than being blocked outright.
        if action == "DECREASE_CPM" and current_cpm is not None:
            if current_cpm <= cpm_floor:
                action, cpm_change = "NO_CHANGE", 0
            else:
                proposed = current_cpm * (1 - min(abs(cpm_change or 10), tolerance_pct) / 100.0)
                if proposed < cpm_floor:
                    cpm_change = round((1 - cpm_floor / current_cpm) * 100)

        # Cap any single move at tolerance_pct regardless of what the LLM asked for.
        if action in ("INCREASE_CPM", "DECREASE_CPM") and cpm_change is not None:
            cpm_change = max(-tolerance_pct, min(tolerance_pct, cpm_change))

        # An LLM-invented PAUSE (rule didn't say PAUSE) needs the numbers to back it.
        if action == "PAUSE" and rule_action != "PAUSE":
            roas_ok = "roi" in explanation.lower() or "roas" in explanation.lower()
            if not roas_ok:
                action, cpm_change = "NO_CHANGE", 0

        confidence = round(confidence, 2)
        quick_action = _quick_action(action, confidence)
        unique_key = f"{campaign_id}_{today_str}_{targeting}_{action}"

        clean_rows.append({
            "unique_key": unique_key,
            "action_date": today_str,
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "targeting": targeting,
            "match_type": match_type,
            "action": action,
            "cpm_change": cpm_change,
            "confidence": confidence,
            "explanation": explanation,
            "alternative_keywords": a.get("alternative_keywords") or [],
            "Brand": brand,
            "current_cpm": current_cpm,
            "campaign_budget": a.get("campaign_budget"),
            "cpm_floor": cpm_floor,
            "search_volume_tier": a.get("search_volume_tier", "UNKNOWN"),
            "quick_action": quick_action,
            "rule_action": rule_action,
            "decision_step": a.get("rule_tag", ""),
        })

    return clean_rows


def save_ondemand_action(engine, action_obj, brand, requested_by=None, tolerance_pct=20):
    """Same upsert-gated-by-undecided pattern as Blinkit's save_ondemand_action,
    writing into voylla.instamart_ondemand_actions."""
    clean_rows = _build_clean_rows(action_obj, brand, tolerance_pct=tolerance_pct)
    if not clean_rows:
        return []

    for r in clean_rows:
        r["requested_by"] = requested_by

    upsert_sql = text("""
        INSERT INTO voylla.instamart_ondemand_actions
        (unique_key, action_date, campaign_id, campaign_name,
         targeting, match_type, action, bid_change, confidence,
         explanation, alternative_keywords, "Brand", current_cpm, campaign_budget,
         cpm_floor, search_volume_tier, quick_action, rule_action, decision_step,
         requested_by)
        VALUES
        (:unique_key, :action_date, :campaign_id, :campaign_name,
         :targeting, :match_type, :action, :cpm_change, :confidence,
         :explanation, :alternative_keywords, :Brand, :current_cpm, :campaign_budget,
         :cpm_floor, :search_volume_tier, :quick_action, :rule_action, :decision_step,
         :requested_by)
        ON CONFLICT (unique_key) DO UPDATE SET
            action               = EXCLUDED.action,
            bid_change           = EXCLUDED.bid_change,
            confidence           = EXCLUDED.confidence,
            explanation          = EXCLUDED.explanation,
            alternative_keywords = EXCLUDED.alternative_keywords,
            current_cpm          = EXCLUDED.current_cpm,
            campaign_budget      = EXCLUDED.campaign_budget,
            cpm_floor            = EXCLUDED.cpm_floor,
            search_volume_tier   = EXCLUDED.search_volume_tier,
            quick_action         = EXCLUDED.quick_action,
            rule_action          = EXCLUDED.rule_action,
            decision_step        = EXCLUDED.decision_step,
            requested_by         = EXCLUDED.requested_by,
            requested_at         = NOW()
        WHERE voylla.instamart_ondemand_actions.user_implemented IS NULL
        RETURNING unique_key
    """)
    written_keys = set()
    with engine.begin() as conn:
        for row in clean_rows:
            result = conn.execute(upsert_sql, {**row, "alternative_keywords": json.dumps(row["alternative_keywords"])})
            written_keys.update(r[0] for r in result)

    fresh_rows = [r for r in clean_rows if r["unique_key"] in written_keys]
    skipped = len(clean_rows) - len(fresh_rows)

    accepted = _auto_accept_if_enabled(brand, clean_rows[0]["campaign_id"], fresh_rows)
    print(f"[instamart-ondemand] wrote {len(fresh_rows)}/{len(clean_rows)} suggestion row(s) for campaign {clean_rows[0]['campaign_id']}"
          + (f", {skipped} already-decided keyword(s) left untouched" if skipped else "")
          + (f" - auto-accepted {accepted} (autonomy mode 'auto')" if accepted else ""))
    return clean_rows


def _auto_accept_if_enabled(brand, campaign_id, clean_rows):
    """Reuses the exact same campaign_autonomy_mode table as Blinkit - it's
    keyed on (campaign_id, brand) only, not channel-scoped, and Instamart's
    UUID campaign_ids can never collide with Blinkit's numeric ones."""
    import queries

    setting = queries.fetch_autonomy_setting(campaign_id, brand)
    if setting["mode"] != "auto" or not setting["ondemand_managed"]:
        return 0

    accepted = 0
    for row in clean_rows:
        if row["action"] not in AUTO_ACCEPT_ACTIONS:
            continue
        if row["quick_action"] in AUTO_ACCEPT_HOLD_BACK_QUICK_ACTIONS:
            continue
        queries.accept_instamart_ondemand_action(row["unique_key"], brand, True, None, "auto-accepted - autonomy mode 'auto'")
        accepted += 1
    return accepted


# ══════════════════════════════════════════════════════════════════════════
# LLM PROMPT + MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

def generate_ondemand_suggestions(brand, campaign_id, requested_by=None):
    """One synchronous call: fetch this campaign's rolling window data, run
    the deterministic rule engine + LLM decision, and write fresh suggestion
    rows into voylla.instamart_ondemand_actions.
    Returns {"ok": bool, "count": int, "rows": [...], "error": str|None}."""
    from db import get_engine, get_anthropic_config
    import anthropic as anthropic_sdk
    import queries as _queries

    engine = get_engine()
    campaign_id = str(campaign_id)

    data = _fetch_aggregated_campaign_data(engine, brand, campaign_id)
    if data is None:
        return {"ok": False, "count": 0, "rows": [], "error": "No recent placement data for this campaign."}

    aggregated_df = data["aggregated_df"]
    campaign_name = data["campaign_name"]
    campaign_budget = data["campaign_budget"]
    campaign_spend = data["campaign_spend"]

    history_df = _fetch_ondemand_history(engine, brand, campaign_id)
    if not history_df.empty:
        history_df["campaign_id"] = history_df["campaign_id"].astype(str)

    data_for_llm = aggregated_df.to_dict(orient="records")
    ground_truth = {}
    for row in data_for_llm:
        row["campaign_id"] = campaign_id
        row["campaign_name"] = campaign_name
        row["campaign_budget"] = campaign_budget
        targeting_key = str(row.get("targeting", "")).strip().lower().replace(" ", "_")
        prev = build_previous_context(history_df, campaign_id, targeting_key)
        row["previous_summary"] = prev["previous_summary"]

        notes = _queries.fetch_relevant_notes(brand, campaign_id=campaign_id, targeting=targeting_key)
        if notes:
            notes_text = " | ".join(f"NOTE ({n['entity_type']}): {n['text']}" for n in notes)
            row["previous_summary"] += f" | {notes_text}"
        row["previous_history"] = prev["previous_history"]
        inject_python_flags(row)

        # Python-computed facts (rule_action, rule_tag, cpm_floor, tier,
        # zombie flag, real current_cpm) are ground truth - the LLM's JSON
        # echo of these fields is untrusted and gets overwritten with this
        # lookup below, keyed by (targeting, match_type), so a dropped or
        # hallucinated field in the model's output can never corrupt them.
        gt_key = (targeting_key, str(row.get("match_type", "")))
        ground_truth[gt_key] = {
            "rule_action": row["rule_action"],
            "rule_tag": row["rule_tag"],
            "cpm_floor": row["cpm_floor"],
            "search_volume_tier": row["search_volume_tier"],
            "current_cpm": row.get("current_cpm"),
            "campaign_budget": campaign_budget,
        }

    cfg = get_anthropic_config()
    client = anthropic_sdk.Anthropic(api_key=cfg["api_key"])
    model = cfg.get("model") or "claude-haiku-4-5-20251001"

    insufficient = campaign_spend < SPEND_THRESHOLD

    if insufficient:
        prompt = f"""
        You are a performance marketing expert analyzing Instamart (Swiggy) ad campaigns.

        CONTEXT:
        - This campaign has a total 7-day spend of ₹{campaign_spend:.0f}, below the ₹500 threshold.
        - All keywords are classified INSUFFICIENT_DATA. Do not recommend PAUSE or a CPM change.
        - Action must always be INSUFFICIENT_DATA.

        OUTPUT FORMAT: Return ONLY a valid JSON array, each object with EXACTLY:
        campaign_id, campaign_name, targeting, match_type, action, cpm_change, confidence,
        explanation, alternative_keywords, current_cpm, campaign_budget.

        CURRENT KEYWORDS:
        {json.dumps(clean_nan(data_for_llm))}

        Return ONLY the JSON array. No markdown fences, no preamble.
        """
    else:
        prompt = f"""
        You are a performance marketing expert analyzing Instamart (Swiggy) ad campaigns.
        CPM bidding, same mechanics as other quick-commerce platforms you know.

        The `action` field is YOUR final call. `rule_action` (Python-computed from the
        7d/15d/30d ROAS trend, or the thin-data matrix when spend is too low to trust a
        trend) is your STRONG DEFAULT in BOTH directions - follow it unless the numbers
        give you a specific reason not to, and say so explicitly when you diverge.

        RULE ENGINE (already computed per row as rule_action / rule_tag):
        - Tier 1 (7d/15d/30d spend all sufficient): trend of ROAS across the three
          windows drives the call. Declining (7d<15d<30d) -> DECREASE. Inclining
          (7d>15d>30d) -> NO_CHANGE, or INCREASE if all three windows are already >3.0.
          Mixed direction -> NO_CHANGE. All three windows <1.0 ROAS -> PAUSE.
        - Tier 2 (some windows insufficient): same trend logic on whatever windows exist.
        - Tier 3 (no window has enough spend to trust): ROI<1.0 + dead search volume ->
          PAUSE. Otherwise an exploratory INCREASE (bigger on thinner data) - Instamart
          treats "we don't know yet" as a reason to spend a little more to find out, not
          a reason to sit still.

        Safety guards Python applies AFTER your response (do NOT self-censor around them):
           1. DECREASE_CPM is clamped so the new CPM never lands below cpm_floor (min_bid);
              if already at/below the floor -> NO_CHANGE.
           2. Any single move is capped at 20% regardless of what you ask for.
           3. A PAUSE you invent (rule_action is not PAUSE) is blocked unless your own
              explanation cites the ROI number backing it. A rule-mandated PAUSE always
              goes through.

        -> Each row has "previous_summary" (copy verbatim into the explanation's
           "Previous:" section) and "previous_history" (use for decision-making, newest
           first). Do NOT repeat an action flagged LOOP. Do NOT cross-reference between
           keywords.
        -> If previous_summary contains "| NOTE (type): ...", that is context a human
           filed - an upcoming event, a standing instruction, a correction - not a
           computed fact. Weigh it alongside the numbers, don't let it override a clear
           numeric signal, and say so explicitly if you act on one.
        -> zombie_keyword_flag=true means near-zero traffic at/above the floor CPM -
           evidence for PAUSE, not an automatic one.

        CURRENT DATA (7d/15d/30d aggregated, keyword + match_type grain):
        {json.dumps(clean_nan(data_for_llm))}

        For each keyword: read rule_action + rule_tag, decide the FINAL action yourself
        (diverge only with a specific numeric reason), then write the explanation in
        4 sections joined by " || ": DATA, ANALYSIS, HISTORY, RECOMMENDATION.

        OUTPUT FORMAT: Return ONLY a valid JSON array, each object with EXACTLY these
        fields in this order: campaign_id, targeting, match_type, action, explanation,
        campaign_name, cpm_change, confidence, cpm_floor, search_volume_tier,
        alternative_keywords, current_cpm, campaign_budget, rule_action.

        confidence: decimal 0.70-0.95, mandatory, never null, never 0.0/1.0.
        cpm_change: integer percent, 10-20 typical for INCREASE/DECREASE, 0 for
        NO_CHANGE/PAUSE/INSUFFICIENT_DATA.
        alternative_keywords: [] unless action = PAUSE.

        Return ONLY the JSON array. No markdown fences, no preamble.
        """

    response = client.messages.create(
        model=model, max_tokens=8000, temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    action_obj = extract_json(response.content[0].text)

    if not action_obj:
        return {"ok": False, "count": 0, "rows": [], "error": "LLM returned no parsable suggestions."}

    for a in action_obj:
        if not isinstance(a, dict):
            continue
        key = (str(a.get("targeting", "")).strip().lower().replace(" ", "_"), str(a.get("match_type", "")))
        gt = ground_truth.get(key)
        if gt:
            a.update(gt)

    setting = _queries.fetch_autonomy_setting(campaign_id, brand)
    tolerance_pct = setting.get("bid_tolerance_pct") or 20

    clean_rows = save_ondemand_action(engine, action_obj, brand, requested_by=requested_by, tolerance_pct=tolerance_pct)

    return {"ok": True, "count": len(clean_rows), "rows": clean_rows, "error": None}
