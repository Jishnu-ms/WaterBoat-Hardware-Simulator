# WaterBoat Hardware Simulator — Standalone

This is a **single-server simulation** of the hardware.

There is no second backend.

The simulator receives waypoints and then behaves like the boat:

```text
POST /waypoints
      ↓
Waypoint 1
      ↓
fake GPS + pH + TDS + turbidity + temperature + camera image
      ↓
wait
      ↓
Waypoint 2
      ↓
fake sensors + camera image
      ↓
...
```

## Folder structure

```text
waterboat-hardware-simulator/
├── server.py
├── requirements.txt
└── images/
    ├── image1.jpg
    ├── image2.jpg
    └── ...
```

Add several water/pollution images to `images/`.

## Setup with venv

Linux/macOS:

```bash
cd waterboat-hardware-simulator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell:

```powershell
cd waterboat-hardware-simulator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Start

```bash
uvicorn server:app --reload --port 9000
```

Or:

```bash
python server.py
```

Swagger:

```text
http://127.0.0.1:9000/docs
```

## Start a mission

POST `/waypoints`:

```json
{
  "mission_id": 1,
  "interval_seconds": 5,
  "waypoints": [
    {
      "lat": 8.52140,
      "lon": 76.94310,
      "point_index": 1
    },
    {
      "lat": 8.52140,
      "lon": 76.94320,
      "point_index": 2
    },
    {
      "lat": 8.52140,
      "lon": 76.94330,
      "point_index": 3
    }
  ]
}
```

The server immediately returns:

```json
{
  "status": "simulation_started",
  "mission_id": 1,
  "waypoint_count": 3,
  "interval_seconds": 5
}
```

The background simulation then generates:

```text
t=0s    waypoint 1 measurement
t=5s    waypoint 2 measurement
t=10s   waypoint 3 measurement
```

## Check progress

GET:

```text
/status
```

Example:

```json
{
  "running": true,
  "mission_id": 1,
  "current_waypoint": 2,
  "total_waypoints": 3,
  "measurements_generated": 2
}
```

## Get all measurements

GET:

```text
/measurements
```

## Get latest measurement

GET:

```text
/measurements/latest
```

Each measurement contains:

- mission ID
- waypoint index
- timestamp
- GPS latitude
- GPS longitude
- pH
- TDS
- turbidity
- temperature
- dissolved oxygen
- camera filename
- image URL
- base64 camera image

## View a generated camera image

If the measurement says:

```text
/images/sample.jpg
```

open:

```text
http://127.0.0.1:9000/images/sample.jpg
```

## Stop

POST `/stop`:

```json
{
  "reason": "operator_stop"
}
```

## Important

The sensor values are **synthetic**. They are designed to be realistic-looking and spatially correlated, not scientifically calibrated measurements.

The camera image is also a sample image selected from the `images/` folder. It represents what the simulated camera would have captured at that waypoint.
