import csv
import io
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
        action_dates = queries.fetch_action_dates(brand)
        default_cutover = date.fromisoformat(action_dates[0]) if action_dates else (date.today() - timedelta(days=1))
        cutover_arg = request.args.get("cutover")
        cutover_date = date.fromisoformat(cutover_arg) if cutover_arg else default_cutover

    since_date = (cutover_date or date.today()) - timedelta(days=since_days)

    all_rows = queries.fetch_roas_impact(brand, since_date, search=search)
    scoreable = [r for r in all_rows if r["verdict"]]
    changes = [float(r["roas_change"]) for r in scoreable if r["roas_change"] is not None]
    kpi = {
        "total": len(all_rows),
        "scoreable": len(scoreable),
        "improved": sum(1 for r in scoreable if r["verdict"] == "IMPROVED"),
        "worsened": sum(1 for r in scoreable if r["verdict"] == "WORSENED"),
        "flat": sum(1 for r in scoreable if r["verdict"] == "FLAT"),
        "avg_roas_change": (sum(changes) / len(changes)) if changes else None,
    }

    daily = {}
    for r in scoreable:
        d = str(r["action_date"])
        bucket = daily.setdefault(d, {"date": d, "improved": 0, "worsened": 0, "flat": 0})
        bucket[r["verdict"].lower()] += 1
    daily_series = [daily[d] for d in sorted(daily.keys())]

    rows = [r for r in all_rows if not verdict or r["verdict"] == verdict]

    brand_summary = queries.fetch_brand_summary(all_rows) if brand == queries.ALL_BRANDS else None

    if brand != queries.ALL_BRANDS:
        window_days = int(request.args.get("window_days", 7))
        granularity = request.args.get("granularity", "day")
        campaign_ids = queries.CHUMBAK_SHOWCASE_CAMPAIGN_IDS if brand == "Chumbak" else None

        daily_channel = queries.fetch_channel_daily(brand, days=365, campaign_ids=campaign_ids)
        if granularity == "day":
            # a year of individual days is illegible as a line chart - zoom
            # the day view around the cutover; week/month keep the full year
            # since bucketing already compresses it.
            chart_start = cutover_date - timedelta(days=max(30, window_days * 3))
            daily_for_chart = [d for d in daily_channel if chart_start <= d["date"]]
        else:
            daily_for_chart = daily_channel

        if granularity == "week":
            iso = cutover_date.isocalendar()
            cutover_label = f"{iso[0]}-W{iso[1]:02d}"
        elif granularity == "month":
            cutover_label = cutover_date.strftime("%Y-%m")
        else:
            cutover_label = str(cutover_date)

        channel = {
            "trend": queries.bucket_channel_series(daily_for_chart, granularity),
            "before_after": queries.channel_before_after(daily_channel, cutover_date, window_days),
            "cutover_date": cutover_date,
            "cutover_label": cutover_label,
            "window_days": window_days,
            "granularity": granularity,
            "scoped_to_showcase": bool(campaign_ids),
        }

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
        action_dates=action_dates,
        active="roas",
    )


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
    rows = queries.fetch_roas_impact(brand, since_date, verdict=verdict, search=search)
    columns = [
        "action_date", "Brand", "campaign_name", "targeting", "action", "current_cpm",
        "cpm_intended", "roas_before", "roas_after", "roas_change", "spend_change",
        "was_implemented", "verdict",
    ]
    return csv_response(rows, columns, f"roas_impact_{brand}.csv")


@app.route("/actions")
@login_required
def actions():
    brand = current_brand()
    session["brand"] = brand

    if brand == queries.ALL_BRANDS:
        choices = []
        for b in queries.fetch_brands():
            d = queries.fetch_latest_action_date(b)
            count = len(queries.fetch_pending_actions(b, d)) if d else 0
            choices.append({"brand": b, "action_date": d, "count": count})
        return render_template("actions.html", chooser=choices, active="actions")

    action_dates = queries.fetch_action_dates(brand)
    requested_date = request.args.get("date")
    action_date = requested_date or (action_dates[0] if action_dates else None)
    all_rows = queries.fetch_pending_actions(brand, action_date) if action_date else []

    action_counts = Counter(r["action"] for r in all_rows)
    action_filter = request.args.get("action") or None
    rows = [r for r in all_rows if not action_filter or r["action"] == action_filter]

    return render_template(
        "actions.html",
        rows=rows,
        total_count=len(all_rows),
        action_counts=action_counts,
        action_filter=action_filter,
        action_date=action_date,
        action_dates=action_dates,
        action_options=queries.ACTION_OPTIONS,
        active="actions",
    )


@app.route("/impact")
@login_required
def impact():
    brand = current_brand()
    session["brand"] = brand

    if brand == queries.ALL_BRANDS:
        return render_template("impact.html", chooser=queries.fetch_brands(), active="impact")

    default_cutover = queries.CHUMBAK_SHOWCASE_GO_LIVE if brand == "Chumbak" else (date.today() - timedelta(days=1))
    cutover = request.args.get("cutover")
    cutover_date = date.fromisoformat(cutover) if cutover else default_cutover
    pre_days = int(request.args.get("pre_days", 7))
    min_spend = float(request.args.get("min_spend", 60))
    campaign_ids = queries.CHUMBAK_SHOWCASE_CAMPAIGN_IDS if brand == "Chumbak" and not request.args.get("all_campaigns") else None

    rows, summary = queries.fetch_before_after_matrix(
        brand, cutover_date, pre_days=pre_days, min_spend=min_spend, campaign_ids=campaign_ids
    )
    return render_template(
        "impact.html",
        rows=rows,
        summary=summary,
        cutover_date=cutover_date,
        pre_days=pre_days,
        min_spend=min_spend,
        scoped_to_showcase=bool(campaign_ids),
        active="impact",
    )


@app.route("/impact.csv")
@login_required
def impact_csv():
    brand = current_brand()
    default_cutover = queries.CHUMBAK_SHOWCASE_GO_LIVE if brand == "Chumbak" else (date.today() - timedelta(days=1))
    cutover = request.args.get("cutover")
    cutover_date = date.fromisoformat(cutover) if cutover else default_cutover
    pre_days = int(request.args.get("pre_days", 7))
    min_spend = float(request.args.get("min_spend", 60))
    campaign_ids = queries.CHUMBAK_SHOWCASE_CAMPAIGN_IDS if brand == "Chumbak" and not request.args.get("all_campaigns") else None
    rows, _ = queries.fetch_before_after_matrix(
        brand, cutover_date, pre_days=pre_days, min_spend=min_spend, campaign_ids=campaign_ids
    )
    columns = [
        "campaign_id", "campaign_name", "targeting", "action", "spend_before", "spend_after",
        "roas_before", "roas_after", "roas_delta", "verdict", "meaningful",
        "expectation", "expectation_reason",
    ]
    return csv_response(rows, columns, f"before_after_{brand}_{cutover_date}.csv")


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


@app.route("/ondemand")
@login_required
def ondemand():
    brand = current_brand()
    session["brand"] = brand

    if brand == queries.ALL_BRANDS:
        return render_template("ondemand.html", chooser=queries.fetch_brands(), active="ondemand")

    campaign_filter = request.args.get("campaign_id") or None
    campaigns = queries.fetch_campaign_status(brand)
    rows = queries.fetch_ondemand_actions(brand, campaign_id=campaign_filter)
    campaign_label_map = {
        f"{c['campaign_name']} (#{c['campaign_id']})": str(c["campaign_id"]) for c in campaigns
    }
    return render_template(
        "ondemand.html",
        campaigns=campaigns,
        campaign_label_map=campaign_label_map,
        rows=rows,
        campaign_filter=campaign_filter,
        action_options=queries.ACTION_OPTIONS,
        active="ondemand",
    )


@app.route("/api/ondemand/generate", methods=["POST"])
@login_required
def api_ondemand_generate():
    payload = request.get_json(force=True) or {}
    campaign_id = payload.get("campaign_id")
    brand = payload.get("brand", current_brand())
    if not campaign_id:
        return jsonify({"ok": False, "error": "campaign_id is required"}), 400

    import ondemand_engine

    try:
        result = ondemand_engine.generate_ondemand_suggestions(brand, campaign_id, requested_by="dashboard")
    except Exception as exc:
        app.logger.exception("on-demand generation failed")
        return jsonify({"ok": False, "count": 0, "rows": [], "error": f"{type(exc).__name__}: {exc}"}), 500
    return jsonify(result), (200 if result["ok"] else 500)


@app.route("/api/ondemand/<unique_key>/accept", methods=["POST"])
@login_required
def api_ondemand_accept(unique_key):
    payload = request.get_json(force=True) or {}
    queries.accept_ondemand_action(
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
    queries.override_ondemand_action(
        unique_key,
        payload.get("brand", current_brand()),
        payload.get("override_action"),
        payload.get("cpm_change_user"),
        payload.get("note"),
    )
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
    return jsonify({"entries": out})


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
    messages = chat.get_messages(thread_id) if thread_id else []
    return render_template(
        "chat.html", threads=threads, thread_id=thread_id, messages=messages, active="chat"
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
    return jsonify({"reply": reply, "tools_used": [t["tool"] for t in tool_log]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5056)), debug=True)
