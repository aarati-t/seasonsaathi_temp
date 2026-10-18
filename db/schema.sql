-- SeasonSaathi PostgreSQL schema (SPEC section 5).
-- BigQuery uses the same table and column names (db/bq_schema.json).

CREATE TABLE IF NOT EXISTS villages (
    village_id  INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    taluka      TEXT NOT NULL,
    district    TEXT NOT NULL,
    lat         DOUBLE PRECISION NOT NULL,   -- entered by hand; never guessed
    lon         DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS households (
    hh_id                    TEXT PRIMARY KEY,
    village_id               INTEGER NOT NULL REFERENCES villages(village_id),
    tenure                   TEXT CHECK (tenure IN ('owner', 'tenant', 'sharecrop')),
    acres                    DOUBLE PRECISION,
    irrigation               TEXT CHECK (irrigation IN ('rainfed', 'well', 'borewell')),
    water_hours_per_week     DOUBLE PRECISION,
    crops_last_season        TEXT,
    cattle_count             INTEGER,
    goats_count              INTEGER,
    fodder_weeks_left        DOUBLE PRECISION,
    off_farm_income          TEXT,
    loan_due_month           SMALLINT CHECK (loan_due_month BETWEEN 1 AND 12),
    cash_buffer_band         TEXT CHECK (cash_buffer_band IN ('low', 'med', 'high')),
    primary_cultivator_gender TEXT,
    consent                  BOOLEAN NOT NULL DEFAULT FALSE,
    profile_text_norm        TEXT,
    profile_embedding        REAL[],         -- embedding vector; cosine in numpy (VECTOR_MODE=python)
    embed_model              TEXT,           -- EMBED_MODEL that produced profile_embedding
    is_synthetic             BOOLEAN NOT NULL,
    created_at               TIMESTAMPTZ,    -- set when saved from F1; NULL for loaded rows (no real creation time)
    soil_type_farmer         TEXT            -- soil in the farmer's words, normalised (P7); never inferred
);
ALTER TABLE households ADD COLUMN IF NOT EXISTS embed_model TEXT;
ALTER TABLE households ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE households ADD COLUMN IF NOT EXISTS soil_type_farmer TEXT;

CREATE TABLE IF NOT EXISTS plots (
    plot_id      TEXT PRIMARY KEY,
    hh_id        TEXT NOT NULL REFERENCES households(hh_id),
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    is_synthetic BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS rain_series (
    village_id      INTEGER NOT NULL REFERENCES villages(village_id),
    date            DATE NOT NULL,
    rain_mm         DOUBLE PRECISION,
    normal_mm       DOUBLE PRECISION,
    cum_deficit_pct DOUBLE PRECISION,
    source          TEXT CHECK (source IN ('CHIRPS', 'Sentinel-2', 'SYNTHETIC')),
    PRIMARY KEY (village_id, date)
);
ALTER TABLE rain_series ADD COLUMN IF NOT EXISTS source TEXT
    CHECK (source IN ('CHIRPS', 'Sentinel-2', 'SYNTHETIC'));

CREATE TABLE IF NOT EXISTS ndvi_series (
    plot_id     TEXT NOT NULL REFERENCES plots(plot_id),
    date        DATE NOT NULL,
    ndvi        DOUBLE PRECISION,
    ndvi_normal DOUBLE PRECISION,
    source      TEXT CHECK (source IN ('CHIRPS', 'Sentinel-2', 'SYNTHETIC')),
    PRIMARY KEY (plot_id, date)
);
ALTER TABLE ndvi_series ADD COLUMN IF NOT EXISTS source TEXT
    CHECK (source IN ('CHIRPS', 'Sentinel-2', 'SYNTHETIC'));

CREATE TABLE IF NOT EXISTS soil_moisture (
    village_id  INTEGER NOT NULL REFERENCES villages(village_id),
    date        DATE NOT NULL,
    sm_surface  DOUBLE PRECISION,
    sm_rootzone DOUBLE PRECISION,       -- SMAP L4 root-zone (0-100 cm) volume fraction (P7)
    PRIMARY KEY (village_id, date)
);
ALTER TABLE soil_moisture ADD COLUMN IF NOT EXISTS sm_rootzone DOUBLE PRECISION;

-- P7: soil properties at the plot point (OpenLandMap via Earth Engine, 0-30 cm mean); is_synthetic follows the plot.
CREATE TABLE IF NOT EXISTS soil_props (
    plot_id             TEXT PRIMARY KEY REFERENCES plots(plot_id),
    texture_class       TEXT,
    clay_pct            DOUBLE PRECISION,
    sand_pct            DOUBLE PRECISION,
    organic_carbon_pct  DOUBLE PRECISION,
    ph                  DOUBLE PRECISION,
    water_fc_pct        DOUBLE PRECISION,
    depth_cm            INTEGER,
    source              TEXT,
    source_url          TEXT,
    is_synthetic        BOOLEAN NOT NULL
);

-- P7: crop water need per season, hand-entered from cited sources (data/crop_water_need.csv); values unchanged.
CREATE TABLE IF NOT EXISTS crop_water_need (
    crop_key            TEXT NOT NULL,
    crop                TEXT,
    season              TEXT NOT NULL,
    need_mm_low         DOUBLE PRECISION,
    need_mm_high        DOUBLE PRECISION,
    root_depth_m_low    DOUBLE PRECISION,
    root_depth_m_high   DOUBLE PRECISION,
    depletion_p         DOUBLE PRECISION,
    critical_stage      TEXT,
    region              TEXT,
    basis               TEXT CHECK (basis IN ('reported', 'derived', 'assumption')),
    is_synthetic        BOOLEAN NOT NULL,
    note                TEXT,
    source_url          TEXT,
    root_source_url     TEXT,
    PRIMARY KEY (crop_key, season)
);

CREATE TABLE IF NOT EXISTS prices (
    commodity         TEXT NOT NULL,
    district          TEXT NOT NULL,
    market            TEXT,                 -- empty when the export is district-level
    date              DATE NOT NULL,
    modal_price       DOUBLE PRECISION NOT NULL,  -- Rs/quintal
    reference_price   DOUBLE PRECISION,     -- MSP, Rs/quintal
    reference_type    TEXT,                 -- 'MSP'
    reference_season  TEXT,                 -- e.g. 'KMS 2026-27'
    pct_vs_reference  DOUBLE PRECISION,
    source            TEXT NOT NULL,
    source_url        TEXT,
    is_synthetic      BOOLEAN NOT NULL,
    UNIQUE NULLS NOT DISTINCT (commodity, district, market, date, source)
);

-- One row per commodity + district; read by tools/prices.py.
CREATE TABLE IF NOT EXISTS price_summary (
    commodity            TEXT NOT NULL,
    district             TEXT NOT NULL,
    latest_date          DATE,              -- empty = no mandi data
    latest_modal_price   DOUBLE PRECISION,  -- median across markets on latest_date; never filled in
    reference_price      DOUBLE PRECISION,
    reference_type       TEXT,
    reference_season     TEXT,
    reference_source_url TEXT,
    pct_vs_reference     DOUBLE PRECISION,
    n_markets            INTEGER,           -- empty = not listed in the export
    note                 TEXT,
    source               TEXT,
    source_url           TEXT,
    is_synthetic         BOOLEAN NOT NULL,
    PRIMARY KEY (commodity, district)
);

-- Fodder/cattle prices and norms, one row per item_key (data/raw/livestock_prices.csv).
CREATE TABLE IF NOT EXISTS livestock_prices (
    item_key     TEXT PRIMARY KEY,
    item         TEXT NOT NULL,
    unit         TEXT NOT NULL,
    region       TEXT,
    period       TEXT,
    value_low    DOUBLE PRECISION,
    value_high   DOUBLE PRECISION,
    value_mid    DOUBLE PRECISION NOT NULL,
    basis        TEXT NOT NULL CHECK (basis IN ('reported', 'derived', 'assumption')),
    is_synthetic BOOLEAN NOT NULL,
    note         TEXT,
    source_url   TEXT,
    CHECK (basis <> 'assumption' OR is_synthetic)
);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id           TEXT PRIMARY KEY,      -- synthetic rows: '<hh_id>:<season>'; real rows: uuid
    hh_id                TEXT NOT NULL REFERENCES households(hh_id),
    season               TEXT NOT NULL,
    decision             TEXT,
    predicted_signal     TEXT CHECK (predicted_signal IN ('ACT', 'WAIT', 'AVOID')),
    predicted_confidence TEXT CHECK (predicted_confidence IN ('HIGH', 'MED', 'LOW')),
    actual_result        TEXT CHECK (actual_result IN ('good', 'partial', 'loss')),
    cattle_sold          INTEGER,
    distress_sale        BOOLEAN,
    request_id           TEXT,                  -- decision_log request this outcome follows up (real outcomes)
    rain_deficit_bucket  TEXT,                  -- '0-15' | '15-30' | '>30' at decision time (else from season_rain)
    crop_key             TEXT,                  -- canonical crop key (else from crop_mix_keys)
    note                 TEXT,
    is_synthetic         BOOLEAN NOT NULL,
    provenance           TEXT                   -- observed | synthetic | demo (P5b); NULL on older rows = derived in
                                                -- calibration_v: synthetic, learning-demo-* = demo, else legacy_unverified
);
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS provenance TEXT;
-- P4 migration from the P1-P3 shape (PRIMARY KEY (hh_id, season)): add columns, key on outcome_id.
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS outcome_id TEXT;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS request_id TEXT;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS rain_deficit_bucket TEXT;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS crop_key TEXT;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS note TEXT;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conname = 'outcomes_pkey' AND array_length(c.conkey, 1) = 2) THEN
        UPDATE outcomes SET outcome_id = hh_id || ':' || season WHERE outcome_id IS NULL;
        ALTER TABLE outcomes DROP CONSTRAINT outcomes_pkey;
        ALTER TABLE outcomes ADD PRIMARY KEY (outcome_id);
    END IF;
END $$;

-- Rain context of past seasons for outcomes that do not carry rain_deficit_bucket themselves.
CREATE TABLE IF NOT EXISTS season_rain (
    village_id          INTEGER NOT NULL REFERENCES villages(village_id),
    season              TEXT NOT NULL,
    rain_deficit_bucket TEXT NOT NULL CHECK (rain_deficit_bucket IN ('0-15', '15-30', '>30')),
    source              TEXT NOT NULL,
    is_synthetic        BOOLEAN NOT NULL,
    PRIMARY KEY (village_id, season)
);

-- Crop-mix decision name -> canonical crop key (config/terms.yaml names), for calibration contexts.
CREATE TABLE IF NOT EXISTS crop_mix_keys (
    decision TEXT PRIMARY KEY,
    crop_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id     TEXT PRIMARY KEY,
    plot_id      TEXT NOT NULL REFERENCES plots(plot_id),
    stress_level TEXT,
    next_step    TEXT CHECK (next_step IN ('WAIT_NEXT_PASS', 'REQUEST_PHOTOS', 'REQUEST_DRONE', 'ALERT_AGENT')),
    text_mr      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    via          TEXT CHECK (via IN ('pubsub', 'local'))
);
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS hh_id TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS text_hi TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS fact_ids TEXT;      -- JSON list of cited fact_ids
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS data_as_of TEXT;    -- last CHIRPS date used
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS request_id TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS message_id TEXT;    -- Pub/Sub messageId (dedupe of push retries)

-- Learning loop: ratings of explanations (insert only).
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id    TEXT PRIMARY KEY,
    request_id     TEXT NOT NULL,
    feature        TEXT,
    context_bucket TEXT,
    rating         TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    note           TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Learning loop: local terms Gemini had to interpret, and human decisions on them (insert only;
-- the latest row per term wins).
CREATE TABLE IF NOT EXISTS term_review (
    review_id  TEXT PRIMARY KEY,
    term       TEXT NOT NULL,
    meaning    TEXT,
    category   TEXT,
    canonical  TEXT,
    status     TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
    request_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- P5b impact simulator (append-only). JSON kept as TEXT so Postgres and BigQuery match.
CREATE TABLE IF NOT EXISTS impact_cache (
    cache_key       TEXT PRIMARY KEY,      -- sha256(hh_id, feature, engine, model, fact snapshot, versions)
    hh_id           TEXT NOT NULL,
    feature         TEXT NOT NULL,
    engine          TEXT,
    model           TEXT,
    snapshot_hash   TEXT NOT NULL,
    version_hash    TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    model_calls     INTEGER,
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS impact_runs (
    run_id                  TEXT PRIMARY KEY,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    population_snapshot_id  TEXT,
    n_households            INTEGER,
    kpis_json               TEXT,
    assumptions_json        TEXT,
    engine_requested        TEXT,
    engines_used            TEXT,
    data_backend            TEXT,
    cache_snapshot_id       TEXT,
    benchmark_run_id        TEXT,
    version_metadata        TEXT
);

CREATE TABLE IF NOT EXISTS verifier_benchmark (
    benchmark_run_id  TEXT NOT NULL,
    case_id           TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    case_type         TEXT NOT NULL,       -- changed_number | missing_fact_id | invented_fact_id | unsupported_claim | valid_control
    expected          TEXT NOT NULL,       -- invalid | valid (set by construction, never by the verifier)
    outcome           TEXT NOT NULL,       -- caught | missed | accepted | false_rejection | error
    detail_json       TEXT,
    latency_ms        INTEGER,
    model_calls       INTEGER,
    version_metadata  TEXT,
    PRIMARY KEY (benchmark_run_id, case_id)
);

CREATE TABLE IF NOT EXISTS decision_log (
    request_id        TEXT PRIMARY KEY,
    hh_id             TEXT,
    feature           TEXT NOT NULL,
    facts_used        JSONB,
    output_json       JSONB,
    verifier_status   TEXT,
    orchestrator_used TEXT,
    fallback_reason   TEXT,
    latency_ms        INTEGER,
    genai_backend     TEXT,                 -- vertex | apikey (NULL for cache/rule results)
    gemini_model      TEXT
);
ALTER TABLE decision_log ADD COLUMN IF NOT EXISTS genai_backend TEXT;
ALTER TABLE decision_log ADD COLUMN IF NOT EXISTS gemini_model TEXT;

-- calibration_v (context_bucket, confidence_level, hit_rate, n) is defined in
-- build block 4 (F7). Its bucket (rain bucket x irrigation x crop) and "hit"
-- rule are feature logic, so they are not fixed here.

-- F7 calibration view: hit rate per context (rain-deficit bucket x irrigation x crop key) and confidence level.
-- Same SQL on BigQuery (db/bq_calibration_v.sql). A call is a hit when ACT -> good, AVOID -> partial/loss,
-- WAIT -> partial. Real outcomes (is_synthetic = false) are counted automatically.
CREATE OR REPLACE VIEW calibration_v AS
WITH base AS (
    SELECT o.predicted_signal, o.predicted_confidence, o.actual_result, o.is_synthetic,
           COALESCE(o.provenance, CASE WHEN o.is_synthetic THEN 'synthetic' WHEN o.season LIKE 'learning-demo%' THEN 'demo' ELSE 'legacy_unverified' END) AS provenance,
           COALESCE(o.rain_deficit_bucket, sr.rain_deficit_bucket, 'unknown') AS bucket,
           COALESCE(h.irrigation, 'unknown') AS irrigation,
           COALESCE(o.crop_key, ck.crop_key, 'unknown') AS crop
    FROM outcomes o
    JOIN households h ON h.hh_id = o.hh_id
    LEFT JOIN season_rain sr ON sr.village_id = h.village_id AND sr.season = o.season
    LEFT JOIN crop_mix_keys ck ON ck.decision = o.decision
    WHERE o.predicted_signal IS NOT NULL AND o.predicted_confidence IS NOT NULL AND o.actual_result IS NOT NULL
)
SELECT CONCAT(bucket, '|', irrigation, '|', crop) AS context_bucket,
       predicted_confidence AS confidence_level,
       AVG(CASE WHEN (predicted_signal = 'ACT' AND actual_result = 'good')
                  OR (predicted_signal = 'AVOID' AND actual_result <> 'good')
                  OR (predicted_signal = 'WAIT' AND actual_result = 'partial') THEN 1 ELSE 0 END) AS hit_rate,
       COUNT(*) AS n,
       SUM(CASE WHEN provenance = 'observed' THEN 1 ELSE 0 END) AS n_real,          -- observed only (P5b D)
       SUM(CASE WHEN provenance = 'synthetic' THEN 1 ELSE 0 END) AS n_synthetic,
       SUM(CASE WHEN provenance = 'demo' THEN 1 ELSE 0 END) AS n_demo,
       SUM(CASE WHEN provenance = 'legacy_unverified' THEN 1 ELSE 0 END) AS n_unverified
FROM base
GROUP BY 1, 2;
