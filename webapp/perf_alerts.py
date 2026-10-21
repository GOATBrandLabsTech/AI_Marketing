"""Daily alert for campaigns NOT on Auto that are doing badly.

Runs after the 12:30 Auto run (server cron, 1:15 pm IST):
  1. Flags non-Auto campaigns whose last-7-day ROAS is below ROAS_RATIO x
     their brand's ROAS over the same days, on at least MIN_SPEND. Comparing
     with the brand over the same days cancels out Blinkit's late sales and
     seasonal swings.
  2. Asks the AI (ALERT_MODEL) for bid suggestions on the worst MAX_PER_DAY.
     They are saved as PENDING (semi-auto: nothing is accepted or pushed
     without a human) - this module never changes a campaign's mode.
  3. Emails the list. A campaign whose suggestions are still unanswered after
     REMIND_AFTER_DAYS goes into a reminder section instead of being asked
     again; after REFRESH_AFTER_DAYS the suggestions are regenerated, since
     the bids they start from are out of date by then.

Call `run()`; `run(send=False, generate=False, log=False)` only reports (no AI, no mail, no writes).
"""
import os
import smtplib
from datetime import date, timedelta
from email.message import EmailMessage

from sqlalchemy import bindparam, text

ROAS_RATIO = 0.6
MIN_SPEND = 2000
WINDOW_DAYS = 7
MAX_PER_DAY = 15
REMIND_AFTER_DAYS = 2
REFRESH_AFTER_DAYS = 7
REVIEWED_COOLDOWN_DAYS = 3      # someone answered recently: do not ask the AI again yet
ALERT_MODEL = "claude-sonnet-5-5"
REQUESTED_BY = "perf_alert"
BRANDS = ["Voylla", "Chumbak", "Petcrux"]
ACTIONABLE = ("INCREASE_CPM", "DECREASE_CPM", "PAUSE")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://reports.goatbrandlabs.in/s/marketing")
PYTHON_SCRIPTS_DIR = r"C:\Users\Amit Singh\Documents\Python_Scripts"


# ── data ────────────────────────────────────────────────────────────────────

def ensure_log_table(engine):
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS voylla.blinkit_perf_alert_log (
                run_date      date NOT NULL,
                brand         text NOT NULL,
                campaign_id   text NOT NULL,
                campaign_name text,
                mode          text,
                spend_7d      numeric,
                roas_7d       numeric,
                brand_roas_7d numeric,
                status        text,          -- new / reminder / waiting / reviewed / failed
                n_changes     integer,
                note          text,
                PRIMARY KEY (run_date, brand, campaign_id)
            )"""))


def fetch_window(engine, brand, start, end):
    """Per campaign spend and sales over [start, end], with mode and last status."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            WITH a AS (
                SELECT "Campaign ID"::text AS campaign_id, MAX("Campaign Name") AS campaign_name,
                       SUM("Estimated Budget Consumed") AS spend, SUM("Direct Sales" + "Indirect Sales") AS sales
                FROM voylla."Blinkit_Ads_Report"
                WHERE "Brand" = :brand
                  AND TO_TIMESTAMP("Date", 'YYYY-MM-DD HH24:MI:SS')::date BETWEEN :start AND :end
                GROUP BY 1),
            rt AS (SELECT DISTINCT ON (campaign_id) campaign_id::text AS campaign_id, last_status
                   FROM voylla."Blinkit_Campaign_Runtime" ORDER BY campaign_id, log_date DESC)
            SELECT a.*, COALESCE(m.mode, 'not set') AS mode, rt.last_status
            FROM a
            LEFT JOIN voylla.campaign_autonomy_mode m ON m.campaign_id::text = a.campaign_id AND m.brand = :brand
            LEFT JOIN rt ON rt.campaign_id = a.campaign_id"""), {"brand": brand, "start": start, "end": end}).mappings().all()
    return [dict(r, spend=float(r["spend"] or 0), sales=float(r["sales"] or 0)) for r in rows]


def fetch_alert_state(engine, brand):
    """Per campaign: oldest unanswered actionable perf_alert suggestion date,
    and the last date someone answered one (accepted or rejected)."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            WITH latest AS (
                SELECT DISTINCT ON (campaign_id, targeting) campaign_id, action_date::date AS d,
                       action, user_implemented, implementation_date
                FROM voylla.blinkit_ondemand_actions
                WHERE "Brand" = :brand AND requested_by = :rb
                ORDER BY campaign_id, targeting, requested_at DESC)
            SELECT campaign_id,
                   MIN(d) FILTER (WHERE user_implemented IS NULL AND action IN :acts) AS pending_since,
                   COUNT(*) FILTER (WHERE user_implemented IS NULL AND action IN :acts) AS n_pending,
                   MAX(implementation_date) FILTER (WHERE user_implemented IS NOT NULL) AS last_reviewed
            FROM latest GROUP BY 1""").bindparams(bindparam("acts", expanding=True)),
            {"brand": brand, "rb": REQUESTED_BY, "acts": list(ACTIONABLE)}).mappings().all()
    return {r["campaign_id"]: dict(r) for r in rows}


# ── decisions (pure, tested) ────────────────────────────────────────────────

def flag_underperformers(rows, ratio=ROAS_RATIO, min_spend=MIN_SPEND):
    """Non-Auto, not STOPPED campaigns with spend >= min_spend and ROAS below
    ratio x the brand's ROAS (all the brand's campaigns, same days). Worst
    first by sales missed against the brand average. Returns (flagged, brand_roas)."""
    spend = sum(r["spend"] for r in rows)
    brand_roas = sum(r["sales"] for r in rows) / spend if spend else None
    if not brand_roas:
        return [], brand_roas
    out = []
    for r in rows:
        if r["mode"] == "auto" or (r.get("last_status") or "") == "STOPPED" or r["spend"] < min_spend:
            continue
        roas = r["sales"] / r["spend"]
        if roas < ratio * brand_roas:
            out.append(dict(r, roas=round(roas, 2), brand_roas=round(brand_roas, 2),
                            missed_sales=r["spend"] * brand_roas - r["sales"]))
    out.sort(key=lambda r: -r["missed_sales"])
    return out, brand_roas


def decide(campaign, state, today):
    """What to do with one flagged campaign: 'new' (ask the AI), 'reminder'
    (unanswered >= REMIND_AFTER_DAYS), 'waiting' (unanswered, too soon to nag),
    or 'reviewed' (someone answered within the cooldown)."""
    st = state.get(campaign["campaign_id"]) or {}
    since = st.get("pending_since")
    if since is not None and st.get("n_pending"):
        age = (today - since).days
        if age >= REFRESH_AFTER_DAYS:
            return "new", age
        return ("reminder" if age >= REMIND_AFTER_DAYS else "waiting"), age
    reviewed = st.get("last_reviewed")
    if reviewed is not None and (today - reviewed).days < REVIEWED_COOLDOWN_DAYS:
        return "reviewed", (today - reviewed).days
    return "new", 0


# ── email ───────────────────────────────────────────────────────────────────

def _mail_config():
    """ALERT_EMAIL / ALERT_EMAIL_PASSWORD / ALERT_SENDER_NAME on the server,
    else the PC's Automationalert_emailid_pass.txt (email, app password, name)."""
    if os.environ.get("ALERT_EMAIL"):
        return (os.environ["ALERT_EMAIL"], os.environ["ALERT_EMAIL_PASSWORD"],
                os.environ.get("ALERT_SENDER_NAME", "Analytics Alerts"))
    with open(os.path.join(PYTHON_SCRIPTS_DIR, "Automationalert_emailid_pass.txt"), encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]
    return lines[0], lines[1], lines[2]


def send_email(subject, html, to=None):
    user, password, name = _mail_config()
    to = to or os.environ.get("ALERT_TO", "amit.singh@voylla.com")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"{name} <{user}>", to
    msg.set_content("This alert is HTML; open it in a mail client that shows HTML.")
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)


def _inr(v):
    return f"&#8377;{v:,.0f}"


def build_email(report, today, keyword_ideas_waiting=0):
    """(subject, html) or None when there is nothing to say."""
    flagged = [c for b in report for c in b["campaigns"]]
    if not flagged:
        return None
    reminders = [c for c in flagged if c["status"] == "reminder"]
    n_new = sum(1 for c in flagged if c["status"] == "new")
    subject = (f"Blinkit: {len(flagged)} campaign(s) failed the ROAS check"
               + (f", {len(reminders)} with AI suggestions still not accepted" if reminders else "")
               + f" ({today:%d %b})")

    def table(rows):
        body = "".join(
            f"<tr><td>{c['brand']}</td><td>{c['campaign_name']}<br><small>#{c['campaign_id']} &middot; {c['mode']}</small></td>"
            f"<td align=right>{_inr(c['spend'])}</td><td align=right><b>{c['roas']:.2f}</b></td>"
            f"<td align=right>{c['brand_roas']:.2f}</td><td>{c['what']}</td></tr>"
            for c in rows)
        return ("<table border=1 cellpadding=6 cellspacing=0 style='border-collapse:collapse;font:13px Arial'>"
                "<tr style='background:#f0f0f0'><th>Brand</th><th>Campaign</th><th>Spend 7d</th><th>ROAS 7d</th>"
                f"<th>Brand ROAS 7d</th><th>AI</th></tr>{body}</table>")

    for c in flagged:
        if c["status"] == "new":
            c["what"] = (f"{c['n_changes']} bid change(s) suggested, waiting for your accept" if c["n_changes"]
                         else "AI suggests no bid change" if c.get("ok", True)
                         else f"AI suggestion failed: {c.get('note') or 'unknown'}")
        elif c["status"] == "reminder":
            c["what"] = f"<b>{c['n_pending']} suggestion(s) not accepted for {c['age']} days</b>"
        elif c["status"] == "not_sent":
            c["what"] = f"not sent to the AI today ({c['note']})"
        elif c["status"] == "waiting":
            c["what"] = f"{c['n_pending']} suggestion(s) waiting since {c['age']} day(s)"
        else:
            c["what"] = f"reviewed {c['age']} day(s) ago"

    parts = [f"<p style='font:14px Arial'>Campaigns not on Auto whose ROAS over the last {WINDOW_DAYS} days "
             f"({report[0]['start']:%d %b} &ndash; {report[0]['end']:%d %b}) is below {int(ROAS_RATIO * 100)}% of "
             f"their brand's ROAS, with at least {_inr(MIN_SPEND)} spent. The AI's bid suggestions are saved as "
             f"<b>pending</b>: nothing reaches Blinkit until you accept them (AI Agent → Bid suggestions), and the 2 pm push "
             f"then sends them. Suggestions not accepted within {REFRESH_AFTER_DAYS} days are replaced with fresh ones.</p>"]
    if reminders:
        parts.append("<h3 style='font:bold 15px Arial'>Still not accepted</h3>" + table(reminders))
    others = [c for c in flagged if c["status"] != "reminder"]
    if others:
        parts.append(f"<h3 style='font:bold 15px Arial'>Failed the ROAS check today ({n_new} sent to the AI)</h3>"
                     + table(others))
    if keyword_ideas_waiting:
        parts.append(f"<p style='font:14px Arial'><b>{keyword_ideas_waiting} new keyword idea(s)</b> are waiting for "
                     "you on AI Agent &rarr; New keywords (untargeted searches that are growing).</p>")
    parts.append(f"<p style='font:13px Arial'>Open the dashboard: <a href='{DASHBOARD_URL}'>{DASHBOARD_URL}</a>. "
                 "If a campaign keeps showing up, consider switching it to Auto (only you can do that).</p>")
    skipped = [f"{b['brand']}: {b['skipped']}" for b in report if b.get("skipped")]
    if skipped:
        parts.append("<p style='font:12px Arial;color:#666'>Not checked today: " + "; ".join(skipped) + "</p>")
    return subject, "".join(parts)


# ── the job ─────────────────────────────────────────────────────────────────

def run(today=None, send=True, generate=True, brands=BRANDS, log=True):
    import ondemand_engine as oe
    from db import get_engine

    today = today or date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    engine = get_engine()
    if log:
        ensure_log_table(engine)

    report = []
    for brand in brands:
        entry = {"brand": brand, "start": start, "end": end, "campaigns": []}
        report.append(entry)
        problem = oe.data_freshness_problem(oe._brand_last_report_day(engine, brand), today)
        if problem:
            entry["skipped"] = problem
            print(f"[perf] {brand}: SKIPPED - {problem}")
            continue
        flagged, brand_roas = flag_underperformers(fetch_window(engine, brand, start, end))
        state = fetch_alert_state(engine, brand)
        print(f"[perf] {brand}: brand ROAS {brand_roas and round(brand_roas, 2)}, {len(flagged)} flagged")
        for c in flagged:
            c["brand"] = brand
            c["status"], c["age"] = decide(c, state, today)
            c["n_pending"] = int((state.get(c["campaign_id"]) or {}).get("n_pending") or 0)
            c["n_changes"], c["note"] = 0, None
            entry["campaigns"].append(c)

    # The daily AI budget goes to the worst campaigns across all brands, not brand by brand.
    to_ask = sorted((c for b in report for c in b["campaigns"] if c["status"] == "new"),
                    key=lambda c: -c["missed_sales"])
    for i, c in enumerate(to_ask):
        if i >= MAX_PER_DAY or not generate:
            c["status"], c["note"] = "not_sent", ("daily AI limit reached" if generate else "report only")
            continue
        try:
            res = oe.generate_ondemand_suggestions(c["brand"], c["campaign_id"], requested_by=REQUESTED_BY,
                                                   model_override=ALERT_MODEL)
            c["ok"], c["note"] = res["ok"], res.get("error")
            c["n_changes"] = sum(1 for r in res.get("rows") or [] if r.get("action") in ACTIONABLE)
        except Exception as exc:  # one bad campaign must not stop the alert
            c["ok"], c["note"] = False, str(exc)[:200]
        print(f"[perf] {c['brand']} #{c['campaign_id']}: {c['n_changes']} change(s) {c['note'] or ''}")

    if log:
        for b in report:
            for c in b["campaigns"]:
                _log(engine, today, c)

    waiting = 0
    if generate:
        # New keyword ideas for every brand, once a day, after the alerts (never stops the alert)
        try:
            import keyword_ideas
            keyword_ideas.run(brands=tuple(brands))
            waiting = sum(1 for b in brands for i in keyword_ideas.list_ideas(engine, b) if i["status"] == "pending")
        except Exception as exc:
            print(f"[perf] keyword ideas failed: {exc}")
    mail = build_email(report, today, keyword_ideas_waiting=waiting)
    if mail and send:
        send_email(*mail)
        print(f"[perf] email sent: {mail[0]}")
    elif not mail:
        print("[perf] nothing flagged, no email")
    return report


def _log(engine, today, c):
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO voylla.blinkit_perf_alert_log
                (run_date, brand, campaign_id, campaign_name, mode, spend_7d, roas_7d, brand_roas_7d, status, n_changes, note)
            VALUES (:d, :brand, :cid, :name, :mode, :spend, :roas, :broas, :status, :n, :note)
            ON CONFLICT (run_date, brand, campaign_id) DO UPDATE SET status = EXCLUDED.status,
                n_changes = EXCLUDED.n_changes, note = EXCLUDED.note, roas_7d = EXCLUDED.roas_7d"""),
            {"d": today, "brand": c["brand"], "cid": c["campaign_id"], "name": c["campaign_name"], "mode": c["mode"],
             "spend": c["spend"], "roas": c["roas"], "broas": c["brand_roas"],
             "status": "failed" if c.get("ok") is False else c["status"], "n": c["n_changes"], "note": c["note"]})
