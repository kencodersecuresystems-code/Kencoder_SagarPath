import os
from typing import Dict, Any, Optional
import httpx


class ULIPClient:
    """
    Unified Logistics Interface Platform (ULIP) Connector for Kencoder Secure Systems Pvt Ltd.
    Supports token authentication and standardized queries across:
      - FOIS / CRIS (Indian Railways freight & goods sheds)
      - PCS 1x / IPA (Port Community System vessel calls & berth status)
      - IWAI PANI (National Waterways Least Available Depth & barge status)
    """

    def __init__(self):
        self.base_url = os.getenv("ULIP_BASE_URL", "https://dpiit.gov.in")
        self.username = os.getenv("ULIP_USERNAME", "")
        self.password = os.getenv("ULIP_PASSWORD", "")
        self._bearer_token: Optional[str] = None

    async def authenticate(self) -> Optional[str]:
        """Obtains a Bearer JWT token from ULIP staging/production gateway."""
        if not self.username or not self.password:
            return None
        login_url = f"{self.base_url}/user/login"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                login_url,
                json={"username": self.username, "password": self.password}
            )
            resp.raise_for_status()
            data = resp.json()
            self._bearer_token = data.get("response", {}).get("id")
            return self._bearer_token

    async def _post_ulip(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._bearer_token:
            token = await self.authenticate()
            if not token:
                return {"status": "SANDBOX_FALLBACK", "message": "ULIP credentials not configured; using benchmark model."}
        headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Content-Type": "application/json"
        }
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(f"{self.base_url}/{endpoint}", json=payload, headers=headers)
            if resp.status_code == 401:
                await self.authenticate()
                headers["Authorization"] = f"Bearer {self._bearer_token}"
                resp = await client.post(f"{self.base_url}/{endpoint}", json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def get_rail_fois_status(self, fnr_number: str) -> Dict[str, Any]:
        """Queries Indian Railways FOIS freight consignment & rake transit status."""
        return await self._post_ulip("FOIS/01", {"fnr": fnr_number})

    async def get_port_pcs_vessels(self, port_unlocode: str) -> Dict[str, Any]:
        """Queries Port Community System (PCS 1x) vessel call schedule by UN/LOCODE (e.g., INVTZ)."""
        return await self._post_ulip("PCS/01", {"portCode": port_unlocode})

    async def get_iwai_lad_status(self, waterway_code: str) -> Dict[str, Any]:
        """Queries IWAI PANI Portal for Least Available Depth (LAD) on NW-1, NW-2, NW-4."""
        return await self._post_ulip("IWAI/01", {"nwCode": waterway_code})
