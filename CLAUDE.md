# Marketing Control - notes for Claude Code

Flask dashboard + AI bid engine for quick-commerce ads (Blinkit first; Instamart, Zepto engines exist).
Owner: Amit (Analytics). Explain things plainly, explain before asking for approval, fix root causes.

## How code goes live (do not bypass)
- `main` is the only deploy branch. Work on a feature branch, push, Amit opens/merges the PR.
- CI (`.github/workflows/ci.yml`) runs `webapp/tests` on every push/PR. The Lightsail server's
  webhook deploy runs the same tests before restarting and rolls back on failure. No server keys in GitHub.
- The daily jobs (`jobs/`) run on Amit's office PC from a clone that resets to `origin/main` before
  every run: 1:30 pm IST suggestions (server cron, `oe.run_auto_campaigns()`), 2:00 pm IST Blinkit push
  (PC Task Scheduler, `jobs/Blinkit_Ondemand_Bid_Push.ipynb`). Credentials live outside git
  (`Python_Scripts` on the PC, `.env.production` on the server).
- Notebooks in `jobs/` are committed with no outputs (a test checks this).
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

## Current project: department agents + boss (branch `sku-intelligence`)
Goal: for a SKU or goal ("increase sales of X", "protect profit", "avoid stock-out", "clear stock"),
department agents report and a boss agent recommends the action (bid, campaign membership, city
targeting, price/offer, channel shift, or mark dead). Voylla only for now. Read-only until Amit approves actions.
- Phase 1 (done): `webapp/sku_intel.py` + `/sku-health` page - per-SKU sales, stock, cover, asked vs live
  price, profit/unit (LP - cost), brand fund, campaigns, and flags.
- Phase 2: Sales / Inventory / Price-Offer / Marketing / Product agents as plain Python + SQL reports
  (no LLM cost), one boss LLM call per question, surfaced in Agent Chat and a SKU detail page.
- Phase 3: boss recommendations become approvable actions.
Open items: Blinkit city-id -> name master (fetch `GET /adservice/v3/campaigns/config` with the push job's
own login and store it; tokens expire in 1 hour - never store tokens); 16 Blinkit warehouses missing from
`QC_City_Mappping`; how often the offers file is refreshed.
