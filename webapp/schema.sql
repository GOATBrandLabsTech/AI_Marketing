-- Agent Chat + Campaign Autonomy Mode
-- New tables only - never touches existing Blinkit_* tables. Run once;
-- every statement is idempotent (IF NOT EXISTS) so re-running is safe.

CREATE TABLE IF NOT EXISTS voylla.agent_chat_threads (
    thread_id   TEXT PRIMARY KEY,
    brand       TEXT NOT NULL,
    title       TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS voylla.agent_chat_messages (
    id          SERIAL PRIMARY KEY,
    thread_id   TEXT NOT NULL REFERENCES voylla.agent_chat_threads(thread_id) ON DELETE CASCADE,
    role        TEXT NOT NULL,          -- 'user' | 'assistant'
    content     TEXT,                   -- the visible reply/question text
    tool_calls  JSONB,                  -- which tools ran + their results, for audit/debug
    created_at  TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_chat_messages_thread ON voylla.agent_chat_messages(thread_id, created_at);

CREATE TABLE IF NOT EXISTS voylla.campaign_autonomy_mode (
    campaign_id TEXT NOT NULL,
    brand       TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'semi_auto' CHECK (mode IN ('auto', 'semi_auto', 'manual')),
    set_by      TEXT,
    set_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (campaign_id, brand)
);

-- One additive column on the existing Blinkit_actions_llm table (everything
-- above is new tables only). Needed so a row that Auto Mode pushed with no
-- human review can be told apart from one a person actually accepted -
-- nullable, defaults to FALSE, touches nothing else about that table.
ALTER TABLE voylla."Blinkit_actions_llm" ADD COLUMN IF NOT EXISTS is_auto_applied BOOLEAN DEFAULT FALSE;

-- On-demand suggestions: a full, independent replica of Blinkit_actions_llm's
-- shape, generated synchronously right now instead of waiting for the
-- weekly/daily batch. Deliberately its own table - bid_implement.ipynb never
-- reads this, so an on-demand suggestion can never be swept into a live bid
-- push on its own. We own this table outright (unlike Blinkit_actions_llm),
-- so it can be freely ALTERed later.
CREATE TABLE IF NOT EXISTS voylla.blinkit_ondemand_actions (
    unique_key           TEXT PRIMARY KEY,
    action_date          DATE NOT NULL,
    campaign_id          TEXT NOT NULL,
    campaign_name        TEXT,
    targeting            TEXT NOT NULL,
    action               TEXT NOT NULL,
    bid_change           INTEGER,
    confidence           NUMERIC,
    explanation          TEXT,
    alternative_keywords JSONB,
    "Brand"              TEXT NOT NULL,
    current_cpm          NUMERIC,
    campaign_budget      NUMERIC,
    cpm_floor            NUMERIC,
    search_volume_tier   TEXT,
    quick_action         TEXT,
    rule_action          TEXT,
    decision_step        TEXT,
    user_implemented     TEXT,
    override_action      TEXT,
    cpm_llm_override     NUMERIC,
    cpm_change_user      NUMERIC,
    override_note        TEXT,
    implementation_date  DATE,
    requested_by         TEXT,
    requested_at         TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ondemand_brand_date
    ON voylla.blinkit_ondemand_actions("Brand", action_date);
CREATE INDEX IF NOT EXISTS idx_ondemand_campaign
    ON voylla.blinkit_ondemand_actions(campaign_id, targeting);
