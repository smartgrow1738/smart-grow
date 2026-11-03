import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, status
from psycopg2.pool import ThreadedConnectionPool
from psycopg2 import OperationalError
from pydantic import BaseModel, Field, ConfigDict


DB_CONFIG: dict = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   "smartgrow",
    "user":     "postgres",
    "password": "bytekeith",
}

API_HOST: str = "0.0.0.0"
API_PORT: int = 8000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("smart-grow.backend")

try:
    db_pool = ThreadedConnectionPool(minconn=1, maxconn=10, **DB_CONFIG)
    log.info("PostgreSQL connection pool initialised (1-10 conns).")
except OperationalError as exc:
    log.critical("Could not create DB pool — is PostgreSQL running? %s", exc)
    raise

class TelemetryPacket(BaseModel):
    """Wire format accepted from ANY sensor node (mock or real)."""
    model_config = ConfigDict(extra="forbid")   # reject unknown fields early

    moisture:    int   = Field(..., ge=0, le=100,
                               description="Soil moisture percentage (0-100)")
    temperature: float = Field(..., ge=-50.0, le=100.0,
                               description="Ambient temperature in Celsius")

app = FastAPI(
    title="Smart-Grow Telemetry API",
    version="0.1.0",
    description="Week 1 sprint — foundation & telemetry loop.",
)

@app.get("/health", tags=["ops"])
def health() -> dict:
    """Liveness probe — also verifies the DB pool is alive."""
    conn = None
    try:
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
        return {"status": "ok", "db": "connected",
                "time": datetime.utcnow().isoformat() + "Z"}
    except Exception as exc:
        log.error("Health check failed: %s", exc)
        return {"status": "degraded", "db": "unreachable"}
    finally:
        if conn:
            db_pool.putconn(conn)


@app.post("/api/v1/telemetry",
          status_code=status.HTTP_201_CREATED,
          tags=["telemetry"])
def ingest_telemetry(packet: TelemetryPacket) -> dict:
    """
    Accept a validated telemetry packet, persist it, and echo back the row id.
    """
    log.info("→ Incoming packet | moisture=%d%% | temperature=%.2f°C",
             packet.moisture, packet.temperature)

    conn = None
    try:
        conn = db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO telemetry_logs (moisture, temperature)
                VALUES (%s, %s)
                RETURNING id, recorded_at;
                """,
                (packet.moisture, packet.temperature),
            )
            row_id, recorded_at = cur.fetchone()
        conn.commit()

        log.info("✓ Committed  | row_id=%s | recorded_at=%s",
                 row_id, recorded_at.isoformat())

        return {
            "status":      "ok",
            "row_id":      row_id,
            "recorded_at": recorded_at.isoformat(),
        }

    except Exception as exc:
        if conn:
            conn.rollback()
        log.exception("DB insert failed — transaction rolled back.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist telemetry packet.",
        ) from exc
    finally:
        if conn:
            db_pool.putconn(conn)


# ---------------------------------------------------------------------------
# DEV LAUNCHER (so `python -m backend.server` also works)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.server:app", host=API_HOST, port=API_PORT, reload=True)