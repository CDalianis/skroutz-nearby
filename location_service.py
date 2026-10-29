"""Find the user's position from Windows location, then their network, then a typed place."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
IP_LOOKUP = "https://ipwho.is/"
APP_UA = "SkroutzNearby/1.0 (personal desktop shopping map)"


class LocationService:
    def detect(self) -> dict | None:
        gps = self._windows_gps()
        if gps:
            return gps
        return self._network()

    def search(self, query: str) -> list[dict]:
        query = (query or "").strip()
        if len(query) < 2:
            return []
        matches = self._nominatim(query, countrycodes="gr,cy")
        if not matches:
            matches = self._nominatim(query, countrycodes=None)
        return matches[:5]

    def reverse_label(self, lat: float, lng: float) -> str | None:
        params = {
            "lat": f"{lat:.7f}",
            "lon": f"{lng:.7f}",
            "format": "jsonv2",
            "addressdetails": 1,
            "zoom": 18,
        }
        url = NOMINATIM_REVERSE + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": APP_UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict) or data.get("error"):
            return None
        display = str(data.get("display_name") or "")
        short = ", ".join(part.strip() for part in display.split(",")[:3] if part.strip())
        return short or None

    def _windows_gps(self) -> dict | None:
        try:
            import clr

            clr.AddReference("System.Device")
            from System.Device.Location import GeoCoordinateWatcher
        except Exception:
            return None
        watcher = GeoCoordinateWatcher()
        watcher.Start()
        deadline = time.time() + 4
        while time.time() < deadline:
            status = str(watcher.Status)
            loc = watcher.Position.Location
            if status == "Ready" and not loc.IsUnknown:
                accuracy = float(loc.HorizontalAccuracy)
                if accuracy != accuracy:
                    accuracy = None
                return {
                    "lat": float(loc.Latitude),
                    "lng": float(loc.Longitude),
                    "label": "This PC",
                    "source": "gps",
                    "accuracy": accuracy,
                }
            if status in ("NoData", "Disabled"):
                return None
            time.sleep(0.25)
        return None

    def _network(self) -> dict | None:
        try:
            request = urllib.request.Request(IP_LOOKUP, headers={"User-Agent": APP_UA, "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=12) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception:
            return None
        if not data.get("success"):
            return None
        try:
            lat = float(data["latitude"])
            lng = float(data["longitude"])
        except (KeyError, TypeError, ValueError):
            return None
        city = data.get("city") or ""
        region = data.get("region") or ""
        label = ", ".join(part for part in (city, region) if part) or "Network location"
        return {"lat": lat, "lng": lng, "label": label, "source": "network", "accuracy": None}

    def _nominatim(self, query: str, countrycodes: str | None) -> list[dict]:
        params = {
            "q": query,
            "format": "jsonv2",
            "limit": 5,
            "addressdetails": 1,
        }
        if countrycodes:
            params["countrycodes"] = countrycodes
        url = NOMINATIM + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": APP_UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                rows = json.loads(response.read().decode("utf-8"))
        except Exception:
            return []
        matches = []
        for row in rows:
            try:
                lat = float(row["lat"])
                lng = float(row["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            display = row.get("display_name") or query
            short = ", ".join(part.strip() for part in display.split(",")[:3])
            matches.append({"lat": lat, "lng": lng, "label": short, "source": "typed"})
        return matches
