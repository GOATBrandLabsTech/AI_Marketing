# Blinkit Marketing Control — handoff

Written 2026-09-15, for picking this up on a different PC (e.g. the shared
server the Voylla task tracker runs on). This is the single doc to hand a
fresh Claude Code session so it has full context without re-deriving
anything.

## 1. What this is

A local Flask dashboard that replaces the old Power BI report
("Blinkit Ads Report") and two Power Apps
(`Blinkit_Marketing_Automation_N_Live`, `Budget_Control_Blinkit_Live`) for
reviewing and managing the AI bid-agent's recommendations across Voylla,
Chumbak, and Petcrux on Blinkit. It grew from "move the dashboard off Power
BI" into a broader project matching 5 requirements a manager wrote on paper:

1. **Agent chat** — done
2. **Channel/Brand level** — explicitly deferred, not built
3. **Performance** — done (ROAS Impact, Before/After)
4. **Modes (auto/semi-auto/manual)** — partially done, see §6
5. **Expectation/Forecast** — done, rule-based (not ML)

Plus two unclear note items never resolved: "apart from product booster"
and "Pricing (Channel Level)" — nobody has explained what these mean yet.

## 2. Getting the code

```
Repo:   https://github.com/GOATBrandLabsTech/AI_Marketing.git
Branch: claude/blinkit-agent-roas-analysis-c4de2b
```

This branch was pushed to origin on 2026-09-15 as part of this handoff — it
was local-only before that. On the server PC:

```bash
git clone https://github.com/GOATBrandLabsTech/AI_Marketing.git
cd AI_Marketing
git checkout claude/blinkit-agent-roas-analysis-c4de2b
```

Everything web-app-related is in `webapp/`. Nothing outside that folder was
touched in this repo.

## 3. Running it (same pattern as the Voylla task tracker)

```bash
cd webapp
pip install -r requirements.txt
copy .env.example .env        # then fill in real values, see §5
python app.py                 # serves on http://localhost:5056
```

Task tracker precedent: LAN-shared Flask app, `claude code\task_tracker`,
table `voylla.team_task_tracker`, port 5055. This app follows the same
shape — `.claude/launch.json` already has a `blinkit-dashboard` entry on
port 5056 for anyone using Claude Code's preview tooling. If you want it
always-on like the task tracker (not just "run when someone opens Claude
Code"), that needs its own setup — a Windows service, a scheduled task that
starts it at boot, or `pythonw app.py` run at login. Not done yet.

**Dev-server caveat**: `app.run(debug=True)` is a development server. It
gets stopped by inactivity/app restarts in this environment repeatedly
during this project — that's expected for `debug=True`, not a bug. For a
genuinely always-on server-PC deployment, run it properly (e.g. `waitress`
or a `gunicorn`-equivalent on Windows, or at minimum `debug=False`).

## 4. Architecture

```
webapp/
  app.py        Flask routes - all pages + JSON API endpoints
  db.py         DB config loader (cred file OR env vars) + Anthropic config loader
  queries.py    All SQL - the source of truth for every number on every page
  chat.py       Agent Chat: Claude tool-calling loop, read-only tools, memory persistence
  schema.sql    CREATE TABLE for the 3 new tables this project added (idempotent)
  templates/    Jinja2 pages: base.html (shell), roas.html, actions.html,
                campaigns.html, impact.html, chat.html, login.html
  static/app.js Soft-navigation (SPA-style, no full page reload), sortable
                tables, toast/postJSON helpers
```

Pages (all under the sidebar nav):
- **ROAS Impact** (`/roas`) — per-recommendation before/after, channel-wide
  ROAS trend with day/week/month toggle, brand-vs-brand summary
- **Pending Actions** (`/actions`) — today's AI recommendations, accept/
  override, action-type filter with counts
- **Campaigns & Budget** (`/campaigns`) — live campaign status, schedule
  windows, the new Mode column
- **Before / After** (`/impact`) — spend-weighted before/after matrix,
  Expectation Met/Missed tracking, CSV export
- **Agent Chat** (`/chat`) — read-only Q&A over all the above

## 5. Config needed (`webapp/.env`, copy from `.env.example`)

| Var | Purpose | Default source |
|---|---|---|
| `DASHBOARD_PASSWORD` | gates the whole app | none - must set |
| `SECRET_KEY` | Flask session signing | none - must set |
| `WEBAPP_CRED_FILE` | Postgres creds (5 lines: host/user/pass/db/port) | `Python_Scripts\Voylla_Cred.txt` |
| `DB_HOST`/`DB_USER`/`DB_PASSWORD`/`DB_NAME`/`DB_PORT` | alt. to the cred file | env vars, for portability |
| `ANTHROPIC_KEY_FILE` | Claude key (2 lines: model, key) | `Python_Scripts\Claude_api_key.txt` — **same key the batch-decision notebooks already use** |
| `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL` | alt. to the key file | env vars, for portability |

**Hard blocker if you want this reachable from outside your office/LAN
network (e.g. Render, or any cloud host):** production Postgres
(`gbl-crawler-production-1....rds.amazonaws.com`) is not reachable from the
public internet — confirmed by a direct connection timeout from an external
sandbox. A cloud-hosted copy of this app would show connection errors on
every page unless either (a) the RDS security group is opened to that
host's IPs, or (b) you tunnel from a machine that's already on the network
(e.g. Cloudflare Tunnel — already installed on at least one of these PCs —
pointed at `localhost:5056`). Neither has been set up. This was mid-decision
when the user paused ("wait..") - pick back up there if cloud access is
still wanted.

## 6. Database — what's new vs. what already existed

Nothing pre-existing was altered. Three new tables (`webapp/schema.sql`,
already applied to production Postgres, safe to re-run — every statement is
`IF NOT EXISTS`):

- `voylla.agent_chat_threads` / `voylla.agent_chat_messages` — Agent Chat's
  memory. One row per conversation, one row per message (user or
  assistant), plus a JSONB record of which tools got called. This is the
  entire "memory" mechanism - plain Postgres, no vector DB.
- `voylla.campaign_autonomy_mode` — `(campaign_id, brand) -> mode`, one of
  `auto` / `semi_auto` / `manual`, default `semi_auto` for anything with no
  row (i.e. every campaign today). Set from the Mode dropdown on Campaigns
  & Budget.

**Mode is currently under-scoped** — the user flagged this directly: "mode
not only meant for budget it meant for all suggestions, bids, budgets
etc." Right now it only gates the CPM bid-push in `bid_implement.ipynb`
(see §7). It doesn't yet touch budget changes or campaign-level
suggestions. Not fixed yet.

## 7. Production notebook changes (⚠️ NOT in this git repo)

`Python_Scripts` is **not a git repo** — these changes are protected only
by manual timestamped `.bak_*` files sitting next to each notebook. If the
server PC has its own copy of `Python_Scripts` (rather than sharing this
PC's), **these two changes need to be manually re-applied or the files
copied over** - git alone won't carry them.

**`Blinkit_Report_Script_bid_implement.ipynb`**
(backup: `.bak_20260914_222334`)
- Added `auto_campaigns` CTE + matching CASE branches in `query3` (Cell 13):
  a campaign with `mode='auto'` in `campaign_autonomy_mode` gets its
  undecided (`user_implemented IS NULL`) rows auto-applied, using the same
  math as an accepted recommendation. A human's explicit accept/reject/
  override on a row always still wins.
- Added `is_auto_applied` column + relabeled `format_changes()` so the
  success alert email flags which lines were pushed with no human review
  ("🤖 AUTO MODE"), and the subject line gets an `(AUTO MODE)` suffix.
- **Currently inert** — zero campaigns have `mode='auto'` set.
- Tested read-only against live data (simulated one real undecided row as
  auto-mode, confirmed correct clamped CPM math) before writing.

**`Blinkit_actions_llm_marketing_Batch_Api.ipynb`**
(backup: `.bak_20260914_232901`)
- Added `MIN_LADDER_CYCLE_DAYS = 6` gate + `_days_since_last_marker()` in
  Cell 6's `resolve_cross_run_state()`: the escalation ladder (increase →
  decrease → pause) and the recheck ladder (hold → pause) now only advance
  once 6+ calendar days have passed since the last stage, not just because
  the pipeline ran again. This was the fix needed to safely run the
  pipeline daily instead of weekly — see §8.
- The deterministic tier-based rule engine itself (the ROAS/position
  thresholds) was **not** touched — it already recomputes from real rolling
  windows every run and needed no gating.
- Tested with direct unit-style scenarios against the extracted function
  (fresh decision, 1-day-old marker → holds, 7-day-old marker → advances) -
  all passed.

Both notebooks: only the cells named above changed. Every other cell
verified byte-identical to its backup before/after.

## 8. What's live vs. what's still switched off

Nothing above runs differently yet. Two manual steps outstanding, both
explicitly the user's to do:

1. **Cadence** — the weekly trigger's location was never found (no Task
   Scheduler export or wrapper script in `Python_Scripts` names either
   notebook). Needs to be changed to daily in Windows Task Scheduler
   directly.
2. **Auto mode** — set per-campaign from Campaigns & Budget. Confirming it
   works means watching one real scheduled run closely, ideally on one
   low-budget campaign first.

## 9. Not built yet (real scope, not forgotten)

- **On-demand suggestion generation** — user wants a way to ask for a fresh
  AI recommendation right now (e.g. from chat) instead of waiting for the
  scheduled run. Investigated thoroughly (the rule engine is a clean,
  reusable per-keyword function; the Batch API the notebook uses is
  async/slow and wrong for this - would need the synchronous API instead,
  writing to a separate table so on-demand runs don't collide with the
  scheduled batch's rows). Not implemented.
- **Mode enforcement beyond bids** — budget changes, campaign-level
  suggestions. See §6.
- **Render / public hosting** — blocked on the DB-reachability question in
  §5, paused mid-decision.
- The two unclear manager-note items (§1).

## 10. Picking this up in a new session

Give the new Claude Code session this file. It has everything: what's
built, what's live vs. inert, where the risk is, and what's next. The git
branch has the full commit-by-commit history if more detail on any specific
feature is needed (`git log --oneline`).
