CREATE TABLE IF NOT EXISTS telemetry_logs (
    id            BIGSERIAL       PRIMARY KEY,
    recorded_at   TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    moisture      INTEGER         NOT NULL
                  CHECK (moisture BETWEEN 0 AND 100),
    temperature   NUMERIC(5, 2)   NOT NULL
                  CHECK (temperature BETWEEN -50.00 AND 100.00)
);

-- AI output per reading (nullable: NULL rate = model unavailable / pre-AI rows).
-- ADD COLUMN IF NOT EXISTS makes this safe to re-run on an existing database.
ALTER TABLE telemetry_logs ADD COLUMN IF NOT EXISTS predicted_drying_rate NUMERIC(5, 2);
ALTER TABLE telemetry_logs ADD COLUMN IF NOT EXISTS pump_decision VARCHAR(8)
    CHECK (pump_decision IN ('PUMP_ON', 'PUMP_OFF'));

CREATE INDEX IF NOT EXISTS idx_telemetry_logs_recorded_at
    ON telemetry_logs (recorded_at DESC);
