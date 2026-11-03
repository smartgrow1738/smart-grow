CREATE TABLE IF NOT EXISTS telemetry_logs (
    id            BIGSERIAL       PRIMARY KEY,
    recorded_at   TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    moisture      INTEGER         NOT NULL
                  CHECK (moisture BETWEEN 0 AND 100),
    temperature   NUMERIC(5, 2)   NOT NULL
                  CHECK (temperature BETWEEN -50.00 AND 100.00)
);

CREATE INDEX IF NOT EXISTS idx_telemetry_logs_recorded_at
    ON telemetry_logs (recorded_at DESC);

SELECT * FROM telemetry_logs ORDER BY recorded_at DESC LIMIT 5;