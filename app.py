"""Desktop window for searching Skroutz and mapping nearby stores."""

from __future__ import annotations

import functools
import http.server
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

import webview

from location_service import LocationService
from skroutz_client import SkroutzClient, SkroutzError

def _resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def _save_path() -> Path:
    if getattr(sys, "frozen", False):
        folder = Path(os.environ.get("APPDATA") or str(Path.home())) / "Where to buy"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / "user_location.json"
    return Path(__file__).resolve().parent / "user_location.json"


ROOT = _resource_root()
SAVE_PATH = _save_path()
UI = ROOT / "ui" / "index.html"


class Api:
    def __init__(self) -> None:
        self.client = SkroutzClient()
        self.locations = LocationService()

    def search(self, query: str, page_path: str | None = None) -> dict:
        try:
            return {"ok": True, **self.client.search(query, page_path or None)}
        except SkroutzError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "Search failed. Check your connection and try again."}

    def stores(self, sku_id, product_url=None) -> dict:
        try:
            return {"ok": True, **self.client.stores(sku_id, product_url)}
        except SkroutzError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "Could not load stores for that product."}

    def locate(self) -> dict:
        saved = _read_location()
        if saved:
            return {"ok": True, **saved}
        return self.detect()

    def detect(self) -> dict:
        found = self.locations.detect()
        if not found:
            return {
                "ok": False,
                "error": "Location is unavailable. Type a city or neighbourhood instead.",
            }
        _write_location(found)
        return {"ok": True, **found}

    def find_places(self, query: str) -> dict:
        try:
            return {"ok": True, "matches": self.locations.search(query)}
        except Exception:
            return {"ok": False, "error": "Could not look up that place.", "matches": []}

    def pin_location(self, lat, lng) -> dict:
        try:
            lat = float(lat)
            lng = float(lng)
        except (TypeError, ValueError):
            return {"ok": False, "error": "That spot is not valid."}
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return {"ok": False, "error": "That spot is not valid."}
        label = self.locations.reverse_label(lat, lng) or f"{lat:.5f}, {lng:.5f}"
        return self.remember_location(lat, lng, label)

    def remember_location(self, lat, lng, label: str) -> dict:
        try:
            place = {
                "lat": float(lat),
                "lng": float(lng),
                "label": (label or "Chosen location").strip()[:120],
                "source": "typed",
                "accuracy": None,
            }
        except (TypeError, ValueError):
            return {"ok": False, "error": "That location is not valid."}
        if not (-90 <= place["lat"] <= 90 and -180 <= place["lng"] <= 180):
            return {"ok": False, "error": "That location is not valid."}
        _write_location(place)
        return {"ok": True, **place}

    def map_places(self, products) -> dict:
        try:
            return {"ok": True, "products": self.client.map_places(products or [])}
        except Exception:
            return {"ok": False, "products": []}

    def store_sites(self, products) -> dict:
        try:
            return {"ok": True, "sites": self.client.store_sites(products or [])}
        except Exception:
            return {"ok": False, "sites": [], "error": "Could not load store links."}

    def open_url(self, url: str) -> dict:
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username:
            return {"ok": False}
        webbrowser.open(url)
        return {"ok": True}

    def open_skroutz(self, url: str) -> dict:
        if not isinstance(url, str) or not url.startswith("https://www.skroutz.gr/"):
            return {"ok": False}
        return self.open_url(url)


def _read_location() -> dict | None:
    try:
        data = json.loads(SAVE_PATH.read_text(encoding="utf-8"))
        lat = float(data["lat"])
        lng = float(data["lng"])
    except Exception:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return {
        "lat": lat,
        "lng": lng,
        "label": str(data.get("label") or "Saved location"),
        "source": "saved",
        "accuracy": None,
    }


def _write_location(place: dict) -> None:
    SAVE_PATH.write_text(
        json.dumps(
            {
                "lat": place["lat"],
                "lng": place["lng"],
                "label": place.get("label") or "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return


def _ui_url() -> str:
    """Serve the window over localhost so map tiles get a normal page address."""
    handler = functools.partial(_QuietHandler, directory=str(ROOT / "ui"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return f"http://{host}:{port}/index.html"


def main() -> None:
    webview.create_window(
        "Where to buy",
        url=_ui_url(),
        js_api=Api(),
        width=1280,
        height=840,
        min_size=(1024, 680),
        background_color="#f3efe7",
        text_select=True,
    )
    webview.start()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
