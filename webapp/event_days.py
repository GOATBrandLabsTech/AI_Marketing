"""Event days vs BAU (business-as-usual) days.

A festival or sale lifts every campaign (Voylla's Raksha Bandhan week ran at
~10x normal sales), so a before/after that mixes event days with normal days
measures the calendar, not the AI. Event periods are kept per brand in
voylla.blinkit_event_days; Ads Performance can then show BAU days only, event
days only, or all. `suggest_events` finds days that look like an event (sales
far above the previous 4 weeks' normal) for someone to confirm.
"""
from datetime import date, timedelta
from statistics import median

from sqlalchemy import text

MODES = ("bau", "event", "all")
DEFAULT_MODE = "bau"
SPIKE_RATIO = 1.6          # a day at 1.6x the previous 4 weeks' median sales looks like an event
SPIKE_LOOKBACK = 28
SPIKE_MIN_HISTORY = 14

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS voylla.blinkit_event_days (
    id          serial PRIMARY KEY,
    brand       text NOT NULL,
    name        text NOT NULL,
    start_date  date NOT NULL,
    end_date    date NOT NULL,
    source      text NOT NULL DEFAULT 'manual',   -- manual / suggested
    created_by  text,
    created_at  timestamp NOT NULL DEFAULT NOW()
)"""

# The clear ones in the data on 2026-10-23 (sales at 3-10x normal), loaded once.
SEED = [
    ("Voylla", "Raksha Bandhan", date(2026, 8, 20), date(2026, 8, 29)),
    ("Voylla", "Independence Day sale", date(2026, 8, 15), date(2026, 8, 15)),
    ("Voylla", "Navratri", date(2026, 10, 10), date(2026, 10, 19)),
    ("Chumbak", "Raksha Bandhan", date(2026, 8, 24), date(2026, 8, 30)),
    ("Chumbak", "Independence Day sale", date(2026, 8, 15), date(2026, 8, 16)),
]


# ── pure helpers (tested) ───────────────────────────────────────────────────

def dates_of(events):
    """Every calendar date covered by the events."""
    out = set()
    for e in events:
        d, end = e["start_date"], e["end_date"]
        while d <= end:
            out.add(d)
            d += timedelta(days=1)
    return out


def apply_mode(daily, event_dates, mode):
    """Rows (dicts with a 'date') kept for the chosen view."""
    if mode == "all":
        return list(daily)
    if mode == "event":
        return [r for r in daily if r["date"] in event_dates]
    return [r for r in daily if r["date"] not in event_dates]


def suggest_events(daily, known_dates, ratio=SPIKE_RATIO, lookback=SPIKE_LOOKBACK):
    """Runs of days whose sales are >= ratio x the median of the previous
    `lookback` days, that no event covers yet. daily: rows with date and sales,
    any order. Returns [{start_date, end_date, days, peak}] newest first."""
    rows = sorted((r for r in daily if r.get("sales") is not None), key=lambda r: r["date"])
    hot = []
    for i, r in enumerate(rows):
        prev = [float(x["sales"]) for x in rows[max(0, i - lookback):i]]
        if len(prev) < SPIKE_MIN_HISTORY:
            continue
        base = median(prev)
        if base > 0 and float(r["sales"]) >= ratio * base and r["date"] not in known_dates:
            hot.append((r["date"], round(float(r["sales"]) / base, 1)))
    runs = []
    for d, x in hot:
        if runs and (d - runs[-1]["end_date"]).days <= 2:
            runs[-1].update(end_date=d, peak=max(runs[-1]["peak"], x))
        else:
            runs.append({"start_date": d, "end_date": d, "peak": x})
    for r in runs:
        r["days"] = (r["end_date"] - r["start_date"]).days + 1
    return sorted(runs, key=lambda r: r["start_date"], reverse=True)


def clean_mode(mode):
    return mode if mode in MODES else DEFAULT_MODE


# ── storage ─────────────────────────────────────────────────────────────────

def ensure_table(engine):
    with engine.begin() as conn:
        conn.execute(text(TABLE_DDL))


def seed(engine):
    """Loads SEED once (only into an empty table)."""
    ensure_table(engine)
    with engine.begin() as conn:
        if conn.execute(text("SELECT COUNT(*) FROM voylla.blinkit_event_days")).scalar():
            return 0
        for brand, name, a, b in SEED:
            conn.execute(text("""INSERT INTO voylla.blinkit_event_days (brand, name, start_date, end_date, source, created_by)
                                 VALUES (:b, :n, :s, :e, 'manual', 'seed 2026-10-23')"""),
                         {"b": brand, "n": name, "s": a, "e": b})
    return len(SEED)


def list_events(engine, brand):
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""SELECT id, brand, name, start_date, end_date, source FROM voylla.blinkit_event_days
                                        WHERE brand = :b ORDER BY start_date DESC"""), {"b": brand}).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []


def add_event(engine, brand, name, start_date, end_date, by=None, source="manual"):
    name = (name or "").strip()[:80]
    if not name:
        raise ValueError("Give the event a name.")
    if end_date < start_date:
        raise ValueError("The event ends before it starts.")
    if (end_date - start_date).days > 45:
        raise ValueError("An event longer than 45 days is not an event - check the dates.")
    ensure_table(engine)
    with engine.begin() as conn:
        return conn.execute(text("""INSERT INTO voylla.blinkit_event_days (brand, name, start_date, end_date, source, created_by)
                                    VALUES (:b, :n, :s, :e, :src, :by) RETURNING id"""),
                            {"b": brand, "n": name, "s": start_date, "e": end_date, "src": source, "by": by}).scalar()


def remove_event(engine, event_id):
    with engine.begin() as conn:
        return conn.execute(text("DELETE FROM voylla.blinkit_event_days WHERE id = :i"), {"i": int(event_id)}).rowcount == 1
