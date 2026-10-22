import asyncio
import base64
import io
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from PIL import Image


app = FastAPI(
    title="WaterBoat Hardware Simulator",
    description="Standalone ESP32 + sensor + camera simulator",
    version="5.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://yoloboat.vercel.app",
        "https://ai-enabled-water-pollution-monitori.vercel.app",
        "http://localhost:5173",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
IMAGE_DIR = BASE_DIR / "images"

SUPPORTED_IMAGES = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_INTERVAL = 5.0

# One independent queue per connected stream client.
subscribers: set[asyncio.Queue] = set()

# Used to safely take a consistent snapshot for new stream clients.
subscriber_lock = asyncio.Lock()

stop_event = asyncio.Event()

state: dict[str, Any] = {
    "status": "idle",
    "mission_id": None,
    "total_waypoints": 0,
    "current_waypoint": 0,
    "measurements_generated": 0,
    "started_at": None,
    "finished_at": None,
    "stop_reason": None,
    "error": None,
    "last_measurement": None,
    "measurements": [],
    "task": None,
}


# ============================================================
# MODELS
# ============================================================

class Waypoint(BaseModel):
    lat: float
    lon: float
    point_index: int | None = None


class Mission(BaseModel):
    mission_id: int = 1
    waypoints: list[Waypoint] = Field(min_length=1)
    interval_seconds: float = Field(
        default=DEFAULT_INTERVAL,
        ge=0.2,
        le=300.0,
    )


class StopRequest(BaseModel):
    reason: str = "operator_stop"


# ============================================================
# HELPERS
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def get_images() -> list[Path]:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    return sorted(
        p
        for p in IMAGE_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGES
    )


def make_rng(lat: float, lon: float, point_index: int) -> random.Random:
    return random.Random(
        f"{lat:.7f}:{lon:.7f}:{point_index}"
    )


def pollution_factor(point_index: int, total_points: int) -> float:
    """
    Smooth synthetic pollution field.
    It creates believable variation across nearby waypoints.
    """

    if total_points <= 1:
        x = 0.5
        y = 0.5
    else:
        side = max(1, math.ceil(math.sqrt(total_points)))
        index = point_index - 1

        row = index // side
        column = index % side

        x = column / max(1, side - 1)
        y = row / max(1, side - 1)

    hotspot_1 = math.exp(
        -(
            ((x - 0.25) ** 2) / 0.035
            + ((y - 0.30) ** 2) / 0.050
        )
    )

    hotspot_2 = math.exp(
        -(
            ((x - 0.78) ** 2) / 0.050
            + ((y - 0.72) ** 2) / 0.035
        )
    )

    background = 0.15 + 0.25 * x + 0.10 * y

    return clamp(
        0.15
        + 0.55 * hotspot_1
        + 0.65 * hotspot_2
        + background,
        0.0,
        1.0,
    )


def generate_sensors(
    lat: float,
    lon: float,
    point_index: int,
    total_points: int,
) -> dict[str, float]:

    rng = make_rng(lat, lon, point_index)
    pollution = pollution_factor(point_index, total_points)

    ph = 7.25 - 0.75 * pollution + rng.gauss(0, 0.08)

    tds = 260 + 620 * pollution + rng.gauss(0, 22)

    turbidity = (
        3
        + 70 * pollution**1.35
        + rng.gauss(0, 2)
    )

    temperature = (
        28
        + 2 * math.sin(
            point_index / max(total_points, 1) * math.pi
        )
        + rng.gauss(0, 0.18)
    )

    dissolved_oxygen = (
        8
        - 2.8 * pollution
        + rng.gauss(0, 0.12)
    )

    return {
        "ph": round(clamp(ph, 5.8, 8.8), 2),
        "tds_ppm": round(clamp(tds, 100, 1400), 1),
        "turbidity_ntu": round(clamp(turbidity, 0.5, 120), 1),
        "temperature_c": round(clamp(temperature, 23, 34), 2),
        "dissolved_oxygen_mg_l": round(
            clamp(dissolved_oxygen, 2, 10), 2
        ),
    }


def encode_image(image_path: Path) -> str:
    """
    Simulates the ESP32-CAM JPEG payload.
    The base64 value is stored internally but never dumped
    by the normal status/measurement APIs.
    """

    with Image.open(image_path) as image:
        image = image.convert("RGB")

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)

        return base64.b64encode(
            buffer.getvalue()
        ).decode("ascii")


def create_measurement(
    waypoint: Waypoint,
    mission_id: int,
    point_index: int,
    total_points: int,
) -> dict[str, Any]:

    images = get_images()

    if not images:
        raise FileNotFoundError(
            f"No camera images found in {IMAGE_DIR}. "
            "Add JPG/PNG/WebP files to the images folder."
        )

    image_path = images[(point_index - 1) % len(images)]

    image_base64 = encode_image(image_path)

    return {
        "mission_id": mission_id,
        "point_index": point_index,
        "timestamp": now_iso(),

        "gps": {
            "latitude": waypoint.lat,
            "longitude": waypoint.lon,
        },

        "sensors": generate_sensors(
            waypoint.lat,
            waypoint.lon,
            point_index,
            total_points,
        ),

        "camera": {
            "filename": image_path.name,
            "mime_type": "image/jpeg",
            "image_url": f"/images/{image_path.name}",

            # Internal hardware-packet simulation.
            "_image_base64": image_base64,
        },
    }


def clean_measurement(
    measurement: dict[str, Any] | None,
) -> dict[str, Any] | None:

    if measurement is None:
        return None

    return {
        "mission_id": measurement["mission_id"],
        "point_index": measurement["point_index"],
        "timestamp": measurement["timestamp"],
        "gps": measurement["gps"],
        "sensors": measurement["sensors"],
        "camera": {
            "filename": measurement["camera"]["filename"],
            "mime_type": measurement["camera"]["mime_type"],
            "image_url": measurement["camera"]["image_url"],
        },
    }


async def broadcast_measurement(measurement: dict[str, Any]) -> None:
    """
    Broadcast to EVERY connected stream client.

    This is the important difference from one shared Queue:
    one client consuming an event cannot steal it from another client.
    """

    data = clean_measurement(measurement)

    async with subscriber_lock:
        clients = list(subscribers)

    for queue in clients:
        await queue.put(data)


# ============================================================
# MISSION ENGINE
# ============================================================

async def run_mission(mission: Mission) -> None:

    state["status"] = "running"
    state["mission_id"] = mission.mission_id
    state["total_waypoints"] = len(mission.waypoints)
    state["current_waypoint"] = 0
    state["measurements_generated"] = 0
    state["measurements"] = []
    state["last_measurement"] = None
    state["started_at"] = now_iso()
    state["finished_at"] = None
    state["stop_reason"] = None
    state["error"] = None

    stop_event.clear()

    try:
        for index, waypoint in enumerate(mission.waypoints):

            if stop_event.is_set():
                state["status"] = "stopped"
                break

            point_index = (
                waypoint.point_index
                if waypoint.point_index is not None
                else index + 1
            )

            state["current_waypoint"] = point_index

            # Simulated hardware reaches waypoint and measures.
            measurement = create_measurement(
                waypoint=waypoint,
                mission_id=mission.mission_id,
                point_index=point_index,
                total_points=len(mission.waypoints),
            )

            state["measurements"].append(measurement)
            state["last_measurement"] = measurement
            state["measurements_generated"] += 1

            # Broadcast this measurement to every live stream.
            await broadcast_measurement(measurement)

            # Wait between waypoints.
            if index < len(mission.waypoints) - 1:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=mission.interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass

        else:
            state["status"] = "completed"

    except Exception as exc:
        state["status"] = "error"
        state["error"] = str(exc)

    finally:
        state["finished_at"] = now_iso()
        state["task"] = None


# ============================================================
# ROOT / STATUS
# ============================================================

@app.get("/")
def root():
    return {
        "service": "WaterBoat Hardware Simulator",
        "version": "5.0.0",
        "status": state["status"],
        "endpoints": {
            "start_mission": "POST /waypoints",
            "status": "GET /status",
            "all_measurements": "GET /measurements",
            "latest": "GET /measurements/latest",
            "live_stream": "GET /measurements/stream",
            "stop": "POST /stop",
            "images": "GET /images/{filename}",
        },
    }


@app.get("/status")
def get_status():
    return {
        "status": state["status"],
        "mission_id": state["mission_id"],
        "current_waypoint": state["current_waypoint"],
        "total_waypoints": state["total_waypoints"],
        "measurements_generated": state["measurements_generated"],
        "started_at": state["started_at"],
        "finished_at": state["finished_at"],
        "stop_reason": state["stop_reason"],
        "error": state["error"],
        "last_measurement": clean_measurement(
            state["last_measurement"]
        ),
    }


# ============================================================
# START MISSION
# ============================================================

@app.post("/waypoints")
async def receive_waypoints(mission: Mission):

    if state["status"] == "running":
        raise HTTPException(
            status_code=409,
            detail="A mission is already running.",
        )

    if not get_images():
        raise HTTPException(
            status_code=400,
            detail=(
                f"No camera images found in {IMAGE_DIR}. "
                "Add JPG/PNG/WebP images first."
            ),
        )

    task = asyncio.create_task(run_mission(mission))
    state["task"] = task

    return {
        "status": "simulation_started",
        "mission_id": mission.mission_id,
        "waypoint_count": len(mission.waypoints),
        "interval_seconds": mission.interval_seconds,
        "message": (
            "One simulated measurement will be generated "
            "at every waypoint."
        ),
    }


# ============================================================
# ALL MEASUREMENTS
# ============================================================

@app.get("/measurements")
def get_measurements():

    return {
        "count": len(state["measurements"]),
        "measurements": [
            clean_measurement(m)
            for m in state["measurements"]
        ],
    }


# ============================================================
# LATEST MEASUREMENT
# ============================================================

@app.get("/measurements/latest")
def get_latest_measurement():

    if state["last_measurement"] is None:
        return {
            "message": "No measurements generated yet.",
            "measurement": None,
        }

    return clean_measurement(
        state["last_measurement"]
    )


# ============================================================
# LIVE SSE STREAM
# ============================================================

@app.get("/measurements/stream")
async def measurement_stream():

    # Every client gets its own queue.
    client_queue: asyncio.Queue = asyncio.Queue()

    # IMPORTANT:
    # Register BEFORE taking the replay snapshot.
    # Therefore a new measurement cannot be lost between
    # "snapshot" and "subscribe".
    async with subscriber_lock:
        subscribers.add(client_queue)

        replay = [
            clean_measurement(m)
            for m in state["measurements"]
        ]

    async def event_generator():

        try:
            # ------------------------------------------------
            # Replay measurements already generated.
            # ------------------------------------------------

            for measurement in replay:
                yield (
                    "event: measurement\n"
                    f"data: {json.dumps(measurement)}\n\n"
                )

            # ------------------------------------------------
            # Connection confirmation.
            # ------------------------------------------------

            yield (
                "event: connected\n"
                'data: {"status":"live"}\n\n'
            )

            # ------------------------------------------------
            # Live measurements.
            # ------------------------------------------------

            while True:

                try:
                    measurement = await asyncio.wait_for(
                        client_queue.get(),
                        timeout=15.0,
                    )

                    yield (
                        "event: measurement\n"
                        f"data: {json.dumps(measurement)}\n\n"
                    )

                except asyncio.TimeoutError:
                    # Keep the HTTP connection alive.
                    yield (
                        ": keep-alive\n\n"
                    )

        except asyncio.CancelledError:
            pass

        finally:
            async with subscriber_lock:
                subscribers.discard(client_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ============================================================
# IMAGE
# ============================================================

@app.get("/images/{filename:path}")
def get_image(filename: str):

    # Resolve safely inside IMAGE_DIR.
    requested = (IMAGE_DIR / filename).resolve()

    try:
        requested.relative_to(
            IMAGE_DIR.resolve()
        )
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid image path.",
        )

    if not requested.exists() or not requested.is_file():
        raise HTTPException(
            status_code=404,
            detail="Image not found.",
        )

    return FileResponse(requested)


# ============================================================
# STOP
# ============================================================

@app.post("/stop")
async def stop_simulation(request: StopRequest):

    if state["status"] != "running":
        return {
            "status": "already_idle",
            "current_status": state["status"],
        }

    state["stop_reason"] = request.reason
    stop_event.set()

    return {
        "status": "stop_requested",
        "reason": request.reason,
    }


# ============================================================
# DIRECT RUN
# ============================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=9000,
        reload=True,
    )
