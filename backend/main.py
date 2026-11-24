import json
import math
from pathlib import Path
from typing import Dict, Any, List, Optional
import searoute as sr
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from ulip_client import ULIPClient
from weather_service import MarineWeatherService

app = FastAPI(
    title="SagarPath Multimodal Routing Engine",
    description="Kencoder Secure Systems Pvt Ltd - Coastal, Waterway, Rail & Road Logistics API",
    version="0.3.0"
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
RATES_PATH = BASE_DIR / "data" / "freight_rates.json"

ulip = ULIPClient()
weather_engine = MarineWeatherService()


def load_rates() -> Dict[str, Any]:
    with open(RATES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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
    lon1, lat1 = math.radians(coord1), math.radians(coord1)
    lon2, lat2 = math.radians(coord2), math.radians(coord2)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def polyline_length_km(coords: List[List[float]]) -> float:
    return sum(haversine_km(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def find_inland_waterway_route(origin_id: str, dest_id: str) -> Optional[Dict[str, Any]]:
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


@app.get("/api/hubs")
def list_hubs():
    return {"hubs": list(load_hubs().values())}


@app.get("/api/ulip/port-status/{port_id}")
async def port_ulip_status(port_id: str):
    return await ulip.get_port_pcs_vessels(port_id)


@app.get("/api/compare")
async def compare_routes(
    origin_id: str,
    dest_id: str,
    cargo_tonnes: float = 20.0,
    commodity: str = "bulk_agri"
):
    hubs = load_hubs()
    rates_cfg = load_rates()
    mode_benchmarks = rates_cfg["modes"]
    multiplier = rates_cfg["commodity_multipliers"].get(commodity, 1.0)

    if origin_id not in hubs or dest_id not in hubs:
        raise HTTPException(status_code=404, detail="Invalid origin_id or dest_id")
    if origin_id == dest_id:
        raise HTTPException(status_code=400, detail="Origin and destination must differ")

    orig, dest = hubs[origin_id], hubs[dest_id]
    straight_km = haversine_km(orig["coords"], dest["coords"])

    def build_option(mode_key: str, custom_label: Optional[str], distance_km: float, geometry: Any, extra: Dict[str, Any] = None):
        cfg = mode_benchmarks[mode_key]
        transit_hrs = round((distance_km / cfg["avg_speed_kmh"]) + cfg["dwell_hours"], 1)
        total_cost = round(
            ((distance_km * cfg["cost_per_tkm"] * multiplier) + cfg["handling_inr_per_t"]) * cargo_tonnes
        )
        co2_kg = round((distance_km * cfg["co2_g_per_tkm"] * cargo_tonnes) / 1000.0, 1)
        res = {
            "mode": mode_key,
            "label": custom_label or cfg["label"],
            "distance_km": round(distance_km, 1),
            "estimated_hours": transit_hrs,
            "estimated_cost_inr": total_cost,
            "co2_emissions_kg": co2_kg,
            "geometry": geometry
        }
        if extra:
            res.update(extra)
        return res

    options = []

    # 1. Inland Waterway Option (NW-1 or NW-4)
    iw_route = find_inland_waterway_route(origin_id, dest_id)
    if iw_route:
        iw_hrs = (iw_route["distance_km"] / mode_benchmarks["inland_waterway"]["avg_speed_kmh"]) + mode_benchmarks["inland_waterway"]["dwell_hours"]
        river_weather = await weather_engine.evaluate_route_weather(
            iw_route["geometry"]["coordinates"], iw_hrs, is_inland=True
        )
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

    # 2. Coastal Sea Shipping Option
    if orig["type"] == "seaport" and dest["type"] == "seaport":
        sea_feature = sr.searoute(orig["coords"], dest["coords"], units="km")
        sea_km = round(sea_feature["properties"]["length"], 1)
        sea_nm = round(sea_km * 0.539957, 1)
        sea_hrs = (sea_km / mode_benchmarks["coastal_sea"]["avg_speed_kmh"]) + mode_benchmarks["coastal_sea"]["dwell_hours"]
        sea_weather = await weather_engine.evaluate_route_weather(
            sea_feature["geometry"]["coordinates"], sea_hrs, is_inland=False
        )
        options.append(build_option(
            "coastal_sea",
            None,
            sea_km,
            sea_feature["geometry"],
            {"distance_nm": sea_nm, "marine_weather": sea_weather}
        ))

    # 3. Rail & Road Options
    direct_line = {"type": "LineString", "coordinates": [orig["coords"], dest["coords"]]}
    options.append(build_option("rail", None, straight_km * mode_benchmarks["rail"]["circuity"], direct_line))
    options.append(build_option("road", None, straight_km * mode_benchmarks["road"]["circuity"], direct_line))

    return {
        "origin": orig,
        "destination": dest,
        "cargo_tonnes": cargo_tonnes,
        "commodity": commodity,
        "options": options
    }
