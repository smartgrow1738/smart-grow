"""
Smart-Grow :: Telemetry Backend with AI Integration
"""

import os
import logging
from datetime import datetime, timezone
import pickle
import threading
from contextlib import asynccontextmanager
from typing import Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
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
# Used by the model when a node has no temperature sensor (stored as NULL, never as a reading)
DEFAULT_TEMPERATURE: float = float(os.getenv("DEFAULT_TEMPERATURE", 25.0))  # C

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
# NODE ACTIVITY (in memory, feeds the dashboard's readiness checks)
# ---------------------------------------------------------------------------
NODE_STALE_S = 20     # firmware posts every 5 s; 4 missed packets -> warning
NODE_OFFLINE_S = 60   # a minute of silence -> node considered offline
SERVER_STARTED = datetime.now(timezone.utc)

_activity_lock = threading.Lock()
_activity = {"last_ip": None, "last_seen": None, "accepted": 0,
             "rejected": 0, "last_reject_at": None, "last_reject": None}


def note_packet(ip: str) -> None:
    with _activity_lock:
        _activity.update(last_ip=ip, last_seen=datetime.now(timezone.utc))
        _activity["accepted"] += 1


def note_reject(ip: str, summary: str) -> None:
    with _activity_lock:
        _activity.update(last_ip=ip, last_reject_at=datetime.now(timezone.utc), last_reject=summary)
        _activity["rejected"] += 1


# ---------------------------------------------------------------------------
# PYDANTIC CONTRACT
# ---------------------------------------------------------------------------
class TelemetryPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # float so firmware may send 45.6; rounded to int before storage.
    moisture: float = Field(..., ge=0, le=100)
    # Optional: nodes without a DHT11 omit it; the model then assumes DEFAULT_TEMPERATURE.
    temperature: Optional[float] = Field(None, ge=-50.0, le=100.0)


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
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Dashboard is self-contained (no CDNs: the demo hotspot may have no internet), so it
# runs under a strict CSP. Not applied to /docs, whose Swagger UI loads from a CDN.
DASHBOARD_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                 "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("Dashboard: http://localhost:%d/  (phones on the same network: http://<laptop-IP>:%d/)",
             API_PORT, API_PORT)
    yield
    db_pool.closeall()


app = FastAPI(
    title="Smart-Grow Telemetry API",
    version="1.0.0",
    description="AI-Integrated Telemetry Backend",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Content-Security-Policy"] = DASHBOARD_CSP
        response.headers["X-Frame-Options"] = "DENY"
    return response


@app.exception_handler(RequestValidationError)
async def log_rejected_packet(request: Request, exc: RequestValidationError):
    # Log the raw body so a misbehaving ESP32 can be debugged from the server console.
    body = (await request.body())[:500]
    client = request.client.host if request.client else "?"
    log.warning("422 from %s | body=%r | errors=%s", client, body, exc.errors())
    if request.url.path.startswith("/api/v1/telemetry"):  # what-if form typos are not node faults
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", ()) if x != "body")
        note_reject(client, f"{loc or 'body'}: {first.get('msg', 'invalid packet')}"[:200])
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


def run_agent(packet: TelemetryPacket) -> dict:
    """Prediction + decision for one reading; shared by ingest and the what-if endpoint."""
    moisture = int(round(packet.moisture))
    temp_measured = packet.temperature is not None
    temperature = packet.temperature if temp_measured else DEFAULT_TEMPERATURE
    drying_rate = predict_drying_rate(temperature, moisture)
    decision, hours_to_critical, reason = decide_irrigation(moisture, drying_rate)
    return {"moisture": moisture, "temperature": temperature, "temp_measured": temp_measured,
            "drying_rate": drying_rate, "decision": decision,
            "hours_to_critical": hours_to_critical, "reason": reason}


def ai_reply(r: dict) -> dict:
    return {
        "predicted_drying_rate": round(r["drying_rate"], 2) if r["drying_rate"] is not None else None,
        "hours_to_critical": round(r["hours_to_critical"], 1) if r["hours_to_critical"] is not None else None,
        "decision": r["decision"],
        "reason": r["reason"],
        "temperature_assumed": not r["temp_measured"],
    }


# Trailing-slash alias: ESP32 HTTPClient does not follow the 307 FastAPI would otherwise send.
@app.post("/api/v1/telemetry/", status_code=status.HTTP_201_CREATED, include_in_schema=False)
@app.post("/api/v1/telemetry", status_code=status.HTTP_201_CREATED, tags=["telemetry"])
def ingest_telemetry(packet: TelemetryPacket, request: Request) -> dict:
    # 1. AI prediction + 2. decision
    r = run_agent(packet)
    moisture, decision = r["moisture"], r["decision"]
    drying_rate, hours_to_critical = r["drying_rate"], r["hours_to_critical"]

    rate_txt = f"{drying_rate:.2f}%/hr" if drying_rate is not None else "n/a"
    eta_txt = f"{hours_to_critical:.1f}h" if hours_to_critical is not None else "n/a"
    temp_txt = f"{r['temperature']:.2f}C" if r["temp_measured"] else f"n/a (assumed {r['temperature']:g}C)"
    log.info("Packet moisture=%d%% temp=%s | rate=%s to_critical=%s | %s (%s)",
             moisture, temp_txt, rate_txt, eta_txt, decision, r["reason"])

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

    note_packet(request.client.host if request.client else "?")

    # 4. Return AI insight to the ESP32/Client
    return {"status": "ok", "row_id": row_id, "ai_prediction": ai_reply(r)}


@app.post("/api/v1/predict", tags=["telemetry"])
def what_if(packet: TelemetryPacket) -> dict:
    """Ask the AI what it would decide for a reading. Nothing is stored and no pump moves."""
    return {"status": "ok", "stored": False, "ai_prediction": ai_reply(run_agent(packet))}


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
STATUS_HISTORY = 120  # ~10 min of readings at one per 5 s


def _check(cid: str, label: str, state: str, detail: str, fix: Optional[str] = None) -> dict:
    return {"id": cid, "label": label, "state": state, "detail": detail, "fix": fix}


def _age_s(now: datetime, then: Optional[datetime]) -> Optional[float]:
    return None if then is None else max(0.0, (now - then).total_seconds())


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"), headers={"Cache-Control": "no-cache"})


@app.get("/api/v1/status", tags=["ops"])
def system_status() -> JSONResponse:
    """Readiness checks + recent readings for the dashboard. Read-only; reports failures, never 500s."""
    now = datetime.now(timezone.utc)
    with _activity_lock:
        act = dict(_activity)
    checks, rows = [], []

    conn = None
    try:
        conn = get_conn(timeout=2.0)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT recorded_at, moisture, temperature, predicted_drying_rate, pump_decision
                FROM telemetry_logs ORDER BY recorded_at DESC LIMIT %s;
                """,
                (STATUS_HISTORY,),
            )
            rows = cur.fetchall()
        checks.append(_check("database", "Database", "ok", "PostgreSQL connected, readings are being stored."))
    except Exception as exc:
        log.error("Status: DB check failed: %s", exc)
        checks.append(_check("database", "Database", "fail",
                             "PostgreSQL is not reachable, so readings cannot be stored.",
                             "Start the PostgreSQL service and check DB_PASSWORD in .env, then restart the backend."))
    finally:
        if conn:
            put_conn(conn)

    if ai_model is not None:
        checks.append(_check("ai_model", "AI model", "ok", "Random Forest loaded: predictive watering is active."))
    else:
        checks.append(_check("ai_model", "AI model", "warn",
                             f"Model not loaded: running on the safety rule only (water below {CRITICAL_MOISTURE:g}%).",
                             "Run: python ml_models/train_predictor.py, then restart the backend."))

    # Sensor node: in-memory since this server started, else the newest stored reading.
    last_seen = act["last_seen"] or (rows[0][0] if rows else None)
    age = _age_s(now, last_seen)
    ip = act["last_ip"]
    source = ("simulator on this laptop" if ip in ("127.0.0.1", "::1")
              else f"ESP32 at {ip}" if ip else "sensor node")
    node_fix = ("Check the ESP32 is powered and its Serial Monitor shows 'Wi-Fi OK', that it is on the same "
                "network as this laptop, that SERVER_URL in secrets.h points at this laptop's IP, and that "
                "port 8000 is allowed through Windows Firewall.")
    if age is None:
        checks.append(_check("node", "Sensor node", "fail", "No reading has ever been received.", node_fix))
    elif age <= NODE_STALE_S:
        checks.append(_check("node", "Sensor node", "ok", f"Online: {source}, last reading {age:.0f}s ago."))
    elif age <= NODE_OFFLINE_S:
        checks.append(_check("node", "Sensor node", "warn",
                             f"Readings are late: last one from {source} {age:.0f}s ago.", node_fix))
    else:
        checks.append(_check("node", "Sensor node", "fail", f"Offline: no reading for {age / 60:.0f} min.", node_fix))

    # Sensor sanity: a probe stuck at an extreme usually means wiring or placement.
    recent = [m for (t, m, *_rest) in rows[:6] if _age_s(now, t) <= NODE_OFFLINE_S]
    if len(recent) >= 6 and all(m == 0 for m in recent):
        checks.append(_check("sensor", "Soil probe", "warn", "Reading 0% on every recent packet.",
                             "If the probe is in soil: push it deeper and press the soil around it. Otherwise "
                             "check the AO wire goes to GPIO34 and the module VCC/GND are connected."))
    elif len(recent) >= 6 and all(m == 100 for m in recent):
        checks.append(_check("sensor", "Soil probe", "warn", "Reading 100% on every recent packet.",
                             "The probe may be standing in water or its prongs are bridged; dry and reseat it."))
    elif recent:
        checks.append(_check("sensor", "Soil probe", "ok", f"Readings look plausible ({min(recent)}-{max(recent)}%)."))

    reject_age = _age_s(now, act["last_reject_at"])
    if reject_age is not None and reject_age <= 300:
        checks.append(_check("packets", "Packet format", "warn",
                             f"{act['rejected']} packet(s) rejected since start; last {reject_age:.0f}s ago: "
                             f"{act['last_reject']}",
                             "The node is sending malformed JSON; see the 422 lines in the backend console."))
    else:
        checks.append(_check("packets", "Packet format", "ok",
                             f"{act['accepted']} packet(s) accepted since the server started, none rejected recently."))

    states = {c["state"] for c in checks}
    overall = "fail" if "fail" in states else "warn" if "warn" in states else "ok"

    latest = None
    if rows:
        t, m, temp, rate, decision = rows[0]
        rate = float(rate) if rate is not None else None
        _decision, hours, reason = decide_irrigation(m, rate)
        latest = {"age_s": _age_s(now, t), "moisture": m,
                  "temperature": float(temp) if temp is not None else None,
                  "predicted_drying_rate": rate, "decision": decision,
                  "hours_to_critical": round(hours, 1) if hours is not None else None, "reason": reason}

    history = [{"age_s": _age_s(now, t), "moisture": m, "decision": d,
                "rate": float(r) if r is not None else None}
               for (t, m, _temp, r, d) in reversed(rows)]

    body = {
        "overall": overall,
        "checks": checks,
        "latest": latest,
        "history": history,
        "uptime_s": _age_s(now, SERVER_STARTED),
        "config": {"critical_moisture": CRITICAL_MOISTURE, "prediction_horizon_h": PREDICTION_HORIZON_H,
                   "default_temperature": DEFAULT_TEMPERATURE},
    }
    return JSONResponse(jsonable_encoder(body), headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.server:app", host=API_HOST, port=API_PORT, reload=True)
