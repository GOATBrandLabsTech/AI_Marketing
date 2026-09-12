import os
from datetime import date, datetime, time
from decimal import Decimal
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

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
    return request.args.get("brand") or session.get("brand") or "Voylla"


@app.context_processor
def inject_globals():
    if not session.get("authed"):
        return {}
    try:
        brands = queries.fetch_brands()
    except Exception:
        brands = ["Voylla", "Chumbak", "Petcrux"]
    return {"brands": brands, "brand": current_brand()}


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

    rows = queries.fetch_roas_impact(brand, since_days=since_days, verdict=verdict, search=search)
    scoreable = [r for r in rows if r["verdict"]]
    changes = [float(r["roas_change"]) for r in scoreable if r["roas_change"] is not None]
    kpi = {
        "total": len(rows),
        "scoreable": len(scoreable),
        "improved": sum(1 for r in scoreable if r["verdict"] == "IMPROVED"),
        "worsened": sum(1 for r in scoreable if r["verdict"] == "WORSENED"),
        "flat": sum(1 for r in scoreable if r["verdict"] == "FLAT"),
        "avg_roas_change": (sum(changes) / len(changes)) if changes else None,
    }
    since_date = date.today().fromordinal(date.today().toordinal() - since_days)
    return render_template(
        "roas.html",
        rows=rows,
        kpi=kpi,
        since_days=since_days,
        since_date=since_date,
        verdict=verdict,
        search=search,
        active="roas",
    )


@app.route("/actions")
@login_required
def actions():
    brand = current_brand()
    session["brand"] = brand
    action_date = queries.fetch_latest_action_date(brand)
    rows = queries.fetch_pending_actions(brand, action_date) if action_date else []
    return render_template(
        "actions.html",
        rows=rows,
        action_date=action_date,
        action_options=queries.ACTION_OPTIONS,
        active="actions",
    )


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


@app.route("/campaigns")
@login_required
def campaigns():
    brand = current_brand()
    session["brand"] = brand
    rows = queries.fetch_campaign_status(brand)
    return render_template("campaigns.html", rows=rows, active="campaigns")


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5056)), debug=True)
