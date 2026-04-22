# AI Marketing Automation — Blinkit & Instamart

Automated ad campaign management system for Voylla on **Blinkit** and **Instamart** platforms. Uses Selenium for browser-based authentication, the platforms' internal APIs for bid/campaign control, Claude AI for intelligent bid recommendations, and PostgreSQL (`voylla` schema) as the central data store.

---

## System Architecture

```
PostgreSQL (voylla schema)
    ├── Brand_user          ← login credentials per brand per platform
    ├── Blinkit_Ads_Report  ← raw ad performance data
    ├── Blinkit_CPM         ← current keyword CPMs per campaign
    ├── Blinkit_CampaignWise_ProductID ← product/region/schedule metadata
    ├── Blinkit_Campaign_Schedule      ← slot-based restart schedule
    ├── Blinkit_Campaign_Runtime       ← hourly budget exhaustion tracker
    ├── Blinkit_actions_llm            ← Claude AI bid recommendations
    ├── Instamart_Campaign_Placement   ← Instamart keyword placement data
    ├── instamart_bid                  ← Instamart keyword bids + min bids
    └── Instamart_actions_llm          ← Claude AI recommendations for Instamart
```

All scripts load DB credentials and email passwords from local `.txt` files in `Python_Scripts/` — **never hardcoded**.

---

## Notebooks

### 1. `Blinkit_Report_Script_bid_implement.ipynb`
**Purpose:** Fetches keyword-level CPM data from Blinkit's API and stores it in the DB.

**Flow:**
1. Reads brand credentials from `DataWarehouse.Brand_user`
2. Authenticates via Selenium → requests a magic sign-in link to the brand email
3. Extracts Firebase token from browser network logs
4. Calls `GET /adservice/v1/campaigns/{id}` for each active campaign (last 7 days)
5. Writes `brand, campaign_id, keyword, match_type, cpm, budget` → `voylla.Blinkit_CPM`
6. Budget preservation logic: before 12:00 keeps prior DB budget; after 12:00 saves fresh API budget

**Output table:** `voylla.Blinkit_CPM`

---

### 2. `Blinkit_actions_llm_marketing_Claude_Api.ipynb`
**Purpose:** Uses Claude AI to analyse performance data and generate bid change recommendations (INCREASE / DECREASE / PAUSE) per keyword per campaign.

**Flow:**
1. Pulls recent performance from `Blinkit_Ads_Report` + current CPMs from `Blinkit_CPM`
2. Sends structured prompt to Claude API (Anthropic SDK)
3. Parses Claude's JSON response — each row: `campaign_id, targeting, action, bid_change`
4. Writes recommendations to `voylla.Blinkit_actions_llm` with `action_date`
5. `user_implemented` flag controls whether the action is executed automatically or requires manual approval

**Output table:** `voylla.Blinkit_actions_llm`

**Depends on:** `Blinkit_CPM`, `Blinkit_Ads_Report`

---

### 3. `Blinkit_Campaign_Control.ipynb`
**Purpose:** Executes the AI-recommended bid changes by restarting campaigns via Blinkit's API.

**Flow:**
1. Reads pending actions from `Blinkit_actions_llm` (where `update_status != 'done'`)
2. Authenticates to Blinkit via Selenium + Firebase token
3. For each campaign: builds a full restart payload with updated keyword CPMs
4. Calls `PUT /adservice/v3/campaigns` with `campaign_request_type: RESTART`
5. On success: marks rows `done` in `Blinkit_actions_llm`; sends email summary
6. On failure: marks `failed`; sends alert email

**Output:** Live campaign bid updates + `update_status` written back to `Blinkit_actions_llm`

**Depends on:** `Blinkit_actions_llm`, `Blinkit_CPM`, `Blinkit_CampaignWise_ProductID`

---

### 4. `Blinkit_Campaign_Runtime_Tracker.ipynb`
**Purpose:** Hourly monitor — detects when campaigns go `ON_HOLD` (budget exhausted) and logs runtime duration.

**Flow:**
1. Fetches all active campaign IDs from `Blinkit_Ads_Report` (last 7 days)
2. Authenticates to Blinkit; polls `GET /adservice/v1/campaigns/{id}` for live status
3. Upserts one row per campaign per day into `Blinkit_Campaign_Runtime`
4. On first detection of `ON_HOLD`: calculates `total_hours` since midnight and fires alert email
5. Carryover logic: prevents re-alerting if campaign was already exhausted on a prior run

**Output table:** `voylla.Blinkit_Campaign_Runtime`

**Depends on:** `Blinkit_Ads_Report`, `Brand_user`

---

### 5. `Instamart_Update_Cpm.ipynb`
**Purpose:** Syncs Instamart keyword bid data (min bids + current bids) to the DB, then applies AI-recommended bid changes.

**Flow:**
1. Authenticates to `partner.instamart.in` via Selenium + OTP (fetched from Gmail via IMAP)
2. Captures JWT `Authorization` token from browser network logs
3. **Min bid fetch:** Calls `POST /api/v1/suggest/keyword/bids` for each campaign → upserts `min_bid, search_count` into `voylla.instamart_bid`
4. **Current bid fetch:** Calls `POST /api/v1/campaigns` → extracts live keyword bids → upserts `current_bid` into `voylla.instamart_bid`
5. **Apply actions:** Reads pending rows from `Instamart_actions_llm`, calls `PUT /api/v1/campaign` to update keyword bids
6. Marks applied rows `done` in `Instamart_actions_llm`

**Output table:** `voylla.instamart_bid`

**Depends on:** `Instamart_Campaign_Placement`, `Instamart_actions_llm`, `Brand_user`

---

### 6. `Instamart_actions_llm_Claude.ipynb`
**Purpose:** Uses Claude AI to generate bid recommendations for Instamart campaigns.

**Flow:**
1. Pulls Instamart performance data + current bids from `instamart_bid`
2. Sends to Claude API for analysis
3. Writes recommendations → `voylla.Instamart_actions_llm`
4. `user_implemented = TRUE` controls execution gate

**Output table:** `voylla.Instamart_actions_llm`

**Depends on:** `instamart_bid`, `Instamart_Campaign_Placement`

---

## Data Flow (End-to-End)

```
[Blinkit Platform]                      [Instamart Platform]
       │                                        │
       ▼                                        ▼
Blinkit_Report_Script_bid_implement    Instamart_Update_Cpm (step 1–4)
  → Blinkit_CPM (DB)                     → instamart_bid (DB)
       │                                        │
       ▼                                        ▼
Blinkit_actions_llm_marketing_Claude_Api   Instamart_actions_llm_Claude
  → Blinkit_actions_llm (DB)               → Instamart_actions_llm (DB)
       │                                        │
       ▼                                        ▼
Blinkit_Campaign_Control               Instamart_Update_Cpm (step 5–6)
  → Live bid updates on Blinkit          → Live bid updates on Instamart
       │
       ▼
Blinkit_Campaign_Runtime_Tracker
  → Budget exhaustion alerts (hourly)
```

---

## Execution Schedule (Recommended)

| Script | Frequency | Notes |
|---|---|---|
| `Blinkit_Report_Script_bid_implement` | Every 6 hrs | Keeps CPM data fresh |
| `Blinkit_actions_llm_marketing_Claude_Api` | Daily (morning) | Generates daily recommendations |
| `Blinkit_Campaign_Control` | Per slot (2× daily) | Slot windows defined in `Blinkit_Campaign_Schedule` |
| `Blinkit_Campaign_Runtime_Tracker` | Every 1 hr | Budget exhaustion monitoring |
| `Instamart_Update_Cpm` | Daily | Fetches bids + applies actions |
| `Instamart_actions_llm_Claude` | Daily (morning) | Generates daily recommendations |

---

## Prerequisites

- Python 3.10+
- Chrome + ChromeDriver (matching version)
- PostgreSQL access (`voylla` schema)
- Credential files in `Python_Scripts/`:
  - `Voylla_Cred.txt` — DB host, user, password, database, port
  - `Automationalert_emailid_pass.txt` — alert sender email + app password + name
  - `Automation_emailid_pass.txt` — report sender email + app password + name
  - `automation_mail_receiver.txt` — alert recipient emails (one per line)
- Anthropic API key (for Claude notebooks)

### Key Python packages
```
pandas, psycopg2, sqlalchemy, selenium, yagmail, anthropic,
beautifulsoup4, imaplib, requests, undetected-chromedriver
```
