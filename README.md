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

1. **Sense** — Every 5 seconds the ESP32 sends `{"moisture": 42.0}` (plus `"temperature"` if a
   DHT11 is fitted) to `POST /api/v1/telemetry`.
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
| `firmware/smart_grow_node/` | ESP32 sketch: soil sensor -> backend -> relay |

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

## Dashboard

Open **http://localhost:8000/** once the backend is running (a phone on the same hotspot can use
`http://192.168.137.1:8000/`). It refreshes every 3 s and shows:

- **Readiness banner**: Ready / Running with warnings / Not ready, from these checks:
  database reachable, AI model loaded, sensor node online (late after 20 s, offline after 60 s),
  soil probe not stuck at 0% or 100%, no recently rejected packets. Each failing check says how to fix it.
- **Live reading**: moisture, pump decision and the AI's reason, drying rate, time to critical.
- **Chart** of the last 10 minutes with the critical line and pump-on moments.
- **Ask the AI (what-if)**: try any moisture/temperature and see the decision. Nothing is stored and no pump moves.

The dashboard is read-only and needs no internet (no CDNs). It runs under a strict Content-Security-Policy,
and all values are rendered as text, so a bad packet cannot inject anything into the page.
Data comes from `GET /api/v1/status`, and the what-if panel uses `POST /api/v1/predict`.

## Hardware rig (demo build)

Sketch: `firmware/smart_grow_node/smart_grow_node.ino` (copy `secrets.h.example` to `secrets.h` for Wi-Fi/server settings) (Arduino IDE, board "ESP32 Dev Module", no extra libraries).

| Part | Pin | Note |
|---|---|---|
| Resistive soil module VCC / GND / AO | 3V3 / GND / GPIO34 | **3V3, not 5V**: AO must stay under 3.3V. GPIO34 is ADC1 (ADC2 dies when Wi-Fi is on) |
| Relay module VCC / GND / IN | VIN(5V) / GND / GPIO26 | Set `RELAY_ACTIVE_LOW` if it is ON when it should be OFF |
| Onboard LED | GPIO2 | Mirrors pump state |

**No pump and no DHT11 on the demo rig.** The pump is shown by the relay click + LEDs (and can be
replaced by a real pump on the relay's COM/NO terminals later with no code change). Without a
temperature sensor the board sends only moisture; the AI assumes `DEFAULT_TEMPERATURE` (25°C)
and the database stores temperature as NULL, so assumed values are never mistaken for readings.

Bring-up order:
1. Flash, open Serial Monitor (115200). Note `SOIL raw=` with probe in air (`DRY_RAW`) and in water (`WET_RAW`), re-flash.
2. Set `WIFI_SSID`/`WIFI_PASS` (2.4 GHz; a phone hotspot that the laptop also joins is the safest demo network).
3. Set `SERVER_URL` to `http://<laptop-IP>:8000/api/v1/telemetry` (`ipconfig`).
4. Allow port 8000 through Windows Firewall once, from an **admin** PowerShell:
   `New-NetFirewallRule -DisplayName "Smart-Grow API" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow`
5. If the board gets HTTP 422, the backend console shows exactly which field was wrong.

## Tuning (in `.env`, no code change)

| Variable | Default | Meaning |
|---|---|---|
| `CRITICAL_MOISTURE` | 30 | Always water below this % |
| `PREDICTION_HORIZON_H` | 2.0 | Water early if critical level is forecast within this many hours |
| `DEFAULT_TEMPERATURE` | 25.0 | Temperature the AI assumes when a node sends none |

## Known limitations

- The model is trained on **synthetic** data (hotter + drier = faster drying). Once the real
  sensor has logged a few days, retrain on real readings from `telemetry_logs`.
- Demo rig has no pump or temperature sensor (see Hardware rig).
- The decision is stateless (one reading at a time); there is no pump run-time limit yet.
- No authentication on the API or dashboard. That's fine on a private network such as the laptop hotspot, but not on the public internet.
