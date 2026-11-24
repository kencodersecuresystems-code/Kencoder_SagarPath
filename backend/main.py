import json
import math
from pathlib import Path
from typing import Dict, Any, List
import httpx
import searoute as sr
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="SagarPath Multimodal Routing Engine",
    description="Kencoder Secure Systems Pvt Ltd - Coastal, Waterway, Rail & Road Logistics API",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "east_coast_ports.geojson"

# Reference Indian Freight Benchmarks (INR per Tonne-Km & Average Commercial Speeds)
MODE_BENCHMARKS = {
    "road": {"cost_per_tkm": 3.80, "avg_speed_kmh": 42.0, "circuity": 1.25, "handling_inr_per_t": 150},
    "rail": {"cost_per_tkm": 1.65, "avg_speed_kmh": 26.0, "circuity": 1.20, "handling_inr_per_t": 350},
    "coastal_sea": {"cost_per_tkm": 0.95, "avg_speed_kmh": 20.4, "circuity": 1.00, "handling_inr_per_t": 450},  # ~11 knots
    "inland_waterway": {"cost_per_tkm": 1.10, "avg_speed_kmh": 12.0, "circuity": 1.15, "handling_inr_per_t": 300}
}


def load_hubs() -> Dict[str, Dict[str, Any]]:
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        geojson = json.load(f)
    hubs = {}
    for feat in geojson["features"]:
        hub_id = feat["properties"]["id"]
        hubs[hub_id] = {
            "id": hub_id,
            "name": feat["properties"]["name"],
            "state": feat["properties"]["state"],
            "type": feat["properties"]["type"],
            "modes": feat["properties"]["modes"],
            "corridor": feat["properties"]["corridor"],
            "coords": feat["geometry"]["coordinates"]  # [lon, lat]
        }
    return hubs


def haversine_km(coord1: List[float], coord2: List[float]) -> float:
    lon1, lat1 = math.radians(coord1[0]), math.radians(coord1[1])
    lon2, lat2 = math.radians(coord2[0]), math.radians(coord2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


async def get_marine_weather(lon: float, lat: float) -> Dict[str, Any]:
    """Fetches live wave height and wind conditions via Open-Meteo Marine & Weather APIs."""
    marine_url = f"https://open-meteo.com{lat}&longitude={lon}&current=wave_height,wave_direction,wave_period"
    wind_url = f"https://open-meteo.com{lat}&longitude={lon}&current=wind_speed_10m,wind_direction_10m"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            m_res, w_res = await client.get(marine_url), await client.get(wind_url)
            m_data = m_res.json().get("current", {})
            w_data = w_res.json().get("current", {})
            wave_m = m_data.get("wave_height", 0.0) or 0.0
            wind_kmh = w_data.get("wind_speed_10m", 0.0) or 0.0
            risk = "LOW"
            if wave_m >= 2.5 or wind_kmh >= 45:
                risk = "HIGH (Cyclone/Swell Advisory)"
            elif wave_m >= 1.5 or wind_kmh >= 30:
                risk = "MODERATE"
            return {
                "wave_height_m": wave_m,
                "wave_period_s": m_data.get("wave_period"),
                "wind_speed_kmh": wind_kmh,
                "weather_risk": risk
            }
    except Exception:
        return {"wave_height_m": None, "wind_speed_kmh": None, "weather_risk": "UNAVAILABLE"}


@app.get("/api/hubs")
def list_hubs():
    return {"hubs": list(load_hubs().values())}


@app.get("/api/compare")
async def compare_routes(origin_id: str, dest_id: str, cargo_tonnes: float = 20.0):
    hubs = load_hubs()
    if origin_id not in hubs or dest_id not in hubs:
        raise HTTPException(status_code=404, detail="Invalid origin_id or dest_id")
    if origin_id == dest_id:
        raise HTTPException(status_code=400, detail="Origin and destination must differ")

    orig, dest = hubs[origin_id], hubs[dest_id]
    straight_km = haversine_km(orig["coords"], dest["coords"])

    # 1. Sea / Coastal Route via SeaRoute
    sea_feature = sr.searoute(orig["coords"], dest["coords"], units="km")
    sea_km = round(sea_feature["properties"]["length"], 1)
    sea_nm = round(sea_km * 0.539957, 1)
    sea_coords = sea_feature["geometry"]["coordinates"]
    mid_point = sea_coords[len(sea_coords) // 2]

    # Live weather at midpoint of sea route
    weather = await get_marine_weather(mid_point[0], mid_point[1])

    def build_option(mode_key: str, label: str, distance_km: float, geometry: Any, extra: Dict[str, Any] = None):
        cfg = MODE_BENCHMARKS[mode_key]
        transit_hrs = round((distance_km / cfg["avg_speed_kmh"]) + (6.0 if mode_key != "road" else 0.0), 1)
        total_cost = round((distance_km * cfg["cost_per_tkm"] * cargo_tonnes) + (cfg["handling_inr_per_t"] * cargo_tonnes))
        res = {
            "mode": mode_key,
            "label": label,
            "distance_km": round(distance_km, 1),
            "estimated_hours": transit_hrs,
            "estimated_cost_inr": total_cost,
            "geometry": geometry
        }
        if extra:
            res.update(extra)
        return res

    # Straight-line GeoJSON placeholder until local OSRM/Valhalla road & rail profiles are connected
    direct_line = {"type": "LineString", "coordinates": [orig["coords"], dest["coords"]]}

    options = [
        build_option(
            "coastal_sea",
            "Coastal Sea Shipping (Port-to-Port)",
            sea_km,
            sea_feature["geometry"],
            {"distance_nm": sea_nm, "marine_weather": weather}
        ),
        build_option(
            "rail",
            "Indian Railways (FOIS Freight Corridor)",
            straight_km * MODE_BENCHMARKS["rail"]["circuity"],
            direct_line
        ),
        build_option(
            "road",
            "All-Road Heavy Truck (NH Corridor)",
            straight_km * MODE_BENCHMARKS["road"]["circuity"],
            direct_line
        )
    ]

    return {
        "origin": orig,
        "destination": dest,
        "cargo_tonnes": cargo_tonnes,
        "options": options
    }
