# Kencoder_SagarPath
Multimodal logistics routing (Coastal Sea, Inland Waterways, Rail, Road, Air) &amp; marine weather intelligence platform for India.
# SagarPath — India Multimodal Logistics & Marine Weather Intelligence
**Developed by:** Kencoder Secure Systems Private Limited

SagarPath is an integrated multimodal freight route, transit-time, cost, and weather-risk engine designed for India's coastal shipping corridors, National Waterways (IWAI), Indian Railways (FOIS), roadways, and air cargo terminals.

## Core Capabilities
1. **Multimodal Route Comparison:** Side-by-side comparison of All-Road vs. Road + Coastal Sea vs. Road + Inland Waterway vs. Road + Rail.
2. **Time-Stepped Metocean Weather Routing:** Queries wave height, swell period, and wind speeds along coastal and river waypoints based on vessel ETA.
3. **ULIP & PM GatiShakti Ready:** Modular connector architecture built to ingest official APIs from India's Unified Logistics Interface Platform (FOIS, PCS 1x, IWAI PANI, AAICLAS).

## Repository Structure
- `data/east_coast_ports.geojson` — East Coast Indian ports, inland waterway terminals & rail ICDs.
- `backend/requirements.txt` — Python dependencies.
- `backend/main.py` — FastAPI routing engine & live marine weather fetcher.
- `frontend/index.html` — Interactive MapLibre GL + OpenSeaMap visualization dashboard.

## Quick Start (Local Prototype)
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```
Then open `frontend/index.html` in your browser to calculate routes (e.g., **Machilipatnam to Visakhapatnam**).

