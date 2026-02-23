import asyncio
import os
import csv
import json
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from tapo import ApiClient

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

CONFIG_FILE = "config.json"
RESULTS_FOLDER = "./results"

measurement_task = None
active_websocket = None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config():
    if not os.path.exists(CONFIG_FILE):
        default = {
            "username": "",
            "password": "",
            "filename": "measurement_data",
            "ip_addresses": [],
            "selected_ip": "",
            "measure_interval": 2,
            "measure_duration": 600,
            "results_folder": RESULTS_FOLDER,
        }
        save_config(default)
        return default
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)


def get_unique_filename(folder, filename):
    base = filename
    counter = 1
    path = os.path.join(folder, f"{filename}.csv")
    while os.path.exists(path):
        path = os.path.join(folder, f"{base}_{counter}.csv")
        counter += 1
    return path


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html") as f:
        return f.read()


@app.get("/config")
async def get_config():
    return load_config()


@app.post("/config")
async def post_config(data: dict):
    config = load_config()
    config.update(data)
    save_config(config)
    return {"ok": True}


@app.post("/connect")
async def connect(data: dict):
    """Test connectivity to a Tapo device."""
    ip = data.get("ip")
    username = data.get("username")
    password = data.get("password")
    if not ip or not username or not password:
        return {"ok": False, "message": "IP, username and password are required."}
    try:
        client = ApiClient(username, password)
        device = await asyncio.wait_for(client.p110(ip), timeout=5)
        await asyncio.wait_for(device.get_device_info_json(), timeout=5)
        return {"ok": True, "message": f"Connected to {ip}"}
    except asyncio.TimeoutError:
        return {"ok": False, "message": f"Timeout: {ip} is unreachable"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@app.post("/stop")
async def stop_measurement():
    global measurement_task
    if measurement_task and not measurement_task.done():
        measurement_task.cancel()
        return {"ok": True, "message": "Measurement stopped."}
    return {"ok": False, "message": "No active measurement."}


@app.get("/results")
async def list_results():
    folder = load_config().get("results_folder", RESULTS_FOLDER)
    os.makedirs(folder, exist_ok=True)
    files = sorted(
        [f for f in os.listdir(folder) if f.endswith(".csv")],
        key=lambda f: os.path.getmtime(os.path.join(folder, f)),
        reverse=True,
    )
    return {"files": files}


@app.get("/download/{filename}")
async def download(filename: str):
    folder = load_config().get("results_folder", RESULTS_FOLDER)
    path = os.path.join(folder, filename)
    if not os.path.exists(path):
        return {"error": "File not found"}
    return FileResponse(path, media_type="text/csv", filename=filename)


# ---------------------------------------------------------------------------
# WebSocket — streaming measurement
# ---------------------------------------------------------------------------

@app.websocket("/ws/measure")
async def websocket_measure(websocket: WebSocket):
    global measurement_task, active_websocket
    await websocket.accept()
    active_websocket = websocket

    async def send(msg: dict):
        try:
            await websocket.send_json(msg)
        except Exception:
            pass

    try:
        # Receive the start payload from the client
        raw = await websocket.receive_json()
        username  = raw.get("username", "")
        password  = raw.get("password", "")
        ip        = raw.get("ip", "")
        filename  = raw.get("filename", "measurement_data")
        folder    = raw.get("results_folder", RESULTS_FOLDER)
        interval  = float(raw.get("measure_interval", 2))
        duration  = int(raw.get("measure_duration", 600))

        if not ip or not username or not password:
            await send({"type": "error", "message": "IP, username and password are required."})
            return

        os.makedirs(folder, exist_ok=True)
        csv_path = get_unique_filename(folder, filename)

        # Save config
        config = load_config()
        config.update({
            "username": username, "password": password,
            "filename": filename, "results_folder": folder,
            "measure_interval": interval, "measure_duration": duration,
        })
        save_config(config)

        await send({"type": "status", "message": f"Connecting to {ip}..."})

        client = ApiClient(username, password)
        try:
            device = await asyncio.wait_for(client.p110(ip), timeout=5)
        except Exception as e:
            await send({"type": "error", "message": f"Connection failed: {e}"})
            return

        await send({"type": "status", "message": "Connected. Measuring..."})

        measurements = []
        start_time = asyncio.get_event_loop().time()
        end_time = start_time + duration
        current_power = 0
        interrupted = False

        measurement_task = asyncio.current_task()

        while asyncio.get_event_loop().time() < end_time:
            try:
                energy_data = await asyncio.wait_for(device.get_energy_usage(), timeout=5)
                current_power = energy_data.current_power
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # keep last known value

            timestamp = datetime.now()
            measurements.append({"timestamp": timestamp.isoformat(), "power": current_power})

            elapsed = asyncio.get_event_loop().time() - start_time
            remaining = max(0, int(duration - elapsed))
            progress = min(100, (elapsed / duration) * 100)

            await send({
                "type": "reading",
                "timestamp": timestamp.strftime("%H:%M:%S"),
                "power": current_power,
                "progress": round(progress, 1),
                "remaining": remaining,
            })

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                interrupted = True
                break

        if not interrupted:
            # Write CSV
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["timestamp", "power"])
                writer.writeheader()
                writer.writerows(measurements)
            csv_filename = os.path.basename(csv_path)
            await send({
                "type": "complete",
                "message": f"Saved {len(measurements)} readings.",
                "filename": csv_filename,
            })
        else:
            # Save on cancel too if we have data
            if measurements:
                with open(csv_path, "w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=["timestamp", "power"])
                    writer.writeheader()
                    writer.writerows(measurements)
                csv_filename = os.path.basename(csv_path)
                await send({
                    "type": "cancelled",
                    "message": f"Measurement stopped. {len(measurements)} readings saved.",
                    "filename": csv_filename,
                })
            else:
                await send({"type": "cancelled", "message": "Measurement stopped. No data saved."})

    except asyncio.CancelledError:
        pass
    except WebSocketDisconnect:
        if measurement_task and not measurement_task.done():
            measurement_task.cancel()
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        active_websocket = None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8081, reload=True)
