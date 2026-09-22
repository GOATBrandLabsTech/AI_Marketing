"""
On-demand suggestion engine - a synchronous, single-campaign replica of the
scheduled pipeline (Blinkit_actions_llm_marketing_Batch_Api.ipynb). The rule
engine, prompt text, and row-cleaning safety guards below are reused
verbatim from that notebook so the two systems make the same calls on the
same data; the only differences are the DB target
(voylla.blinkit_ondemand_actions, never Blinkit_actions_llm) and using the
synchronous Messages API instead of the Batch API for a single campaign.
"""
import json
import math
import re
import time
from datetime import datetime

import pandas as pd
from sqlalchemy import text

# ======= verbatim from Blinkit_actions_llm_marketing_Batch_Api.ipynb, helper cell =======
# ── Helper functions (unchanged from original) ─────────────────────────────────

def clean_nan(obj):
    """Recursively replace NaN/Inf floats with None so json.dumps produces valid JSON."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_nan(v) for v in obj]
    return obj

def extract_json(response: str) -> list:
    response = re.sub(r'```(?:json)?', '', response).strip()

    json_start = response.find('[')
    if json_start < 0:
        raise ValueError("No JSON array found — response likely cut off before JSON started")
    response = response[json_start:]

    json_end = response.rfind(']') + 1
    if json_end == 0:
        raise ValueError(f"No closing bracket — truncated. Last 100: {response[-100:]}")
    response = response[:json_end]

    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    response_fixed = re.sub(r',\s*([}\]])', r'\1', response)
    try:
        return json.loads(response_fixed)
    except json.JSONDecodeError:
        pass

    objects = []
    depth = 0
    current = []
    in_string = False
    escape_next = False

    for char in response_fixed:
        if escape_next:
            current.append(char)
            escape_next = False
            continue
        if char == '\\' and in_string:
            escape_next = True
            current.append(char)
            continue
        if char == '"' and not escape_next:
            in_string = not in_string
        if not in_string:
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
        current.append(char)
        if depth == 0 and current and not in_string:
            candidate = ''.join(current).strip().strip(',').strip()
            if candidate.startswith('{'):
                try:
                    objects.append(json.loads(candidate))
                except json.JSONDecodeError:
                    cleaned = re.sub(r'[\x00-\x1f\x7f]', ' ', candidate)
                    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
                    try:
                        objects.append(json.loads(cleaned))
                    except json.JSONDecodeError:
                        print(f"⚠️ Skipping unparseable object: {candidate[:100]}")
            current = []

    if objects:
        print(f"⚠️ Recovered {len(objects)} objects via character-level parsing")
        return objects

    raise ValueError(f"Could not parse JSON after all attempts. Last 100 chars: {response[-100:]}")


def aggregate_window(df, days):
    agg = df.groupby(['Campaign ID', 'Targeting Value']).agg({
        'spend':               'sum',
        'total_sales':         'sum',
        'impressions':         'sum',
        'total_atc':           'sum',
        'total_units':         'sum',
        'most_viewed_position':'median',
        'Campaign Name':       'last'
    }).reset_index()

    agg[f'roas_{days}d'] = (agg['total_sales'] / agg['spend']).replace(
        [float('inf'), -float('inf')], 0
    ).fillna(0).round(2)

    agg[f'ctr_{days}d'] = (agg['total_atc'] / agg['impressions']).fillna(0).round(4)

    agg = agg.rename(columns={
        'Campaign ID':         'campaign_id',
        'Targeting Value':     'targeting',
        'most_viewed_position':'position',
        'spend':               f'spend_{days}d',
        'total_sales':         f'sales_{days}d'
    })
    return agg


def resolve_impl(user_impl):
    if user_impl is None:
        return "Unknown"
    if isinstance(user_impl, str):
        val = user_impl.strip().lower()
        if val == 'true':
            return "Implemented"
        elif val == 'false':
            return "Not implemented"
        else:
            return "Unknown"
    try:
        if pd.isna(user_impl):
            return "Unknown"
    except (TypeError, ValueError):
        pass
    return "Implemented" if bool(user_impl) else "Not implemented"


def detect_oscillation(history_list):
    if len(history_list) < 3:
        return False, ""
    change_actions = {"INCREASE_CPM", "DECREASE_CPM"}
    change_seq = [h.get("action", "") for h in history_list if h.get("action", "") in change_actions]
    if len(change_seq) >= 3:
        alternating = all(change_seq[i] != change_seq[i + 1] for i in range(len(change_seq) - 1))
        if alternating:
            return True, " -> ".join(change_seq[:4])
    return False, ""


def get_cooldown_flag(history_list):
    if not history_list:
        return ""
    last        = history_list[0]
    last_action = last.get("action", "")
    last_impl   = last.get("implemented", "Unknown")
    if last_impl != "Implemented":
        return ""
    if last_action == "INCREASE_CPM":
        return "COOLDOWN: Last implemented action was INCREASE_CPM. Do NOT recommend DECREASE_CPM this cycle unless 7d ROAS < 1.0."
    if last_action == "DECREASE_CPM":
        return "COOLDOWN: Last implemented action was DECREASE_CPM. Do NOT recommend INCREASE_CPM this cycle unless 7d ROAS > 4.5."
    return ""


def build_previous_context(history_df, campaign_id_str, targeting_key):
    filtered = history_df[
        (history_df["campaign_id"].astype(str) == campaign_id_str) &
        (history_df["targeting"].astype(str)
             .str.strip().str.lower().str.replace(" ", "_")
         == targeting_key.strip().lower().replace(" ", "_"))
    ].sort_values("action_date", ascending=False)

    if filtered.empty:
        return {
            "previous_summary": "No previous recommendation - fresh cycle.",
            "previous_history": []
        }

    rec      = filtered.iloc[0].to_dict()
    action   = rec.get("action", "UNKNOWN")
    date     = str(rec.get("action_date", ""))
    impl_str = resolve_impl(rec.get("user_implemented"))
    override = rec.get("override_note") or ""
    note_str = f" | Note: {override}" if override else ""

    summary = f"[{date}] {action} | {impl_str}{note_str}"

    history_rows = [
        {
            "date":          str(r.get("action_date", "")),
            "action":        r.get("action", "UNKNOWN"),
            "implemented":   resolve_impl(r.get("user_implemented")),
            "override_note": r.get("override_note") or "",
            # v3 FIX: the escalation ladder / recheck markers live inside the stored
            # explanation. Carry ONLY the markers (not the whole 5-section text) so
            # resolve_cross_run_state() can read them back without bloating the prompt.
            "state_markers": " ".join(
                re.findall(r"\[(?:ESC|RECHECK):[^\]]*\]", str(r.get("explanation") or ""))
            ),
        }
        for r in filtered.head(5).to_dict(orient="records")
    ]

    is_loop, loop_desc = detect_oscillation(history_rows)
    if is_loop:
        summary += f" | WARNING LOOP DETECTED ({loop_desc}) - FINAL_ACTION MUST BE NO_CHANGE this cycle. No exceptions."

    cooldown = get_cooldown_flag(history_rows)
    if cooldown and not is_loop:
        summary += f" | WARNING {cooldown}"

    return {
        "previous_summary": summary,
        "previous_history": history_rows
    }


def _safe_int(val, default=0):
    try:
        if val is None:
            return default
        f = float(val)
        return default if math.isnan(f) else int(f)
    except (TypeError, ValueError, OverflowError):
        return default
def compute_quick_action(row_dict):
    """Single priority label for the marketing team."""
    action      = str(row_dict.get("action", "") or "").upper().strip()
    confidence  = _f(row_dict.get("confidence"), 0.75)
    current_cpm = _f(row_dict.get("current_cpm"))
    min_bid     = (_f(row_dict.get("cpm_floor"))
                   or _f(row_dict.get("min_bid"))
                   or _f(row_dict.get("exact_min")))
    explanation = str(row_dict.get("explanation", "") or "")

    if min_bid > 0 and current_cpm > 0 and current_cpm < min_bid:
        return "UPDATE_CPM"
    if action == "PAUSE":
        return "PAUSE_NOW"
    if action == "ZOMBIE_FLAG":
        return "REVIEW"
    if action in ("INSUFFICIENT DATA", "INSUFFICIENT_DATA"):
        return "LOW_PRIORITY"
    if action in ("INCREASE_CPM", "DECREASE_CPM"):
        return "HIGH_PRIORITY" if confidence >= 0.85 else "PRIORITY"
    if action == "NO_CHANGE":
        return "MONITOR" if ("DECLIN" in explanation.upper()) else "STABLE"
    return "MONITOR"
# ══════════════════════════════════════════════════════════════════════════════
# KEYWORD EXPLANATION FORMATTER  (Python-built, consistent, step-by-step)
# ══════════════════════════════════════════════════════════════════════════════
# The LLM used to hand-build the explanation with mixed "\n" and " || " separators,
# which rendered inconsistently. Now the LLM returns its reasoning as plain text
# fields and Python assembles ONE clean, numbered, step-by-step layout.
#
# We keep the " || " section delimiter (PowerApps splits on it) but make every
# section internally consistent and always in the same order.
# ══════════════════════════════════════════════════════════════════════════════

SECTION_SEP = "  ||  "   # PowerApps Split() token — consistent everywhere


def _clean_txt(s):
    """Normalise whitespace so stray newlines from the LLM don't break sections."""
    if not s:
        return ""
    s = str(s).replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s{2,}", " ", s).strip()




def _extract_llm_reasoning(raw_explanation):
    """Pull the LLM's free-text reasoning out of whatever it returned."""
    if not raw_explanation:
        return ""
    s = str(raw_explanation)
    # Prefer the 'LLM →' line if present
    m = re.search(r"LLM\s*[→:>-]+\s*[A-Z_]+\s*:?\s*(.+?)(?:\|\||\n\n|$)", s, re.S)
    if m:
        return _clean_txt(m.group(1))
    # else the RECOMMENDATION justification
    m = re.search(r"✅[^\n]*\n(.+?)(?:\|\||\n\n|Options:|$)", s, re.S)
    if m:
        return _clean_txt(m.group(1))
    # fallback: first 300 chars cleaned
    return _clean_txt(s)[:300]


def _extract_history(raw_explanation):
    """Pull the HISTORY section if the LLM included one."""
    if not raw_explanation:
        return ""
    m = re.search(r"HISTORY\s*(.+?)(?:\|\||✅|$)", str(raw_explanation), re.S)
    return _clean_txt(m.group(1)) if m else ""


def build_keyword_explanation(row, llm_reasoning, rule_action, rule_tag,
                              decision_step, final_action, confidence,
                              diverged, previous_summary=""):
    """
    Assemble the final explanation as clean, ordered sections joined by SECTION_SEP.
    Sections: DECISION PATH · DATA · ANALYSIS · HISTORY · RECOMMENDATION.
    """
    stier = row.get("search_volume_tier", "UNKNOWN")
    tier  = row.get("tier", "?")

    sp7  = _f(row.get("spend_7d"));  r7  = _f(row.get("roas_7d"))
    sp15 = _f(row.get("spend_15d")); r15 = _f(row.get("roas_15d"))
    sp30 = _f(row.get("spend_30d")); r30 = _f(row.get("roas_30d"))
    cpm  = _f(row.get("current_cpm")); floor = _f(row.get("cpm_floor")) or 200.0
    pos  = row.get("position") or row.get("most_viewed_position") or "?"
    pct  = _f(row.get("keyword_budget_pct_7d"))
    cbud = _f(row.get("campaign_budget"))
    cspend7 = _f(row.get("campaign_budget_7d"))   # campaign 7d spend
    _sv_raw = row.get("keyword_searches")
    try:   # NaN is truthy, so `or "?"` never fired -> explanations printed "(nan)"
        searches = "?" if (_sv_raw is None or (isinstance(_sv_raw, float) and math.isnan(_sv_raw))) \
                   else f"{int(float(_sv_raw)):,}"
    except (TypeError, ValueError):
        searches = "?"
    _pct_str = f"{pct:.1f}" if 0 < pct < 10 else f"{pct:.0f}"

    # ── STEP 1 — DECISION PATH (what rule fired, what LLM chose) ───────────────
    verdict = "DIVERGED" if diverged else "ALIGNED"
    s1 = (
        f"🧭 STEP 1 · DECISION PATH\n"
        f"Rule Decision : {rule_action or '—'}\n"
        f"Which step     : {decision_step or '—'}\n"
        f"LLM Action     : {final_action}  [{verdict}]"
    )

    # ── STEP 2 — DATA (the numbers the decision is based on) ───────────────────
    share_flag = "🔴 HIGH SHARE" if (pct >= 50) else "✅ normal share"
    s2 = (
        f"📊 STEP 2 · DATA\n"
        f"Search tier: {stier} ({searches})  |  Data tier: {tier}  |  Position: {pos}\n"
        f"CPM: ₹{cpm:,.0f}  (floor ₹{floor:,.0f})\n"
        f"7D : spend ₹{sp7:,.0f} ({_pct_str}% of campaign\u2019s ₹{cspend7:,.0f}) · ROAS {r7:.2f}\n"
        f"15D: spend ₹{sp15:,.0f} · ROAS {r15:.2f}\n"
        f"30D: spend ₹{sp30:,.0f} · ROAS {r30:.2f}\n"
        f"Campaign daily budget: ₹{cbud:,.0f}  ·  {share_flag}"
    )

    # ── STEP 3 — ANALYSIS (rule reasoning + LLM free-will reasoning) ───────────
    s3 = (
        f"🤖 STEP 3 · ANALYSIS\n"
        f"Rule  → {rule_action or '—'}: {_clean_txt(rule_tag)}\n"
        f"LLM   → {final_action}: {_clean_txt(llm_reasoning)}"
    )

    # ── STEP 4 — HISTORY ───────────────────────────────────────────────────────
    s4 = f"📋 STEP 4 · HISTORY\n{_clean_txt(previous_summary) or 'No previous recommendation — fresh cycle.'}"

    # ── STEP 5 — RECOMMENDATION ────────────────────────────────────────────────
    if diverged:
        rec_line = (f"⚠️ LLM OVERRODE the rule ({rule_action} → {final_action}). "
                    f"Reason must be in the analysis above.")
    else:
        rec_line = f"LLM action matches the rule."
    # Applied CPM move — makes the floor clamp visible instead of silently
    # showing a smaller-than-10% bid_change with no explanation.
    _step = _f(row.get("cpm_change"))
    if final_action == "INCREASE_CPM" and _step > 0:
        _new = cpm + _step
        _move = f"CPM: ₹{cpm:,.0f} → ₹{_new:,.0f}  (+₹{_step:,.0f})"
    elif final_action == "DECREASE_CPM" and _step > 0:
        _new = cpm - _step
        _move = f"CPM: ₹{cpm:,.0f} → ₹{_new:,.0f}  (−₹{_step:,.0f})"
        if abs(_new - floor) < 1e-6 and abs(_step - round(cpm * 0.10)) >= 1:
            _move += f"  [clamped: a full 10% cut would breach the ₹{floor:,.0f} floor]"
    else:
        _move = f"CPM unchanged at ₹{cpm:,.0f}"

    s5 = (
        f"✅ STEP 5 · RECOMMENDATION\n"
        f"FINAL: {final_action}  (confidence {confidence:.2f})\n"
        f"{_move}\n"
        f"{rec_line}"
    )

    return SECTION_SEP.join([s1, s2, s3, s4, s5])


# ======= verbatim from Blinkit_actions_llm_marketing_Batch_Api.ipynb, rule-engine + prompt cell =======
# ── System prompt (unchanged from original) ────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# BLINKIT v3 DECISION ENGINE  (deterministic — matches the signed-off flowchart)
# ══════════════════════════════════════════════════════════════════════════════
# Python computes the RULE DECISION (rule_action) — the signed-off flowchart
# outcome. The LLM then sets the FINAL action with free will, using rule_action as
# its strong default; any divergence is flagged (quick_action = REVIEW) and a small
# set of Python safety guards run afterwards (see save_llm_action).
#
# Flow:
#   Point 1  → per-search-tier sufficiency across 4 windows (1D/7D/15D/30D)
#              → Tier 1 (all sufficient) / Tier 2 (some) / Tier 3 (all insufficient)
#   Tier 3   → Point 2 (poor ROI) + Point 3 (good ROI) matrices → decide & EXIT
#   Tier 1/2 → Point 5 (position 1–3) + Point 6 (position >3), 7-day data
#   Point 4  → budget-share advisory (separate-campaign suggestion)
#   Point 6c → campaign-level ROI advisory
#   Final    → CPM floor / base-level clamp (absolute override)
# ══════════════════════════════════════════════════════════════════════════════


def _f(val, default=0.0):
    """Safe float."""
    try:
        if val is None:
            return default
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


# ── SEARCH-VOLUME TIER (v3 definitions) ───────────────────────────────────────
def blinkit_search_tier(searches):
    """Main >10k | Moderate 4k–10k | Low 100–3999 | Dead <100 (non-overlapping)."""
    s = _f(searches, 0)
    if s <= 0:
        return "UNKNOWN"
    if s < 100:
        return "DEAD"
    if s < 4000:
        return "LOW"
    if s <= 10000:
        return "MODERATE"
    return "MAIN"


# ── POINT 1 — per-tier spend sufficiency thresholds (1D / 7D / 15D / 30D) ──────
# Confirmed #2: 1D is an additional required window — Tier 1 needs ALL FOUR.
SUFFICIENCY = {
    # tier      : (1d,   7d,    15d,   30d)
    "MAIN":       (500,  3000,  6000,  12000),
    "MODERATE":   (200,  1500,  3000,  6000),
    "LOW":        (100,  700,   1400,  2800),
    "DEAD":       (100,  700,   1400,  2800),
    "UNKNOWN":    (100,  700,   1400,  2800),   # treat unknown like Low/Dead
}


def compute_blinkit_tier(row):
    """
    Sets is_*_sufficient (incl. 1D) and tier (1/2/3) per the search-tier thresholds.
    Tier 1 = all 4 windows sufficient · Tier 3 = all 4 insufficient · else Tier 2.
    """
    stier = blinkit_search_tier(row.get("keyword_searches"))
    row["search_volume_tier"] = stier
    t1, t7, t15, t30 = SUFFICIENCY.get(stier, SUFFICIENCY["UNKNOWN"])

    sp1  = _f(row.get("spend_1d"))
    sp7  = _f(row.get("spend_7d"))
    sp15 = _f(row.get("spend_15d"))
    sp30 = _f(row.get("spend_30d"))

    row["is_1d_sufficient"]  = sp1  >= t1
    row["is_7d_sufficient"]  = sp7  >= t7
    row["is_15d_sufficient"] = sp15 >= t15
    row["is_30d_sufficient"] = sp30 >= t30

    flags = [row["is_1d_sufficient"], row["is_7d_sufficient"],
             row["is_15d_sufficient"], row["is_30d_sufficient"]]

    if all(flags):
        row["tier"] = 1
    elif not any(flags):
        row["tier"] = 3
    else:
        row["tier"] = 2
    return row["tier"]


# ── POINT 2 & 3 — Tier-3 Position × ROI matrices ──────────────────────────────
# Confirmed #3: Point 3 uses >=5 / <5 (position 5 included), matching Point 2.
# Confirmed #4: Main poor-ROI cutoff <=3; all other tiers <=2. Good-ROI is the
#               complement (Main >3 for the <5 NO_CHANGE row; else >2).
def blinkit_tier3_decision(row):
    """
    Deterministic action for a Tier-3 (all-insufficient) keyword.
    Returns (action, cpm_change_pct, reason_tag).
    Uses 7-day ROI/position as the working window (only data we have).
    """
    stier = row.get("search_volume_tier", "UNKNOWN")
    pos   = _f(row.get("position"), 999)
    roi   = _f(row.get("roas_7d"))

    # Poor-ROI cutoff differs for Main
    poor_cut = 3.0 if stier == "MAIN" else 2.0

    # ── INCREASE arm: position >= 5 with poor ROI (Point 2) ───────────────────
    if pos >= 5 and roi <= poor_cut:
        return ("INCREASE_CPM", 12, f"STEP 2 · Tier-3 Poor-ROI Matrix · {stier} · pos>=5 · ROI<={poor_cut:.0f} -> INCREASE 10-15%")

    # ── Poor-ROI, position < 5 (Point 2) ──────────────────────────────────────
    if pos < 5 and roi <= poor_cut:
        if stier == "MAIN":
            return ("ZOMBIE_FLAG", 0, "STEP 2 · Tier-3 Poor-ROI Matrix · Main · pos<5 · ROI<=3 -> ZOMBIE")
        if stier == "MODERATE":
            return ("PAUSE", 0, "STEP 2 · Tier-3 Poor-ROI Matrix · Moderate · pos<5 · ROI<=2 -> PAUSE")
        if stier == "LOW":
            # Confirmed #5: recheck 1 cycle then pause (handled via cross-run state)
            return ("NO_CHANGE_RECHECK", 0, "STEP 2 · Tier-3 Poor-ROI Matrix · Low · pos<5 · ROI<=2 -> NO_CHANGE (recheck 1 cycle)")
        # DEAD / UNKNOWN
        return ("PAUSE", 0, "STEP 2 · Tier-3 Poor-ROI Matrix · Dead · pos<5 · ROI<=2 -> PAUSE")

    # ── Good-ROI matrix (Point 3) ─────────────────────────────────────────────
    if pos >= 5 and roi > 2.0:
        return ("INCREASE_CPM", 12, f"STEP 3 · Tier-3 Good-ROI Matrix · {stier} · pos>=5 · ROI>2 -> INCREASE 10-15%")

    if pos < 5:
        good_cut = 3.0 if stier == "MAIN" else 2.0
        if roi > good_cut:
            return ("NO_CHANGE", 0, f"STEP 3 · Tier-3 Good-ROI Matrix · {stier} · pos<5 · ROI>{good_cut:.0f} -> NO_CHANGE")

    # Fallback — no matrix row matched (e.g. Main pos<5 with 2 < ROI <= 3)
    return ("NO_CHANGE", 0, "STEP 2/3 · Tier-3 · no matrix row matched -> NO_CHANGE")


# ── POINT 5 — Tier 1/2, position 1–3 (7-day data) ─────────────────────────────
# Confirmed #7: CPM at base + ROI<=2 -> ZOMBIE (Main/Moderate) / PAUSE (Low/Dead).
def blinkit_point5_decision(row):
    """Position 1–3 CPM rules. Returns (action, cpm_change_pct, reason_tag)."""
    stier = row.get("search_volume_tier", "UNKNOWN")
    cpm   = _f(row.get("current_cpm"))
    base  = _f(row.get("cpm_floor")) or 200.0
    roi   = _f(row.get("roas_7d"))

    at_base = cpm <= base + 1e-6   # treat "<= base" as at-base

    # "CPM at base + ROI <= 2" takes precedence (Confirmed #7)
    if at_base and roi <= 2.0:
        if stier in ("MAIN", "MODERATE"):
            return ("ZOMBIE_FLAG", 0, "STEP 5 · Position 1-3 CPM Rules · CPM at base · ROI<=2 -> ZOMBIE (Main/Moderate)")
        return ("PAUSE", 0, "STEP 5 · Position 1-3 CPM Rules · CPM at base · ROI<=2 -> PAUSE (Low/Dead)")

    if cpm > 200 and roi <= 3.0:
        return ("DECREASE_CPM", 12, "STEP 5 · Position 1-3 CPM Rules · CPM>200 · ROI<=3 -> DECREASE 10-15% (min=base)")

    if cpm > 200 and roi > 3.0:
        return ("NO_CHANGE", 0, "STEP 5 · Position 1-3 CPM Rules · CPM>200 · ROI>3 -> NO_CHANGE")

    # CPM <= 200 but not at base and ROI>2 → nothing to cut, hold
    return ("NO_CHANGE", 0, "STEP 5 · Position 1-3 CPM Rules · no cut condition -> NO_CHANGE")


# ── POINT 6 — Tier 1/2, position > 3 (7-day data) ─────────────────────────────
# Confirmed #6: pos>3 ROI>=3 -> NO_CHANGE.
#               pos>3 ROI<=3 -> INCREASE, then escalate across cycles:
#               cycle1 INCREASE -> cycle2 DECREASE -> cycle3 PAUSE (if same persists).
def blinkit_point6_decision(row):
    """Position >3 rules. Returns (action, cpm_change_pct, reason_tag)."""
    roi = _f(row.get("roas_7d"))
    if roi >= 3.0:
        return ("NO_CHANGE", 0, "STEP 6 · Position >3 Rules · ROI>=3 -> NO_CHANGE")
    # ROI <= 3 → escalation entry point; ladder rung resolved by cross-run state
    return ("INCREASE_ESCALATE", 12, "STEP 6 · Position >3 Rules · ROI<=3 -> INCREASE 10-15% (escalation ladder)")


# ── TOP-LEVEL DETERMINISTIC DECISION ──────────────────────────────────────────
def blinkit_rule_decision(row):
    """
    Full v3 decision. Sets row['rule_action'], row['rule_cpm_change_pct'],
    row['rule_tag'] and returns the tuple. Escalation/recheck actions are
    resolved to concrete actions later by resolve_cross_run_state().
    """
    tier = compute_blinkit_tier(row)

    if tier == 3:
        action, pct, tag = blinkit_tier3_decision(row)
    else:
        pos = _f(row.get("position"), 999)
        if 1 <= pos <= 3:
            action, pct, tag = blinkit_point5_decision(row)
        else:
            action, pct, tag = blinkit_point6_decision(row)

    row["rule_action"]         = action
    row["rule_cpm_change_pct"] = pct
    row["rule_tag"]            = tag
    return action, pct, tag


# ── POINT 4 — budget-share advisory ───────────────────────────────────────────
def blinkit_budget_advisory(row):
    """If keyword spends 50%+ of campaign budget → suggest a dedicated campaign."""
    pct = _f(row.get("keyword_budget_pct_7d"))
    if pct >= 50:
        return (f"Keyword consumes {pct:.0f}% of campaign spend — consider a "
                f"separate dedicated campaign for proper budget utilisation.")
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# CROSS-RUN STATE RESOLVER
# Resolves the two multi-cycle behaviours from the flowchart:
#   • Point 2 (Low, pos<5, poor ROI): NO_CHANGE_RECHECK
#       1st fire  -> NO_CHANGE  (tagged)
#       same again next cycle -> PAUSE
#   • Point 6 (pos>3, ROI<=3): INCREASE_ESCALATE ladder
#       cycle1 INCREASE -> cycle2 DECREASE -> cycle3 PAUSE
#       any improvement (i.e. condition no longer fires) resets the ladder.
#
# "Same condition" is keyed by (campaign_id, targeting, rule_tag family). We look
# at previous_history (already attached to each row) to count consecutive prior
# cycles whose stored explanation carried the same escalation marker.
# ══════════════════════════════════════════════════════════════════════════════

ESCALATION_MARKER = "[ESC:"       # written into explanation, e.g. [ESC:P6:1]
RECHECK_MARKER    = "[RECHECK:"   # e.g. [RECHECK:1]

# v3.2 DAILY-CADENCE FIX: this ladder used to advance once per pipeline RUN,
# which was safe when the pipeline only ran weekly (one run ~= one real
# review cycle). Now that the pipeline can run daily, advancing on every run
# would compress a 3-week escalation (increase -> decrease -> pause) into 3
# days on the exact same underlying data. MIN_LADDER_CYCLE_DAYS gates ladder
# ADVANCEMENT (not the tier-based rule engine itself, which already
# recomputes from real rolling 7/15/30-day windows and needs no gating) so a
# stage only moves forward once enough calendar time has passed for the
# 7-day ROAS window to actually reflect the last change. Running daily still
# means the AI is asked every day - it just holds at the current stage
# instead of ratcheting forward until there's been time to tell.
MIN_LADDER_CYCLE_DAYS = 6


def _days_since_last_marker(history_rows, marker):
    """Calendar days since the most recent history row carrying `marker`,
    scanning the same newest-first/consecutive window _count_prior_marker
    uses. None if the marker isn't present or its date can't be parsed (in
    which case callers treat it as "gate satisfied" rather than silently
    blocking the ladder forever)."""
    for h in history_rows:
        note = (str(h.get("state_markers") or "") + " "
                + str(h.get("override_note") or "") + " "
                + str(h.get("explanation") or ""))
        if marker in note:
            try:
                d = datetime.strptime(str(h.get("date", "")).strip(), "%Y-%m-%d").date()
                return (datetime.now().date() - d).days
            except (ValueError, TypeError):
                return None
        else:
            break
    return None


def _count_prior_marker(history_rows, marker):
    """
    Return the highest consecutive stage number recorded in prior explanations
    for the given marker family, scanning newest-first and stopping at the first
    cycle that does NOT carry the marker (i.e. the ladder was reset).
    """
    stage = 0
    for h in history_rows:
        # v3 FIX: build_previous_context() extracts the [ESC:*] / [RECHECK:*] markers
        # from the stored explanation into "state_markers". Reading "explanation" here
        # always missed (history rows never carried it), so the ladder never advanced.
        note = (str(h.get("state_markers") or "") + " "
                + str(h.get("override_note") or "") + " "
                + str(h.get("explanation") or ""))
        if marker in note:
            # extract trailing integer inside marker, e.g. [ESC:P6:2]
            try:
                seg = note.split(marker, 1)[1]
                num = seg.split("]", 1)[0].strip().split(":")[-1]
                stage = max(stage, int(num))
            except (ValueError, IndexError):
                stage = max(stage, 1)
        else:
            break   # consecutive run broken → ladder reset
    return stage


def resolve_cross_run_state(row):
    """
    Convert NO_CHANGE_RECHECK / INCREASE_ESCALATE placeholder actions into a
    concrete action using prior-cycle history. Also stamps the escalation marker
    into row['_state_marker'] so it can be appended to the explanation on save.
    """
    action = row.get("rule_action")
    history = row.get("previous_history", []) or []

    # ── Point 2 Low recheck ───────────────────────────────────────────────────
    if action == "NO_CHANGE_RECHECK":
        prior = _count_prior_marker(history, RECHECK_MARKER)
        days_since = _days_since_last_marker(history, RECHECK_MARKER)
        ready = days_since is None or days_since >= MIN_LADDER_CYCLE_DAYS
        if prior >= 1 and ready:
            row["rule_action"] = "PAUSE"
            row["rule_cpm_change_pct"] = 0
            row["_state_marker"] = ""   # ladder consumed
            row["rule_tag"] += " | STEP 2 recheck: 2nd cycle same -> PAUSE"
        elif prior >= 1:
            # stage 1 already stamped, but not enough calendar days have
            # passed since then to trust the 7-day window yet - hold, don't
            # advance to PAUSE just because the pipeline happened to run again.
            row["rule_action"] = "NO_CHANGE"
            row["rule_cpm_change_pct"] = 0
            row["_state_marker"] = f"{RECHECK_MARKER}1]"
            row["rule_tag"] += f" | STEP 2 recheck: holding ({days_since}d < {MIN_LADDER_CYCLE_DAYS}d, waiting for more signal)"
        else:
            row["rule_action"] = "NO_CHANGE"
            row["rule_cpm_change_pct"] = 0
            row["_state_marker"] = f"{RECHECK_MARKER}1]"
        return row["rule_action"]

    # ── Point 6 escalation ladder ─────────────────────────────────────────────
    if action == "INCREASE_ESCALATE":
        prior = _count_prior_marker(history, ESCALATION_MARKER)   # 0,1,2,...
        days_since = _days_since_last_marker(history, ESCALATION_MARKER)
        ready = days_since is None or days_since >= MIN_LADDER_CYCLE_DAYS
        stage = (prior + 1) if (prior == 0 or ready) else prior
        if not ready and prior >= 1:
            row["rule_tag"] += f" | STEP 6 ladder: holding stage {prior} ({days_since}d < {MIN_LADDER_CYCLE_DAYS}d, waiting for more signal)"
        if stage == 1:
            row["rule_action"] = "INCREASE_CPM"
            row["rule_cpm_change_pct"] = 12
            row["_state_marker"] = f"{ESCALATION_MARKER}P6:1]"
            row["rule_tag"] += " | STEP 6 ladder cycle 1 -> INCREASE"
        elif stage == 2:
            row["rule_action"] = "DECREASE_CPM"
            row["rule_cpm_change_pct"] = 12
            row["_state_marker"] = f"{ESCALATION_MARKER}P6:2]"
            row["rule_tag"] += " | STEP 6 ladder cycle 2 (same) -> DECREASE"
        else:  # stage >= 3
            row["rule_action"] = "PAUSE"
            row["rule_cpm_change_pct"] = 0
            row["_state_marker"] = f"{ESCALATION_MARKER}P6:3]"
            row["rule_tag"] += " | STEP 6 ladder cycle 3 (same) -> PAUSE"
        return row["rule_action"]

    row.setdefault("_state_marker", "")
    return action


SYSTEM_PROMPT = """
   You are a senior performance marketing analyst specializing in quick commerce
    advertising on Blinkit. You have managed ₹1Cr+ in CPM keyword campaigns and
    understand the nuances of bid optimization, position dynamics, and ROAS
    protection in high-velocity commerce environments.

    Your decisions are data-driven, balanced (you cut and scale with equal readiness), and fully traceable.
    Every action must be justified by exact numbers from the data provided.

    ═══════════════════════════════════════════════════════════════
    CAMPAIGN CONTEXT
    ═══════════════════════════════════════════════════════════════
    Platform         : Blinkit (quick commerce)
    Ad type          : CPM keyword targeting
    Weekly budget    : ₹500 per keyword
    Data windows     : 7-day (most recent), 15-day (trend), 30-day (oldest baseline)
    Primary goal     : Maximize ROAS — scale winners and cut losers with equal readiness
    Run type         : RECURRING — prior recommendations evaluated every cycle

    ═══════════════════════════════════════════════════════════════
    ⚠️ CRITICAL: HOW TO READ THE DATA WINDOWS
    ═══════════════════════════════════════════════════════════════
    30-day = OLDEST (historical baseline)
    15-day = MID-TERM
    7-day  = MOST RECENT (current performance)

    IMPROVING keyword: roas_30d < roas_15d < roas_7d  (getting better over time)
    DECLINING keyword: roas_30d > roas_15d > roas_7d  (getting worse over time)

    Example: roas_7d=20.53, roas_15d=8.72, roas_30d=4.66
    → This is STRONGLY IMPROVING. The keyword was at 4.66 historically,
      improved to 8.72 mid-term, and is now at 20.53. DO NOT read this as declining.

    NEVER assess trend direction by reading left-to-right in the display.
    Always compare: is roas_7d higher or lower than roas_30d?
    Higher = improving. Lower = declining.

    ═══════════════════════════════════════════════════════════════
    CORE PHILOSOPHY  (balanced — the Rule Decision is your default)
    ═══════════════════════════════════════════════════════════════
    The Python RULE DECISION (rule_action) is the signed-off v3 policy. Treat it as
    your STRONG DEFAULT in BOTH directions — upward (INCREASE) and downward
    (DECREASE / PAUSE / ZOMBIE_FLAG) carry equal weight. The rules were built by the
    marketing team specifically to cut wasted spend, so downward actions are
    first-class outcomes, NOT last resorts.

    1. Default to the Rule Decision. Only override when the scenario gives a
       specific, data-backed reason — and overriding a cut REQUIRES the same burden
       of proof as overriding an increase. Do not reflexively soften cuts to NO_CHANGE.

    2. Downward actions are normal and expected:
       - DECREASE_CPM when the rule says CPM is high for the ROI delivered.
       - PAUSE when the rule says a low/dead-tier keyword with poor ROI is bleeding.
       - ZOMBIE_FLAG when the rule flags a high-CPM keyword with near-zero traffic
         for human review. Passing it back as NO_CHANGE hides the problem — don't.

    3. Historical ROAS is context, not a shield. A strong 30-day ROAS can justify
       holding through a weak week — but it does NOT automatically cancel a rule cut.
       Say WHY the history outweighs the current signal, with numbers, or accept the cut.

    4. Position matters, but is not sacred. A high position funded by an unprofitable
       CPM is not worth protecting. If the rule cuts a poorly-converting position,
       that is usually correct.

    5. Insufficient data (Tier 3) still gets a rule decision from the Points 2/3
       matrices — respect it. "Not enough data" is not an automatic NO_CHANGE.

    When you DO override the rule, you must state it explicitly:
      "Rule Decision: PAUSE | LLM Action: NO_CHANGE — overriding because [specific
       numeric reason]." An override without a concrete reason is not allowed.

    Decision priority (highest to lowest):
      1. The Rule Decision (rule_action) — your default in both directions
      2. Data sufficiency tier + search-volume tier
      3. 30-day ROAS as supporting context (not an automatic veto on cuts)
      4. 15-day / 7-day ROAS trend
      5. Previous recommendation outcome


    ═══════════════════════════════════════════════════════════════
    STEP 1-3 — v3 RULE DECISION (Python-computed, injected per row)
    ═══════════════════════════════════════════════════════════════
    Each keyword row already contains a Python-computed field:

        rule_action   : the RULE DECISION per the signed-off v3 flowchart
        rule_tag      : short trace of which matrix row produced it
        tier          : 1 (all 4 windows sufficient) / 2 (some) / 3 (all insufficient)
        search_volume_tier : MAIN (>10k) / MODERATE (4k-10k) / LOW (100-3999) / DEAD (<100)

    Treat rule_action as the DETERMINISTIC RULE DECISION. It already encodes:

    ── POINT 1 — sufficiency (per search tier, 4 windows incl. 1D) ─────────────
       Tier 1 = 1D+7D+15D+30D all sufficient; Tier 3 = all insufficient; else Tier 2.

    ── TIER 3 PATH (insufficient windows) — Points 2 & 3 ───────────────────────
       Poor ROI (Main ROI<=3, others ROI<=2):
         pos>=5 -> INCREASE 10-15%
         pos<5  -> Main:ZOMBIE | Moderate:PAUSE | Low:NO_CHANGE(recheck 1 cycle->PAUSE) | Dead:PAUSE
       Good ROI (Main ROI>3 at pos<5, else ROI>2):
         pos>=5 -> INCREASE 10-15%
         pos<5  -> NO_CHANGE
       Tier 3 decision is final for that branch.

    ── TIER 1/2 PATH (sufficient windows), 7-day data ──────────────────────────
       Point 5 (position 1-3):
         CPM>200 & ROI<=3 -> DECREASE 10-15% (never below base)
         CPM>200 & ROI>3  -> NO_CHANGE
         CPM at base & ROI<=2 -> ZOMBIE (Main/Moderate) / PAUSE (Low/Dead)
       Point 6 (position >3):
         ROI>=3 -> NO_CHANGE
         ROI<=3 -> INCREASE 10-15%, escalate across cycles if same persists
                   (cycle1 INCREASE -> cycle2 DECREASE -> cycle3 PAUSE)

    ── POINT 4 (advisory) ──────────────────────────────────────────────────────
       If keyword spends 50%+ of campaign budget -> suggest a separate campaign
       (field: budget_advisory). Advisory only; does not change the bid action.

    ═══════════════════════════════════════════════════════════════
    YOUR JOB — LLM ACTION (free will, sets the action field)
    ═══════════════════════════════════════════════════════════════
    1. Read rule_action (the RULE DECISION) as your strong reference point.
    2. Then decide the FINAL action yourself based on the actual scenario —
       trends, momentum, position value, history. You MAY agree with the rule
       or override it when the data justifies a different call.
    3. The action field = YOUR decision (LLM action), NOT necessarily rule_action.
    4. If your action DIFFERS from rule_action, you MUST say so explicitly in the
       explanation, e.g.  "Rule Decision: DECREASE_CPM | LLM Action: NO_CHANGE —
       overriding because 30d ROAS 8.4 is strong and the 7d dip is noise."
    5. If they AGREE, still print both, e.g. "Rule Decision: INCREASE_CPM |
       LLM Action: INCREASE_CPM (aligned)."

    Safety overrides enforced by Python AFTER you respond (do NOT self-censor
    around them):
       - most_viewed_position = 1 AND action=INCREASE_CPM -> forced NO_CHANGE
       - action=DECREASE_CPM: the cut is clamped so the new CPM never lands below
         cpm_floor; if current_cpm is already at/below the floor -> NO_CHANGE
       - action=PAUSE while rule_action is NOT PAUSE -> blocked unless the numbers
         back it up (a rule-mandated PAUSE is always allowed through)

    ═══════════════════════════════════════════════════════════════
    STEP 4 — SANITY GATE (v3 — replaces the old position-protection gate)
    ═══════════════════════════════════════════════════════════════
    Position is ALREADY priced into the v3 matrices (Points 2/3 split at pos ≥5 / <5,
    Point 5 covers positions 1–3, Point 6 covers position >3). Do NOT apply a second,
    separate position veto on top of them — that is exactly what collapsed every
    downward decision into NO_CHANGE in v2.

      • PAUSE is legitimate whenever rule_action = PAUSE. Position does not veto it.
      • If you choose PAUSE while rule_action is NOT PAUSE, justify it with numbers.
        Python will block an unjustified one (Tier 3, position ≤5, or a proven ROAS
        signal without a full-burn-zero-return pattern).
      • ZOMBIE_FLAG is a valid final action. Use it when rule_action = ZOMBIE_FLAG, or
        when a keyword sits at a high CPM with near-zero traffic. It parks the keyword
        for human review; it does not move the bid.
      • Position 1 + INCREASE_CPM is blocked by Python afterwards — don't work around it.
      • A DECREASE that would breach the CPM floor is clamped to the floor by Python
        (or dropped to NO_CHANGE if the CPM is already at the floor).

    ═══════════════════════════════════════════════════════════════
    STEP 5 — PREVIOUS RECOMMENDATION EVALUATION
    ═══════════════════════════════════════════════════════════════
    Read "previous_history" list from each keyword's own row for decision-making.
    Use "previous_summary" string only for writing the explanation — copy it verbatim.
    Do NOT cross-reference between keywords.
      SUCCESS (user_implemented = true, ROAS improved):
        → Confidence +0.10. Continue direction. Do not reverse without new evidence.

      FAILURE (user_implemented = true, ROAS declined):
        → Do NOT repeat same action.
        → INCREASE_CPM failed → NO_CHANGE or DECREASE_CPM.
        → DECREASE_CPM failed → NO_CHANGE.
        → Two consecutive INCREASE_CPM failures → NO_CHANGE for one full cycle.

      IGNORED (user_implemented = false):
        → Override produced better ROAS → align with user instinct, note it.
        → Override produced worse ROAS AND original was PAUSE → re-recommend PAUSE,
          confidence ≥ 0.92, flag urgency in explanation.

      UNKNOWN (user_implemented = null):
        → Treat as fresh. Apply Steps 1–4 normally.

    CRITICAL OVERRIDE RULE:
    If any later step changes the action, you MUST overwrite the action completely.
    No earlier action should remain in output, explanation, or JSON.
    Only FINAL_ACTION is allowed to appear anywhere.

    ═══════════════════════════════════════════════════════════════
    LOOP PREVENTION (highest priority after CPM floor)
    ═══════════════════════════════════════════════════════════════
    The Python layer auto-detects oscillation and injects flags into previous_summary.
    You MUST honour these flags as hard constraints, not suggestions.

    ⚠️ LOOP DETECTED in previous_summary:
      → FINAL_ACTION MUST BE NO_CHANGE. No exceptions.
      → Do not evaluate ROAS, history gate, or any other rule for this cycle.
      → The keyword needs one cycle of stabilisation before further CPM changes.
      → Write in scratchpad: LOOP_BLOCK:YES

    ⚠️ COOLDOWN in previous_summary (last implemented action was INCREASE or DECREASE):
      → Block the exact opposite action unless the ROAS condition in the warning is met.
      → Example: "COOLDOWN: Last INCREASE_CPM. Block DECREASE unless 7d ROAS < 1.0"
         — If 7d ROAS = 2.5: FINAL_ACTION must NOT be DECREASE_CPM → force NO_CHANGE.
         — If 7d ROAS = 0.7: DECREASE_CPM is allowed (extreme underperformance).
      → Write in scratchpad: COOLDOWN_BLOCK:YES if the block fires.

    SELF-CHECK (mandatory before writing JSON):
      After computing FINAL_ACTION, check previous_history for the pattern:
      If last 3 entries contain alternating INCREASE_CPM and DECREASE_CPM →
      override FINAL_ACTION to NO_CHANGE even if LOOP flag was not injected.

        ═══════════════════════════════════════════════════════════════
    CONFIDENCE SCORING
    ═══════════════════════════════════════════════════════════════
      Base range : 0.70–0.85
      +0.10      : previous recommendation succeeded
      +0.05      : all sufficient windows agree on same direction
      -0.10      : only one window sufficient
      -0.05      : previous recommendation outcome unknown
      Maximum    : 0.95. Never output 1.0.


    PAUSE confidence scoring:
      If all three windows ROAS < 0.5: confidence 0.92
      If all three windows ROAS 0.5–0.99: confidence 0.90
      Add +0.05 if position > 50 (low position = low recovery potential)
      Add +0.05 if 30d spend > ₹3000 (significant budget already burned)



    ═══════════════════════════════════════════════════════════════
    ALTERNATIVE KEYWORD RULES (PAUSE action only)
    ═══════════════════════════════════════════════════════════════
      0. SEARCH VOLUME PRIORITY: when pausing due to search_pause_flag or DEAD_SEARCH_PAUSE,
         ONLY suggest alternatives with total_searches ≥ 400 (MEDIUM or HIGH tier).
         Sort by total_searches DESC. A low-volume replacement defeats the purpose.

      1. SEMANTIC RELEVANCE (mandatory): suggest only jewellery-related terms.
         ✗ Never suggest: flower, red, black, gift, color names, generic nouns
         ✓ If pausing "jhumka" → suggest "jhumki", "oxidised jhumka", "silver jhumka",
           "traditional earrings", "jhumki earrings"
         ✓ If pausing "earrings" → suggest "ear rings", "earring", "oxidised earrings",
           "silver earrings", "gold earrings"

      2. INTENT MATCH:
         Product type paused → suggest similar product types
         Style keyword paused → suggest same product with different style
         Brand paused → suggest category alternatives

      3. NO ACTIVE OVERLAP: never suggest a keyword already running in this campaign

      4. POOL CONSTRAINT: select only from provided KEYWORD POOL.
         If no relevant jewellery alternatives exist in pool → return []

      5. Maximum 5 per paused keyword.

    ═══════════════════════════════════════════════════════════════
    MANDATORY PRE-OUTPUT SCRATCHPAD
    ═══════════════════════════════════════════════════════════════
    Before writing the JSON array, write one decision line per keyword:

      [targeting] → TIER:[1/2/3] | SEARCH:[MAIN/MODERATE/LOW/DEAD] | POS:[n] |
      RULE:[rule_action] | FINAL_ACTION:[action] | AGREE:[YES/NO] | CONFIDENCE:[value]

    Examples:
      jhumka       → TIER:1 | SEARCH:MAIN | POS:2 | RULE:DECREASE_CPM | FINAL_ACTION:DECREASE_CPM | AGREE:YES | CONFIDENCE:0.85
      silver ring  → TIER:3 | SEARCH:LOW  | POS:7 | RULE:INCREASE_CPM | FINAL_ACTION:INCREASE_CPM | AGREE:YES | CONFIDENCE:0.75
      oxidised set → TIER:1 | SEARCH:MAIN | POS:1 | RULE:ZOMBIE_FLAG  | FINAL_ACTION:NO_CHANGE    | AGREE:NO  | CONFIDENCE:0.80

    AGREE:NO requires a specific numeric justification in the explanation.
    Complete ALL scratchpad lines before writing any JSON.
    JSON action MUST exactly match FINAL_ACTION in the scratchpad.
    If they differ → scratchpad wins. Fix the JSON before returning.

    ═══════════════════════════════════════════════════════════════
    OUTPUT FORMAT
    ═══════════════════════════════════════════════════════════════
    Output: scratchpad lines first, then the JSON array.

    Confidence should be mandatory
    cpm_change: percentage of CPM change.

    Each JSON object must contain ALL of these fields in this exact order:

    {{
      "campaign_id": "",
      "targeting": "",
      "action": "INCREASE_CPM | DECREASE_CPM | PAUSE | ZOMBIE_FLAG | NO_CHANGE",
      "explanation": "",
      "campaign_name": "",
      "cpm_change": 0,
      "confidence": 0.80,
      "cpm_floor": 0,
      "search_volume_tier": "",
      "alternative_keywords": [],
      "current_cpm":current_cpm,
      "campaign_budget":campaign_budget
    }}

    CONFIDENCE FIELD RULE — NON-NEGOTIABLE:
      "confidence" must be a decimal between 0.70 and 0.95.
      Valid examples: 0.70, 0.75, 0.80, 0.85, 0.90, 0.95.
      INVALID values: 0, 0.0, null, 1, 1.0, any value below 0.70.
      If you write 0 or 0.0, the output is rejected. Minimum is 0.70.
        CONFIDENCE OUTPUT RULE (mandatory, no exceptions):
      Every JSON object MUST contain a "confidence" field with a numeric value.
      Confidence is NEVER 0.0 unless explicitly calculated to be 0.0.
      If confidence calculation is skipped for any reason → default to 0.75.
      Omitting confidence or outputting null is a critical output error.




    Field rules:
      campaign_id         : use the campaign_id value from CURRENT DATA
      campaign_name       : use the campaign_name value from CURRENT DATA
      cpm_change          : 10 for INCREASE_CPM / DECREASE_CPM. 0 for NO_CHANGE / PAUSE / ZOMBIE_FLAG.
      confidence : MANDATORY. Calculate using CONFIDENCE SCORING rules above.
             Output as decimal (e.g. 0.80, not 80). Never null. Never 0.0 as default.
             If unsure → floor is 0.70. Maximum is 0.95. Never 1.0.
             Omitting this field invalidates the entire JSON object.
    alternative_keywords: populated ONLY when action = PAUSE. Otherwise [].

      All 12 fields present per object: campaign_id, targeting, action, explanation, campaign_name,
        cpm_change, confidence, alternative_keywords, current_cpm, campaign_budget,
        cpm_floor, search_volume_tier. No nulls. No missing keys.


    ═══════════════════════════════════════════════════════════════
    CPM FLOOR ENFORCEMENT (runs after every action decision — no exceptions)
    ═══════════════════════════════════════════════════════════════
    Before writing ANY JSON object, check this for EVERY keyword:

      IF action = DECREASE_CPM:
        → Read current_cpm exactly as given in the data.
        → Is current_cpm > cpm_floor? (strictly greater than, NOT equal to)
            YES (current_cpm > cpm_floor) → DECREASE_CPM is valid. Proceed.
            NO  (current_cpm ≤ cpm_floor) → DECREASE_CPM is FORBIDDEN.
                Override action to NO_CHANGE immediately.
                Set cpm_change to 0.
                Do NOT write DECREASE_CPM anywhere in output.

      BOUNDARY: if current_cpm = cpm_floor → NO_CHANGE (floor is exclusive lower bound)
            if current_cpm > cpm_floor → DECREASE_CPM valid

      This check overrides ALL previous steps.
      If DECREASE_CPM appears in your scratchpad but CPM ≤ cpm_floor →
      fix the scratchpad FINAL_ACTION to NO_CHANGE before writing JSON.
      ═══════════════════════════════════════════════════════════════
        🚨 FINAL ACTION OVERRIDE — CPM HARD RULE (ABSOLUTE PRIORITY)
        ═══════════════════════════════════════════════════════════════

        This rule overrides ALL previous steps, gates, and decisions.

        For EVERY keyword, BEFORE writing the scratchpad:

        IF current_cpm ≤ cpm_floor (or ≤ ₹200 if cpm_floor is null):
          → FINAL_ACTION MUST BE NO_CHANGE
          → cpm_change MUST BE 0
          → DECREASE_CPM is strictly forbidden

        This is NOT a guideline. This is a hard override.

        Even if:
        - ROAS suggests decrease
        - Position is low
        - History is weak/moderate

        YOU MUST:
        → force FINAL_ACTION = NO_CHANGE

        ═══════════════════════════════════════════════════════════════

    ═══════════════════════════════════════════════════════════════
    FINAL JSON VALIDATION (run before returning — no exceptions)
    ═══════════════════════════════════════════════════════════════
    Before returning the JSON array, verify EVERY object contains ALL 12 fields:
      1. campaign_id       → string
      2. targeting         → string
      3. action            → one of: INCREASE_CPM / DECREASE_CPM / NO_CHANGE / PAUSE / ZOMBIE_FLAG.
      4. explanation       → string (4-section || delimited format)
      5. campaign_name     → string (from CURRENT DATA)
      6. cpm_change        → integer: 10 for INCREASE/DECREASE, 0 for NO_CHANGE/PAUSE/ZOMBIE_FLAG
      7. confidence        → decimal between 0.70–0.95 (from scratchpad CONFIDENCE value)
      8. alternative_keywords → list ([] unless action = PAUSE)
      9.  current_cpm
      10. campaign_budget
      11. cpm_floor        (echo from row data; null if not available)
      12. search_volume_tier (echo from row data; UNKNOWN if not available)

    If ANY field is missing from ANY object → add it before returning.
    confidence must match the CONFIDENCE value written in the scratchpad line.
    Do not return until all 12 fields are present in every object.


    POSITION 1 FINAL CHECK (absolute last check before returning):
      For every object where most_viewed_position = 1:
        IF action = INCREASE_CPM → this is wrong. Change to NO_CHANGE immediately.

    ═══════════════════════════════════════════════════════════════
    EXPLANATION FORMAT (mandatory — exact numbers only)
    ═══════════════════════════════════════════════════════════════
    Use this exact 4-section structure. Sections are separated by the literal string " || ".
    This delimiter allows PowerApps and other tools to split and display each section independently.

    ── SECTION 1: DATA ─────────────────────────────────────────────
    📊 DATA
    7D  | Spend ₹[exact] ([keyword_budget_pct_7d]% of ₹[campaign_budget_7d]) | ROAS [exact] | Pos [exact] | CPM ₹[current_cpm] (floor ₹[cpm_floor]) | Search: [search_volume_tier] ([keyword_searches]) | [Met ₹500 / Not met ₹500]
    15D | Spend ₹[exact] | ROAS [exact] | [Met ₹1000 / Not met ₹1000] | Trend: [Improving ↑ / Declining ↓ / Stable → / Insufficient]
    30D | Spend ₹[exact] | ROAS [exact] | [Met ₹2000 / Not met ₹2000] | Strength: [Strong / Moderate / Weak / Insufficient data]

    ── SECTION 2: ANALYSIS ─────────────────────────────────────────
    🤖 ANALYSIS
    Rule  → [action]: [1 sentence — exact numbers, which window triggered it]
    LLM   → [action]: [1 sentence — free expert view; flag budget concern if high_budget_low_roas=true]
    consumption & Budget: ₹[spend_7d] of ₹[campaign_budget_7d] ([keyword_budget_pct_7d]%) | Daily budget: ₹[campaign_budget] [🔴 HIGH SHARE — ROAS below target / ✅ Normal share]

    ── SECTION 3: HISTORY ──────────────────────────────────────────
    📋 HISTORY
    [copy previous_summary verbatim from this keyword's row — do not paraphrase]

    ── SECTION 4: RECOMMENDATION ───────────────────────────────────
    ✅ [ACTION] (conf [confidence])
    [1 sentence justification with exact numbers]
    Options:
    (1) NO_CHANGE     — [one-line tradeoff]
    (2) INCREASE_CPM  — [one-line tradeoff, or "N/A: reason" if not feasible]
    (3) DECREASE_CPM  — [one-line tradeoff, or "N/A: reason" if not feasible]
    (4) PAUSE         — [one-line tradeoff, or "N/A: reason" if not feasible]

    ASSEMBLY RULE: join the four sections with exactly " || " between each.
    Final string looks like:  📊 DATA\n...[data lines]... || 🤖 ANALYSIS\n...[analysis lines]... || 📋 HISTORY\n...[history]... || ✅ [ACTION]\n...[recommendation]...

    TREND READING RULE (mandatory):
      Trend vs 30-day = comparing roas_15d against roas_30d.
      Improving ↑  = roas_15d > roas_30d
      Declining ↓  = roas_15d < roas_30d
      Stable →     = within 10% of each other
      Insufficient = either window not spend-sufficient

    STRENGTH LABEL RULE:
      Only label strength for spend-sufficient windows.
      Insufficient window → always write "Insufficient data" regardless of ROAS.
      Example: 30d ROAS = 3.63 but spend ₹1937 < ₹2000 → "Insufficient data" not "Strong".

    BUDGET FLAG RULE:
      high_budget_low_roas=true → write "🔴 HIGH SHARE — ROAS below target" in Budget line
      otherwise → write "✅ Normal share"

    BANNED WORDS: approximately, around, roughly, borderline, generally, varies,
    seems, appears, likely performing, near.
    No internal step names in explanations (Step 1, Rule A, TIER 3, etc.).
    State conclusions with exact numbers only.

    ═══════════════════════════════════════════════════════════════
    ANALYSIS WORKFLOW (execute in this exact order, every keyword)
    ═══════════════════════════════════════════════════════════════
     1. Read rule_action, rule_tag, tier and search_volume_tier from the row.
        That IS the signed-off decision path — start from it, never re-derive it.
     2. LOOP CHECK: if "LOOP DETECTED" appears in previous_summary →
        FINAL_ACTION = NO_CHANGE. Stop here.
     3. COOLDOWN CHECK: honour any COOLDOWN warning in previous_summary.
     4. Read the numbers: spend_1d/7d/15d/30d, roas_7d/15d/30d, position,
        current_cpm vs cpm_floor, keyword_budget_pct_7d.
     5. Trend: compare roas_7d against roas_30d (higher = improving, lower = declining).
     6. Previous-recommendation evaluation (STEP 5 rules above).
     7. Decide FINAL_ACTION. Default = rule_action. Override only with a specific
        numeric reason — overriding a cut needs the same burden of proof as
        overriding an increase.
     8. Calculate confidence per the CONFIDENCE SCORING rules.
     9. Write the scratchpad line, then the explanation, then the JSON array.
    10. Final check: every JSON action equals its scratchpad FINAL_ACTION.

        """


# ── Batch poll helpers (retry-with-backoff) ────────────────────────────────────
# A production run died on a transient APIConnectionError (WinError 10054) while
# polling. The batch runs server-side, so retrying retrieve()/results() by
# batch_id is always safe.

# ── Pre-compute Python flags (same logic as original, extracted into a function) ──

def inject_python_flags(row):
    """
    v3: compute search tier, per-tier 4-window sufficiency, tier (1/2/3),
    CPM floor, budget-share, then the deterministic RULE DECISION (advisory).
    The rule decision is a reference the LLM sees — it does NOT force the action.
    """
    # ── Search tier + per-tier sufficiency + tier (Point 1) ───────────────────
    compute_blinkit_tier(row)   # sets search_volume_tier, is_*_sufficient, tier

    _sp7  = _f(row.get("spend_7d"))
    _sp15 = _f(row.get("spend_15d"))
    _sp30 = _f(row.get("spend_30d"))

    # ── CPM floor (base level) ────────────────────────────────────────────────
    _exact_min = _f(row.get("exact_min"))
    _cpm_val   = _f(row.get("current_cpm"))
    row["cpm_floor"]        = round(_exact_min, 2) if _exact_min > 0 else None
    row["cpm_to_min_ratio"] = round(_cpm_val / _exact_min, 2) if _exact_min > 0 else None

    # ── Zombie signal (kept as a safety flag, not a rule action) ──────────────
    _imp = _f(row.get("impressions"))
    _pos = _f(row.get("position"), 999)
    _base = _f(row.get("cpm_floor")) or 200.0
    row["zombie_keyword_flag"] = bool(
        _cpm_val >= _base and _imp < 50 and _sp7 < 100 and _sp15 < 200 and _pos <= 5
    )

    # ── Positive-ROAS + sufficient-burn safety flags ──────────────────────────
    _r7  = _f(row.get("roas_7d")); _r15 = _f(row.get("roas_15d")); _r30 = _f(row.get("roas_30d"))
    row["has_positive_roas_signal"] = bool((_r30 >= 2.0 and _sp30 > 0) or (_r15 >= 2.0 and _sp15 > 0))
    row["sufficient_burn_no_roas"]  = bool(
        _sp30 >= 500 and _r7 == 0.0 and _r15 == 0.0 and _r30 == 0.0
        and not row["has_positive_roas_signal"]
    )

    # ── Budget-share (Point 4) ────────────────────────────────────────────────
    _campaign_spend = _f(row.get("campaign_spend"))
    _sp7_b = _f(row.get("spend_7d"))
    if _campaign_spend > 0:
        row["campaign_budget_7d"]    = round(_campaign_spend, 2)
        row["keyword_budget_pct_7d"] = round((_sp7_b / _campaign_spend) * 100, 1)
        row["high_budget_low_roas"]  = (row["keyword_budget_pct_7d"] > 50 and _r7 < 3.0)
    else:
        row["campaign_budget_7d"]    = None
        row["keyword_budget_pct_7d"] = None
        row["high_budget_low_roas"]  = False
    row["budget_advisory"] = blinkit_budget_advisory(row)

    # ── RULE DECISION (v3 matrices) — advisory ground truth ───────────────────
    blinkit_rule_decision(row)       # sets rule_action / rule_cpm_change_pct / rule_tag
    resolve_cross_run_state(row)     # resolves escalation ladder + recheck to concrete rule_action

def patch_search_line(row, explanation):
    """Fix the Search: line in explanation using Python ground-truth values."""
    _correct_tier  = str(row.get("search_volume_tier") or "UNKNOWN")
    _correct_count = _safe_int(row.get("keyword_searches"), 0)
    _searches_str  = f"Search: {_correct_tier} ({_correct_count})"

    # Match both old "Searches:" / "Search volume:" and new "Search:" formats
    explanation = re.sub(
        r"(?:Searches?|Search volume):\s*\S+\s*\(\d+\)",
        _searches_str,
        explanation
    )

    if "Search:" not in explanation and "Searches:" not in explanation:
        # Fallback: insert before the | Met/Not met suffix in 7D line
        explanation_new = re.sub(
            r"(floor ₹[\d.]+\))\s*\|",
            rf"\1 | {_searches_str} |",
            explanation,
            count=1
        )
        if explanation_new != explanation:
            explanation = explanation_new
        else:
            explanation = f"{_searches_str} | " + explanation

    return explanation


def fix_explanation_sections(explanation: str) -> str:
    section_headers = ["📊 DATA", "🤖 ANALYSIS", "📋 HISTORY", "✅"]
    
    for header in section_headers:
        explanation = re.sub(
            rf"\s*\|\|\s*{re.escape(header)}",
            f"\n\n{header}",
            explanation
        )

    explanation = re.sub(r"\n{3,}", "\n\n", explanation).strip()
    
    return explanation


# ══════════════════════════════════════════════════════════════════════════════
# ON-DEMAND ORCHESTRATION  (new - not from the notebook)
# ══════════════════════════════════════════════════════════════════════════════
# Generates a fresh suggestion for ONE campaign, right now, synchronously -
# instead of waiting for the weekly/daily batch. Reuses the exact rule engine
# and prompt above, but:
#   - runs for a single campaign_id instead of looping every campaign/brand
#   - calls the synchronous Messages API instead of the Batch API
#   - reads/writes voylla.blinkit_ondemand_actions, never Blinkit_actions_llm
#   - skips the Phase 6 campaign-level (Point 4 / 6c) suggestion layer - v1
#     is per-keyword decisions only
# ══════════════════════════════════════════════════════════════════════════════

SPEND_THRESHOLD = 500  # same cutoff as the scheduled pipeline


def _build_clean_rows(action_obj, brand, tolerance_pct=20):
    """
    Same row-cleaning / safety-guard logic as the scheduled pipeline's
    save_llm_action (zombie override, floor clamp, position-1 block, PAUSE
    validity guards, quick_action, explanation builder) - but returns the
    cleaned rows instead of writing them, so the caller can pick the
    destination table.

    `tolerance_pct` is the hard authorized ceiling on any single CPM move
    (set per-campaign, default 20%) - the rule engine already targets
    ~10-12%, so this is a backstop, not the normal step size.
    """
    if not action_obj:
        return []

    if isinstance(action_obj, dict):
        action_obj = [action_obj]

    clean_rows = []

    for a in action_obj:
        if not isinstance(a, dict):
            continue

        _row_targeting = str(a.get("targeting", "unknown"))
        try:
            campaign_id   = a.get("campaign_id")
            campaign_name = a.get("campaign_name", "")
            targeting     = str(a.get("targeting", "")).strip().lower().replace(" ", "_")
            action        = str(a.get("action", "")).upper()
            explanation   = a.get("explanation", "")

            confidence = a.get("confidence")
            try:
                confidence = float(str(confidence).strip())
                if not (0.0 < confidence <= 1.0):
                    raise ValueError("out of range")
            except Exception:
                print(f"  CONFIDENCE DEFAULTED: {targeting} | missing/invalid -> 0.75 (flagged REVIEW)")
                confidence = 0.75
                a["_confidence_defaulted"] = True

            cpm_change = a.get("cpm_change")
            try:
                cpm_change = int(cpm_change)
            except Exception:
                cpm_change = None

            current_cpm = a.get("current_cpm")
            if current_cpm is None:
                current_cpm = None
            else:
                try:
                    current_cpm = float(current_cpm)
                    if math.isnan(current_cpm):
                        current_cpm = None
                except Exception:
                    current_cpm = None

            # ZOMBIE HARD OVERRIDE
            zombie_flag = a.get("zombie_keyword_flag", False)
            if zombie_flag and action == "NO_CHANGE" and \
               str(a.get("rule_action") or "").upper().strip() == "ZOMBIE_FLAG":
                print(f"  ZOMBIE RULE HONOURED: {targeting} | NO_CHANGE -> ZOMBIE_FLAG (rule decision)")
                action          = "ZOMBIE_FLAG"
                a["action"]     = "ZOMBIE_FLAG"
                a["cpm_change"] = 0
            elif zombie_flag and action == "NO_CHANGE":
                raw_cpm_z   = a.get("current_cpm")
                floor_val_z = a.get("cpm_floor")
                try:
                    check_cpm_z   = float(raw_cpm_z)
                    check_floor_z = float(floor_val_z) if floor_val_z else 200
                    if check_cpm_z > check_floor_z:
                        new_cpm_z   = round(check_cpm_z * 0.90)
                        kw_searches = _safe_int(a.get("keyword_searches"), 0)
                        print(f"  ZOMBIE OVERRIDE: {targeting} | {check_cpm_z} -> {new_cpm_z} ({kw_searches} searches)")
                        action          = "DECREASE_CPM"
                        a["action"]     = "DECREASE_CPM"
                        a["cpm_change"] = round(check_cpm_z * 0.10)
                        _expl = a.get("explanation", "")
                        _expl = re.sub(
                            r"Final Recommendation:\s*NO_CHANGE[^|]*",
                            f"Final Recommendation: DECREASE_CPM — Zombie override: CPM {check_cpm_z} -> {new_cpm_z} (10% cut)",
                            _expl
                        )
                        _expl += (
                            f" | PYTHON OVERRIDE: Zombie keyword ({kw_searches} searches,"
                            f" <50 impressions/week). Position 1 with near-zero traffic"
                            f" has nothing to protect. CPM {check_cpm_z} -> {new_cpm_z}."
                        )
                        a["explanation"] = _expl
                        explanation = _expl
                except (TypeError, ValueError):
                    pass

            # Dynamic CPM floor enforcement
            if action == "DECREASE_CPM":
                raw_cpm   = a.get("current_cpm")
                floor_val = a.get("cpm_floor")
                try:
                    check_cpm   = float(raw_cpm)
                    check_floor = float(floor_val) if floor_val else 200
                    if check_cpm <= check_floor:
                        print(f"  CPM FLOOR OVERRIDE: {targeting} | CPM {check_cpm} <= floor {check_floor} -> NO_CHANGE")
                        action          = "NO_CHANGE"
                        a["action"]     = "NO_CHANGE"
                        a["cpm_change"] = 0
                except (TypeError, ValueError):
                    pass

            campaign_budget = a.get("campaign_budget")
            try:
                campaign_budget = float(campaign_budget) if campaign_budget is not None else None
            except Exception:
                campaign_budget = None

            if current_cpm is None:
                explanation = explanation.replace("CPM ₹nan", "CPM unavailable")
                explanation = explanation.replace("CPM ₹NaN", "CPM unavailable")

            alt = a.get("alternative_keywords")
            if not isinstance(alt, list):
                alt = []

            action_date = a.get("action_date")
            if not action_date:
                action_date = datetime.now().date()
            date_str = action_date.strftime("%Y-%m-%d")

            # LOW SEARCH + ZERO ROAS OVERRIDE
            if action == "NO_CHANGE" and a.get("low_search_zero_roas", False):
                _cpm_ls   = float(a.get("current_cpm") or 0)
                _floor_ls = float(a.get("cpm_floor") or 200)
                if _cpm_ls > _floor_ls:
                    _new_cpm_ls = round(_cpm_ls * 0.90)
                    _sv_ls      = str(a.get("search_volume_tier") or "UNKNOWN")
                    _kw_s_ls    = _safe_int(a.get("keyword_searches"), 0)
                    action          = "DECREASE_CPM"
                    a["action"]     = "DECREASE_CPM"
                    a["cpm_change"] = round(_cpm_ls * 0.10)
                    _expl_ls = a.get("explanation", "")
                    _expl_ls = re.sub(
                        r"Final Recommendation:\s*NO_CHANGE[^|]*",
                        f"Final Recommendation: DECREASE_CPM - Low-search override ({_sv_ls}, {_kw_s_ls} searches, zero ROAS all windows). CPM {_cpm_ls} -> {_new_cpm_ls}.",
                        _expl_ls
                    )
                    _expl_ls += f" | PYTHON OVERRIDE: {_sv_ls} keyword ({_kw_s_ls} searches), zero ROAS across 30 days. CPM {_cpm_ls} -> {_new_cpm_ls} (10% cut)."
                    a["explanation"] = _expl_ls
                    explanation = _expl_ls

            cpm_change = _safe_int(a.get("cpm_change"), 0)

            # Position 1 + INCREASE_CPM safeguard
            try:
                _pos_check = float(a.get("most_viewed_position") or 99)
            except (TypeError, ValueError):
                _pos_check = 99
            if action == "INCREASE_CPM" and _pos_check == 1:
                action          = "NO_CHANGE"
                a["action"]     = "NO_CHANGE"
                cpm_change      = 0
                a["cpm_change"] = 0

            # Universal bid_change correction (10% of current_cpm)
            if action in ("DECREASE_CPM", "INCREASE_CPM"):
                try:
                    _cpm_val = float(current_cpm) if current_cpm is not None else None
                    if _cpm_val and _cpm_val > 0:
                        _correct_change = round(_cpm_val * 0.10)
                        if cpm_change != _correct_change:
                            cpm_change      = _correct_change
                            a["cpm_change"] = _correct_change
                except (TypeError, ValueError):
                    pass

            # Authorized-tolerance ceiling: no single move exceeds tolerance_pct
            # of current_cpm, regardless of what the rule engine/LLM proposed.
            if action in ("DECREASE_CPM", "INCREASE_CPM"):
                try:
                    _cpm_tol = float(current_cpm) if current_cpm is not None else None
                    if _cpm_tol and _cpm_tol > 0:
                        _ceiling = round(_cpm_tol * (float(tolerance_pct) / 100.0))
                        if cpm_change > _ceiling:
                            _expl_tol = a.get("explanation", "")
                            _expl_tol += (f" | TOLERANCE CLAMP: {cpm_change} would exceed the authorized "
                                          f"{tolerance_pct:.0f}% ceiling for this campaign; capped to {_ceiling}.")
                            a["explanation"] = _expl_tol
                            explanation = _expl_tol
                            cpm_change      = _ceiling
                            a["cpm_change"] = _ceiling
                except (TypeError, ValueError):
                    pass

            # Floor guard: DECREASE only if result stays at/above floor
            if action == "DECREASE_CPM":
                try:
                    _cpm_fg   = float(current_cpm) if current_cpm is not None else None
                    _floor_fg = float(a.get("cpm_floor") or 200)
                    if _cpm_fg is not None and _floor_fg and _cpm_fg > 0:
                        _projected_cpm = _cpm_fg - round(_cpm_fg * 0.10)
                        if _projected_cpm < _floor_fg:
                            _room = int(round(_cpm_fg - _floor_fg))
                            if _room >= 1:
                                cpm_change      = _room
                                a["cpm_change"] = _room
                                _expl_fg = a.get("explanation", "")
                                _expl_fg += (f" | FLOOR CLAMP: 10% would take CPM {_cpm_fg} -> {_projected_cpm}, "
                                             f"below floor {_floor_fg}; cut clamped to {_room} so CPM lands on the floor.")
                                a["explanation"] = _expl_fg
                                explanation = _expl_fg
                            else:
                                action          = "NO_CHANGE"
                                a["action"]     = "NO_CHANGE"
                                cpm_change      = 0
                                a["cpm_change"] = 0
                                _expl_fg = a.get("explanation", "")
                                _expl_fg = re.sub(
                                    r"Final Recommendation:\s*DECREASE_CPM[^|]*",
                                    f"Final Recommendation: NO_CHANGE — CPM already at floor (current {_cpm_fg}, floor {_floor_fg}). ",
                                    _expl_fg
                                )
                                _expl_fg += f" | FLOOR GUARD: CPM {_cpm_fg} is at floor {_floor_fg}; no room to cut."
                                a["explanation"] = _expl_fg
                                explanation = _expl_fg
                except (TypeError, ValueError):
                    pass

            # Sufficient-burn PAUSE override
            _sburn         = bool(a.get("sufficient_burn_no_roas", False))
            _llm_wants_pause = "LLM Insight: PAUSE" in (explanation or "")
            if _sburn and _llm_wants_pause and action != "PAUSE":
                action          = "PAUSE"
                a["action"]     = "PAUSE"
                cpm_change      = 0
                a["cpm_change"] = 0
                _expl_sb = a.get("explanation", "")
                _expl_sb = re.sub(
                    r"Final Recommendation:\s*[^|]+",
                    "Final Recommendation: PAUSE — sufficient_burn_no_roas override; burn with zero ROAS is structural failure, not position worth protecting. ",
                    _expl_sb
                )
                _expl_sb += " | PYTHON OVERRIDE: sufficient_burn_no_roas=True and LLM Insight=PAUSE; position 1 block lifted."
                a["explanation"] = _expl_sb
                explanation = _expl_sb

            # PAUSE validity guards (Python-enforced, post-LLM)
            if action == "PAUSE" and str(a.get("rule_action") or "").upper().strip() != "PAUSE":
                _tier_g  = int(a.get("tier", 3))
                _pos_g   = float(a.get("most_viewed_position") or 99)
                _proas_g = bool(a.get("has_positive_roas_signal", False))
                _sburn_g = bool(a.get("sufficient_burn_no_roas", False))

                if _tier_g == 3 and not _sburn_g:
                    action          = "NO_CHANGE"
                    a["action"]     = "NO_CHANGE"
                    cpm_change      = 0
                    a["cpm_change"] = 0
                elif _pos_g <= 5 and not _sburn_g:
                    action          = "NO_CHANGE"
                    a["action"]     = "NO_CHANGE"
                    cpm_change      = 0
                    a["cpm_change"] = 0
                elif _proas_g and not _sburn_g:
                    action          = "NO_CHANGE"
                    a["action"]     = "NO_CHANGE"
                    cpm_change      = 0
                    a["cpm_change"] = 0

            # Deterministic (no action/timestamp in the key): re-running
            # generation for this campaign later the same day must land on
            # the SAME row for a given keyword, not stack up duplicates -
            # save_ondemand_action upserts on this key.
            unique_key    = f"ondemand_{campaign_id}_{date_str}_{targeting}"
            cpm_floor_val = a.get("cpm_floor")
            search_tier   = str(a.get("search_volume_tier") or "UNKNOWN")

            _rule_action = str(a.get("rule_action") or "").upper().strip()
            _rule_tag    = str(a.get("rule_tag") or "")
            _decision_step = _rule_tag.split(" | ")[0] if _rule_tag else "—"
            _diverged    = bool(_rule_action and _rule_action != action)

            _llm_reason = _extract_llm_reasoning(explanation)
            _prev_sum   = _extract_history(explanation)
            explanation = build_keyword_explanation(
                row=a,
                llm_reasoning=_llm_reason,
                rule_action=_rule_action,
                rule_tag=_rule_tag,
                decision_step=_decision_step,
                final_action=action,
                confidence=confidence,
                diverged=_diverged,
                previous_summary=_prev_sum,
            )
            _marker = str(a.get("_state_marker") or "")
            if _marker and _marker not in (explanation or ""):
                explanation = (explanation or "") + f" {_marker}"

            _qa = compute_quick_action({
                "action": action, "confidence": confidence,
                "current_cpm": current_cpm, "cpm_floor": cpm_floor_val,
                "explanation": explanation,
            })
            if _diverged and _qa in ("STABLE", "MONITOR", "LOW_PRIORITY"):
                _qa = "REVIEW"
            if a.get("_llm_missing") or a.get("_confidence_defaulted"):
                _qa = "REVIEW"

            clean_rows.append({
                "quick_action":         _qa,
                "rule_action":          _rule_action,
                "decision_step":        _decision_step,
                "unique_key":           unique_key,
                "action_date":          action_date,
                "campaign_id":          str(campaign_id),
                "campaign_name":        campaign_name,
                "targeting":            targeting,
                "action":               action,
                "cpm_change":           cpm_change,
                "confidence":           confidence,
                "explanation":          explanation,
                "alternative_keywords": alt,
                "Brand":                brand,
                "current_cpm":          current_cpm,
                "campaign_budget":      campaign_budget,
                "cpm_floor":            cpm_floor_val,
                "search_volume_tier":   search_tier,
            })

        except Exception as _row_err:
            print(f"  WARNING: Skipped row [{_row_targeting}] due to error: {_row_err}")
            continue

    return clean_rows


def save_ondemand_action(engine, action_obj, brand, requested_by=None, tolerance_pct=20):
    """Same safety-guard pipeline as the scheduled pipeline's save_llm_action,
    writing into voylla.blinkit_ondemand_actions instead of Blinkit_actions_llm.

    unique_key is one row per (campaign_id, targeting, action_date) - running
    generation twice in a day for the same campaign (a manual "Generate now"
    click plus the daily schedule, say) upserts the SAME row with the fresher
    analysis instead of stacking duplicates. The upsert is gated by
    `WHERE user_implemented IS NULL`: once a row has been accepted, rejected,
    or auto-accepted, a later regeneration that same day is silently dropped
    for that keyword rather than overwriting a decision already made - the
    returned/auto-accept-eligible rows are only the ones actually written.
    """
    clean_rows = _build_clean_rows(action_obj, brand, tolerance_pct=tolerance_pct)
    if not clean_rows:
        return []

    for r in clean_rows:
        r["requested_by"] = requested_by

    upsert_sql = text("""
        INSERT INTO voylla.blinkit_ondemand_actions
        (unique_key, action_date, campaign_id, campaign_name,
         targeting, action, bid_change, confidence,
         explanation, alternative_keywords, "Brand", current_cpm, campaign_budget,
         cpm_floor, search_volume_tier, quick_action, rule_action, decision_step,
         requested_by)
        VALUES
        (:unique_key, :action_date, :campaign_id, :campaign_name,
         :targeting, :action, :cpm_change, :confidence,
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
        WHERE voylla.blinkit_ondemand_actions.user_implemented IS NULL
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
    print(f"[ondemand] wrote {len(fresh_rows)}/{len(clean_rows)} suggestion row(s) for campaign {clean_rows[0]['campaign_id']}"
          + (f", {skipped} already-decided keyword(s) left untouched" if skipped else "")
          + (f" - auto-accepted {accepted} (autonomy mode 'auto')" if accepted else ""))
    return clean_rows


AUTO_ACCEPT_ACTIONS = {"INCREASE_CPM", "DECREASE_CPM", "PAUSE"}
AUTO_ACCEPT_HOLD_BACK_QUICK_ACTIONS = {"REVIEW"}


def _auto_accept_if_enabled(brand, campaign_id, clean_rows):
    """When this campaign is on-demand-managed AND its autonomy mode is
    'auto', accept every actionable suggestion (INCREASE_CPM/DECREASE_CPM/
    PAUSE) as-is - no human click - so the scheduled push notebook can pick
    it up unattended. A row flagged REVIEW (LLM diverged from the rule
    decision, confidence was defaulted, or the LLM skipped the keyword) is
    held back even in auto mode - those still need a human look. ZOMBIE_FLAG
    is deliberately never auto-accepted; it exists to park a keyword for
    manual review, not to move a bid, so nothing is lost by leaving it
    pending. Returns how many rows were auto-accepted."""
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
        queries.accept_ondemand_action(
            row["unique_key"], brand, True, None,
            f"auto-accepted - autonomy mode 'auto', tolerance {float(setting['bid_tolerance_pct']):.0f}%",
        )
        accepted += 1
    return accepted


def _fetch_aggregated_campaign_data(engine, brand, campaign_id):
    """Same windowed aggregation as the scheduled pipeline's Phase 0, scoped
    to a single campaign so an on-demand click stays fast and cheap."""
    query = f"""
  WITH base AS (
        SELECT
            TO_TIMESTAMP(a."Date", 'YYYY-MM-DD HH24:MI:SS')::date AS report_date,
            a."Campaign ID",
            a."Campaign Name",
            a."Targeting Type",
            a."Targeting Value",
            a."Match Type",
            SUM(a."Impressions") AS impressions,
            SUM(a."Direct ATC" + a."Indirect ATC") AS total_atc,
            SUM(a."Direct Quantities Sold" + a."Indirect Quantities Sold") AS total_units,
            SUM(a."Direct Sales" + a."Indirect Sales") AS total_sales,
            SUM(a."Estimated Budget Consumed") AS spend,
            MAX(ks.searches) AS "searches",
            MAX(ks.weighted_score) AS "weighted_score",
            MAX(ks.exact_min) AS "min_cpm",
            MAX(cp.cpm) AS current_cpm,
            MAX(a."Pacing Type") AS pacing_type,
            MAX(a."Most Viewed Position") AS most_viewed_position

        FROM voylla."Blinkit_Ads_Report" a
        LEFT JOIN voylla."Blinkit_CPM" cp
        ON a."Campaign ID"::TEXT = cp.campaign_id::TEXT AND a."Targeting Value" = cp.keyword AND cp."Brand"= '{brand}'
        LEFT JOIN voylla."Blinkit_keyword_suggestions" ks ON a."Targeting Value" = ks.keyword AND ks.brand_name = '{brand}'
        WHERE TO_TIMESTAMP(a."Date", 'YYYY-MM-DD HH24:MI:SS')
              >= (CURRENT_DATE - INTERVAL '3 day') - INTERVAL '31 days'
          AND TO_TIMESTAMP(a."Date", 'YYYY-MM-DD HH24:MI:SS')
              <= (CURRENT_DATE - INTERVAL '3 day')
          AND a."Brand" = '{brand}'
          AND a."Campaign ID"::TEXT = '{campaign_id}'

        GROUP BY
            report_date,
            a."Campaign ID",
            a."Campaign Name",
            a."Targeting Type",
            a."Targeting Value",
            a."Match Type"
    ),

    metrics AS (
        SELECT *,
            CASE WHEN impressions > 0
                 THEN total_atc::FLOAT / impressions
            END AS ctr,

            CASE WHEN total_atc > 0
                 THEN total_units::FLOAT / total_atc
            END AS cvr,

            CASE WHEN spend > 0
                 THEN total_sales / spend
            END AS roas
        FROM base
    )

    SELECT
        *,

        AVG(roas) OVER (
            PARTITION BY "Campaign ID", "Targeting Value"
            ORDER BY report_date
            RANGE BETWEEN INTERVAL '7 day' PRECEDING
                  AND INTERVAL '1 day' PRECEDING
        ) AS roas_7d_avg,

        AVG(roas) OVER (
            PARTITION BY "Campaign ID", "Targeting Value"
            ORDER BY report_date
            RANGE BETWEEN INTERVAL '15 day' PRECEDING
                  AND INTERVAL '1 day' PRECEDING
        ) AS roas_15d_avg,

        AVG(roas) OVER (
            PARTITION BY "Campaign ID", "Targeting Value"
            ORDER BY report_date
            RANGE BETWEEN INTERVAL '30 day' PRECEDING
                  AND INTERVAL '1 day' PRECEDING
        ) AS roas_30d_avg,

        AVG(ctr) OVER (
            PARTITION BY "Campaign ID", "Targeting Value"
            ORDER BY report_date
            RANGE BETWEEN INTERVAL '7 day' PRECEDING
                  AND INTERVAL '1 day' PRECEDING
        ) AS ctr_7d_avg,

        roas -
        AVG(roas) OVER (
            PARTITION BY "Campaign ID", "Targeting Value"
            ORDER BY report_date
            RANGE BETWEEN INTERVAL '30 day' PRECEDING
                  AND INTERVAL '1 day' PRECEDING
        ) AS roas_vs_30d

    FROM metrics WHERE current_cpm IS NOT NULL;
    """
    df = pd.read_sql(query, engine)
    if df.empty:
        return None

    df["report_date"] = pd.to_datetime(df["report_date"])
    today = df["report_date"].max()

    df1  = df[df["report_date"] >= today - pd.Timedelta(days=0)]
    df7  = df[df["report_date"] >= today - pd.Timedelta(days=6)]
    df15 = df[df["report_date"] >= today - pd.Timedelta(days=14)]
    df30 = df[df["report_date"] >= today - pd.Timedelta(days=29)]

    agg1  = aggregate_window(df1,  1)
    agg7  = aggregate_window(df7,  7)
    agg15 = aggregate_window(df15, 15)
    agg30 = aggregate_window(df30, 30)

    aggregated_df = agg7.merge(
        agg1[["campaign_id", "targeting", "spend_1d", "roas_1d"]],
        on=["campaign_id", "targeting"], how="left"
    ).merge(
        agg15[["campaign_id", "targeting", "spend_15d", "roas_15d", "ctr_15d"]],
        on=["campaign_id", "targeting"], how="left"
    ).merge(
        agg30[["campaign_id", "targeting", "spend_30d", "roas_30d", "ctr_30d"]],
        on=["campaign_id", "targeting"], how="left"
    )
    aggregated_df["spend_1d"] = aggregated_df["spend_1d"].fillna(0)

    budget_df = pd.read_sql(f"""
        SELECT
            b.campaign_id::TEXT AS campaign_id,
            COALESCE(
                GREATEST(MAX(s.slot1_budget)::INT, MAX(s.slot2_budget)::INT),
                MAX(b.budget)
            ) AS campaign_budget,
            CASE WHEN (br.total_hours IS NULL OR br.total_hours >= 23)
                 THEN 24.0 ELSE br.total_hours
            END AS campaign_runtime_hours
        FROM voylla."Blinkit_CPM" b
        LEFT JOIN voylla."Blinkit_Campaign_Runtime" br
               ON b.campaign_id::TEXT = br.campaign_id::TEXT
        LEFT JOIN voylla."Blinkit_Campaign_Schedule" s
               ON br.campaign_id::TEXT = s.campaign_id::TEXT
        WHERE b."Brand" = '{brand}' AND b.campaign_id::TEXT = '{campaign_id}'
          AND br.log_date = (
              SELECT MAX(log_date) - INTERVAL '1 day'
              FROM voylla."Blinkit_Campaign_Runtime"
          )
        GROUP BY 1, 3
        """, engine).drop_duplicates(subset=["campaign_id"])
    budget_df["campaign_id"] = budget_df["campaign_id"].astype(str).str.strip()
    aggregated_df["campaign_id"] = aggregated_df["campaign_id"].astype(str).str.strip()
    aggregated_df = aggregated_df.merge(budget_df, on="campaign_id", how="left")

    active_days_df = (
        df7[df7["spend"] > 0]
        .groupby(["Campaign ID", "Targeting Value"])["report_date"]
        .nunique()
        .reset_index()
        .rename(columns={
            "Campaign ID": "campaign_id",
            "Targeting Value": "targeting",
            "report_date": "active_days_7d",
        })
    )
    active_days_df["campaign_id"] = active_days_df["campaign_id"].astype(str).str.strip()
    aggregated_df = aggregated_df.merge(active_days_df, on=["campaign_id", "targeting"], how="left")
    aggregated_df["active_days_7d"] = aggregated_df["active_days_7d"].fillna(7).clip(upper=7).astype(int)

    keyword_constants_df = (
        df.groupby(["Campaign ID", "Targeting Value"])
        .agg(
            current_cpm=("current_cpm", "max"),
            keyword_searches=("searches", "max"),
            kw_weighted_score=("weighted_score", "max"),
            exact_min=("min_cpm", "max"),
        )
        .reset_index()
        .rename(columns={"Campaign ID": "campaign_id", "Targeting Value": "targeting"})
    )
    keyword_constants_df["campaign_id"] = keyword_constants_df["campaign_id"].astype(str).str.strip()
    aggregated_df = aggregated_df.merge(keyword_constants_df, on=["campaign_id", "targeting"], how="left")

    campaign_name = df["Campaign Name"].iloc[0] if not df.empty else ""

    campaign_spend_df = pd.read_sql(f"""
        SELECT SUM("Estimated Budget Consumed") AS campaign_spend
        FROM voylla."Blinkit_Ads_Report" a
        WHERE TO_TIMESTAMP("Date",'YYYY-MM-DD HH24:MI:SS')
              > (CURRENT_DATE - INTERVAL '3 day') - INTERVAL '7 days'
          AND TO_TIMESTAMP(a."Date", 'YYYY-MM-DD HH24:MI:SS')
              <= (CURRENT_DATE - INTERVAL '3 day')
          AND a."Brand" = '{brand}' AND a."Campaign ID"::TEXT = '{campaign_id}'
    """, engine)
    campaign_spend = float(campaign_spend_df["campaign_spend"].iloc[0] or 0)

    aggregated_df["campaign_spend"] = campaign_spend
    aggregated_df["campaign_id"] = aggregated_df["campaign_id"].astype(str)

    return {
        "aggregated_df": aggregated_df,
        "campaign_name": campaign_name,
        "campaign_spend": campaign_spend,
    }


def _fetch_keyword_pool(engine):
    df = pd.read_sql("""
        SELECT
            REPLACE(ks.keyword, ' ', '_') AS suggested_value,
            MAX(ks.searches) AS total_searches,
            MAX(ks.weighted_score) AS weighted_score,
            BOOL_OR(ks.is_brand_keyword) AS is_brand
        FROM voylla."Blinkit_keyword_suggestions" ks
        GROUP BY ks.keyword
        ORDER BY weighted_score DESC;
    """, engine)
    return df.to_dict(orient="records")


def _fetch_ondemand_history(engine, brand, campaign_id):
    return pd.read_sql(f"""
        SELECT unique_key, campaign_id, campaign_name, targeting, action,
               bid_change, confidence, explanation, action_date,
               user_implemented, override_note
        FROM voylla.blinkit_ondemand_actions
        WHERE "Brand" = '{brand}' AND campaign_id = '{campaign_id}'
        ORDER BY action_date DESC;
    """, engine)


# ══════════════════════════════════════════════════════════════════════════════
# OUTCOME FEEDBACK  (the memory/learning loop - new, not from the notebook)
# ══════════════════════════════════════════════════════════════════════════════
# Before generating a fresh suggestion, look back at the last decision THIS
# system actually got accepted for this keyword, and grade it: did ROAS
# improve or not after that change went live? That verdict gets folded into
# the next LLM call's history section, so the model can see not just "what
# did we decide" but "and did it work" - reusing the exact same
# spend-weighted MET/MISSED logic as the dashboard's Before/After page
# (queries.EXPECTATION_TOLERANCE / _expectation_verdict), so a "good call"
# here means the same thing it means everywhere else in this project.

OUTCOME_GRADE_WAIT_DAYS = 7  # a bid change needs at least this long before its ROAS is trustworthy


def compute_outcome_feedback(engine, brand, campaign_id, targeting_key, wait_days=OUTCOME_GRADE_WAIT_DAYS):
    """
    Returns a short, human-readable line grading the last ACCEPTED on-demand
    decision for this keyword, or None if there is nothing to grade yet
    (never accepted, or too recent to judge). Meant to be appended to
    previous_summary before it goes into the prompt.
    """
    from queries import _expectation_verdict

    last = pd.read_sql(
        """
        SELECT action, implementation_date
        FROM voylla.blinkit_ondemand_actions
        WHERE "Brand" = %(brand)s AND campaign_id = %(campaign_id)s
          AND targeting = %(targeting)s AND user_implemented = 'true'
          AND implementation_date IS NOT NULL
        ORDER BY implementation_date DESC
        LIMIT 1
        """,
        engine,
        params={"brand": brand, "campaign_id": str(campaign_id), "targeting": targeting_key},
    )
    if last.empty:
        return None

    action    = str(last.iloc[0]["action"] or "").upper()
    impl_date = last.iloc[0]["implementation_date"]
    days_since = (datetime.now().date() - impl_date).days
    if days_since < wait_days:
        return (f"Accepted {action} on {impl_date} - only {days_since}d ago, "
                f"too recent to grade (need {wait_days}d).")

    # Same-shape before/after: `wait_days` on each side of the implementation date,
    # scoped to this one keyword. Targeting join deliberately case/underscore
    # normalised - Blinkit_Ads_Report uses spaces and original case, this
    # table stores lower_snake_case.
    window = pd.read_sql(
        """
        SELECT
            (TO_TIMESTAMP(a."Date",'YYYY-MM-DD HH24:MI:SS')::date < %(impl_date)s) AS is_before,
            SUM(a."Estimated Budget Consumed") AS spend,
            SUM(a."Direct Sales" + a."Indirect Sales") AS sales
        FROM voylla."Blinkit_Ads_Report" a
        WHERE a."Brand" = %(brand)s
          AND a."Campaign ID"::TEXT = %(campaign_id)s
          AND LOWER(REPLACE(a."Targeting Value", ' ', '_')) = %(targeting)s
          AND TO_TIMESTAMP(a."Date",'YYYY-MM-DD HH24:MI:SS')::date
              BETWEEN %(impl_date)s - INTERVAL '%(wait_days)s day'
                  AND %(impl_date)s + INTERVAL '%(wait_days)s day'
        GROUP BY is_before
        """,
        engine,
        params={
            "brand": brand, "campaign_id": str(campaign_id), "targeting": targeting_key,
            "impl_date": impl_date, "wait_days": wait_days,
        },
    )
    spend_before = float(window.loc[window["is_before"] == True, "spend"].sum())
    sales_before = float(window.loc[window["is_before"] == True, "sales"].sum())
    spend_after  = float(window.loc[window["is_before"] == False, "spend"].sum())
    sales_after  = float(window.loc[window["is_before"] == False, "sales"].sum())

    roas_before = round(sales_before / spend_before, 2) if spend_before > 0 else None
    roas_after  = round(sales_after / spend_after, 2) if spend_after > 0 else None

    if action == "PAUSE" and spend_after < max(0.05 * spend_before, 20):
        naive_verdict = "PAUSED"
    elif roas_before is None:
        naive_verdict = "NEW"
    else:
        naive_verdict = "IMPROVED" if (roas_after or 0) >= roas_before else "WORSENED"

    verdict, reason = _expectation_verdict(action, roas_before, roas_after, naive_verdict)

    if verdict == "MET" and naive_verdict == "PAUSED":
        return f"Last accepted decision ({impl_date}): {action}. Outcome: spend stopped as intended - MET."
    if roas_before is None or roas_after is None:
        return f"Last accepted decision ({impl_date}): {action}. Outcome: not enough spend on one side to grade ({reason})."
    return (f"Last accepted decision ({impl_date}): {action}. "
            f"ROAS {roas_before} -> {roas_after}. Verdict: {verdict} ({reason}).")


def fetch_active_campaign_ids(engine, brand):
    """Campaigns with any spend in the last 7 days - the daily run's scope."""
    df = pd.read_sql(
        """
        SELECT DISTINCT "Campaign ID"::TEXT AS campaign_id
        FROM voylla."Blinkit_Ads_Report"
        WHERE "Brand" = %(brand)s
          AND TO_TIMESTAMP("Date",'YYYY-MM-DD HH24:MI:SS') >= CURRENT_DATE - INTERVAL '7 days'
        """,
        engine, params={"brand": brand},
    )
    return df["campaign_id"].tolist()


def generate_ondemand_suggestions(brand, campaign_id, requested_by=None):
    """
    One synchronous call: fetch this campaign's rolling window data, run the
    same deterministic rule engine + LLM decision as the scheduled pipeline,
    and write fresh suggestion rows into voylla.blinkit_ondemand_actions.
    Returns {"ok": bool, "count": int, "rows": [...], "error": str|None}.
    """
    from db import get_engine, get_anthropic_config
    import anthropic as anthropic_sdk

    engine = get_engine()
    campaign_id = str(campaign_id)

    data = _fetch_aggregated_campaign_data(engine, brand, campaign_id)
    if data is None:
        return {"ok": False, "count": 0, "rows": [], "error": "No recent ad-report data for this campaign."}

    aggregated_df  = data["aggregated_df"]
    campaign_name  = data["campaign_name"]
    campaign_spend = data["campaign_spend"]

    history_df = _fetch_ondemand_history(engine, brand, campaign_id)
    history_df["campaign_id"] = history_df["campaign_id"].astype(str)

    data_for_llm = aggregated_df.to_dict(orient="records")
    for row in data_for_llm:
        row["campaign_id"]   = campaign_id
        row["campaign_name"] = campaign_name
        targeting_key = str(row.get("targeting", "")).strip().lower().replace(" ", "_")
        prev = build_previous_context(history_df, campaign_id, targeting_key)
        outcome = compute_outcome_feedback(engine, brand, campaign_id, targeting_key)
        row["previous_summary"] = (
            prev["previous_summary"] + (f" | OUTCOME: {outcome}" if outcome else "")
        )
        # Shared notes "wiki" - anything filed via Agent Chat (or elsewhere)
        # against this keyword, this campaign, or the whole brand. Dynamic:
        # a note doesn't have to be about this exact keyword to show up here.
        import queries as _queries
        notes = _queries.fetch_relevant_notes(brand, campaign_id=campaign_id, targeting=targeting_key)
        if notes:
            notes_text = " | ".join(f"NOTE ({n['entity_type']}): {n['text']}" for n in notes)
            row["previous_summary"] += f" | {notes_text}"
        row["previous_history"] = prev["previous_history"]
        inject_python_flags(row)

    cfg = get_anthropic_config()
    client = anthropic_sdk.Anthropic(api_key=cfg["api_key"])
    model = cfg.get("model") or "claude-haiku-4-5-20251001"

    keyword_pool_records = _fetch_keyword_pool(engine)
    KEYWORD_POOL_BLOCK = {
        "type": "text",
        "text": ("KEYWORD POOL — the ONLY source for alternative_keywords (used when "
                  "action = PAUSE). Sorted by weighted_score desc.\n"
                  + json.dumps(clean_nan(keyword_pool_records))),
    }

    insufficient = campaign_spend < SPEND_THRESHOLD

    if insufficient:
        prompt = f"""
        You are a performance marketing expert analyzing Blinkit ad campaigns.

        CONTEXT:
        - This campaign has a total 7-day spend of ₹{campaign_spend:.0f}, which is below the ₹500 threshold.
        - All keywords in this campaign are classified as INSUFFICIENT DATA.
        - Do NOT recommend PAUSE or INCREASE_CPM. Action must always be INSUFFICIENT_DATA.
        - Your only task: suggest 2-3 semantically similar alternative keywords for EACH keyword below.

        RULES FOR ALTERNATIVE KEYWORDS:
        1. Suggest keywords semantically similar to the targeting keyword
        2. DO NOT suggest the same keyword being analyzed
        3. Each keyword MUST receive DIFFERENT alternative suggestions
        4. No duplicates across the entire batch
        5. If similarity is low, still suggest the closest 2 keywords from the keyword pool. Never return an empty array.

        OUTPUT FORMAT: Return ONLY a valid JSON array, each object with EXACTLY:
        campaign_id, campaign_name, targeting, action, cpm_change, confidence,
        explanation, alternative_keywords, current_cpm, campaign_budget.

        KEYWORD POOL: provided in the system context above — use ONLY those keywords.

        CURRENT KEYWORDS IN THIS CAMPAIGN:
        {json.dumps(clean_nan(data_for_llm))}

        Return ONLY the JSON array. No markdown fences, no preamble.
        """
        system_blocks = [KEYWORD_POOL_BLOCK]
    else:
        user_message = f"""
        The `action` field is YOUR final call. `rule_action` (the signed-off v3 Rule
        Decision) is your STRONG DEFAULT in BOTH directions — follow it unless the
        numbers give you a specific reason not to, and say so explicitly when you diverge.

        Safety guards Python applies AFTER your response (do NOT self-censor around them):
           1. position = 1 AND INCREASE_CPM -> forced NO_CHANGE.
           2. DECREASE_CPM is clamped so the new CPM never lands below cpm_floor;
              if current_cpm is already at/below the floor -> NO_CHANGE.
           3. A PAUSE you invent (rule_action is not PAUSE) is blocked unless the
              numbers back it. A rule-mandated PAUSE always goes through.

        -> Each row contains two fields:
           "previous_summary" -> copy this verbatim into the explanation "Previous:" section.
           "previous_history" -> use this list for Step 5 decision-making (newest first).
        -> Do NOT repeat an action flagged with LOOP in previous_summary.
        -> Do NOT cross-reference history between keywords.
        -> If previous_summary contains "| OUTCOME: ...", that is a grade of whether your
           LAST accepted decision for this exact keyword actually worked (MET/MISSED/PAUSED,
           with the real before/after ROAS). Weigh it like a second opinion from a colleague
           checking your last call: if it says MISSED, treat that as real evidence the last
           move was wrong for this keyword and be more willing to reverse or try something
           different this cycle. If it says MET, that's confirmation the reasoning that led
           to it was sound — lean on the same logic again unless the numbers have moved. If
           it's "too recent to grade", ignore it and decide on the current data alone.
        -> If previous_summary contains one or more "| NOTE (type): ..." entries, those are
           context a human filed - an upcoming event, a standing instruction, a correction -
           not a computed fact. A NOTE (keyword)/(campaign) is specific to this exact row; a
           NOTE (brand)/(general) applies more broadly and may be less directly relevant to
           this specific keyword. Weigh notes alongside the numbers, don't let them override
           a clear numeric signal, and if you act on one say so explicitly in the explanation.

        CURRENT DATA (7-day aggregated with 15-day and 30-day ROAS and spend):
        {json.dumps(clean_nan(data_for_llm))}

        KEYWORD POOL: provided in the system context above. Use ONLY those keywords
        for alternative_keywords, and only when action = PAUSE.

        For each keyword: read rule_action + rule_tag, decide the FINAL action yourself
        (diverge only with a specific numeric reason), then write the explanation in
        4 sections joined by " || ": DATA, ANALYSIS, HISTORY, RECOMMENDATION - same
        format as the scheduled pipeline.

        Return ONLY a JSON array. Each object MUST contain ALL of these fields:
        campaign_id, targeting, action, explanation, campaign_name, cpm_change,
        confidence, alternative_keywords, current_cpm, campaign_budget.
        confidence MUST be a decimal between 0.70 and 0.95, never missing.
        No markdown fences, no preamble, no commentary.
        """
        system_blocks = [{"type": "text", "text": SYSTEM_PROMPT}, KEYWORD_POOL_BLOCK]
        prompt = user_message

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=12000,
            temperature=0,
            system=system_blocks,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_response = resp.content[0].text
        suggestion_response = extract_json(raw_response)
    except Exception as e:
        return {"ok": False, "count": 0, "rows": [], "error": f"LLM call failed: {e}"}

    if insufficient:
        for row in suggestion_response:
            if not isinstance(row, dict):
                continue
            row["campaign_id"]   = campaign_id
            row["campaign_name"] = campaign_name
            row["action"]        = "INSUFFICIENT DATA"
    else:
        row_lookup = {
            str(r.get("targeting", "")).strip().lower().replace(" ", "_"): r
            for r in data_for_llm
        }
        for row in suggestion_response:
            if not isinstance(row, dict):
                continue
            row["campaign_id"]   = campaign_id
            row["campaign_name"] = campaign_name
            tkey = str(row.get("targeting", "")).strip().lower().replace(" ", "_")
            orig = row_lookup.get(tkey, {})

            row["zombie_keyword_flag"]      = orig.get("zombie_keyword_flag", False)
            row["has_positive_roas_signal"] = orig.get("has_positive_roas_signal", False)
            row["sufficient_burn_no_roas"]  = orig.get("sufficient_burn_no_roas", False)
            row["tier"]                     = orig.get("tier", 3)
            row["rule_action"]              = orig.get("rule_action", "")
            row["rule_cpm_change_pct"]      = orig.get("rule_cpm_change_pct", 0)
            row["rule_tag"]                 = orig.get("rule_tag", "")
            row["_state_marker"]            = orig.get("_state_marker", "")
            row["cpm_floor"]                = orig.get("cpm_floor", row.get("cpm_floor"))
            row["keyword_budget_pct_7d"]    = orig.get("keyword_budget_pct_7d", None)
            for _fld in ("spend_1d", "spend_7d", "spend_15d", "spend_30d",
                         "roas_1d", "roas_7d", "roas_15d", "roas_30d",
                         "position", "most_viewed_position", "impressions",
                         "keyword_searches", "search_volume_tier",
                         "campaign_budget_7d", "campaign_spend"):
                if orig.get(_fld) is not None:
                    row[_fld] = orig.get(_fld)
            row["low_search_zero_roas"] = orig.get("low_search_zero_roas", False)
            row["high_budget_low_roas"] = orig.get("high_budget_low_roas", False)
            row["active_days_7d"]       = orig.get("active_days_7d", 7)
            row["most_viewed_position"] = orig.get("most_viewed_position") or orig.get("position", 99)

        # backfill any keyword the LLM skipped, using the deterministic rule
        returned = {
            str(r.get("targeting", "")).strip().lower().replace(" ", "_")
            for r in suggestion_response if isinstance(r, dict)
        }
        for tkey, orig in row_lookup.items():
            if tkey in returned:
                continue
            ra = str(orig.get("rule_action") or "NO_CHANGE").upper().strip()
            cpm = _f(orig.get("current_cpm"))
            bf = dict(orig)
            bf["campaign_id"]   = campaign_id
            bf["campaign_name"] = campaign_name
            bf["action"]        = ra
            bf["cpm_change"]    = round(cpm * 0.10) if ra in ("INCREASE_CPM", "DECREASE_CPM") else 0
            bf["confidence"]    = 0.70
            bf["alternative_keywords"] = []
            bf["_llm_missing"]  = True
            bf["explanation"]   = "LLM -> no response for this keyword; the signed-off rule decision was applied unchanged."
            suggestion_response.append(bf)

    import queries
    tolerance_pct = float(queries.fetch_autonomy_setting(campaign_id, brand)["bid_tolerance_pct"])

    rows = save_ondemand_action(engine, suggestion_response, brand, requested_by=requested_by, tolerance_pct=tolerance_pct)
    return {"ok": True, "count": len(rows), "rows": rows, "error": None}


# ══════════════════════════════════════════════════════════════════════════════
# DAILY ORCHESTRATOR  (new - not from the notebook)
# ══════════════════════════════════════════════════════════════════════════════
# Runs generate_ondemand_suggestions for every active campaign in a brand, one
# at a time. This is the function the daily scheduled notebook
# (Blinkit_Ondemand_Daily_Suggestions.ipynb) calls - the manual "Generate now"
# button and the daily run share this exact same per-campaign logic, so a
# suggestion looks and behaves identically whichever way it was triggered.
# ══════════════════════════════════════════════════════════════════════════════

def generate_daily_suggestions_for_brand(brand, campaign_ids=None, requested_by="daily_scheduled"):
    """
    Loops generate_ondemand_suggestions() over every on-demand-managed
    campaign for a brand (voylla.campaign_autonomy_mode.ondemand_managed =
    true) that ALSO has real spend in the last 7 days, or a caller-supplied
    list. The spend filter matters even when every campaign in the catalog
    is marked managed: a campaign with zero recent spend has nothing for
    the rule engine to reason about (a wasted LLM call), and - more
    importantly - it's usually dormant/stopped on purpose, so never even
    generating a suggestion for it means the push script can never be
    handed an INCREASE/DECREASE for a campaign whose RESTART payload would
    revive something that was deliberately paused. Never raises - a failed
    campaign is recorded and the loop continues, so one bad campaign can't
    take down the whole daily run.

    Returns {"brand", "campaigns_run", "total_suggestions", "failures": [...]}.
    """
    from db import get_engine
    import queries

    engine = get_engine()
    if campaign_ids is not None:
        ids = campaign_ids
    else:
        managed = queries.fetch_ondemand_managed_ids(brand)
        spending = set(fetch_active_campaign_ids(engine, brand))
        ids = sorted(managed & spending)
    if not ids:
        return {"brand": brand, "campaigns_run": 0, "total_suggestions": 0, "failures": []}

    total = 0
    failures = []
    for campaign_id in ids:
        try:
            result = generate_ondemand_suggestions(brand, campaign_id, requested_by=requested_by)
            if result["ok"]:
                total += result["count"]
                print(f"[daily] {brand} #{campaign_id}: {result['count']} suggestion(s)")
            else:
                failures.append({"campaign_id": campaign_id, "error": result["error"]})
                print(f"[daily] {brand} #{campaign_id}: FAILED - {result['error']}")
        except Exception as e:
            failures.append({"campaign_id": campaign_id, "error": str(e)})
            print(f"[daily] {brand} #{campaign_id}: EXCEPTION - {e}")

    return {
        "brand": brand,
        "campaigns_run": len(ids),
        "total_suggestions": total,
        "failures": failures,
    }


# ══════════════════════════════════════════════════════════════════════════════
# NOTES LINT  (new - weekly maintenance for the shared notes "wiki")
# ══════════════════════════════════════════════════════════════════════════════
# A note has no built-in expiry - "Navratri starts Oct 1" is true forever as
# a sentence, it just stops being RELEVANT once Navratri has passed. Rather
# than force the user to specify an end date at write time, this asks the
# LLM once a week: given today's date and everything currently active, what
# should stop being folded into prompts? Cheap - a handful of notes per
# brand at most, one small Haiku call, no daily cost.

def lint_notes(brand):
    """Reviews every active note for a brand and deactivates (still_relevant
    = False) anything whose event/window has clearly passed or that's
    superseded by a newer note - never a standing instruction just for being
    old. Returns {"brand", "reviewed", "deactivated": [{"id","reason"}]}."""
    import queries
    from db import get_anthropic_config
    import anthropic as anthropic_sdk

    notes = queries.fetch_notes(brand, include_stale=False, limit=200)
    if not notes:
        return {"brand": brand, "reviewed": 0, "deactivated": []}

    today = datetime.now().date()
    notes_text = "\n".join(
        f"id={n['id']} | {n['entity_type']}"
        f"{(':' + n['entity_id']) if n['entity_id'] else ''} | "
        f"filed {n['created_at'].date()} | {n['text']}"
        for n in notes
    )

    cfg = get_anthropic_config()
    client = anthropic_sdk.Anthropic(api_key=cfg["api_key"])
    model = cfg.get("model") or "claude-haiku-4-5-20251001"

    prompt = f"""Today's date: {today}.

Below are active notes for the {brand} Blinkit bidding account - context a
human gave for future bid decisions (an upcoming event, a standing
instruction, a correction).

{notes_text}

Identify which notes should be deactivated because:
- The event/window they describe has clearly passed (e.g. a note about an
  October festival is stale well after that festival has ended)
- They are directly contradicted or superseded by a newer note on the same topic

Do NOT deactivate a standing instruction just because it's old - only
deactivate it if a newer note clearly supersedes it or it names an end date
that has passed.

Return ONLY a JSON array, one object per note to deactivate:
[{{"id": 12, "reason": "one short sentence"}}]
Return an empty array [] if nothing should be deactivated. No other text.
"""

    resp = client.messages.create(
        model=model, max_tokens=1000, temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text
    try:
        to_deactivate = extract_json(raw)
    except Exception:
        to_deactivate = []

    deactivated = []
    for item in to_deactivate:
        try:
            note_id = int(item["id"])
            queries.set_note_relevance(note_id, False)
            deactivated.append({"id": note_id, "reason": item.get("reason", "")})
        except Exception:
            continue

    return {"brand": brand, "reviewed": len(notes), "deactivated": deactivated}
