"""
Smart-Grow :: Mock ESP32 Field Node
===================================
Emulates the real ESP32 + capacitive moisture + DHT11 rig until hardware
arrives. Produces a *realistic* soil profile (slow drying between watering
events, daily temperature curve) so your ML pipeline later sees believable
data.

Run with:
    python -m simulations.mock_esp32
"""

import math
import random
import time
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# CONFIGURATION BLOCK
# Point TARGET_URL at a LAN/remote IP later — no other code changes needed.
# ---------------------------------------------------------------------------
TARGET_URL: str       = "http://127.0.0.1:8000/api/v1/telemetry"
INTERVAL_SECONDS: int = 5              # packet cadence
REQUEST_TIMEOUT: int  = 5              # seconds before giving up

# ---------------------------------------------------------------------------
# REALISTIC SENSOR PROFILE
# ---------------------------------------------------------------------------
def simulate_field_reading() -> dict:
    """
    Produce a plausible (moisture, temperature) pair.

    - Moisture dries slowly (evaporation) but "refills" periodically to
      simulate a watering event.
    - Temperature follows a gentle diurnal-ish sine curve + small noise.
    """
    now = datetime.now()
    minute_of_day = now.hour * 60 + now.minute

    # ---- Temperature: ~22°C mean, ±5°C daily swing ----
    temp_base  = 22.0 + 5.0 * math.sin((minute_of_day / 1440.0) * 2 * math.pi - math.pi / 2)
    temperature = round(temp_base + random.uniform(-0.4, 0.4), 2)

    # ---- Moisture: drifts downward, random watering spikes ----
    # deterministic seed per minute so it "trends" rather than jumps randomly
    random.seed(minute_of_day)
    drying = (minute_of_day % 120) / 120.0        # 0 → 1 over 2 hours
    moisture = int(75 - (drying * 35))            # 75% → 40%
    if minute_of_day % 120 >= 115:                # watering event near cycle end
        moisture = 78

    return {"moisture": max(0, min(100, moisture)),
            "temperature": temperature}


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
def run_mock_node() -> None:
    print("=" * 62)
    print(" Smart-Grow :: Mock ESP32 node starting")
    print(f" Target endpoint : {TARGET_URL}")
    print(f" Cadence         : {INTERVAL_SECONDS}s")
    print("=" * 62)

    while True:
        payload = simulate_field_reading()
        try:
            resp = requests.post(
                TARGET_URL,
                json=payload,
                timeout=REQUEST_TIMEOUT,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 201:
                body = resp.json()
                print(
                    f"[{datetime.now():%H:%M:%S}] ✓ moisture={payload['moisture']}%  "
                    f"temp={payload['temperature']}°C  → row_id={body.get('row_id')}"
                )
            else:
                print(
                    f"[{datetime.now():%H:%M:%S}] ✗ HTTP {resp.status_code}: {resp.text}"
                )
        except requests.exceptions.ConnectionError:
            print(f"[{datetime.now():%H:%M:%S}] ✗ Backend unreachable at {TARGET_URL}. "
                  "Is uvicorn running?")
        except requests.exceptions.Timeout:
            print(f"[{datetime.now():%H:%M:%S}] ✗ Request timed out after "
                  f"{REQUEST_TIMEOUT}s.")
        except requests.exceptions.RequestException as exc:
            print(f"[{datetime.now():%H:%M:%S}] ✗ Unexpected transport error: {exc}")

        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        run_mock_node()
    except KeyboardInterrupt:
        print("\n[stopped] Mock node terminated by user.")