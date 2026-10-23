import csv
import io
import json
import os
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

import chat
import queries

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")


def jsonable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, Decimal):
        return float(value)
    return value


def dictify(row):
    return {k: jsonable(v) for k, v in dict(row).items()}


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)

    return wrapper


def current_brand():
    return request.args.get("brand") or session.get("brand") or "Chumbak"


CHANNELS = ["Blinkit", "Instamart", "Zepto"]
SETTLING_DAYS = 3  # report days whose sales Blinkit is still adding to


def current_channel():
    ch = request.args.get("channel") or session.get("channel") or "Blinkit"
    return ch if ch in CHANNELS else "Blinkit"


def _channel_funcs(channel):
    """The four channel-specific query functions used across the On-Demand
    AI / Pending Actions routes, keyed by channel name so those routes don't
    repeat the same three-way branch seven times."""
    if channel == "Instamart":
        return {
            "fetch_actions": queries.fetch_instamart_ondemand_actions,
            "fetch_dates": queries.fetch_instamart_ondemand_action_dates,
            "campaign_status": queries.fetch_instamart_campaign_status,
            "bulk_accept": queries.bulk_accept_instamart_ondemand_actions,
            "accept": queries.accept_instamart_ondemand_action,
            "override": queries.override_instamart_ondemand_action,
        }
    if channel == "Zepto":
        return {
            "fetch_actions": queries.fetch_zepto_ondemand_actions,
            "fetch_dates": queries.fetch_zepto_ondemand_action_dates,
            "campaign_status": queries.fetch_zepto_campaign_status,
            "bulk_accept": queries.bulk_accept_zepto_ondemand_actions,
            "accept": queries.accept_zepto_ondemand_action,
            "override": queries.override_zepto_ondemand_action,
        }
    return {
        "fetch_actions": queries.fetch_ondemand_actions,
        "fetch_dates": queries.fetch_ondemand_action_dates,
        "campaign_status": queries.fetch_campaign_status,
        "bulk_accept": queries.bulk_accept_ondemand_actions,
        "accept": queries.accept_ondemand_action,
        "override": queries.override_ondemand_action,
    }


@app.template_filter("steps")
def split_steps(text):
    """The LLM explanation is one long '||'-joined string, one segment per
    decision step (STEP 1 · DECISION PATH, STEP 2 · DATA, ...) - split it so
    each step renders as its own line instead of a wall of text."""
    if not text:
        return []
    return [s.strip() for s in text.split("||") if s.strip()]


def csv_response(rows, columns, filename):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(dict(r))
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.context_processor
def inject_globals():
    if not session.get("authed"):
        return {}
    try:
        brands = queries.fetch_brands()
    except Exception:
        brands = ["Voylla", "Chumbak", "Petcrux"]
    return {
        "brands": brands,
        "brand": current_brand(),
        "ALL_BRANDS": queries.ALL_BRANDS,
        "channel": current_channel(),
        "channels": CHANNELS,
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        expected = os.environ.get("DASHBOARD_PASSWORD", "")
        if expected and password == expected:
            session["authed"] = True
            return redirect(request.args.get("next") or url_for("roas"))
        return render_template("login.html", error="Incorrect password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return redirect(url_for("roas"))


@app.route("/roas")
@login_required
def roas():
    brand = current_brand()
    session["brand"] = brand
    since_days = int(request.args.get("since_days", 30))
    verdict = request.args.get("verdict") or None
    search = request.args.get("search") or None

    # The cutover date picked in the Channel ROAS panel anchors the whole
    # page's time window - "since_days" means "N days before the cutover",
    # not "N days before today", so picking an older action date actually
    # changes what the recommendation tiles below show instead of always
    # sitting on a fixed trailing-30-days-from-today window.
    channel = None
    action_dates = []
    cutover_date = None
    if brand != queries.ALL_BRANDS:
        action_dates = queries.fetch_ondemand_action_dates(brand)
        # Default to the newest action date with at least SETTLING_DAYS of
        # results after it - the newest date of all is usually today, which
        # leaves the "after" side empty.
        settled = [d for d in action_dates if date.fromisoformat(d) <= date.today() - timedelta(days=SETTLING_DAYS)]
        default_cutover = (date.fromisoformat(settled[0]) if settled
                           else date.fromisoformat(action_dates[0]) if action_dates
                           else date.today() - timedelta(days=1))
        cutover_arg = request.args.get("cutover")
        cutover_date = date.fromisoformat(cutover_arg) if cutover_arg else default_cutover

    since_date = (cutover_date or date.today()) - timedelta(days=since_days)

    all_rows = queries.fetch_ondemand_roas_impact(brand, since_date, search=search)
    scoreable = [r for r in all_rows if r["verdict"]]
    changes = [float(r["roas_change"]) for r in scoreable if r["roas_change"] is not None]
    waiting = [r for r in all_rows if r.get("status") == "waiting"]
    kpi = {
        "total": len(all_rows),
        "scoreable": len(scoreable),
        "waiting": len(waiting),
        "not_pushed": sum(1 for r in all_rows if r.get("status") == "not_pushed"),
        "holds": sum(1 for r in all_rows if r.get("status") == "hold"),
        "improved": sum(1 for r in scoreable if r["verdict"] == "IMPROVED"),
        "worsened": sum(1 for r in scoreable if r["verdict"] == "WORSENED"),
        "flat": sum(1 for r in scoreable if r["verdict"] == "FLAT"),
        "avg_roas_change": (sum(changes) / len(changes)) if changes else None,
    }

    daily = {}
    for r in scoreable + waiting:
        d = str(r["action_date"])
        bucket = daily.setdefault(d, {"date": d, "improved": 0, "worsened": 0, "flat": 0, "waiting": 0})
        bucket[r["verdict"].lower() if r["verdict"] else "waiting"] += 1
    daily_series = [daily[d] for d in sorted(daily.keys())]

    rows = [r for r in all_rows if not verdict or r["verdict"] == verdict]

    brand_summary = queries.fetch_brand_summary(all_rows) if brand == queries.ALL_BRANDS else None

    if brand != queries.ALL_BRANDS:
        window_days = int(request.args.get("window_days", 7))
        granularity = request.args.get("granularity", "day")
        # "ai" = only the campaigns the AI actually changes bids on (default
        # whenever there are any); "all" = the whole channel. Comparing the
        # whole channel before/after an AI date credits or blames the AI for
        # campaigns it never touched.
        ai_ids = queries.fetch_ai_managed_campaign_ids(brand)
        scope = request.args.get("scope") or ("ai" if ai_ids else "all")
        if scope not in ("ai", "all") or (scope == "ai" and not ai_ids):
            scope = "all"
        # campaign picker: any subset of the AI-managed campaigns (default all)
        selected_ids = queries.pick_campaigns(",".join(request.args.getlist("campaigns")), ai_ids) if ai_ids else []
        campaign_ids = selected_ids if scope == "ai" else None

        daily_channel = queries.fetch_channel_daily(brand, days=365, campaign_ids=campaign_ids)
        # the brand's campaigns the AI does not run, as the comparison line: a
        # festive week lifts everything, and should not be credited to the AI
        daily_others = (queries.fetch_channel_daily(brand, days=365, exclude_ids=ai_ids)
                        if scope == "ai" else [])
        # BAU vs event days: a festival or sale lifts every campaign, so by
        # default the page leaves event days out of every number below
        import event_days
        from db import get_engine
        try:   # the event list must never take the page down (e.g. no database login)
            events = event_days.list_events(get_engine(), brand)
        except Exception:
            events = []
        ev_dates = event_days.dates_of(events)
        days_mode = event_days.clean_mode(request.args.get("days"))
        brand_daily = daily_channel if scope == "all" else queries.fetch_channel_daily(brand, days=365)
        suggestions = [s_ for s_ in event_days.suggest_events(brand_daily, ev_dates)
                       if s_["start_date"] >= date.today() - timedelta(days=180)][:6]
        all_channel_days = daily_channel
        daily_channel = event_days.apply_mode(daily_channel, ev_dates, days_mode)
        daily_others = event_days.apply_mode(daily_others, ev_dates, days_mode)
        # the trend's date range, like Blinkit's own dashboard: presets or custom
        range_preset, chart_start, chart_end = queries.resolve_range(
            request.args.get("range"), request.args.get("from"), request.args.get("to"), date.today())
        span = (chart_end - chart_start).days + 1
        event_days_in_range = sum(1 for d in all_channel_days
                                  if chart_start <= d["date"] <= chart_end and d["date"] in ev_dates)
        totals = queries.period_totals(daily_channel, chart_start, chart_end)
        prev_totals = queries.period_totals(daily_channel, chart_start - timedelta(days=span),
                                            chart_start - timedelta(days=1))
        daily_for_chart = [d for d in daily_channel if chart_start <= d["date"] <= chart_end]
        others_for_chart = [d for d in daily_others if chart_start <= d["date"] <= chart_end]
        trend = queries.attach_comparison(
            queries.bucket_channel_series(daily_for_chart, granularity),
            queries.bucket_channel_series(others_for_chart, granularity),
            queries.fetch_push_counts(brand, selected_ids, chart_start),
            granularity)

        if granularity == "week":
            iso = cutover_date.isocalendar()
            cutover_label = f"{iso[0]}-W{iso[1]:02d}"
        elif granularity == "month":
            cutover_label = cutover_date.strftime("%Y-%m")
        else:
            cutover_label = str(cutover_date)

        channel = {
            "trend": trend,
            "range": {"preset": range_preset, "start": chart_start, "end": chart_end, "days": span,
                      "presets": queries.RANGE_PRESETS},
            "totals": totals,
            "vs_prev": queries.compare_totals(totals, prev_totals),
            "before_after": queries.channel_before_after(daily_channel, cutover_date, window_days),
            "others_before_after": (queries.channel_before_after(daily_others, cutover_date, window_days)
                                    if daily_others else None),
            "campaign_options": queries.fetch_ai_campaign_options(brand, ai_ids),
            "selected_ids": [str(i) for i in selected_ids],
            "all_selected": len(selected_ids) == len(ai_ids),
            "campaigns_arg": ",".join(str(i) for i in selected_ids) if len(selected_ids) != len(ai_ids) else None,
            "cutover_date": cutover_date,
            "cutover_label": cutover_label,
            "window_days": window_days,
            "granularity": granularity,
            "scoped_to_showcase": brand == "Chumbak" and scope == "ai",
            "scope": scope,
            "ai_campaign_count": len(ai_ids),
            "selected_count": len(selected_ids),
            # Blinkit keeps adding sales to recent days (~30% for yesterday),
            # so the last few points are drawn as still settling.
            "settling_from": str(date.today() - timedelta(days=SETTLING_DAYS)),
            "settling_days": SETTLING_DAYS,
            "decisions_on_cutover": queries.count_ondemand_decisions_on_date(brand, cutover_date),
            "scoreboard": (queries.fetch_campaign_scoreboard(brand, selected_ids, cutover_date, window_days,
                                                             event_dates=ev_dates, days_mode=days_mode)
                           if scope == "ai" else []),
            "days_mode": days_mode,
            "events": events,
            "event_days_in_range": event_days_in_range,
            "event_suggestions": suggestions,
        }
        if channel["scoreboard"]:
            sparks = queries.fetch_campaign_roas_daily(brand, selected_ids, days=30)
            for c in channel["scoreboard"]:
                c["spark"] = queries.sparkline_points(sparks.get(str(c["cid"])))

    return render_template(
        "roas.html",
        rows=rows,
        kpi=kpi,
        daily_series=daily_series,
        since_days=since_days,
        since_date=since_date,
        verdict=verdict,
        search=search,
        brand_summary=brand_summary,
        channel=channel,
        action_dates=[d for d in action_dates
                      if d not in {str(m) for m, _ in queries.AI_MILESTONES.get(brand, [])}],
        milestones=queries.AI_MILESTONES.get(brand, []),
        verdict_after_days=queries.VERDICT_AFTER_DAYS,
        verdict_settle_days=queries.VERDICT_SETTLE_DAYS,
        active="roas",
    )


@app.route("/api/event-days", methods=["POST"])
@login_required
def api_event_days_add():
    """Marks a period as an event (festival, sale) for one brand, so Ads
    Performance can show BAU days only."""
    import event_days
    from db import get_engine
    p = request.get_json(force=True) or {}
    try:
        start = date.fromisoformat(str(p.get("start_date")))
        end = date.fromisoformat(str(p.get("end_date") or p.get("start_date")))
        new_id = event_days.add_event(get_engine(), p.get("brand") or current_brand(), p.get("name"), start, end,
                                      by=current_brand(), source=p.get("source") or "manual")
    except ValueError as exc:
        return jsonify({"error": str(exc) if "isoformat" not in str(exc) else "Dates must be YYYY-MM-DD."}), 400
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/event-days/<int:event_id>/remove", methods=["POST"])
@login_required
def api_event_days_remove(event_id):
    import event_days
    from db import get_engine
    if not event_days.remove_event(get_engine(), event_id):
        return jsonify({"error": "event not found"}), 404
    return jsonify({"ok": True})


@app.route("/roas.csv")
@login_required
def roas_csv():
    brand = current_brand()
    since_days = int(request.args.get("since_days", 30))
    verdict = request.args.get("verdict") or None
    search = request.args.get("search") or None
    cutover_arg = request.args.get("cutover")
    anchor = date.fromisoformat(cutover_arg) if cutover_arg else date.today()
    since_date = anchor - timedelta(days=since_days)
    rows = queries.fetch_ondemand_roas_impact(brand, since_date, verdict=verdict, search=search)
    columns = [
        "action_date", "Brand", "campaign_name", "targeting", "action", "current_cpm",
        "cpm_intended", "roas_before", "roas_after", "roas_change", "spend_change",
        "was_implemented", "verdict",
    ]
    return csv_response(rows, columns, f"roas_impact_{brand}.csv")


@app.route("/actions")
@login_required
def actions():
    """Pending Actions now shows on-demand history (voylla.blinkit_ondemand_actions)
    - Blinkit_actions_llm is fully retired from this page. No date picker: this
    lists recent suggestions across all campaigns, most recent first, same as
    the On-Demand AI page's default view."""
    brand = current_brand()
    session["brand"] = brand
    channel = current_channel()
    session["channel"] = channel

    cfuncs = _channel_funcs(channel)
    fetch_actions_fn = cfuncs["fetch_actions"]
    fetch_dates_fn = cfuncs["fetch_dates"]

    if brand == queries.ALL_BRANDS:
        choices = [{"brand": b, "count": len(fetch_actions_fn(b))} for b in queries.fetch_brands()]
        return render_template("actions.html", chooser=choices, active="actions", channel=channel, channels=CHANNELS)

    action_dates = fetch_dates_fn(brand)
    date_filter = request.args.get("date") or None

    all_rows = fetch_actions_fn(brand, limit=1000)
    if date_filter:
        all_rows = [r for r in all_rows if str(r["action_date"]) == date_filter]
    action_counts = Counter(r["action"] for r in all_rows)
    action_filter = request.args.get("action") or None
    rows = [r for r in all_rows if not action_filter or r["action"] == action_filter]

    return render_template(
        "actions.html",
        rows=rows,
        total_count=len(all_rows),
        action_counts=action_counts,
        action_filter=action_filter,
        action_dates=action_dates,
        date_filter=date_filter,
        channel=channel,
        channels=CHANNELS,
        action_options=queries.ACTION_OPTIONS,
        active="actions",
    )


@app.route("/api/ondemand/bulk_accept", methods=["POST"])
@login_required
def api_ondemand_bulk_accept():
    payload = request.get_json(force=True) or {}
    channel = payload.get("channel", current_channel())
    bulk_fn = _channel_funcs(channel)["bulk_accept"]
    n = bulk_fn(payload.get("unique_keys", []), payload.get("brand", current_brand()))
    return jsonify({"ok": True, "count": n})


@app.route("/impact")
@app.route("/impact.csv")
@login_required
def impact():
    """Before/After was retired on 2026-10-21: Ads Performance covers it. Old links land there."""
    return redirect(url_for("roas", brand=current_brand()))


@app.route("/api/actions/<unique_key>/accept", methods=["POST"])
@login_required
def api_accept_action(unique_key):
    payload = request.get_json(force=True) or {}
    queries.accept_action(
        unique_key,
        payload.get("brand", current_brand()),
        bool(payload.get("accept")),
        payload.get("cpm_llm_override"),
        payload.get("note"),
    )
    return jsonify({"ok": True})


@app.route("/api/actions/bulk_accept", methods=["POST"])
@login_required
def api_bulk_accept_actions():
    payload = request.get_json(force=True) or {}
    unique_keys = payload.get("unique_keys") or []
    count = queries.bulk_accept_actions(unique_keys, payload.get("brand", current_brand()))
    return jsonify({"ok": True, "count": count})


@app.route("/api/actions/<unique_key>/override", methods=["POST"])
@login_required
def api_override_action(unique_key):
    payload = request.get_json(force=True) or {}
    queries.override_action(
        unique_key,
        payload.get("brand", current_brand()),
        payload.get("override_action"),
        payload.get("cpm_change_user"),
        payload.get("note"),
    )
    return jsonify({"ok": True})


def chat_allowed():
    """Chat can read any brand, so the portal shows it only to full-access
    accounts. If the portal tells us who is asking (header named in
    PORTAL_ALL_BRANDS_HEADER, value 'false' for a brand-limited account), the
    Chat tab is hidden for them too; without that header, it shows."""
    header = os.environ.get("PORTAL_ALL_BRANDS_HEADER")
    return not (header and (request.headers.get(header) or "").strip().lower() == "false")


def _agent_counts(brand, bid_rows=None):
    """Numbers on the AI Agent tabs: suggestions and keyword ideas waiting for a person."""
    import keyword_ideas
    from db import get_engine
    counts = {}
    try:   # a badge must never take the page down (e.g. no database login)
        counts["keywords"] = sum(1 for i in keyword_ideas.list_ideas(get_engine(), brand) if i.get("status") == "pending")
    except Exception:
        pass
    if bid_rows is not None:
        counts["bids"] = sum(1 for r in bid_rows if r.get("user_implemented") in (None, "")
                             and r.get("action") not in (None, "NO_CHANGE"))
    return counts


@app.route("/ondemand")
@login_required
def ondemand():
    brand = current_brand()
    session["brand"] = brand
    channel = current_channel()
    session["channel"] = channel

    if brand == queries.ALL_BRANDS:
        return render_template("ondemand.html", chooser=queries.fetch_brands(), active="ondemand",
                                channel=channel, channels=CHANNELS)

    if request.args.get("view") == "keywords":
        import keyword_ideas
        from db import get_engine
        ideas = keyword_ideas.list_ideas(get_engine(), brand)
        return render_template("keyword_ideas.html", ideas=ideas, channel=channel, channels=CHANNELS,
                               active="ondemand", agent_tab="keywords", show_chat=chat_allowed(),
                               tab_counts={"keywords": sum(1 for i in ideas if i.get("status") == "pending")})

    campaign_filter = request.args.get("campaign_id") or None
    cfuncs = _channel_funcs(channel)
    campaigns = cfuncs["campaign_status"](brand)
    rows = cfuncs["fetch_actions"](brand, campaign_id=campaign_filter)
    campaign_label_map = {
        f"{c['campaign_name']} (#{c['campaign_id']})": str(c["campaign_id"]) for c in campaigns
    }
    automation = None
    if campaign_filter:
        automation = queries.fetch_autonomy_setting(campaign_filter, brand)
        match = next((c for c in campaigns if str(c["campaign_id"]) == str(campaign_filter)), None)
        automation["campaign_name"] = match["campaign_name"] if match else None
    return render_template(
        "ondemand.html",
        campaigns=campaigns,
        campaign_label_map=campaign_label_map,
        rows=rows,
        campaign_filter=campaign_filter,
        automation=automation,
        action_options=queries.ACTION_OPTIONS,
        channel=channel,
        channels=CHANNELS,
        active="ondemand",
        agent_tab="bids",
        show_chat=chat_allowed(),
        tab_counts=_agent_counts(brand, rows),
    )


@app.route("/api/keyword-ideas/<int:idea_id>/decide", methods=["POST"])
@login_required
def api_keyword_idea_decide(idea_id):
    """Accept (optionally with a different starting bid) or reject one idea.
    Accepting only queues it: the 2 pm push adds the keyword to the campaign."""
    import keyword_ideas
    from db import get_engine
    payload = request.get_json(force=True) or {}
    if not isinstance(payload.get("accept"), bool):
        return jsonify({"error": "accept must be true or false"}), 400
    cpm = payload.get("cpm")
    if cpm not in (None, ""):
        try:
            cpm = float(cpm)
        except (TypeError, ValueError):
            return jsonify({"error": "bid must be a number"}), 400
    else:
        cpm = None
    if not keyword_ideas.decide(get_engine(), idea_id, payload["accept"], cpm=cpm, by=current_brand()):
        return jsonify({"error": "only a pending idea can be decided"}), 400
    return jsonify({"ok": True})


@app.route("/api/keyword-ideas/find", methods=["POST"])
@login_required
def api_keyword_ideas_find():
    """Runs the daily idea finder for one brand now (one AI call)."""
    import keyword_ideas
    from db import get_engine
    brand = (request.get_json(force=True) or {}).get("brand") or current_brand()
    r = keyword_ideas.find_for_brand(get_engine(), brand)
    return jsonify({"ok": True, "brand": brand, "added": r.get("added", 0), "refreshed": r.get("refreshed", 0),
                    "candidates": r["candidates"], "relevant": r["relevant"]})


@app.route("/api/campaigns/<campaign_id>/automation", methods=["POST"])
@login_required
def api_set_automation(campaign_id):
    """The single 'auto button': turns full automation on/off for one
    campaign - delinks it from Blinkit_actions_llm (ondemand_managed) and
    sets autonomy mode to 'auto' (daily generation auto-accepts) in one
    call, or reverses both."""
    payload = request.get_json(force=True) or {}
    brand = payload.get("brand", current_brand())
    enabled = bool(payload.get("enabled"))
    tolerance_pct = payload.get("bid_tolerance_pct", 20)
    queries.set_ondemand_managed(campaign_id, brand, enabled, bid_tolerance_pct=tolerance_pct, set_by=session.get("brand"))
    queries.set_autonomy_mode(campaign_id, brand, "auto" if enabled else "semi_auto", set_by=session.get("brand"))
    swept = queries.auto_accept_pending_ondemand(campaign_id, brand, tolerance_pct=tolerance_pct) if enabled else 0
    return jsonify({"ok": True, "swept": swept})


@app.route("/api/ondemand/generate", methods=["POST"])
@login_required
def api_ondemand_generate():
    payload = request.get_json(force=True) or {}
    campaign_id = payload.get("campaign_id")
    brand = payload.get("brand", current_brand())
    channel = payload.get("channel", current_channel())
    if not campaign_id:
        return jsonify({"ok": False, "error": "campaign_id is required"}), 400

    if channel == "Instamart":
        import instamart_engine as engine_module
    elif channel == "Zepto":
        import zepto_engine as engine_module
    else:
        import ondemand_engine as engine_module

    try:
        result = engine_module.generate_ondemand_suggestions(brand, campaign_id, requested_by="dashboard")
    except Exception as exc:
        app.logger.exception("on-demand generation failed")
        return jsonify({"ok": False, "count": 0, "rows": [], "error": f"{type(exc).__name__}: {exc}"}), 500
    return jsonify(result), (200 if result["ok"] else 500)


@app.route("/api/ondemand/<unique_key>/accept", methods=["POST"])
@login_required
def api_ondemand_accept(unique_key):
    payload = request.get_json(force=True) or {}
    channel = payload.get("channel", current_channel())
    accept_fn = _channel_funcs(channel)["accept"]
    accept_fn(
        unique_key,
        payload.get("brand", current_brand()),
        bool(payload.get("accept")),
        payload.get("cpm_llm_override"),
        payload.get("note"),
    )
    return jsonify({"ok": True})


@app.route("/api/ondemand/<unique_key>/override", methods=["POST"])
@login_required
def api_ondemand_override(unique_key):
    payload = request.get_json(force=True) or {}
    channel = payload.get("channel", current_channel())
    override_fn = _channel_funcs(channel)["override"]
    override_fn(
        unique_key,
        payload.get("brand", current_brand()),
        payload.get("override_action"),
        payload.get("cpm_change_user"),
        payload.get("note"),
    )
    return jsonify({"ok": True})


@app.route("/sku-health")
@login_required
def sku_health():
    """Phase 1 of the SKU agents: one row per Voylla Blinkit SKU with sales,
    stock, price, offer, profit and campaign data, and what is wrong with it."""
    import sku_intel
    from db import get_engine
    brand = request.args.get("brand")
    if brand and brand != sku_intel.BRAND:
        # SKU data is Voylla only for now; showing it under another brand's
        # heading (the portal lists this page per brand) would be wrong data.
        return render_template("sku_health_other_brand.html", other=brand, only=sku_intel.BRAND,
                               active="sku_health")
    flag = request.args.get("flag") or None
    search = (request.args.get("q") or "").strip()
    rows = sku_intel.sku_snapshot(get_engine())
    counts = sku_intel.flag_counts(rows)
    s = search.lower()
    shown = [r for r in rows
             if (not flag or flag in r["flags"])
             and (not s or s in (r.get("sku") or "").lower() or s in (r.get("name") or "").lower()
                       or s == str(r.get("item_id") or "").lower() or s == str(r.get("pid") or "").lower())]
    shown.sort(key=lambda r: (-len(r["flags"]), -(r.get("units_30d") or 0)))
    engine = get_engine()
    ids = sorted({c for r in shown for c in str(r.get("campaign_ids") or "").split(",") if c})
    names = sku_intel.campaign_names(engine, ids)
    import sku_actions
    removals = {f"{c}|{p}": v for (c, p), v in sku_actions.open_removals(engine, sku_intel.BRAND).items()}
    return render_template("sku_health.html", rows=shown, counts=counts, labels=sku_intel.FLAG_LABELS,
                           flag=flag, search=search, total=len(rows), campaign_names=names,
                           removals=removals, active="sku_health")


@app.route("/sku-health/sku/<path:sku>")
@login_required
def sku_detail(sku):
    """One SKU: the five department reports and the boss's recommendation for a goal."""
    import sku_agents
    import sku_intel
    from db import get_engine
    brand = request.args.get("brand")
    if brand and brand != sku_intel.BRAND:
        return render_template("sku_health_other_brand.html", other=brand, only=sku_intel.BRAND,
                               active="sku_health")
    found = sku_agents.sku_reports(get_engine(), sku)
    if "row" not in found:
        return redirect(url_for("sku_health", q=sku))
    return render_template("sku_detail.html", row=found["row"], detail=found["detail"], reports=found["reports"],
                           goals=sku_agents.GOALS, actions=sku_agents.ACTIONS, active="sku_health")


@app.route("/api/sku/boss", methods=["POST"])
@login_required
def api_sku_boss():
    """One boss LLM call for a SKU and a goal. Advice only - changes nothing."""
    import sku_agents
    from db import get_engine
    payload = request.get_json(force=True) or {}
    goal = payload.get("goal")
    if goal not in sku_agents.GOALS:
        return jsonify({"error": f"goal must be one of {list(sku_agents.GOALS)}"}), 400
    try:
        out = sku_agents.recommend(get_engine(), payload.get("sku") or "", goal, requested_by="dashboard")
    except Exception as exc:
        return jsonify({"error": f"boss failed: {exc}"}), 502
    if "recommendation" not in out:
        return jsonify({"error": "SKU not found or not unique"}), 404
    import json
    return jsonify({"recommendation": json.loads(json.dumps(out["recommendation"], default=str))})


@app.route("/api/sku/removals", methods=["POST"])
@login_required
def api_sku_queue_removals():
    """Queues 'take this product out of this campaign'. The 2 pm push job applies it;
    nothing is sent to Blinkit from here."""
    import sku_actions
    import sku_intel
    from db import get_engine
    payload = request.get_json(force=True) or {}
    items = payload.get("items") or []
    if not items:
        return jsonify({"error": "nothing selected"}), 400
    engine = get_engine()
    ok, rejected = sku_actions.valid_requests(items, sku_intel.sku_snapshot(engine), sku_intel.FLAG_LABELS)
    # each removal records the product's own flags, not the page view it was clicked from
    added = sku_actions.queue_removals(engine, sku_intel.BRAND, ok, requested_by="dashboard")
    return jsonify({"ok": True, "queued": added, "already_pending": len(ok) - added, "rejected": rejected})


@app.route("/api/sku/removals/<int:removal_id>/cancel", methods=["POST"])
@login_required
def api_sku_cancel_removal(removal_id):
    import sku_actions
    from db import get_engine
    if not sku_actions.cancel_removal(get_engine(), removal_id):
        return jsonify({"error": "only a pending removal can be cancelled"}), 400
    return jsonify({"ok": True})


@app.route("/campaigns")
@login_required
def campaigns():
    brand = current_brand()
    session["brand"] = brand
    rows = queries.fetch_campaign_status(brand)

    status_counts = {}
    for r in rows:
        status_counts[r["last_status"]] = status_counts.get(r["last_status"], 0) + 1

    return render_template(
        "campaigns.html",
        rows=rows,
        status_counts=status_counts,
        active="campaigns",
    )


@app.route("/campaigns.csv")
@login_required
def campaigns_csv():
    brand = current_brand()
    rows = queries.fetch_campaign_status(brand)
    columns = [
        "brand", "campaign_id", "campaign_name", "budget", "last_status",
        "window_count", "next_start", "last_end",
    ]
    return csv_response(rows, columns, f"campaigns_{brand}.csv")


@app.route("/api/schedule/<campaign_id>")
@login_required
def api_schedule_list(campaign_id):
    entries = queries.fetch_schedule_entries(campaign_id)
    out = []
    for e in entries:
        d = dictify(e)
        st = e.get("start_time")
        et = e.get("end_time")
        d["start_hour"] = st.hour if st else 0
        d["end_hour"] = 24 if (et and et.hour == 23 and et.minute == 59) else (et.hour if et else 0)
        out.append(d)
    return jsonify({"entries": out, "pause_on_gap": queries.campaign_pauses_on_gap(entries)})


@app.route("/api/schedule/<campaign_id>/pause_on_gap", methods=["POST"])
@login_required
def api_schedule_pause_on_gap(campaign_id):
    payload = request.get_json(force=True) or {}
    if not isinstance(payload.get("value"), bool):
        return jsonify({"error": "value must be true or false"}), 400
    n = queries.set_pause_on_gap(campaign_id, payload["value"])
    return jsonify({"ok": True, "windows": n})


@app.route("/api/schedule/<campaign_id>/add", methods=["POST"])
@login_required
def api_schedule_add(campaign_id):
    payload = request.get_json(force=True) or {}
    queries.add_schedule_window(
        campaign_id,
        payload.get("campaign_name", ""),
        payload.get("brand", current_brand()),
    )
    return jsonify({"ok": True})


@app.route("/api/schedule/entry/<entry_id>/save", methods=["POST"])
@login_required
def api_schedule_save(entry_id):
    payload = request.get_json(force=True) or {}
    try:
        queries.save_schedule_window(
            entry_id,
            payload["campaign_id"],
            date.fromisoformat(payload["start_date"]),
            date.fromisoformat(payload["end_date"]),
            int(payload["start_hour"]),
            int(payload["end_hour"]),
            payload.get("budget"),
        )
    except queries.ScheduleConflict as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@app.route("/api/schedule/entry/<entry_id>/remove", methods=["POST"])
@login_required
def api_schedule_remove(entry_id):
    queries.remove_schedule_window(entry_id)
    return jsonify({"ok": True})


@app.route("/api/campaigns/<campaign_id>/mode", methods=["POST"])
@login_required
def api_set_campaign_mode(campaign_id):
    payload = request.get_json(force=True) or {}
    mode = payload.get("mode")
    brand = payload.get("brand", current_brand())
    if mode not in queries.AUTONOMY_MODES:
        return jsonify({"error": f"mode must be one of {queries.AUTONOMY_MODES}"}), 400
    queries.set_autonomy_mode(campaign_id, brand, mode, set_by=session.get("brand"))
    return jsonify({"ok": True})


@app.route("/chat")
@login_required
def chat_page():
    brand = current_brand()
    session["brand"] = brand
    threads = chat.list_threads(brand)
    thread_id = request.args.get("thread")
    if not thread_id and threads:
        thread_id = threads[0]["thread_id"]
    messages = [dict(m) for m in (chat.get_messages(thread_id) if thread_id else [])]
    for m in messages:
        calls = m.get("tool_calls")
        if isinstance(calls, str):
            try:
                calls = json.loads(calls)
            except ValueError:
                calls = None
        m["tables"] = [c["table"] for c in (calls or []) if isinstance(c, dict) and c.get("table")]
    notes = queries.fetch_notes(brand, include_stale=False, limit=50)
    return render_template(
        "chat.html", threads=threads, thread_id=thread_id, messages=messages, notes=notes, active="chat",
        agent_tab="chat", show_chat=True, channel="Blinkit", tab_counts=_agent_counts(brand),
    )


@app.route("/api/chat/threads", methods=["POST"])
@login_required
def api_chat_new_thread():
    brand = current_brand()
    thread_id = chat.create_thread(brand)
    return jsonify({"thread_id": thread_id})


@app.route("/api/chat/threads/<thread_id>/messages", methods=["GET"])
@login_required
def api_chat_messages(thread_id):
    rows = chat.get_messages(thread_id)
    return jsonify({"messages": [dictify(r) for r in rows]})


@app.route("/api/chat/threads/<thread_id>/messages", methods=["POST"])
@login_required
def api_chat_send(thread_id):
    payload = request.get_json(force=True) or {}
    message = (payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    brand = payload.get("brand", current_brand())
    try:
        reply, tool_log = chat.send_message(thread_id, brand, message)
    except Exception as exc:
        return jsonify({"error": f"chat failed: {exc}"}), 502
    return jsonify({"reply": reply, "tools_used": [t["tool"] for t in tool_log],
                    "tables": [t["table"] for t in tool_log if t.get("table")]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5056)), debug=True)
