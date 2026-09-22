"""
Agent Chat: a mostly-read-only conversational layer over the same data the
dashboard shows. Almost every tool is a *read* wrapper around queries.py -
"chat only, no implementation" holds for campaigns, bids, and schedules: no
tool here can pause a keyword, accept a recommendation, or change a budget.
The one deliberate exception is file_note, which writes a row to the shared
"notes" table (voylla.notes) - context (upcoming events, standing
instructions, corrections) that the on-demand suggestion engine reads back
before every future decision. It never touches campaigns/bids directly; it
only remembers something for the *next* suggestion to consider.
Conversation history persists in Postgres (agent_chat_threads /
agent_chat_messages) so a thread survives across sessions.
"""
import json
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import anthropic

import queries
from db import get_anthropic_config, get_cursor

MAX_TOOL_ROUNDS = 6

SYSTEM_PROMPT = """You are the Blinkit Marketing Control chat assistant, built for Voylla and \
its sister brands (Chumbak, Petcrux) on the Blinkit ads platform. You help the marketing team \
understand what the AI bid-agent has done, why, and how well it is working.

Ground rules - follow these strictly:
- You cannot pause a keyword, accept or override a recommendation, change a bid, or modify a \
schedule or budget, and you have no tool that does any of those things. If asked to take an \
action like that, say plainly that you can't do it from chat, and point to the right page \
(Pending Actions to accept/override a recommendation, Campaigns & Budget to manage schedule \
windows) - then, if useful, say what you'd do in their position and why, as advice, not as a \
thing you're about to execute.
- The one thing you CAN write is a note (file_note) - a piece of context that should inform \
future AI bid suggestions: an upcoming event, a standing instruction ("leave X alone for two \
weeks"), a correction. Use it whenever the user tells you something like that - don't just \
acknowledge it in the chat and let it evaporate. Pick entity_type/entity_id based on what the \
note is actually about (a specific campaign, a keyword across campaigns, the whole brand, or \
'general' for something with no natural entity) - don't default to one type. After filing, tell \
the user what you filed and where, so they can correct you if you guessed the scope wrong. This \
never touches a campaign, bid, or schedule directly - it only shapes what the suggestion engine \
sees next time it runs for that entity.
- Always use a tool to fetch real data before answering a question about performance, campaigns, \
spend, or past decisions. Never invent or estimate a number that a tool could have given you.
- Be concise and specific - cite the actual figures a tool returned (spend, ROAS, counts, dates) \
rather than vague language like "significant" or "a lot."
- If a tool returns no data (nothing matches a search, no recommendations for a date), say so \
plainly instead of guessing why.
- The current brand in view is: {brand}. Use it by default; only look at a different brand if the \
user names one explicitly.
- Today's date is {today}. Blinkit revises the last ~2 days of data as more conversions land, so \
flag numbers from the last 2 days as provisional if it's relevant to the question.
- get_pending_actions_summary and get_ondemand_suggestions read the SAME table \
(voylla.blinkit_ondemand_actions) - Blinkit_actions_llm, the old scheduled-batch table, has been \
fully retired from every surface of this dashboard (Pending Actions, ROAS Impact, Before/After all \
switched over). Every campaign, across all three brands, is on-demand-managed. \
get_pending_actions_summary defaults to recent history across all campaigns; get_ondemand_suggestions \
is the same data with a campaign-name search filter. Use whichever the question's phrasing suggests, \
they'll agree.
- A managed campaign in autonomy mode "auto" runs end-to-end with no manual step: the daily run \
generates a suggestion, auto-accepts it (skipping only rows flagged REVIEW), and the scheduled push \
script sends it to Blinkit live - within an authorized per-move CPM tolerance (bid_tolerance_pct, \
e.g. 20%). "semi_auto" (the default) means suggestions still need a human accept/override on the \
On-Demand AI or Pending Actions page. Use get_automation_status to check a campaign's mode.
- ROAS Impact and Before/After now score against voylla.blinkit_ondemand_actions, not the old table. \
Their "was_implemented"/verdict logic requires an actual live push (push_status = 'done' from the \
independent Blinkit_Ondemand_Bid_Push.ipynb), which stays in DRY_RUN until a human turns it off - so \
until then, expect verdicts to be empty/NULL for everything. That's correct, not a bug: nothing has \
actually gone live yet, so there is nothing to honestly attribute a ROAS change to.
"""

TOOLS = [
    {
        "name": "get_channel_performance",
        "description": (
            "Whole-channel spend/ROAS for a brand, before vs. after a cutover date (defaults to "
            "the most recent action date), plus the last 14 days of daily ROAS for trend context. "
            "Use for 'how is X doing', 'did the change help', 'what's the trend' type questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string", "description": "Voylla, Chumbak, or Petcrux"},
                "cutover_date": {"type": "string", "description": "YYYY-MM-DD, optional - defaults to the latest action date"},
                "window_days": {"type": "integer", "description": "Days either side of the cutover to compare, default 7"},
            },
            "required": ["brand"],
        },
    },
    {
        "name": "get_before_after_keywords",
        "description": (
            "Per-keyword before/after ROAS comparison around a cutover date, spend-weighted "
            "verdict summary (better/worse/same/paused), an EXPECTATION verdict per row (met/"
            "missed/n-a against a stated rule-based bar for that action type - not a predictive "
            "model), and the top keywords by spend. Use for 'which keywords got better/worse', "
            "'did the pauses help', 'what moved the ROAS', 'is this meeting expectations', "
            "'are we on track', or any forecast/expectation question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string"},
                "cutover_date": {"type": "string", "description": "YYYY-MM-DD, optional"},
                "pre_days": {"type": "integer", "description": "Days before cutover to average, default 7"},
                "min_spend": {"type": "number", "description": "Rs/day noise cutoff, default 60"},
            },
            "required": ["brand"],
        },
    },
    {
        "name": "get_pending_actions_summary",
        "description": (
            "Pending Actions page: recent AI recommendations for a brand (on-demand engine, same "
            "table get_ondemand_suggestions reads - Blinkit_actions_llm is fully retired from this "
            "surface). Counts by action type (NO_CHANGE/INCREASE_CPM/DECREASE_CPM/PAUSE/"
            "ZOMBIE_FLAG/etc.), counts by review status (accepted/rejected/undecided/overridden), "
            "and a few sample explanations. Use for 'what did the agent recommend', 'how many "
            "pauses', 'what's still undecided'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string"},
                "action_date": {"type": "string", "description": "YYYY-MM-DD, optional - omit for all recent history"},
                "action_type": {"type": "string", "description": "Filter to one action, e.g. PAUSE - optional"},
            },
            "required": ["brand"],
        },
    },
    {
        "name": "get_campaign_status",
        "description": (
            "Live campaign status for a brand: counts by status (ACTIVE/STOPPED/ON_HOLD/"
            "BUDGET_EXHAUSTED) and, optionally, campaigns matching a name search. Use for "
            "'which campaigns are stopped', 'is X campaign active', 'budget exhausted campaigns'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string"},
                "search": {"type": "string", "description": "Campaign name substring to filter to, optional"},
            },
            "required": ["brand"],
        },
    },
    {
        "name": "get_recommendation_impact",
        "description": (
            "Per-keyword-recommendation before/after ROAS (7 days either side of each "
            "recommendation's own action date), with IMPROVED/WORSENED/FLAT verdict counts. This "
            "is narrower than get_before_after_keywords: only keywords the AI actually recommended "
            "a change for, each measured against its own date. Use for 'how many recommendations "
            "actually improved things'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string"},
                "since_days": {"type": "integer", "description": "How far back to include, default 30"},
                "verdict": {"type": "string", "description": "IMPROVED, WORSENED, or FLAT - optional filter"},
            },
            "required": ["brand"],
        },
    },
    {
        "name": "get_automation_status",
        "description": (
            "Whether a campaign is fully automated: on-demand-managed (delinked from "
            "Blinkit_actions_llm, suggestions only from the on-demand engine), its autonomy "
            "mode (auto/semi_auto/manual - 'auto' means the daily run auto-accepts with no "
            "manual click), and its authorized bid-move tolerance percent. Use for 'is auto "
            "mode on for X', 'is X fully automated', 'what campaigns are on autopilot'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string"},
                "search": {"type": "string", "description": "Campaign name substring to filter to, optional"},
            },
            "required": ["brand"],
        },
    },
    {
        "name": "get_ondemand_suggestions",
        "description": (
            "On-Demand AI Suggestions: recommendations generated manually from that page, right "
            "now, for one campaign at a time - a completely separate table from the scheduled "
            "Pending Actions batch, never pushed to a live bid on its own. Returns counts by "
            "action/status and recent rows with their explanations. Use for 'did we generate/run/"
            "check anything on-demand for X', 'what did the on-demand tool say about this "
            "campaign', or any question naming 'on-demand'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string"},
                "search": {"type": "string", "description": "Campaign name substring to filter to, optional - not a numeric ID"},
            },
            "required": ["brand"],
        },
    },
    {
        "name": "file_note",
        "description": (
            "The only write action you have. Saves a piece of context - an upcoming event, a "
            "standing instruction, a correction - that the on-demand suggestion engine will read "
            "before every future decision for the entity you attach it to. Use whenever the user "
            "tells you something that should shape future bidding, not just this conversation. "
            "Does NOT touch any campaign, bid, or schedule - it only files a note for later."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string"},
                "entity_type": {
                    "type": "string",
                    "enum": ["campaign", "keyword", "brand", "general"],
                    "description": (
                        "What the note is about. 'campaign': one specific campaign_id. 'keyword': "
                        "a keyword name, applies wherever it appears for this brand (not one "
                        "campaign). 'brand': applies to everything in this brand. 'general': no "
                        "natural entity, applies broadly."
                    ),
                },
                "entity_id": {
                    "type": "string",
                    "description": "The campaign_id or keyword name this note is about. Omit for entity_type brand/general.",
                },
                "text": {"type": "string", "description": "The note itself, written plainly - this gets pasted into future prompts verbatim."},
            },
            "required": ["brand", "entity_type", "text"],
        },
    },
    {
        "name": "get_notes",
        "description": (
            "Lists notes filed for a brand (via file_note or elsewhere) - what's currently active "
            "and shaping future suggestions. Use for 'what notes do we have', 'did I already tell "
            "you about X', 'what's still relevant'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string"},
                "include_stale": {"type": "boolean", "description": "Include notes marked no-longer-relevant too, default false"},
            },
            "required": ["brand"],
        },
    },
]


def _jsonable(v):
    """json.dumps(default=...) calls this ONLY for values it can't already
    serialize. Returning `v` unchanged for a type we don't recognise (e.g.
    Decimal, from any NUMERIC/DECIMAL Postgres column) hands json.dumps the
    exact same non-serializable object right back - it calls default() on it
    again, forever, until Python's json module gives up with "Circular
    reference detected". Every branch here must return a JSON-primitive
    type; the final str(v) fallback guarantees that even for a type nobody
    thought to add explicitly."""
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, time):
        return v.strftime("%H:%M:%S")
    if isinstance(v, Decimal):
        return float(v)
    return str(v)


def _norm(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum() or ch.isspace())


def _matches(campaign_name, campaign_id, search):
    """Forgiving match for LLM-guessed search strings: an exact numeric ID,
    or every whitespace-separated word in `search` appearing somewhere in
    the campaign name, punctuation/case-insensitive (so "City Targeting
    Mumbai" matches "City Targeting (Mumbai) 28 aug")."""
    if not search:
        return True
    s = search.strip()
    if s.isdigit():
        return str(campaign_id) == s
    name = _norm(campaign_name)
    return all(word in name for word in _norm(s).split())


def _cap_rows(rows, n, key=None):
    if key:
        rows = sorted(rows, key=key, reverse=True)
    return rows[:n], max(0, len(rows) - n)


def _tool_channel_performance(brand, cutover_date=None, window_days=7):
    cutover = date.fromisoformat(cutover_date) if cutover_date else None
    if not cutover:
        dates = queries.fetch_ondemand_action_dates(brand)
        cutover = date.fromisoformat(dates[0]) if dates else date.today() - timedelta(days=1)
    campaign_ids = queries.CHUMBAK_SHOWCASE_CAMPAIGN_IDS if brand == "Chumbak" else None
    daily = queries.fetch_channel_daily(brand, days=90, campaign_ids=campaign_ids)
    ba = queries.channel_before_after(daily, cutover, int(window_days or 7))
    recent = daily[-14:]
    return {
        "brand": brand,
        "cutover_date": str(cutover),
        "window_days": ba["window_days"],
        "before": ba["before"],
        "after": ba["after"],
        "after_is_partial": ba["after_is_partial"],
        "recent_daily_roas": [
            {"date": str(d["date"]), "roas": d["roas"], "spend": round(d["spend"], 2)} for d in recent
        ],
    }


def _tool_before_after_keywords(brand, cutover_date=None, pre_days=7, min_spend=60):
    if cutover_date:
        cutover = date.fromisoformat(cutover_date)
    else:
        cutover = queries.fetch_latest_ondemand_action_date(brand) or (date.today() - timedelta(days=1))
    campaign_ids = queries.CHUMBAK_SHOWCASE_CAMPAIGN_IDS if brand == "Chumbak" else None
    rows, summary = queries.fetch_before_after_matrix(
        brand, cutover, pre_days=int(pre_days or 7), min_spend=float(min_spend or 60), campaign_ids=campaign_ids
    )
    meaningful = [r for r in rows if r["meaningful"]]
    top, more = _cap_rows(meaningful, 12, key=lambda r: r["spend_after"])
    return {
        "summary": summary,
        "top_keywords_by_spend": top,
        "additional_meaningful_keywords_not_shown": more,
    }


def _tool_pending_actions_summary(brand, action_date=None, action_type=None):
    """Pending Actions is now on-demand-sourced (voylla.blinkit_ondemand_actions) -
    Blinkit_actions_llm is fully retired from this surface, so this tool reads the
    same table get_ondemand_suggestions does. action_date filters to one day;
    omit it to get the whole recent history, same as the page's default view."""
    rows = queries.fetch_ondemand_actions(brand)
    if action_date:
        rows = [r for r in rows if str(r["action_date"]) == action_date]
    if not rows and action_date:
        return {"error": f"no recommendations found for {brand} on {action_date}"}
    if action_type:
        rows = [r for r in rows if r["action"] == action_type]

    from collections import Counter

    action_counts = dict(Counter(r["action"] for r in rows))
    status_counts = Counter()
    for r in rows:
        if r["override_action"]:
            status_counts["overridden"] += 1
        elif r["user_implemented"] == "true":
            status_counts["accepted"] += 1
        elif r["user_implemented"] == "false":
            status_counts["rejected"] += 1
        else:
            status_counts["undecided"] += 1

    samples = [
        {
            "campaign": r["campaign_name"],
            "keyword": r["targeting"],
            "action": r["action"],
            "confidence": r["confidence"],
            "explanation": (r["explanation"] or "")[:400],
        }
        for r in rows[:8]
    ]
    return {
        "action_date_filter": action_date,
        "most_recent_requested_at": rows[0]["requested_at"] if rows else None,
        "total": len(rows),
        "by_action": action_counts,
        "by_status": dict(status_counts),
        "sample_rows": samples,
    }


def _tool_campaign_status(brand, search=None):
    rows = queries.fetch_campaign_status(brand)
    from collections import Counter

    status_counts = Counter(r["last_status"] for r in rows)
    matched = None
    if search:
        matched = [
            {
                "campaign_id": r["campaign_id"],
                "campaign_name": r["campaign_name"],
                "status": r["last_status"],
                "budget": r["budget"],
                "window_count": r["window_count"],
            }
            for r in rows
            if _matches(r["campaign_name"], r["campaign_id"], search)
        ][:20]
    return {
        "total_campaigns": len(rows),
        "by_status": dict(status_counts),
        "matched_campaigns": matched,
    }


def _tool_recommendation_impact(brand, since_days=30, verdict=None):
    since_date = date.today() - timedelta(days=int(since_days or 30))
    rows = queries.fetch_roas_impact(brand, since_date, verdict=verdict)
    scoreable = [r for r in rows if r["verdict"]]
    from collections import Counter

    counts = Counter(r["verdict"] for r in scoreable)
    changes = [float(r["roas_change"]) for r in scoreable if r["roas_change"] is not None]
    top, more = _cap_rows(scoreable, 10, key=lambda r: abs(float(r["roas_change"] or 0)))
    return {
        "total_recommendations": len(rows),
        "scoreable": len(scoreable),
        "verdict_counts": dict(counts),
        "avg_roas_change": round(sum(changes) / len(changes), 2) if changes else None,
        "top_moves": [
            {
                "campaign": r["campaign_name"], "keyword": r["targeting"], "action": r["action"],
                "roas_before": r["roas_before"], "roas_after": r["roas_after"],
                "roas_change": r["roas_change"], "verdict": r["verdict"],
            }
            for r in top
        ],
        "additional_scoreable_not_shown": more,
    }


def _tool_ondemand_suggestions(brand, search=None):
    rows = queries.fetch_ondemand_actions(brand)
    if search:
        rows = [r for r in rows if _matches(r["campaign_name"], r["campaign_id"], search)]
    if not rows:
        return {"total": 0, "note": "no on-demand suggestions found for this brand/campaign name"}

    from collections import Counter

    action_counts = dict(Counter(r["action"] for r in rows))
    status_counts = Counter()
    for r in rows:
        if r["override_action"]:
            status_counts["overridden"] += 1
        elif r["user_implemented"] == "true":
            status_counts["accepted"] += 1
        elif r["user_implemented"] == "false":
            status_counts["rejected"] += 1
        else:
            status_counts["undecided"] += 1

    samples = [
        {
            "requested_at": r["requested_at"],
            "campaign": r["campaign_name"],
            "campaign_id": r["campaign_id"],
            "keyword": r["targeting"],
            "action": r["action"],
            "confidence": r["confidence"],
            "explanation": (r["explanation"] or "")[:400],
        }
        for r in rows[:8]
    ]
    return {
        "total": len(rows),
        "by_action": action_counts,
        "by_status": dict(status_counts),
        "most_recent_requested_at": rows[0]["requested_at"],
        "sample_rows": samples,
    }


def _tool_automation_status(brand, search=None):
    rows = queries.fetch_campaign_status(brand)
    if search:
        rows = [r for r in rows if _matches(r["campaign_name"], r["campaign_id"], search)]
    matched = [
        {
            "campaign_id": r["campaign_id"],
            "campaign_name": r["campaign_name"],
            "ondemand_managed": r["ondemand_managed"],
            "autonomy_mode": r["autonomy_mode"],
            "bid_tolerance_pct": float(r["bid_tolerance_pct"]) if r["bid_tolerance_pct"] is not None else None,
        }
        for r in rows
    ]
    fully_automated = [m for m in matched if m["ondemand_managed"] and m["autonomy_mode"] == "auto"]
    return {
        "matched_count": len(matched),
        "fully_automated_count": len(fully_automated),
        "campaigns": matched[:20],
    }


def _tool_file_note(brand, entity_type, text, entity_id=None):
    if entity_type in ("campaign", "keyword") and not entity_id:
        return {"error": f"entity_type '{entity_type}' requires entity_id"}
    note_id = queries.file_note(brand, entity_type, text, entity_id=entity_id, source="chat")
    return {
        "ok": True,
        "note_id": note_id,
        "filed_as": f"{entity_type}" + (f":{entity_id}" if entity_id else ""),
        "text": text,
    }


def _tool_get_notes(brand, include_stale=False):
    rows = queries.fetch_notes(brand, include_stale=bool(include_stale))
    return {
        "total": len(rows),
        "notes": [
            {
                "id": r["id"], "entity_type": r["entity_type"], "entity_id": r["entity_id"],
                "text": r["text"], "created_at": r["created_at"], "still_relevant": r["still_relevant"],
            }
            for r in rows
        ],
    }


TOOL_FUNCTIONS = {
    "get_channel_performance": _tool_channel_performance,
    "get_before_after_keywords": _tool_before_after_keywords,
    "get_pending_actions_summary": _tool_pending_actions_summary,
    "get_campaign_status": _tool_campaign_status,
    "get_recommendation_impact": _tool_recommendation_impact,
    "get_ondemand_suggestions": _tool_ondemand_suggestions,
    "get_automation_status": _tool_automation_status,
    "file_note": _tool_file_note,
    "get_notes": _tool_get_notes,
}


def _execute_tool(name, tool_input, brand):
    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        return {"error": f"unknown tool {name}"}
    kwargs = dict(tool_input or {})
    kwargs.setdefault("brand", brand)
    try:
        return fn(**kwargs)
    except Exception as exc:  # keep the chat alive even if one lookup fails
        return {"error": str(exc)}


def create_thread(brand, title=None):
    thread_id = str(uuid.uuid4())
    with get_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO voylla.agent_chat_threads (thread_id, brand, title) VALUES (%s, %s, %s)",
            (thread_id, brand, title or "New conversation"),
        )
    return thread_id


def list_threads(brand, limit=30):
    with get_cursor() as cur:
        cur.execute(
            "SELECT thread_id, brand, title, created_at, updated_at "
            "FROM voylla.agent_chat_threads WHERE brand = %s "
            "ORDER BY updated_at DESC LIMIT %s",
            (brand, limit),
        )
        return cur.fetchall()


def get_messages(thread_id):
    with get_cursor() as cur:
        cur.execute(
            "SELECT role, content, tool_calls, created_at FROM voylla.agent_chat_messages "
            "WHERE thread_id = %s ORDER BY created_at",
            (thread_id,),
        )
        return cur.fetchall()


def _save_message(thread_id, role, content, tool_calls=None):
    with get_cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO voylla.agent_chat_messages (thread_id, role, content, tool_calls) "
            "VALUES (%s, %s, %s, %s)",
            (thread_id, role, content, json.dumps(tool_calls) if tool_calls else None),
        )
        cur.execute(
            "UPDATE voylla.agent_chat_threads SET updated_at = NOW() WHERE thread_id = %s",
            (thread_id,),
        )


def _maybe_set_title(thread_id, user_message):
    with get_cursor() as cur:
        cur.execute(
            "SELECT title FROM voylla.agent_chat_threads WHERE thread_id = %s", (thread_id,)
        )
        row = cur.fetchone()
    if row and row["title"] == "New conversation":
        title = user_message.strip()[:60]
        with get_cursor(commit=True) as cur:
            cur.execute(
                "UPDATE voylla.agent_chat_threads SET title = %s WHERE thread_id = %s",
                (title, thread_id),
            )


def send_message(thread_id, brand, user_message):
    """Runs one full chat turn: persists the user message, drives the
    Claude tool-use loop against read-only data tools, persists and returns
    the assistant's reply."""
    _maybe_set_title(thread_id, user_message)
    _save_message(thread_id, "user", user_message)

    history_rows = get_messages(thread_id)
    cfg = get_anthropic_config()
    client = anthropic.Anthropic(api_key=cfg["api_key"])
    system = SYSTEM_PROMPT.format(brand=brand, today=date.today().isoformat())

    messages = [{"role": r["role"], "content": r["content"]} for r in history_rows[:-1]]
    messages.append({"role": "user", "content": user_message})

    tool_log = []
    final_text = "Sorry, I couldn't find an answer to that."
    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.messages.create(
            model=cfg["model"], max_tokens=1200, system=system, tools=TOOLS, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            final_text = "".join(b.text for b in resp.content if b.type == "text").strip()
            break

        messages.append({"role": "assistant", "content": resp.content})
        tool_result_blocks = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            result = _execute_tool(block.name, block.input, brand)
            tool_log.append({"tool": block.name, "input": block.input})
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=_jsonable),
            })
        messages.append({"role": "user", "content": tool_result_blocks})
    else:
        final_text = "That took more lookups than I can do in one turn - try asking something narrower."

    _save_message(thread_id, "assistant", final_text, tool_calls=tool_log or None)
    return final_text, tool_log
