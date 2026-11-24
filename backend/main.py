import json
import math
from pathlib import Path
from typing import Dict, Any, List, Optional
import httpx
import searoute as sr
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from ulip_client import ULIPClient

app = FastAPI(
    title="SagarPath Multimodal Routing Engine",
    description="Kencoder Secure Systems Pvt Ltd - Coastal, Waterway, Rail & Road Logistics API",
    version="0.2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent.parent
PORTS_PATH = BASE_DIR / "data" / "east_coast_ports.geojson"
WATERWAYS_PATH = BASE_DIR / "data" / "national_waterways.geojson"
ulip = ULIPClient()

MODE_BENCHMARKS = {
    "road": {"cost_per_tkm": 3.80, "avg_speed_kmh": 42.0, "circuity": 1.25, "handling_inr_per_t": 150},
    "rail": {"cost_per_tkm": 1.65, "avg_speed_kmh": 26.0, "circuity": 1.20, "handling_inr_per_t": 350},
    "coastal_sea": {"cost_per_tkm": 0.95, "avg_speed_kmh": 20.4, "circuity": 1.00, "handling_inr_per_t": 450},
    "inland_waterway": {"cost_per_tkm": 1.10, "avg_speed_kmh": 12.0, "circuity": 1.00, "handling_inr_per_t": 300}
}


def load_hubs() -> Dict[str, Dict[str, Any]]:
    with open(PORTS_PATH, "r", encoding="utf-8") as f:
        geojson = json.load(f)
    return {
        feat["properties"]["id"]: {
            **feat["properties"],
            "coords": feat["geometry"]["coordinates"]
        }
        for feat in geojson["features"]
    }


def haversine_km(coord1: List[float], coord2: List[float]) -> float:
    lon1, lat1 = math.radians(coord1[0]), math.radians(coord1[1])
    lon2, lat2 = math.radians(coord2[0]), math.radians(coord2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def polyline_length_km(coords: List[List[float]]) -> float:
    return sum(haversine_km(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def find_inland_waterway_route(origin_id: str, dest_id: str) -> Optional[Dict[str, Any]]:
    """Checks if both origin and destination share an active National Waterway channel."""
    with open(WATERWAYS_PATH, "r", encoding="utf-8") as f:
        nw_data = json.load(f)
    for feat in nw_data["features"]:
        props = feat["properties"]
        terminals = props.get("terminals", [])
        if origin_id in terminals and dest_id in terminals:
            coords = feat["geometry"]["coordinates"]
            if terminals.index(origin_id) > terminals.index(dest_id):
                coords = list(reversed(coords))
            return {
                "waterway_id": props["waterway_id"],
                "waterway_name": props["name"],
                "min_lad_meters": props["min_lad_meters"],
                "distance_km": round(polyline_length_km(coords), 1),
                "geometry": {"type": "LineString", "coordinates": coords}
            }
    return None


async def get_marine_weather(lon: float, lat: float) -> Dict[str, Any]:
    marine_url = f"https://open-meteo.com{lat}&longitude={lon}&current=wave_height,wave_period"
    wind_url = f"https://open-meteo.com{lat}&longitude={lon}&current=wind_speed_10m"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            m_res, w_res = await client.get(marine_url), await client.get(wind_url)
            m_data = m_res.json().get("current", {})
            w_data = w_res.json().get("current", {})
            wave_m = m_data.get("wave_height", 0.0) or 0.0
            wind_kmh = w_data.get("wind_speed_10m", 0.0) or 0.0
            risk = "HIGH (Cyclone/Swell Advisory)" if (wave_m >= 2.5 or wind_kmh >= 45) else (
                "MODERATE" if (wave_m >= 1.5 or wind_kmh >= 30) else "LOW"
            )
            return {"wave_height_m": wave_m, "wind_speed_kmh": wind_kmh, "weather_risk": risk}
    except Exception:
        return {"wave_height_m": None, "wind_speed_kmh": None, "weather_risk": "UNAVAILABLE"}


@app.get("/api/hubs")
def list_hubs():
    return {"hubs": list(load_hubs().values())}


@app.get("/api/ulip/port-status/{port_id}")
async def port_ulip_status(port_id: str):
    """Fetches live vessel calls via ULIP PCS 1x (falls back cleanly if sandbox keys are unset)."""
    return await ulip.get_port_pcs_vessels(port_id)


@app.get("/api/compare")
async def compare_routes(origin_id: str, dest_id: str, cargo_tonnes: float = 20.0):
    hubs = load_hubs()
    if origin_id not in hubs or dest_id not in hubs:
        raise HTTPException(status_code=404, detail="Invalid origin_id or dest_id")
    if origin_id == dest_id:
        raise HTTPException(status_code=400, detail="Origin and destination must differ")

    orig, dest = hubs[origin_id], hubs[dest_id]
    straight_km = haversine_km(orig["coords"], dest["coords"])

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

    options = []

    # 1. Inland Waterway Option (if connected via NW-1 or NW-4)
    iw_route = find_inland_waterway_route(origin_id, dest_id)
    if iw_route:
        iw_mid = iw_route["geometry"]["coordinates"][len(iw_route["geometry"]["coordinates"]) // 2]
        river_weather = await get_marine_weather(iw_mid[0], iw_mid[1])
        options.append(build_option(
            "inland_waterway",
            f"Inland Barge ({iw_route['waterway_id']})",
            iw_route["distance_km"],
            iw_route["geometry"],
            {
                "waterway_name": iw_route["waterway_name"],
                "min_lad_meters": iw_route["min_lad_meters"],
                "marine_weather": river_weather
            }
        ))

    # 2. Coastal Sea Shipping Option (for seaports)
    if orig["type"] == "seaport" and dest["type"] == "seaport":
        sea_feature = sr.searoute(orig["coords"], dest["coords"], units="km")
        sea_km = round(sea_feature["properties"]["length"], 1)
        sea_nm = round(sea_km * 0.539957, 1)
        sea_coords = sea_feature["geometry"]["coordinates"]
        mid_pt = sea_coords[len(sea_coords) // 2]
        sea_weather = await get_marine_weather(mid_pt[0], mid_pt[1])
        options.append(build_option(
            "coastal_sea",
            "Coastal Sea Shipping (Port-to-Port)",
            sea_km,
            sea_feature["geometry"],
            {"distance_nm": sea_nm, "marine_weather": sea_weather}
        ))

    # 3. Rail & Road Options
    direct_line = {"type": "LineString", "coordinates": [orig["coords"], dest["coords"]]}
    options.append(build_option(
        "rail",
        "Indian Railways (FOIS Freight Corridor)",
        straight_km * MODE_BENCHMARKS["rail"]["circuity"],
        direct_line
    ))
    options.append(build_option(
        "road",
        "All-Road Heavy Truck (NH Corridor)",
        straight_km * MODE_BENCHMARKS["road"]["circuity"],
        direct_line
    ))

    return {
        "origin": orig,
        "destination": dest,
        "cargo_tonnes": cargo_tonnes,
        "options": options
    }
