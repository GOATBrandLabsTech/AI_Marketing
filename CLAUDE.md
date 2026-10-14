# Marketing Control - notes for Claude Code

Flask dashboard + AI bid engine for quick-commerce ads (Blinkit first; Instamart, Zepto engines exist).
Owner: Amit (Analytics). Explain things plainly, explain before asking for approval, fix root causes.

## How code goes live (do not bypass)
- `main` is the only deploy branch. Work on a feature branch, push, Amit opens/merges the PR.
- CI (`.github/workflows/ci.yml`) runs `webapp/tests` on every push/PR. The Lightsail server's
  webhook deploy runs the same tests before restarting and rolls back on failure. No server keys in GitHub.
- Daily jobs (details in `jobs/README.md`): 12:30 pm IST suggestions = Lightsail ubuntu crontab
  (`0 7 * * *` UTC) calling `oe.run_auto_campaigns()` in the deploy checkout, log
  `/home/ubuntu/ondemand-daily.log`; 2:00 pm IST Blinkit push = PC Task Scheduler running
  `jobs/Blinkit_Ondemand_Bid_Push.ipynb` from a clone that resets to `origin/main`. The cron line must
  never list campaign ids - the Auto switch is the only list (until 2026-10-13 it hard-coded two ids).
  Credentials live outside git (`Python_Scripts` on the PC, `.env.production` on the server).
- Notebooks in `jobs/` are committed with no outputs (a test checks this).
- A NEW dashboard page is invisible on reports.goatbrandlabs.in/s/marketing until it is added to `PAGES` in
  Amal's portal (`/opt/dsr-portal/src/reports/marketing/index.js` on Lightsail, runs as `dsr`, restart with
  `sudo systemctl restart dsr-portal`). The portal hides our sidebar and 404s any path not listed. Keys have
  no hyphens (report ids are split on `-`); `needsAllBrands: true` hides a page from brand-restricted accounts.
- Keep `webapp/tests` green and add a test for every rule that protects money.

## Safety rules (money is live)
- Scope = campaigns set to **Auto** in the dashboard (`voylla.campaign_autonomy_mode`). Never widen it
  in code; never push anything outside it. Before any live push, list what will change and filter to scope.
- Decide only on fresh data: the stale-data guard skips a brand whose report is not up to yesterday.
- Blinkit keeps adding sales to recent days (~30% for yesterday): never judge a keyword on fresh data.
- The push sends a campaign's FULL keyword list with `campaign_request_type: UPDATE` (never RESTART).
  PAUSE = the keyword is left out of the list (it is removed). Use only the newest load of
  `Blinkit_CampaignWise_ProductID` (old rows are never deleted); refuse a CITY campaign with no cities.
- Bid moves: AI picks the step; caps = raise 15%, cut 20%, Rs 80 per move, floor Rs 200, no raise at position 1.
- Auto campaigns decide on `claude-sonnet-5-5`; the keyword pool sent to the AI is the campaign's own
  products' keywords (capped at 300) - keep prompts small, that is the main cost driver.

## Data map (Postgres; schemas `voylla`, `DataWarehouse`, `public`)
- Ads: `voylla."Blinkit_Ads_Report"` (keyword level; Blinkit has NO SKU-level ads report).
- Bids/campaigns: `voylla."Blinkit_CPM"` (hourly), `voylla."Blinkit_CampaignWise_ProductID"`
  (campaign -> product_id -> region_ids; 11:30 and 18:00 loads), `voylla."Blinkit_Campaign_Runtime"` (hourly).
- SKU keys: `voylla."Blinkit_Mapping"` (SKU, EAN, ITEM ID, PID = Blinkit product_id, MRP, SP = price we
  asked Blinkit to run, LP = landing price). Text columns.
- Sales: `"DataWarehouse".blinkit_sales` (item_id, city_id/city_name, date, qty_sold).
- Stock: `"DataWarehouse".blinkit_soh` (item_id, backend_facility_name, backend+frontend qty; latest created_at).
- Live price: `"DataWarehouse".blinkit_pricing` (product_id, selling_price, mrp, in_stock, scraped_date).
  An out-of-stock listing often shows MRP - only compare prices on in-stock listings.
- Offers: `voylla.blinkit_brand_fund` (PartnersBiz spends export; loader `sku_intel.load_brand_fund_csv`).
- Warehouse -> cluster -> city: `voylla."QC_City_Mappping"` (Channel, City Name, Cluster Name, Facility).
- Item master: `voylla.voylla_in_unicommerce_itemmaster_dedup` (EAN, Product Code, Category, Cost Price).
- Other channels (Instamart, Zepto, Myntra, Amazon, offline) join on EAN.

## Current project: department agents + boss (merged to main; Render test copy runs main)
Goal: for a SKU or goal ("increase sales of X", "protect profit", "avoid stock-out", "clear stock"),
department agents report and a boss agent recommends the action (bid, campaign membership, city
targeting, price/offer, channel shift, or mark dead). Voylla only for now. Read-only until Amit approves actions.
- Phase 1 (done): `webapp/sku_intel.py` + `/sku-health` page - per-SKU sales, stock, cover, asked vs live
  price, profit/unit (LP - cost), brand fund, campaigns, and flags.
- Phase 2 (built, needs checking on live data): `webapp/sku_agents.py` - Sales / Inventory / Price-Offer /
  Marketing / Product agents (plain Python over `sku_intel.sku_detail`, no LLM), one boss call
  (`BOSS_MODEL`, ~2k tokens) per SKU + goal, checked by `blocked_actions` (no extra ad spend with no stock,
  < 7 days cover, or below cost; never mark a selling SKU dead). Answers logged to
  `voylla.sku_boss_recommendations`. Page `/sku-health/sku/<sku>`; chat tools `get_sku_report`, `recommend_for_sku`.
- First approved action: take a SKU out of a campaign from SKU Health (per campaign or bulk). The page only
  queues it (`voylla.sku_campaign_removals`, `webapp/sku_actions.py`); the push notebook applies it with the
  campaign's product list minus the product (`REMOVALS_DRY_RUN = True` until Amit checks one run). Never leaves
  a campaign with no products; a done removal stays out of later bid pushes until a newer campaign load.
- City master: the push notebook GETs `/adservice/v3/campaigns/config` after login (`webapp/blinkit_config.py`)
  into `voylla.blinkit_city_master` (city_id, city_name). Token never stored. Only runs on days something is pushed.
- Phase 3: boss recommendations become approvable actions.
Open items: confirm the config reply shape fills the city master (the job prints its top-level keys if not);
16 Blinkit warehouses missing from `QC_City_Mappping`; how often the offers file is refreshed.

## Working from a cloud session (read first)
A cloud session has this repo only. It does NOT have: the Postgres credentials (`Python_Scripts\Voylla_Cred.txt`
on the PC / `.env.production` on the server), the office PC (Task Scheduler, push job, Blinkit login mailbox),
or SSH to Lightsail (no key; Amal owns the box). So: write code + tests, push a feature branch, Amit merges.
Anything needing live data, the server or a live push -> give Amit the exact commands and ask him to paste output.
Never run or re-run the push notebook to "see an error" - it is live (`DRY_RUN = False`) and sends real bids.

## Live state (2026-10-14)
- Auto campaigns (`campaign_autonomy_mode.mode='auto'`): Voylla 296464 (since 2026-09-18); Chumbak 607078
  (since 2026-10-08) + 354499, 354509, 359470, 359869, 379520, 429819, 497432, 497437, 607168, 500151, 624587
  (since 2026-10-12). 500151 and 624587 are STOPPED on Blinkit, so the daily run skips them (11 run).
- Push notebook `DRY_RUN = False` (live), `REMOVALS_DRY_RUN = True`.
- Server crontab line (Amit-edited 2026-10-13, backup `~/crontab.backup_2026-10-13`):
  `0 7 * * * cd /home/ubuntu/blinkit-marketing-repo/webapp && set -a && . ./.env.production && set +a &&
  /home/ubuntu/blinkitwebvenv/bin/python -c "import ondemand_engine as oe; print(oe.run_auto_campaigns())" >> /home/ubuntu/ondemand-daily.log 2>&1`
- Portal `PAGES` (dsr-portal, hand-edited by Amit 2026-10-13, backup `index.js.bak-20261013`) now lists
  `skuhealth` (/sku-health, needsAllBrands); it also lists `autotracker` (/auto-tracker), a page removed on 2026-10-14:
  delete that entry. An update by Amal may overwrite the file.
- Deploy log on server: `/home/ubuntu/ai-marketing-deploys.log`. Dashboard = systemd `blinkit-marketing` (gunicorn 127.0.0.1:3002).

## Session log 2026-10-13 / 14
- Found: the server cron hard-coded `[('Voylla',['296464']),('Chumbak',['607078'])]`, so the 9 Chumbak campaigns
  switched to Auto on 10-12 never got daily suggestions (also `CLAUDE.md` wrongly said 1:30 / run_auto_campaigns).
  Fixed the cron to `run_auto_campaigns()`; 10-14 run covered all 11 (12:30-12:38 IST, 39 suggestions, 14 changes).
- 10-13: deleted that day's 607078 rows (backup `Python_Scripts\logs\ondemand_actions_backup_2026-10-13.csv`;
  2 of them had already been pushed: ladies bag 601->541, sling bag gift for women 847->915), re-ran
  `run_auto_campaigns()` for real, then pushed 10 changes over 8 Chumbak campaigns. 10-14: pushed 12 changes over
  8 campaigns manually before 2 pm (first try died on a DB connection blip; nothing was sent).
- Before any live push we check: the due list is only today's rows, only Auto campaigns, and each row's
  `current_cpm` equals live `Blinkit_CPM.cpm` (the push applies bid_change to the ROW's current_cpm, and rebuilds
  the rest of the keyword list from `Blinkit_CPM`).
- Built `/auto-tracker` (removed 2026-10-14 at Amit's request: ROAS Impact covers it; renamed Ads Performance, see below).
  Auto campaigns vs before their start, Daily/Weekly/Monthly in 1/7/30-day blocks counted FROM the start date
  (not calendar weeks), ROAS = total sales / total spend, the brand's non-Auto campaigns as the comparison line,
  per-campaign table from each campaign's own switch date. No chart library (inline SVG). Day 1 (10-12):
  Auto ROAS 4.75 -> 4.47 (-6%), other campaigns 6.76 -> 4.24 (-37%) - too early to read.

## Ads Performance (`/roas`, was ROAS Impact; 2026-10-14)
- AI-managed scope has a campaign picker (`?campaigns=` repeated; `queries.pick_campaigns`, default all AI campaigns).
- Trend: metric switch (ROAS, spend, sales, impressions, CTR = ATC / impressions as the bid engine uses - Blinkit has no
  clicks, add to carts, CPM), spend bars, dashed line = the brand's non-AI campaigns (`fetch_channel_daily(exclude_ids=)`),
  triangles = days the AI pushed bids (`fetch_push_counts`). Before/after table for all metrics; 30-day ROAS sparkline per campaign.

## Open items (next session)
- Chumbak 607168 malformed reply (13-14 Oct, "Recovered N objects", rest backfilled at 0.70 -> REVIEW): fixed
  2026-10-14 in `generate_ondemand_suggestions` - Sonnet 5.5 thinks by default and thinking counts against
  max_tokens (was 12000). Now: `SUGGESTION_MAX_TOKENS` 32000, streamed, structured outputs
  (`SUGGESTION_SCHEMA`) for supported models, and a cut-off/refused reply saves nothing (error) instead of
  half + backfill. Confirm on the next run: 607168 rows auto-accepted, no "Recovered" line in the cron log.
- Repeated cuts: ladies bag (607078) cut 5 times since 10-08 (now 487); women hand bag (354499) 965->885->805;
  tote bag 685->617->543. Watch on Ads Performance (pick those campaigns) whether ROAS actually improves; the floor (Rs 200) is the only stop.
- After deleting/regenerating a day, the pushed history is lost (the AI's cooldown/escalation reads it). Prefer
  not to delete pushed rows.
- `HANDOFF.md` is from 2026-09-15 and out of date; this file is the current source.
