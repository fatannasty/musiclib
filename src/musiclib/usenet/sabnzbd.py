from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx


class SabnzbdError(Exception):
    pass


class Sabnzbd:
    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.Client(timeout=30, follow_redirects=True)

    def _call(self, mode: str, extra: Optional[Dict[str, Any]] = None,
              files: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.api_key:
            raise SabnzbdError("sabnzbd api_key not configured")
        params = {"mode": mode, "apikey": self.api_key, "output": "json"}
        params.update(extra or {})
        try:
            if files:
                response = self.client.post(
                    f"{self.url}/api", params=params, files=files
                )
            else:
                response = self.client.get(f"{self.url}/api", params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise SabnzbdError(f"cannot reach SABnzbd at {self.url}: {exc}") from exc
        except ValueError as exc:
            raise SabnzbdError(f"invalid JSON from SABnzbd: {exc}") from exc
        if isinstance(payload.get("status"), bool) and not payload["status"] and "error" in payload:
            raise SabnzbdError(payload.get("error", "unknown SABnzbd error"))
        return payload

    def version(self) -> str:
        return str(self._call("version").get("version", ""))

    def add_url(self, nzb_url: str, name: Optional[str] = None,
                category: Optional[str] = None) -> str:
        extra: Dict[str, Any] = {"name": nzb_url}
        if name:
            extra["nzbname"] = name
        if category:
            extra["cat"] = category
        payload = self._call("addurl", extra)
        ids = payload.get("nzo_ids") or []
        if not ids:
            raise SabnzbdError("SABnzbd accepted the job but returned no nzo_id")
        return ids[0]

    def add_file(self, path: str, name: Optional[str] = None,
                 category: Optional[str] = None) -> str:
        nzb_path = Path(path)
        extra: Dict[str, Any] = {}
        if name:
            extra["nzbname"] = name
        if category:
            extra["cat"] = category
        payload = self._call(
            "addfile", extra, files={"nzbfile": (nzb_path.name, nzb_path.read_bytes())}
        )
        ids = payload.get("nzo_ids") or []
        if not ids:
            raise SabnzbdError("SABnzbd accepted the job but returned no nzo_id")
        return ids[0]

    def queue(self, start: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        payload = self._call("queue", {"start": start, "limit": limit})
        return payload.get("queue", {}).get("slots", [])

    def history(self, start: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        payload = self._call("history", {"start": start, "limit": limit})
        return payload.get("history", {}).get("slots", [])

    def job_status(self, nzo_id: str) -> Optional[str]:
        for slot in self.queue(limit=500):
            if slot.get("nzo_id") == nzo_id:
                return "downloading"
        for slot in self.history(limit=500):
            if slot.get("nzo_id") == nzo_id:
                status = slot.get("status", "")
                if status == "Completed":
                    return "completed"
                if "FAIL" in status.upper():
                    return "failed"
                return "completed"
        return None
