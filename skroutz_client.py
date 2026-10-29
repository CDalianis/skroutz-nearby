"""Read public Skroutz search pages and the per-product store map."""

from __future__ import annotations

import base64
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

BASE = "https://www.skroutz.gr"


class SkroutzError(Exception):
    pass


class SkroutzClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tls = threading.local()
        self._http = requests.Session(impersonate="chrome")
        self._cache: dict[str, dict] = {}

    def search(self, query: str, page_path: str | None = None) -> dict:
        query = (query or "").strip()
        if page_path:
            path = _safe_path(page_path)
            url = BASE + path
            cache_key = "page:" + path
        else:
            if len(query) < 2:
                raise SkroutzError("Type at least 2 characters.")
            url = f"{BASE}/search?keyphrase={quote(query)}"
            cache_key = "q:" + query.casefold()

        with self._lock:
            cached = self._cache.get(cache_key)
            if cached:
                return cached
            result = self._search_url(url)
            self._cache[cache_key] = result
            if len(self._cache) > 200:
                self._cache.pop(next(iter(self._cache)))
            return result

    def store_sites(self, products: list) -> list[dict]:
        jobs = []
        for product in products or []:
            if not isinstance(product, dict):
                continue
            try:
                sku_id = int(product.get("id"))
            except (TypeError, ValueError):
                continue
            url = product.get("url") if isinstance(product.get("url"), str) else ""
            name = product.get("name") if isinstance(product.get("name"), str) else ""
            jobs.append((sku_id, url, name))
        if not jobs:
            return []
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(lambda job: self._store_site(*job), jobs))

    def map_places(self, products: list) -> list[dict]:
        jobs = []
        for product in products or []:
            if not isinstance(product, dict):
                continue
            try:
                sku_id = int(product.get("id"))
            except (TypeError, ValueError):
                continue
            name = product.get("name") if isinstance(product.get("name"), str) else ""
            jobs.append((sku_id, name))
        if not jobs:
            return []
        with ThreadPoolExecutor(max_workers=6) as pool:
            return list(pool.map(lambda job: self._map_places_one(*job), jobs))

    def _map_places_one(self, sku_id: int, name: str) -> dict:
        try:
            places = self._locations(sku_id)
        except SkroutzError:
            places = []
        return {
            "id": sku_id,
            "name": name,
            "places": [
                {"lat": place["lat"], "lng": place["lng"], "shop_name": place.get("shop_name") or "Store"}
                for place in places
            ],
        }

    def _locations(self, sku_id: int) -> list[dict]:
        sku_id = int(sku_id)
        cache_key = f"loc:{sku_id}"
        with self._lock:
            cached = self._cache.get(cache_key)
        if isinstance(cached, list):
            return cached
        response = self._fetch(
            f"{BASE}/s/{sku_id}/locations.json",
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE}/s/{sku_id}",
            },
        )
        if response is None:
            raise SkroutzError("Skroutz did not return store locations.")
        try:
            payload = response.json()
        except Exception as exc:
            raise SkroutzError("Skroutz did not return store locations.") from exc
        if isinstance(payload, dict):
            payload = payload.get("data", [])
        if not isinstance(payload, list):
            raise SkroutzError("Skroutz returned an unexpected store list.")
        places = []
        for shop in payload:
            if isinstance(shop, dict):
                places.extend(_places_for_shop(shop, {}))
        with self._lock:
            existing = self._cache.get(cache_key)
            if isinstance(existing, list):
                return existing
            self._cache[cache_key] = places
            if len(self._cache) > 200:
                self._cache.pop(next(iter(self._cache)))
        return places

    def stores(self, sku_id: int, product_url: str | None = None) -> dict:
        sku_id = int(sku_id)
        cache_key = f"sku:{sku_id}"
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached:
                return cached
        places = [dict(place) for place in self._locations(sku_id)]
        prices = self._shop_prices(sku_id, product_url)
        for place in places:
            try:
                shop_id = int(place.get("shop_id")) if place.get("shop_id") is not None else None
            except (TypeError, ValueError):
                shop_id = None
            price = prices.get(shop_id) if shop_id is not None else None
            place["price"] = price
            place["price_label"] = _euro(price) if price is not None else None
        result = {"places": places}
        with self._lock:
            self._cache[cache_key] = result
        return result

    def _search_url(self, url: str) -> dict:
        response = self._get(url, headers={"Accept": "text/html"})
        soup = BeautifulSoup(response.text, "html.parser")
        script = soup.find("script", attrs={"data-hypernova-key": "SSRListing"})
        if script is None:
            if "result" in response.text.lower() and "0" in (soup.get_text(" ", strip=True)[:400]):
                return {
                    "products": [],
                    "total": 0,
                    "next_page": None,
                    "page": 1,
                    "filters": _filters_from_soup(soup),
                }
            raise SkroutzError("Skroutz did not return a product list for that search.")
        raw = script.string or script.get_text() or ""
        try:
            data = _decode_listing(raw)
            block = data["initialData"]["data"]
            skus = block.get("skus") or []
            page = block.get("page") or {}
        except Exception as exc:
            raise SkroutzError("Could not read the Skroutz search results.") from exc

        products = []
        seen = set()
        for sku in skus:
            item = _map_sku(sku)
            if not item or item["id"] in seen:
                continue
            seen.add(item["id"])
            products.append(item)

        return {
            "products": products,
            "total": page.get("results") or len(products),
            "page": page.get("current_page") or 1,
            "next_page": _next_page(soup, page),
            "filters": _filters_from_soup(soup),
        }

    def _get(self, url: str, headers: dict | None = None):
        merged = {"Accept-Language": "el-GR,el;q=0.9,en;q=0.8"}
        if headers:
            merged.update(headers)
        try:
            response = self._http.get(url, headers=merged, timeout=30, allow_redirects=True)
        except Exception as exc:
            raise SkroutzError("Could not reach Skroutz. Check your internet connection.") from exc
        if response.status_code == 403 or "cf-mitigated" in response.headers:
            raise SkroutzError("Skroutz blocked this request. Wait a moment and try again.")
        if response.status_code >= 400:
            raise SkroutzError(f"Skroutz returned an error ({response.status_code}).")
        return response

    def _shop_prices(self, sku_id: int, product_url: str | None = None) -> dict[int, float]:
        url = f"{BASE}/s/{sku_id}"
        if isinstance(product_url, str):
            clean = product_url.split("#", 1)[0].split("?", 1)[0]
            if clean.startswith(f"{BASE}/s/{sku_id}/") or clean.startswith(f"{BASE}/s/{sku_id}.html"):
                url = clean
        try:
            response = self._get(
                url,
                headers={"Accept": "text/html", "Referer": url},
            )
        except SkroutzError:
            return {}
        text = response.text.replace("&quot;", '"').replace("&#34;", '"')
        prices: dict[int, float] = {}
        for shop_id, raw_price in re.findall(r'"shop_id":(\d+),"type":"[^"]+","price":([0-9]+(?:\.[0-9]+)?)', text):
            value = float(raw_price)
            shop_id = int(shop_id)
            if shop_id not in prices or value < prices[shop_id]:
                prices[shop_id] = value
        return prices

    def _store_site(self, sku_id: int, product_url: str, product_name: str = "") -> dict:
        cache_key = f"site:{sku_id}"
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached:
            return cached
        result = {"id": sku_id, "shop_name": None, "shop_site": None}
        if not _product_page_url(sku_id, product_url):
            return result
        offers = _offer_cards(self._read(f"{BASE}/s/{sku_id}/shops_list"))
        if not offers:
            shop_path, shop_name = _shop_profile(self._read(_product_page_url(sku_id, product_url) or ""))
            if shop_path:
                offers = [{"shop_name": shop_name, "shop_path": shop_path, "title": product_name, "price": 0}]
        searches = 0
        for offer in offers[:2]:
            tokens = _search_queries(offer.get("title") or "", product_name)
            origin = self._origin_for_shop(offer.get("shop_path") or "")
            if not tokens or not origin:
                continue
            for token in tokens[:2]:
                found = self._find_shop_product(origin, token)
                searches += 1
                if found:
                    result["shop_name"] = offer.get("shop_name")
                    result["shop_site"] = found
                    break
                if searches >= 4:
                    break
            if result["shop_site"] or searches >= 4:
                break
        with self._lock:
            self._cache[cache_key] = result
            if len(self._cache) > 200:
                self._cache.pop(next(iter(self._cache)))
        return result

    def _origin_for_shop(self, shop_path: str) -> str | None:
        if not shop_path.startswith("/shop/"):
            return None
        cache_key = "origin:" + shop_path
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return cached.get("origin")
        origin = _shop_origin(self._read(BASE + shop_path))
        with self._lock:
            self._cache[cache_key] = {"origin": origin}
        return origin

    def _find_shop_product(self, origin: str, token: str) -> str | None:
        host_key = _bare_host(urlsplit(origin).netloc)
        cache_key = "home:" + host_key
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached is None:
            home = self._open_home(origin)
            targets = []
            host = host_key
            if home is not None:
                final = urlsplit(home.url)
                home_url = f"{final.scheme}://{final.netloc}"
                host = _bare_host(final.netloc)
                targets = _search_targets(home_url, home.text)[:2]
            cached = {"targets": targets, "host": host}
            with self._lock:
                self._cache[cache_key] = cached
        for action, field, extra in cached.get("targets") or []:
            found = self._search_for_token(action, field, token, cached.get("host") or host_key, extra)
            if found:
                return found
        return None

    def _search_for_token(self, action: str, field: str, token: str, host: str, extra: dict | None = None) -> str | None:
        parts = urlsplit(action)
        if parts.scheme not in ("http", "https"):
            return None
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(extra or {})
        query[field] = token
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
        response = self._fetch(url)
        if response is None:
            return None
        return _match_product(response.url, response.text, token, host)

    def _open_home(self, origin: str):
        home = self._fetch(origin)
        if home is not None:
            return home
        parts = urlsplit(origin)
        host = parts.netloc
        alternate = host[4:] if host.startswith("www.") else "www." + host
        return self._fetch(urlunsplit((parts.scheme or "https", alternate, "/", "", "")))

    def _fetch(self, url: str, headers: dict | None = None):
        merged = {"Accept": "text/html", "Accept-Language": "el-GR,el;q=0.9,en;q=0.8"}
        if headers:
            merged.update(headers)
        try:
            response = self._session().get(
                url,
                headers=merged,
                timeout=20,
                allow_redirects=True,
            )
        except Exception:
            return None
        if response.status_code >= 400:
            return None
        return response

    def _session(self):
        session = getattr(self._tls, "session", None)
        if session is None:
            session = requests.Session(impersonate="chrome")
            self._tls.session = session
        return session

    def _read(self, url: str) -> str:
        response = self._fetch(url)
        return response.text if response is not None else ""


def _product_page_url(sku_id: int, product_url: str) -> str | None:
    if not isinstance(product_url, str):
        return None
    clean = product_url.split("#", 1)[0].split("?", 1)[0]
    prefix = f"{BASE}/s/{sku_id}"
    if clean == prefix or clean.startswith(prefix + "/") or clean.startswith(prefix + ".html"):
        return clean
    return None


def _shop_profile(html: str) -> tuple[str | None, str | None]:
    match = re.search(r'href="(/shop/(\d+)/([^"/?#]+))(?:#|")', html or "")
    if not match:
        return None, None
    slug = unquote(match.group(3)).replace("-", " ").strip()
    return match.group(1), slug or None


_MODEL = re.compile(r"\b[A-Za-z]{2,20}\d+[A-Za-z0-9-]*\b")
_SPACED_MODEL = re.compile(r"\b([A-Za-z]{3,20})\s+(\d{1,4})\b")
_GENERIC_WORDS = {"nespresso", "black", "white", "with", "from", "plus", "the", "and", "for", "set"}
_QUERY_NAMES = {"q", "s", "query", "search", "search_query", "keyword", "k"}
_FREE_MAIL = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.gr",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "icloud.com",
    "aol.com",
}


def _model_tokens(*texts: str) -> list[str]:
    found = []
    for text in texts:
        found.extend(_MODEL.findall(text or ""))
        for word, number in _SPACED_MODEL.findall(text or ""):
            if int(number) > 9:
                continue
            found.append(f"{word}{number}")
            found.append(f"{word} {number}")
    tokens = []
    seen = set()
    for token in sorted(found, key=len, reverse=True):
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


def _search_queries(*texts: str) -> list[str]:
    tokens = _model_tokens(*texts)
    if tokens:
        return tokens[:2]
    words = []
    seen = set()
    for text in texts:
        for word in re.findall(r"[A-Za-z]{4,}", text or ""):
            key = word.lower()
            if key in _GENERIC_WORDS or key in seen:
                continue
            seen.add(key)
            words.append(word)
    if len(words) >= 2:
        return [" ".join(words[:4])]
    return []


def _offer_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    cards = []
    for card in soup.select("li.product-card-redesigned"):
        title_el = card.select_one(".product-name")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        image = card.select_one("img[alt]")
        shop_name = image.get("alt").strip() if image and image.get("alt") else None
        shop_path = None
        for anchor in card.select("a[href]"):
            match = re.match(r"(/shop/\d+/[^/?#]+)", anchor.get("href") or "")
            if not match:
                continue
            shop_path = match.group(1)
            if not shop_name:
                shop_name = unquote(shop_path.rsplit("/", 1)[-1]).replace("-", " ")
            break
        if not shop_path and card.get("data-shop-id"):
            shop_path = f"/shop/{card.get('data-shop-id')}"
        try:
            price = float(card.get("data-raw-price"))
        except (TypeError, ValueError):
            price = 10**9
        if shop_path:
            cards.append({"shop_name": shop_name, "shop_path": shop_path, "title": title, "price": price})
    cards.sort(key=lambda item: item["price"])
    return cards


def _shop_origin(html: str) -> str | None:
    site = _shop_website(html)
    if site:
        return site
    domain = ""
    tagged = re.search(r"\[AT\]([A-Za-z0-9.-]+\.[A-Za-z]{2,})", html or "", re.I)
    if tagged:
        domain = tagged.group(1).lower()
    else:
        mail = re.search(r"mailto:([^\"'?]+)", html or "", re.I)
        if mail and "@" in mail.group(1):
            domain = mail.group(1).split("@", 1)[1].lower().strip()
    if not domain or domain in _FREE_MAIL or domain.endswith("skroutz.gr"):
        return None
    if domain.startswith("www."):
        return "https://" + domain
    return "https://www." + domain


def _bare_host(netloc: str) -> str:
    host = (netloc or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _same_site(url: str, host: str) -> bool:
    return _bare_host(urlsplit(url).netloc) == host


def _is_search_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return "catalogsearch" in path or path.rstrip("/").endswith("/search")


def _search_targets(origin: str, html: str) -> list[tuple[str, str, dict]]:
    soup = BeautifulSoup(html or "", "html.parser")
    targets = []
    for form in soup.select("form"):
        method = (form.get("method") or "get").lower()
        if method not in ("get", ""):
            continue
        field = None
        extra = {}
        for control in form.select("input"):
            name = control.get("name") or ""
            kind = (control.get("type") or "text").lower()
            if kind == "hidden" and name and control.get("value"):
                extra[name] = control.get("value")
            elif field is None and (kind == "search" or name.lower() in _QUERY_NAMES):
                field = name
        if not field:
            continue
        action = urljoin(origin.rstrip("/") + "/", form.get("action") or "")
        targets.append((action, field, extra))
        break
    root = origin.rstrip("/")
    lowered = (html or "").lower()
    guesses = []
    if "catalogsearch" in lowered or not targets:
        guesses.append((root + "/catalogsearch/result/", "q", {}))
    if "wp-content" in lowered or "woocommerce" in lowered:
        guesses.append((root + "/", "s", {}))
    if not targets:
        guesses.append((root + "/search", "q", {}))
    for guess in guesses:
        if guess not in targets:
            targets.append(guess)
    return targets


def _mentions(blob: str, token: str) -> bool:
    parts = [part for part in token.lower().replace("-", " ").split() if part]
    haystack = blob.lower().replace("-", " ")
    return bool(parts) and all(part in haystack for part in parts)


def _match_product(final_url: str, html: str, token: str, host: str) -> str | None:
    if not _same_site(final_url, host):
        return None
    soup = BeautifulSoup(html or "", "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    canonical_el = soup.select_one("link[rel=canonical]")
    canonical = (canonical_el.get("href") if canonical_el else "") or ""
    if not _is_search_url(final_url) and _mentions(f"{final_url} {title} {canonical}", token):
        chosen = canonical if canonical and _same_site(canonical, host) and not _is_search_url(canonical) else final_url
        if _mentions(f"{chosen} {title}", token) and not _is_search_url(chosen):
            return chosen.split("#", 1)[0]
    for selector in (".product-item-link", ".product-name a", ".product-title a"):
        for anchor in soup.select(selector):
            href = urljoin(final_url, anchor.get("href") or "")
            blob = f"{href} {anchor.get_text(' ', strip=True)}"
            if not _mentions(blob, token) or not _same_site(href, host) or _is_search_url(href):
                continue
            return href.split("#", 1)[0]
    return None


def _shop_website(html: str) -> str | None:
    soup = BeautifulSoup(html or "", "html.parser")
    anchor = soup.select_one("a.shop-web")
    href = (anchor.get("href") if anchor else "") or ""
    href = href.strip()
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    if parsed.scheme not in ("http", "https") or not host:
        return None
    if "skroutz." in host or host.endswith("scdn.gr"):
        return None
    return href


def _decode_listing(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("<!--"):
        text = text[4:]
    if text.endswith("-->"):
        text = text[:-3]
    text = re.sub(r"\s+", "", text)
    pad = "=" * ((-len(text)) % 4)
    return json.loads(base64.b64decode(text + pad))


def _safe_path(path: str) -> str:
    path = (path or "").strip()
    if not path.startswith("/") or path.startswith("//") or "\\" in path or "://" in path:
        raise SkroutzError("That results page is not valid.")
    return path


def _plain(value) -> str:
    if not value:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _https(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return url


def _map_sku(sku: dict) -> dict | None:
    sku_id = sku.get("id")
    name = (sku.get("name") or "").strip()
    if not sku_id or not name:
        return None
    path = ((sku.get("sku_url") or "").split("?")[0]) or f"/s/{sku_id}"
    delivery = sku.get("unified_delivery_label") or {}
    return {
        "id": int(sku_id),
        "name": _plain(name),
        "price": sku.get("price") or None,
        "shop_count": sku.get("shop_count") or 0,
        "shop_name": sku.get("shop_name") or None,
        "image": _https(sku.get("image_url")),
        "rating": sku.get("review_score") or None,
        "reviews": sku.get("reviews_count") or 0,
        "specs": sku.get("spec_summary") or "",
        "delivery": delivery.get("estimate") or delivery.get("text") or "",
        "url": BASE + path,
    }


def _rating_score(shop: dict) -> float | None:
    text = str(shop.get("formatted_latest_review_score") or "").strip().replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        value = None
    if value is not None:
        return value
    percent = shop.get("rating_percent")
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        return None
    if percent <= 0:
        return None
    return percent / 20


def _euro(value: float) -> str:
    whole, frac = f"{value:.2f}".split(".")
    groups = []
    while whole:
        groups.append(whole[-3:])
        whole = whole[:-3]
    return f"{'.'.join(reversed(groups))},{frac} €"


def _places_for_shop(shop: dict, prices: dict[int, float] | None = None) -> list[dict]:
    places = []
    shop_url = _https(shop.get("url"))
    for loc in shop.get("locations") or []:
        try:
            lat = float(loc.get("lat"))
            lng = float(loc.get("lng"))
        except (TypeError, ValueError):
            continue
        if lat != lat or lng != lng:
            continue
        address = loc.get("address") or {}
        hours = loc.get("extended_working_hours") or {}
        schedule = hours.get("working_schedule") or {}
        clean_hours = {}
        if isinstance(schedule, dict):
            for day, slots in schedule.items():
                if isinstance(slots, list):
                    clean_hours[str(day)] = [str(slot) for slot in slots]
                elif slots:
                    clean_hours[str(day)] = [str(slots)]
        try:
            shop_id = int(shop.get("id"))
        except (TypeError, ValueError):
            shop_id = None
        price = (prices or {}).get(shop_id) if shop_id is not None else None
        places.append(
            {
                "shop_id": shop.get("id"),
                "shop_name": _plain(shop.get("name")) or "Store",
                "shop_url": shop_url,
                "logo": _https(shop.get("logo_url")),
                "rating": shop.get("formatted_latest_review_score") or None,
                "rating_score": _rating_score(shop),
                "price": price,
                "price_label": _euro(price) if price is not None else None,
                "reviews": shop.get("latest_reviews_and_feedbacks_count") or 0,
                "phone": shop.get("phone") or None,
                "lat": lat,
                "lng": lng,
                "address": _plain(loc.get("full_address") or address.get("full")),
                "city": _plain(address.get("city")),
                "pickup": bool(loc.get("pickup_point")),
                "store": bool(loc.get("store")),
                "note": _plain(loc.get("feature_description")),
                "hours_label": hours.get("label") or "",
                "hours": clean_hours,
            }
        )
    return places


def _filters_from_soup(soup: BeautifulSoup) -> dict:
    sort = []
    groups = []
    for group in soup.select(".filter-group"):
        title_el = group.select_one(".filter-group-title h3")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        options = []
        for anchor in group.select("a.filter-option"):
            href = (anchor.get("href") or "").strip()
            if not href.startswith("/") or href.startswith("//"):
                continue
            label_el = anchor.select_one(".filter-text")
            name = (anchor.get("title") or "").strip()
            if not name and label_el:
                name = label_el.get_text(" ", strip=True)
            if not name:
                continue
            small = anchor.select_one("small")
            count_text = small.get_text(strip=True) if small else ""
            count = int(count_text) if count_text.isdigit() else None
            classes = anchor.get("class") or []
            options.append(
                {
                    "name": name,
                    "count": count,
                    "url": href,
                    "selected": "selected" in classes,
                }
            )
        if not options:
            continue
        if title == "Ταξινόμηση" or "order_by=" in options[0]["url"]:
            sort = options
            continue
        visible = [option for option in options if option["selected"] or option["count"] is None or option["count"] > 0]
        if not visible:
            continue
        groups.append({"name": title or visible[0]["name"], "options": visible})
    return {"sort": sort, "groups": groups}


def _next_page(soup: BeautifulSoup, page: dict) -> str | None:
    current = int(page.get("current_page") or 1)
    total = int(page.get("total_pages") or 1)
    if current >= total:
        return None
    for anchor in soup.select("a[href*='page=']"):
        classes = " ".join(anchor.get("class") or [])
        href = anchor.get("href") or ""
        if "next" in classes and href.startswith("/"):
            return href
    current_url = page.get("current_url") or ""
    if not current_url.startswith("/"):
        return None
    current_url = re.sub(r"([?&])page=\d+", "", current_url).rstrip("?&")
    sep = "&" if "?" in current_url else "?"
    return f"{current_url}{sep}page={current + 1}"
