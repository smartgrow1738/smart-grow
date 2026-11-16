"""
Smart-Grow :: Telemetry Backend with AI Integration
"""

import os
import logging
from datetime import datetime, timezone
import pickle
import threading

import pandas as pd
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg2.pool import ThreadedConnectionPool
from psycopg2 import OperationalError
from pydantic import BaseModel, Field, ConfigDict
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# CONFIGURATION BLOCK
# ---------------------------------------------------------------------------
DB_CONFIG: dict = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "dbname": os.getenv("DB_NAME", "smartgrow"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
}

API_HOST: str = "0.0.0.0"
API_PORT: int = 8000

# Irrigation agent tuning (override via .env without touching code)
CRITICAL_MOISTURE: float = float(os.getenv("CRITICAL_MOISTURE", 30))   # % — always water below this
PREDICTION_HORIZON_H: float = float(os.getenv("PREDICTION_HORIZON_H", 2.0))  # water early if critical is this close

# Feature ranges the model was trained on (keep in sync with ml_models/train_predictor.py).
# Random Forests cannot extrapolate, so inputs are clipped into this box before predicting.
MODEL_TEMP_RANGE = (0.0, 45.0)
MODEL_MOISTURE_RANGE = (0.0, 100.0)

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
# ASCII-only log messages: emoji and the degree sign garble on the Windows console.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("smart-grow.backend")

# ---------------------------------------------------------------------------
# LOAD AI MODEL
# ---------------------------------------------------------------------------
MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "ml_models", "moisture_predictor.pkl")
try:
    with open(MODEL_PATH, "rb") as f:
        ai_model = pickle.load(f)
    # Column order must match training exactly; take it from the model itself.
    MODEL_FEATURES = list(getattr(ai_model, "feature_names_in_", ["temperature", "moisture"]))
    log.info("AI model loaded (features=%s).", MODEL_FEATURES)
except Exception as e:
    log.error("Failed to load AI model from %s: %s -- running in rule-only mode.", MODEL_PATH, e)
    ai_model = None
    MODEL_FEATURES = ["temperature", "moisture"]

# ---------------------------------------------------------------------------
# DATABASE POOL
# ---------------------------------------------------------------------------
DB_MAX_CONN = 10
try:
    db_pool = ThreadedConnectionPool(minconn=1, maxconn=DB_MAX_CONN, **DB_CONFIG)
    log.info("PostgreSQL connection pool initialised (1-%d conns).", DB_MAX_CONN)
except OperationalError as exc:
    log.critical("Could not create DB pool -- is PostgreSQL running? %s", exc)
    raise

# ThreadedConnectionPool raises PoolError instead of waiting when all connections are busy,
# while FastAPI serves sync endpoints from ~40 threads. The semaphore makes callers wait.
_pool_slots = threading.BoundedSemaphore(DB_MAX_CONN)


def get_conn(timeout: float = 5.0):
    if not _pool_slots.acquire(timeout=timeout):
        raise RuntimeError(f"No DB connection free after {timeout}s")
    try:
        return db_pool.getconn()
    except Exception:
        _pool_slots.release()
        raise


def put_conn(conn) -> None:
    db_pool.putconn(conn)
    _pool_slots.release()


# ---------------------------------------------------------------------------
# PYDANTIC CONTRACT
# ---------------------------------------------------------------------------
class TelemetryPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # float so firmware may send 45.6; rounded to int before storage.
    moisture: float = Field(..., ge=0, le=100)
    temperature: float = Field(..., ge=-50.0, le=100.0)


# ---------------------------------------------------------------------------
# AI: PREDICTION + DECISION
# ---------------------------------------------------------------------------
def predict_drying_rate(temperature: float, moisture: int):
    """Return predicted moisture loss in %/hr, or None if the model is unavailable."""
    if ai_model is None:
        return None
    t = min(max(temperature, MODEL_TEMP_RANGE[0]), MODEL_TEMP_RANGE[1])
    m = min(max(moisture, MODEL_MOISTURE_RANGE[0]), MODEL_MOISTURE_RANGE[1])
    if t != temperature:
        log.warning("Temperature %.2fC outside model range %s; clipped to %.2fC.", temperature, MODEL_TEMP_RANGE, t)
    try:
        row = {"temperature": float(t), "moisture": float(m)}
        features = pd.DataFrame([[row[name] for name in MODEL_FEATURES]], columns=MODEL_FEATURES)
        return max(0.0, float(ai_model.predict(features)[0]))
    except Exception:
        log.exception("AI prediction failed; falling back to rule-only decision.")
        return None


def decide_irrigation(moisture: int, drying_rate):
    """
    Predictive reflex agent.
      1. Below CRITICAL_MOISTURE -> water (safety rule, works without AI).
      2. AI forecasts critical level within PREDICTION_HORIZON_H -> water early.
      3. Otherwise -> no water.
    Returns (decision, hours_to_critical, reason).
    """
    hours_to_critical = None
    if drying_rate and drying_rate > 0:
        hours_to_critical = max(0.0, (moisture - CRITICAL_MOISTURE) / drying_rate)

    if moisture < CRITICAL_MOISTURE:
        return "PUMP_ON", hours_to_critical, f"moisture below critical {CRITICAL_MOISTURE:g}%"
    if hours_to_critical is not None and hours_to_critical < PREDICTION_HORIZON_H:
        return "PUMP_ON", hours_to_critical, f"forecast to reach {CRITICAL_MOISTURE:g}% in {hours_to_critical:.1f}h"
    if drying_rate is None:
        return "PUMP_OFF", None, "rule-only mode (AI unavailable), moisture ok"
    return "PUMP_OFF", hours_to_critical, "moisture ok"


# ---------------------------------------------------------------------------
# FASTAPI APP
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Smart-Grow Telemetry API",
    version="1.0.0",
    description="AI-Integrated Telemetry Backend",
)


@app.exception_handler(RequestValidationError)
async def log_rejected_packet(request: Request, exc: RequestValidationError):
    # Log the raw body so a misbehaving ESP32 can be debugged from the server console.
    body = (await request.body())[:500]
    client = request.client.host if request.client else "?"
    log.warning("422 from %s | body=%r | errors=%s", client, body, exc.errors())
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@app.get("/health", tags=["ops"])
def health() -> JSONResponse:
    conn = None
    ai_status = "loaded" if ai_model is not None else "unavailable"
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
        return JSONResponse({"status": "ok", "db": "connected", "ai_model": ai_status,
                             "time": datetime.now(timezone.utc).isoformat()})
    except Exception as exc:
        log.error("Health check failed: %s", exc)
        return JSONResponse({"status": "degraded", "db": "unreachable", "ai_model": ai_status},
                            status_code=503)
    finally:
        if conn:
            put_conn(conn)


# Trailing-slash alias: ESP32 HTTPClient does not follow the 307 FastAPI would otherwise send.
@app.post("/api/v1/telemetry/", status_code=status.HTTP_201_CREATED, include_in_schema=False)
@app.post("/api/v1/telemetry", status_code=status.HTTP_201_CREATED, tags=["telemetry"])
def ingest_telemetry(packet: TelemetryPacket) -> dict:
    moisture = int(round(packet.moisture))

    # 1. AI prediction + 2. decision
    drying_rate = predict_drying_rate(packet.temperature, moisture)
    decision, hours_to_critical, reason = decide_irrigation(moisture, drying_rate)

    rate_txt = f"{drying_rate:.2f}%/hr" if drying_rate is not None else "n/a"
    eta_txt = f"{hours_to_critical:.1f}h" if hours_to_critical is not None else "n/a"
    log.info("Packet moisture=%d%% temp=%.2fC | rate=%s to_critical=%s | %s (%s)",
             moisture, packet.temperature, rate_txt, eta_txt, decision, reason)

    # 3. Save reading + AI output to database
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO telemetry_logs (moisture, temperature, predicted_drying_rate, pump_decision)
                VALUES (%s, %s, %s, %s) RETURNING id, recorded_at;
                """,
                (moisture, packet.temperature,
                 round(drying_rate, 2) if drying_rate is not None else None, decision),
            )
            row_id, recorded_at = cur.fetchone()
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        log.exception("DB insert failed.")
        raise HTTPException(status_code=500, detail="Failed to persist packet.")
    finally:
        if conn:
            put_conn(conn)

    # 4. Return AI insight to the ESP32/Client
    return {
        "status": "ok",
        "row_id": row_id,
        "ai_prediction": {
            "predicted_drying_rate": round(drying_rate, 2) if drying_rate is not None else None,
            "hours_to_critical": round(hours_to_critical, 1) if hours_to_critical is not None else None,
            "decision": decision,
            "reason": reason,
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.server:app", host=API_HOST, port=API_PORT, reload=True)
