"""
Zepto on-demand AI suggestion engine - same shape as instamart_engine.py,
adapted to Zepto's real data (DataWarehouse.Keyword_Performance_Zepto /
voylla.Zepto_Keyword_Bid) and its own, more elaborate rule tree (ported from
the legacy Zepto_actions_llm_marketing_Batch_Api.ipynb). Writes to its own
table, voylla.zepto_ondemand_actions - never touches the legacy
Zepto_actions_llm / Zepto_Update_CPC.ipynb pipeline.

CPC bidding, not CPM - confirmed from the legacy notebook's own field names
(current_cpc, cpc_change) and real data (bids in the low tens of rupees).

Action set: INCREASE_CPC / DECREASE_CPC / PAUSE / NO_CHANGE / INSUFFICIENT_DATA.
No hard zombie/search-pause override - same reasoning as Blinkit's retired
ZOMBIE_FLAG and Instamart's engine: a flagged keyword is evidence the LLM
weighs, not a Python-forced action.

KNOWN LIMITATION: campaign_autonomy_mode is keyed on (campaign_id, brand)
only, with no channel column. Zepto's campaign_ids are plain integers, same
ID space as Blinkit's (unlike Instamart's UUIDs) - a real, if currently
zero-probability, collision risk if a Zepto and a Blinkit campaign for the
same brand ever share a numeric ID. Checked against live data at build time:
zero overlap (442 Blinkit vs 16 Zepto campaign_ids). Not fixed here since it
would mean a breaking migration across already-shipped Blinkit/Instamart
code for a risk that isn't materialized - worth a deliberate decision if
this ever needs to scale further, not a silent guess.
"""

import json
import math
import re
from datetime import datetime, date

import pandas as pd
from sqlalchemy import text

SPEND_THRESHOLD = 500.0

TIER_7D_MIN = 500
TIER_15D_MIN = 1000
TIER_30D_MIN = 2000

CPC_FLOOR_FALLBACK = 5.0
ZOMBIE_CPC_MIN = 300
ZOMBIE_IMPRESSION_MAX = 50
ZOMBIE_SPEND_7D_MAX = 100
ZOMBIE_SPEND_15D_MAX = 200

AUTO_ACCEPT_ACTIONS = {"INCREASE_CPC", "DECREASE_CPC", "PAUSE"}
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


def detect_oscillation(history_list):
    if len(history_list) < 3:
        return False, ""
    recent = [h.get("action", "") for h in history_list[:3]]
    pairs = [(recent[0], recent[1]), (recent[1], recent[2])]
    osc = all(
        (a in ("INCREASE_CPC", "DECREASE_CPC")) and
        (b in ("INCREASE_CPC", "DECREASE_CPC")) and
        a != b
        for a, b in pairs
    )
    if osc:
        return True, f"{recent[2]}→{recent[1]}→{recent[0]}"
    return False, ""


def get_cooldown_flag(history_list):
    """Zepto's own tuning (from the legacy notebook) - deliberately not the
    same thresholds as Instamart's equivalent (1.0/4.0): Zepto uses 1.0 after
    an increase, 4.5 after a decrease."""
    if not history_list:
        return ""
    last = history_list[0]
    if last.get("implemented") != "IMPLEMENTED":
        return ""
    action = last.get("action", "")
    if action == "INCREASE_CPC":
        return "COOLDOWN: Last INCREASE_CPC. Block DECREASE unless 7d ROAS < 1.0."
    if action == "DECREASE_CPC":
        return "COOLDOWN: Last DECREASE_CPC. Block INCREASE unless 7d ROAS > 4.5."
    return ""


# ══════════════════════════════════════════════════════════════════════════
# RULE ENGINE - ported from the legacy Zepto notebook's gated decision tree.
# ══════════════════════════════════════════════════════════════════════════

def zepto_search_tier(searches):
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


def compute_zepto_tier(row):
    s7 = _f(row.get("spend_7d")) >= TIER_7D_MIN
    s15 = _f(row.get("spend_15d")) >= TIER_15D_MIN
    s30 = _f(row.get("spend_30d")) >= TIER_30D_MIN
    if s7 and s15 and s30:
        return 1
    if not s7 and not s15 and not s30:
        return 3
    return 2


def zepto_tier3_decision(row):
    """No window has enough spend to trust in isolation. PAUSE is forbidden
    here unless there's been real burn with zero return across every window
    - otherwise this always defaults to NO_CHANGE, same "no judgment on thin
    data" philosophy the legacy notebook documents explicitly."""
    r7, r15, r30 = _f(row.get("roas_7d")), _f(row.get("roas_15d")), _f(row.get("roas_30d"))
    spend_30d = _f(row.get("spend_30d"))
    sufficient_burn_no_roas = spend_30d >= 500 and r7 == 0 and r15 == 0 and r30 == 0

    if sufficient_burn_no_roas:
        return ("DECREASE_CPC", 10, "TIER 3 · 30d spend ≥₹500 with zero ROAS on every window -> DECREASE 10%")
    return ("NO_CHANGE", 0, "TIER 3 · no window has enough spend to trust -> NO_CHANGE (no judgment on thin data)")


def zepto_history_gate(row):
    """STRONG/MODERATE/WEAK gate on 30d/15d ROAS, evaluated before the Step 3
    threshold rules - a strong or moderate history can lock PAUSE and/or
    DECREASE out entirely regardless of what Step 3 would otherwise say.
    Returns (gate, locks_pause, locks_decrease) or (None, False, False) if
    no gate fires (WEAK - proceed to Step 3)."""
    r30, r15 = _f(row.get("roas_30d")), _f(row.get("roas_15d"))
    if r30 >= 3.0 or r15 >= 3.0:
        return "STRONG", True, True
    if 2.0 <= r30 < 3.0 and r15 < 3.0:
        return "MODERATE", True, False
    return None, False, False


def zepto_rule_decision(row):
    """Single entry point - tier routing, then (for Tier 1/2) the history
    gate, then the Step 3 ROAS threshold rules. Returns (action,
    cpc_change_pct, reason_tag)."""
    tier = compute_zepto_tier(row)
    row["tier"] = tier
    if tier == 3:
        return zepto_tier3_decision(row)

    cpc_floor = _f(row.get("cpc_floor"), CPC_FLOOR_FALLBACK) or CPC_FLOOR_FALLBACK
    current_cpc = _f(row.get("current_cpc"))
    r7, r15, r30 = _f(row.get("roas_7d")), _f(row.get("roas_15d")), _f(row.get("roas_30d"))

    gate, locks_pause, locks_decrease = zepto_history_gate(row)
    if gate == "STRONG":
        if r7 > 4.0 and r15 > 3.5 and r30 > 3.0:
            return ("INCREASE_CPC", 10, "HISTORY GATE · STRONG+SCALE (7d>4.0,15d>3.5,30d>3.0) -> INCREASE 10%")
        return ("NO_CHANGE", 0, "HISTORY GATE · STRONG -> HOLD (PAUSE/DECREASE locked out)")
    if gate == "MODERATE":
        if current_cpc > cpc_floor:
            return ("DECREASE_CPC", 10, "HISTORY GATE · MODERATE (30d 2.0-2.99, 15d<3.0) -> DECREASE 10% (PAUSE locked out)")
        return ("NO_CHANGE", 0, "HISTORY GATE · MODERATE, already at floor -> NO_CHANGE")

    # WEAK - no gate fired, fall through to Step 3 ROAS threshold rules.
    if r7 < 1.0 and r15 < 1.0 and r30 < 1.0:
        return ("PAUSE", 0, "STEP 3 · Rule A: 7d/15d/30d ROAS all <1.0 -> PAUSE")
    if 1.0 <= r7 < 3.0:
        if current_cpc > cpc_floor:
            return ("DECREASE_CPC", 10, f"STEP 3 · Rule B: 7d ROAS {r7:.2f} in [1.0,3.0) -> DECREASE 10%")
        return ("NO_CHANGE", 0, f"STEP 3 · Rule B condition met but already at floor -> NO_CHANGE")
    if 3.0 <= r7 <= 4.0:
        return ("NO_CHANGE", 0, f"STEP 3 · Rule C: 7d ROAS {r7:.2f} in [3.0,4.0] -> NO_CHANGE")
    if r7 > 4.0 and r15 > 3.5 and r30 > 3.0:
        return ("INCREASE_CPC", 10, "STEP 3 · Rule D: 7d>4.0,15d>3.5,30d>3.0 -> INCREASE 10%")
    return ("NO_CHANGE", 0, "STEP 3 · no rule matched -> NO_CHANGE")


def inject_python_flags(row):
    """Computes tier, search-volume tier, and the advisory zombie/search-pause
    flags (evidence for the LLM, never a forced action), plus rule_action."""
    row["search_volume_tier"] = zepto_search_tier(row.get("keyword_searches"))
    action, cpc_change, tag = zepto_rule_decision(row)
    row["rule_action"] = action
    row["rule_cpc_change"] = cpc_change
    row["rule_tag"] = tag
    row["cpc_floor"] = _f(row.get("min_bid"), CPC_FLOOR_FALLBACK) or CPC_FLOOR_FALLBACK

    current_cpc = _f(row.get("current_cpc"))
    row["zombie_keyword_flag"] = bool(
        current_cpc > ZOMBIE_CPC_MIN and
        _f(row.get("impressions_7d"), 9999) < ZOMBIE_IMPRESSION_MAX and
        _f(row.get("spend_7d"), 9999) < ZOMBIE_SPEND_7D_MAX and
        _f(row.get("spend_15d"), 9999) < ZOMBIE_SPEND_15D_MAX
    )
    r7, r15 = _f(row.get("roas_7d")), _f(row.get("roas_15d"))
    row["low_search_zero_roas"] = bool(
        row["search_volume_tier"] in ("DEAD", "LOW") and r7 == 0 and r15 == 0
    )
    return row


# ══════════════════════════════════════════════════════════════════════════
# DATA FETCH - Keyword_Performance_Zepto (DataWarehouse schema, not voylla)
# is keyword-grain; Zepto_Keyword_Bid (section='EXISTING_KEYWORDS') is the
# current-bid/min-bid state.
# ══════════════════════════════════════════════════════════════════════════

def _fetch_aggregated_campaign_data(engine, brand, campaign_id):
    query = text("""
        SELECT TO_TIMESTAMP("Date", 'YYYY-MM-DD HH24:MI:SS') AS report_date,
               "Campaign_id" AS campaign_id, "Campaign_name" AS campaign_name,
               "KeywordName" AS targeting, "KeywordMatchType" AS match_type,
               "Spend" AS spend, "Revenue" AS sales,
               "Impressions" AS impressions, "Clicks" AS clicks
        FROM "DataWarehouse"."Keyword_Performance_Zepto"
        WHERE "Brand" = :brand AND "Campaign_id" = :campaign_id
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"brand": brand, "campaign_id": int(campaign_id)})

    if df.empty:
        return None

    df["report_date"] = pd.to_datetime(df["report_date"]).dt.tz_localize(None)
    df = df[df["report_date"] >= pd.Timestamp.now() - pd.Timedelta(days=31)]
    if df.empty:
        return None
    df["spend"] = df["spend"].fillna(0)
    df["sales"] = df["sales"].fillna(0)
    campaign_name = df["campaign_name"].dropna().iloc[-1] if not df["campaign_name"].dropna().empty else ""

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
        SELECT keyword AS targeting, match_type, original_bid AS current_cpc,
               min_bid, search_volume AS keyword_searches, daily_budget AS campaign_budget
        FROM voylla."Zepto_Keyword_Bid"
        WHERE "Brand" = :brand AND campaign_id = :campaign_id AND section = 'EXISTING_KEYWORDS'
    """)
    with engine.connect() as conn:
        bid_df = pd.read_sql(bid_query, conn, params={"brand": brand, "campaign_id": int(campaign_id)})

    campaign_budget = _f(bid_df["campaign_budget"].dropna().iloc[0]) if not bid_df.empty and not bid_df["campaign_budget"].dropna().empty else None
    merged = merged.merge(bid_df.drop(columns=["campaign_budget"], errors="ignore"), on=["targeting", "match_type"], how="left")
    merged["current_cpc"] = merged["current_cpc"].fillna(0)

    campaign_spend_7d = merged["spend_7d"].sum()

    return {
        "aggregated_df": merged,
        "campaign_name": campaign_name,
        "campaign_budget": campaign_budget,
        "campaign_spend": campaign_spend_7d,
    }


def _fetch_ondemand_history(engine, brand, campaign_id):
    query = text("""
        SELECT unique_key, action_date, campaign_id, targeting, action,
               user_implemented, override_note, explanation
        FROM voylla.zepto_ondemand_actions
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
# ROW CLEANING / SAFETY GUARDS
# ══════════════════════════════════════════════════════════════════════════

def _quick_action(action, confidence):
    if action == "PAUSE":
        return "PAUSE_NOW"
    if action == "INSUFFICIENT_DATA":
        return "LOW_PRIORITY"
    if action in ("INCREASE_CPC", "DECREASE_CPC"):
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
            cpc_change = int(a.get("cpc_change"))
        except Exception:
            cpc_change = None

        try:
            current_cpc = float(a.get("current_cpc"))
            if math.isnan(current_cpc):
                current_cpc = None
        except Exception:
            current_cpc = None

        cpc_floor = _f(a.get("cpc_floor"), CPC_FLOOR_FALLBACK) or CPC_FLOOR_FALLBACK

        # Floor enforcement: a cut that would breach the floor clamps to
        # exactly the floor rather than being blocked outright.
        if action == "DECREASE_CPC" and current_cpc is not None:
            if current_cpc <= cpc_floor:
                action, cpc_change = "NO_CHANGE", 0
            else:
                proposed = current_cpc * (1 - min(abs(cpc_change or 10), tolerance_pct) / 100.0)
                if proposed < cpc_floor:
                    cpc_change = round((1 - cpc_floor / current_cpc) * 100)

        if action in ("INCREASE_CPC", "DECREASE_CPC") and cpc_change is not None:
            cpc_change = max(-tolerance_pct, min(tolerance_pct, cpc_change))

        if action == "PAUSE" and rule_action != "PAUSE":
            roas_ok = "roi" in explanation.lower() or "roas" in explanation.lower()
            if not roas_ok:
                action, cpc_change = "NO_CHANGE", 0

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
            "cpc_change": cpc_change,
            "confidence": confidence,
            "explanation": explanation,
            "alternative_keywords": a.get("alternative_keywords") or [],
            "Brand": brand,
            "current_cpc": current_cpc,
            "campaign_budget": a.get("campaign_budget"),
            "cpc_floor": cpc_floor,
            "search_volume_tier": a.get("search_volume_tier", "UNKNOWN"),
            "quick_action": quick_action,
            "rule_action": rule_action,
            "decision_step": a.get("rule_tag", ""),
        })

    return clean_rows


def save_ondemand_action(engine, action_obj, brand, requested_by=None, tolerance_pct=20):
    clean_rows = _build_clean_rows(action_obj, brand, tolerance_pct=tolerance_pct)
    if not clean_rows:
        return []

    for r in clean_rows:
        r["requested_by"] = requested_by

    upsert_sql = text("""
        INSERT INTO voylla.zepto_ondemand_actions
        (unique_key, action_date, campaign_id, campaign_name,
         targeting, match_type, action, bid_change, confidence,
         explanation, alternative_keywords, "Brand", current_cpc, campaign_budget,
         cpc_floor, search_volume_tier, quick_action, rule_action, decision_step,
         requested_by)
        VALUES
        (:unique_key, :action_date, :campaign_id, :campaign_name,
         :targeting, :match_type, :action, :cpc_change, :confidence,
         :explanation, :alternative_keywords, :Brand, :current_cpc, :campaign_budget,
         :cpc_floor, :search_volume_tier, :quick_action, :rule_action, :decision_step,
         :requested_by)
        ON CONFLICT (unique_key) DO UPDATE SET
            action               = EXCLUDED.action,
            bid_change           = EXCLUDED.bid_change,
            confidence           = EXCLUDED.confidence,
            explanation          = EXCLUDED.explanation,
            alternative_keywords = EXCLUDED.alternative_keywords,
            current_cpc          = EXCLUDED.current_cpc,
            campaign_budget      = EXCLUDED.campaign_budget,
            cpc_floor            = EXCLUDED.cpc_floor,
            search_volume_tier   = EXCLUDED.search_volume_tier,
            quick_action         = EXCLUDED.quick_action,
            rule_action          = EXCLUDED.rule_action,
            decision_step        = EXCLUDED.decision_step,
            requested_by         = EXCLUDED.requested_by,
            requested_at         = NOW()
        WHERE voylla.zepto_ondemand_actions.user_implemented IS NULL
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
    print(f"[zepto-ondemand] wrote {len(fresh_rows)}/{len(clean_rows)} suggestion row(s) for campaign {clean_rows[0]['campaign_id']}"
          + (f", {skipped} already-decided keyword(s) left untouched" if skipped else "")
          + (f" - auto-accepted {accepted} (autonomy mode 'auto')" if accepted else ""))
    return clean_rows


def _auto_accept_if_enabled(brand, campaign_id, clean_rows):
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
        queries.accept_zepto_ondemand_action(row["unique_key"], brand, True, None, "auto-accepted - autonomy mode 'auto'")
        accepted += 1
    return accepted


# ══════════════════════════════════════════════════════════════════════════
# LLM PROMPT + MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

def generate_ondemand_suggestions(brand, campaign_id, requested_by=None):
    from db import get_engine, get_anthropic_config
    import anthropic as anthropic_sdk
    import queries as _queries

    engine = get_engine()
    campaign_id = str(campaign_id)

    data = _fetch_aggregated_campaign_data(engine, brand, campaign_id)
    if data is None:
        return {"ok": False, "count": 0, "rows": [], "error": "No recent performance data for this campaign."}

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

        gt_key = (targeting_key, str(row.get("match_type", "")))
        ground_truth[gt_key] = {
            "rule_action": row["rule_action"],
            "rule_tag": row["rule_tag"],
            "cpc_floor": row["cpc_floor"],
            "search_volume_tier": row["search_volume_tier"],
            "current_cpc": row.get("current_cpc"),
            "campaign_budget": campaign_budget,
        }

    cfg = get_anthropic_config()
    client = anthropic_sdk.Anthropic(api_key=cfg["api_key"])
    model = cfg.get("model") or "claude-haiku-4-5-20251001"

    insufficient = campaign_spend < SPEND_THRESHOLD

    if insufficient:
        prompt = f"""
        You are a performance marketing expert analyzing Zepto quick-commerce ad campaigns.

        CONTEXT:
        - This campaign has a total 7-day spend of ₹{campaign_spend:.0f}, below the ₹500 threshold.
        - All keywords are classified INSUFFICIENT_DATA. Do not recommend PAUSE or a CPC change.
        - Action must always be INSUFFICIENT_DATA.

        OUTPUT FORMAT: Return ONLY a valid JSON array, each object with EXACTLY:
        campaign_id, campaign_name, targeting, match_type, action, cpc_change, confidence,
        explanation, alternative_keywords, current_cpc, campaign_budget.

        CURRENT KEYWORDS:
        {json.dumps(clean_nan(data_for_llm))}

        Return ONLY the JSON array. No markdown fences, no preamble.
        """
    else:
        prompt = f"""
        You are a performance marketing expert analyzing Zepto quick-commerce ad campaigns.
        CPC bidding - a bid is rupees per click, not per thousand impressions.

        The `action` field is YOUR final call. `rule_action` (Python-computed from
        Zepto's own signed-off decision tree below) is your STRONG DEFAULT in BOTH
        directions - follow it unless the numbers give you a specific reason not to,
        and say so explicitly when you diverge.

        RULE ENGINE (already computed per row as rule_action / rule_tag):
        - Tier 1 (7d≥₹500, 15d≥₹1000, 30d≥₹2000 spend, all sufficient) and Tier 2
          (some insufficient): a HISTORY GATE runs first - 30d or 15d ROAS ≥3.0 is
          STRONG (locks out PAUSE and DECREASE entirely; INCREASE only if 7d>4.0 AND
          15d>3.5 AND 30d>3.0, otherwise hold). 30d ROAS in [2.0,3.0) with 15d<3.0 is
          MODERATE (locks out PAUSE; DECREASE allowed if above floor). Otherwise WEAK -
          falls through to plain 7d ROAS thresholds: <1.0 on all three windows ->
          PAUSE; [1.0,3.0) -> DECREASE 10%; [3.0,4.0] -> NO_CHANGE; >4.0 with 15d>3.5
          and 30d>3.0 -> INCREASE 10%.
        - Tier 3 (no window has enough spend to trust): Zepto's own philosophy is
          "no judgment on thin data" - NO_CHANGE unless there's been real burn
          (30d spend ≥₹500) with zero ROAS on every window, which allows one
          DECREASE 10%. PAUSE is never valid on Tier 3 data alone.

        Safety guards Python applies AFTER your response (do NOT self-censor around
        them): DECREASE_CPC is clamped so the new CPC never lands below cpc_floor
        (min_bid); if already at/below the floor -> NO_CHANGE. Any single move is
        capped at 20%. A PAUSE you invent (rule_action is not PAUSE) is blocked
        unless your own explanation cites the ROAS number backing it.

        -> Each row has "previous_summary" (copy verbatim into the explanation's
           "Previous:" section) and "previous_history" (use for decision-making,
           newest first). Do NOT repeat an action flagged LOOP.
        -> If previous_summary contains "| NOTE (type): ...", that is context a human
           filed - weigh it alongside the numbers, don't let it override a clear
           numeric signal, say so explicitly if you act on one.
        -> zombie_keyword_flag=true (CPC>₹300, near-zero traffic, low spend) and
           low_search_zero_roas=true are evidence for PAUSE/DECREASE, not automatic -
           you still need the numbers to back the call.

        CURRENT DATA (7d/15d/30d aggregated, keyword + match_type grain):
        {json.dumps(clean_nan(data_for_llm))}

        For each keyword: read rule_action + rule_tag, decide the FINAL action yourself
        (diverge only with a specific numeric reason), then write the explanation in
        4 sections joined by " || ": DATA, ANALYSIS, HISTORY, RECOMMENDATION.

        OUTPUT FORMAT: Return ONLY a valid JSON array, each object with EXACTLY these
        fields in this order: campaign_id, targeting, match_type, action, explanation,
        campaign_name, cpc_change, confidence, cpc_floor, search_volume_tier,
        alternative_keywords, current_cpc, campaign_budget, rule_action.

        confidence: decimal 0.70-0.95, mandatory, never null, never 0.0/1.0.
        cpc_change: integer percent, 10-20 typical for INCREASE/DECREASE, 0 for
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
