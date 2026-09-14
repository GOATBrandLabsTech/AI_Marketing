"""
Agent Chat: a read-only conversational layer over the same data the
dashboard shows. The LLM only ever gets *read* tools - wrappers around the
existing queries.py functions - so "chat only, no implementation" is an
architectural guarantee, not a prompt instruction that a clever question
could talk around. Conversation history persists in Postgres
(agent_chat_threads / agent_chat_messages) so a thread survives across
sessions and becomes part of the same "memory" the rest of the app reads.
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
- You are READ-ONLY. You cannot pause a keyword, accept or override a recommendation, change a \
bid, or modify a schedule or budget, and you have no tool that does any of those things. If asked \
to take an action, say plainly that you can't do it from chat, and point to the right page \
(Pending Actions to accept/override a recommendation, Campaigns & Budget to manage schedule \
windows) - then, if useful, say what you'd do in their position and why, as advice, not as a \
thing you're about to execute.
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
            "Today's (or a given date's) AI recommendations for a brand: counts by action type "
            "(NO_CHANGE/INCREASE_CPM/DECREASE_CPM/PAUSE/ZOMBIE_FLAG/etc.), counts by review status "
            "(accepted/rejected/undecided/overridden), and a few sample explanations. Use for "
            "'what did the agent recommend', 'how many pauses', 'what's still undecided'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brand": {"type": "string"},
                "action_date": {"type": "string", "description": "YYYY-MM-DD, optional - defaults to the latest"},
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


def _cap_rows(rows, n, key=None):
    if key:
        rows = sorted(rows, key=key, reverse=True)
    return rows[:n], max(0, len(rows) - n)


def _tool_channel_performance(brand, cutover_date=None, window_days=7):
    cutover = date.fromisoformat(cutover_date) if cutover_date else None
    if not cutover:
        dates = queries.fetch_action_dates(brand)
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
    elif brand == "Chumbak":
        cutover = queries.CHUMBAK_SHOWCASE_GO_LIVE
    else:
        cutover = date.today() - timedelta(days=1)
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
    if not action_date:
        dates = queries.fetch_action_dates(brand)
        action_date = dates[0] if dates else None
    if not action_date:
        return {"error": "no recommendations found for this brand"}
    rows = queries.fetch_pending_actions(brand, action_date)
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
        "action_date": action_date,
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
        s = search.lower()
        matched = [
            {
                "campaign_id": r["campaign_id"],
                "campaign_name": r["campaign_name"],
                "status": r["last_status"],
                "budget": r["budget"],
                "window_count": r["window_count"],
            }
            for r in rows
            if s in (r["campaign_name"] or "").lower()
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


TOOL_FUNCTIONS = {
    "get_channel_performance": _tool_channel_performance,
    "get_before_after_keywords": _tool_before_after_keywords,
    "get_pending_actions_summary": _tool_pending_actions_summary,
    "get_campaign_status": _tool_campaign_status,
    "get_recommendation_impact": _tool_recommendation_impact,
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
