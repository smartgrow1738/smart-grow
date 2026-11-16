# Smart-Grow

An AI-integrated IoT irrigation system (SWE414). A soil sensor reports moisture and
temperature, an AI model forecasts how fast the soil is drying, and the system decides
whether to water — **before** the plant gets too dry, not after.

## How it works (one reading, start to finish)

```
 ESP32 (sensors)  --HTTP POST JSON-->  FastAPI backend  --->  AI model  --->  Decision  --->  PostgreSQL
       ^                                                                          |
       +--------------------- reply: PUMP_ON / PUMP_OFF + reason -----------------+
```

1. **Sense** — Every few seconds the ESP32 sends `{"moisture": 42, "temperature": 24.5}`
   to `POST /api/v1/telemetry`.
2. **Validate** — The backend rejects anything malformed (out-of-range, missing, extra fields,
   failed sensor = `null`) with HTTP 422 and logs the raw packet so we can debug the board.
3. **Predict** — A Random Forest model (`ml_models/moisture_predictor.pkl`) predicts the
   soil's **drying rate** (% moisture lost per hour) from temperature and current moisture.
4. **Decide** — From that rate the backend works out *how many hours until the soil hits
   the critical level (30%)*:
   - moisture already below 30% → **PUMP_ON** (safety rule)
   - AI forecasts 30% within the next 2 hours → **PUMP_ON** (predictive — waters early on hot days)
   - otherwise → **PUMP_OFF**
5. **Store** — The reading, the AI's predicted rate and the decision are saved together in
   `telemetry_logs`, so every decision is auditable later.
6. **Act** — The decision and a human-readable reason go back to the ESP32 in the reply,
   which switches the pump relay.

Example reply:
```json
{"status": "ok", "row_id": 412,
 "ai_prediction": {"predicted_drying_rate": 3.23, "hours_to_critical": 0.9,
                   "decision": "PUMP_ON", "reason": "forecast to reach 30% in 0.9h"}}
```

**Why "AI-integrated" and not just a threshold:** a plain rule waters only once the soil is
already dry. Our agent looks ahead: at 33% on a 40°C afternoon it waters now (0.9h left);
at 33% on a cool night it waits. If the model is ever unavailable the system keeps running
on the safety rule alone, so plants are never left unprotected.

## Project map

| Path | What it is |
|---|---|
| `backend/server.py` | FastAPI API: validation, AI prediction, irrigation decision, DB writes |
| `ml_models/train_predictor.py` | Trains the drying-rate model and prints its accuracy (test MAE ≈ 0.43 %/hr, R² ≈ 0.73) |
| `database/schema.sql` | `telemetry_logs` table (safe to re-run; migrates older databases) |
| `simulations/mock_esp32.py` | Fake ESP32 — lets us test the whole pipeline without hardware |
| `notebooks/` | Week 1 telemetry validation |
| `firmware/` | ESP32 code (hardware arriving) |

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env                    # then set DB_PASSWORD
psql -U postgres -d smartgrow -f database/schema.sql
python ml_models/train_predictor.py     # only if the .pkl is missing or you change the data
python -m backend.server                # API on 0.0.0.0:8000, docs at http://localhost:8000/docs
python simulations/mock_esp32.py        # in a second terminal: fake sensor traffic
```

Health check: `GET /health` → `{"status": "ok", "db": "connected", "ai_model": "loaded"}`.

## Connecting the real ESP32

- Post to `http://<laptop-LAN-IP>:8000/api/v1/telemetry` (find the IP with `ipconfig`;
  laptop and ESP32 must be on the same Wi-Fi).
- Header `Content-Type: application/json`; body exactly `{"moisture": 0-100, "temperature": float}`.
- Read `ai_prediction.decision` from the reply: `"PUMP_ON"` → relay on.
- Windows Firewall blocks port 8000 by default. Allow it once from an **admin** PowerShell:
  `New-NetFirewallRule -DisplayName "Smart-Grow API" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow`
- If the board gets HTTP 422, the backend console shows exactly which field was wrong.

## Tuning (in `.env`, no code change)

| Variable | Default | Meaning |
|---|---|---|
| `CRITICAL_MOISTURE` | 30 | Always water below this % |
| `PREDICTION_HORIZON_H` | 2.0 | Water early if critical level is forecast within this many hours |

## Known limitations

- The model is trained on **synthetic** data (hotter + drier = faster drying). Once the real
  sensor has logged a few days, retrain on real readings from `telemetry_logs`.
- The decision is stateless (one reading at a time); there is no pump run-time limit yet.
- No authentication on the API — fine on a private network, not for the public internet.
