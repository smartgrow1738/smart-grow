import math
import random
import time
from datetime import datetime

import requests

TARGET_URL: str = "http://127.0.0.1:8000/api/v1/telemetry"
INTERVAL_SECONDS: int = 5  # packet cadence
REQUEST_TIMEOUT: int = 5  # seconds before giving up
CYCLE_SECONDS: int = 180  # one dry-down + watering cycle (compressed for demos)

START_TIME: float = time.monotonic()  # cycle starts when the script starts


def simulate_field_reading() -> dict:
    now = datetime.now()
    minute_of_day = now.hour * 60 + now.minute

    # ---- Temperature: ~22°C mean, ±5°C daily swing ----
    temp_base = 22.0 + 5.0 * math.sin((minute_of_day / 1440.0) * 2 * math.pi - math.pi / 2)
    temperature = round(temp_base + random.uniform(-0.4, 0.4), 2)

    # ---- Moisture: dries 75% → 25% over 90% of the cycle, then a watering spike ----
    phase = ((time.monotonic() - START_TIME) % CYCLE_SECONDS) / CYCLE_SECONDS  # 0 → 1
    if phase < 0.9:
        moisture = int(75 - (phase / 0.9) * 50)  # crosses the 30% pump threshold
    else:  # watering event at cycle end
        moisture = 78

    return {"moisture": max(0, min(100, moisture)),
            "temperature": temperature}


# MAIN LOOP
def run_mock_node() -> None:
    print("=" * 62)
    print(" Smart-Grow :: Mock ESP32 node starting")
    print(f" Target endpoint : {TARGET_URL}")
    print(f" Cadence         : {INTERVAL_SECONDS}s | dry-down cycle: {CYCLE_SECONDS}s")
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
                ai = body.get("ai_prediction", {})
                print(
                    f"[{datetime.now():%H:%M:%S}] ✓ moisture={payload['moisture']}%  "
                    f"temp={payload['temperature']}°C  → row_id={body.get('row_id')}  "
                    f"AI={ai.get('decision')} ({ai.get('reason')})"
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
