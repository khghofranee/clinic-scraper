-- ─────────────────────────────────────────────
-- Smiler Marketplace DB Schema
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS clinics (
    id          TEXT PRIMARY KEY,          -- e.g. "clinic_ie_0001"
    name        TEXT NOT NULL,
    city        TEXT NOT NULL,
    clinic_type TEXT,
    country     TEXT NOT NULL,             -- ISO code: IE, GB, DE, HR, CH
    address     TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS platform_snapshots (
    id                  SERIAL PRIMARY KEY,
    clinic_id           TEXT NOT NULL REFERENCES clinics(id),
    country             TEXT NOT NULL,
    week_stamp          TEXT NOT NULL,     -- e.g. "2026-W01"
    platform            TEXT NOT NULL,     -- google, facebook, trustpilot, etc.
    platform_rating     NUMERIC(3,2),      -- avg stars e.g. 4.20
    platform_reviews    INTEGER,           -- total review count
    source_url          TEXT,
    scraped_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS marketplace_snapshots (
    id                       SERIAL PRIMARY KEY,
    clinic_id                TEXT NOT NULL REFERENCES clinics(id),
    country                  TEXT NOT NULL,
    week_stamp               TEXT NOT NULL,     -- e.g. "2026-W01"
    marketplace_rating       NUMERIC(4,2),      -- weighted avg
    marketplace_reviews      INTEGER,           -- sum of all platform reviews
    platforms_scraped        TEXT[],            -- ["google","trustpilot",...]
    computed_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (clinic_id, week_stamp)              -- one row per clinic per week
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_platform_clinic_week
    ON platform_snapshots (clinic_id, week_stamp);

CREATE INDEX IF NOT EXISTS idx_platform_country_week
    ON platform_snapshots (country, week_stamp);

CREATE INDEX IF NOT EXISTS idx_marketplace_clinic
    ON marketplace_snapshots (clinic_id);

CREATE INDEX IF NOT EXISTS idx_marketplace_week
    ON marketplace_snapshots (week_stamp);
