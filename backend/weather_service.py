import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List
import httpx


class MarineWeatherService:
    """
    Time-stepped Metocean & River Weather Engine for SagarPath.
    Samples waypoints along a sea or river polyline and evaluates wave height,
    swell period, and wind gusts at the vessel's estimated time of arrival (ETA).
    """

    MARINE_API = "https://open-meteo.com"
    WEATHER_API = "https://open-meteo.com"

    def _classify_risk(self, wave_m: float, wind_kmh: float, is_inland: bool = False) -> str:
        if is_inland:
            if wind_kmh >= 40:
                return "HIGH (Strong River Crosswinds)"
            if wind_kmh >= 25:
                return "MODERATE"
            return "LOW"

        if wave_m >= 2.5 or wind_kmh >= 45:
            return "HIGH (Cyclone / Rough Swell Advisory)"
        if wave_m >= 1.5 or wind_kmh >= 30:
            return "MODERATE"
        return "LOW"

    async def fetch_point_weather(
        self,
        lon: float,
        lat: float,
        eta_hours: float = 0.0,
        is_inland: bool = False
    ) -> Dict[str, Any]:
        """Fetches forecast conditions closest to (now + eta_hours) at a given coordinate."""
        target_utc = datetime.now(timezone.utc) + timedelta(hours=eta_hours)
        target_hour_prefix = target_utc.strftime("%Y-%m-%dT%H")

        try:
            async with httpx.AsyncClient(timeout=7.0) as client:
                w_params = {
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "wind_speed_10m,wind_gusts_10m",
                    "forecast_days": 5
                }
                w_resp = await client.get(self.WEATHER_API, params=w_params)
                w_json = w_resp.json().get("hourly", {})

                times = w_json.get("time", [])
                idx = next((i for i, t in enumerate(times) if t.startswith(target_hour_prefix)), 0)
                wind_kmh = (w_json.get("wind_speed_10m") or [0.0])[idx] or 0.0
                gust_kmh = (w_json.get("wind_gusts_10m") or [0.0])[idx] or 0.0

                wave_m = 0.0
                wave_period = None
                if not is_inland:
                    m_params = {
                        "latitude": lat,
                        "longitude": lon,
                        "hourly": "wave_height,wave_period",
                        "forecast_days": 5
                    }
                    m_resp = await client.get(self.MARINE_API, params=m_params)
                    m_json = m_resp.json().get("hourly", {})
                    m_times = m_json.get("time", [])
                    m_idx = next((i for i, t in enumerate(m_times) if t.startswith(target_hour_prefix)), 0)
                    wave_m = (m_json.get("wave_height") or [0.0])[m_idx] or 0.0
                    wave_period = (m_json.get("wave_period") or [None])[m_idx]

                return {
                    "eta_offset_hours": round(eta_hours, 1),
                    "wave_height_m": round(wave_m, 2) if not is_inland else None,
                    "wave_period_s": wave_period,
                    "wind_speed_kmh": round(wind_kmh, 1),
                    "wind_gusts_kmh": round(gust_kmh, 1),
                    "weather_risk": self._classify_risk(wave_m, wind_kmh, is_inland=is_inland)
                }
        except Exception:
            return {
                "eta_offset_hours": round(eta_hours, 1),
                "wave_height_m": None,
                "wind_speed_kmh": None,
                "weather_risk": "UNAVAILABLE"
            }

    async def evaluate_route_weather(
        self,
        coordinates: List[List[float]],
        total_transit_hours: float,
        is_inland: bool = False
    ) -> Dict[str, Any]:
        """Samples Start, Midpoint, and Destination waypoints across voyage duration."""
        if not coordinates:
            return {"weather_risk": "UNAVAILABLE"}

        sample_indices = [0, len(coordinates) // 2, len(coordinates) - 1]
        fractions = [0.0, 0.5, 1.0]

        tasks = [
            self.fetch_point_weather(
                lon=coordinates[idx],
                lat=coordinates[idx],
                eta_hours=total_transit_hours * frac,
                is_inland=is_inland
            )
            for idx, frac in zip(sample_indices, fractions)
        ]
        checkpoints = await asyncio.gather(*tasks)

        # Midpoint represents primary open-water/mid-corridor exposure for summary card
        mid = checkpoints[1]
        highest_risk = mid["weather_risk"]
        for cp in checkpoints:
            if cp["weather_risk"].startswith("HIGH"):
                highest_risk = cp["weather_risk"]
                break
            if cp["weather_risk"] == "MODERATE" and highest_risk == "LOW":
                highest_risk = "MODERATE"

        return {
            "wave_height_m": mid.get("wave_height_m"),
            "wind_speed_kmh": mid.get("wind_speed_kmh"),
            "weather_risk": highest_risk,
            "checkpoints": [
                {"stage": "Departure", **checkpoints},
                {"stage": "Mid-Route", **checkpoints[1]},
                {"stage": "Arrival", **checkpoints[2]}
            ]
        }
