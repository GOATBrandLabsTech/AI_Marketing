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
