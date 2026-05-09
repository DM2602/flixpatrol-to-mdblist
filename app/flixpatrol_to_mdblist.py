#!/usr/bin/env python3
"""
FlixPatrol Top 10 → MDBList Sync

Scrapes today's top 10 lists from FlixPatrol and syncs them to MDBList static lists.
Runs as a long-lived container with a built-in smart scheduler.

Inspired by https://github.com/Navino16/flixpatrol-top10-on-trakt
"""

import json
import logging
import os
import re
import signal
import sys
import time
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag

from scheduler import Scheduler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FLIXPATROL_BASE = "https://flixpatrol.com"
MDBLIST_API_BASE = "https://api.mdblist.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/config"))
CONFIG_FILE = CONFIG_DIR / "default.json"

TOP10_PLATFORMS = [
    "9now", "abema", "amazon", "amazon-channels", "amazon-prime", "amc-plus",
    "antenna-tv", "apple-tv", "bbc", "canal", "catchplay", "cda", "chili",
    "claro-video", "coupang-play", "crunchyroll", "discovery-plus", "disney",
    "francetv", "friday", "globoplay", "go3", "google", "hami-video", "hayu",
    "hbo-max", "hrti", "hulu", "hulu-nippon", "itunes", "jiocinema",
    "jiohotstar", "joyn", "lemino", "m6plus", "mgm-plus", "myvideo",
    "neon-tv", "netflix", "now", "oneplay", "osn", "paramount-plus",
    "peacock", "player", "pluto-tv", "raiplay", "rakuten-tv", "rtl-plus",
    "sbs", "shahid", "skyshowtime", "stan", "starz", "streamz", "telasa",
    "tf1", "tod", "trueid", "tubi", "tv-2-norge", "u-next", "viaplay",
    "videoland", "vidio", "viki", "viu", "vix", "voyo", "vudu", "watchit",
    "wavve", "wow", "zee5",
]

POPULAR_PLATFORMS = [
    "facebook", "imdb", "instagram", "letterboxd", "movie-db", "reddit",
    "rotten-tomatoes", "tmdb", "trakt", "twitter", "wikipedia", "youtube",
]

DEFAULT_CONFIG = {
    "FlixPatrolTop10": [
        {
            "platform": "netflix",
            "location": "world",
            "fallback": False,
            "limit": 10,
            "type": "movies",
            "name": "Netflix Top 10 Movies",
            "normalizeName": True,
        },
        {
            "platform": "netflix",
            "location": "world",
            "fallback": False,
            "limit": 10,
            "type": "shows",
            "name": "Netflix Top 10 Shows",
            "normalizeName": True,
        },
    ],
    "FlixPatrolPopular": [],
    "MDBList": {
        "apiKey": "YOUR_MDBLIST_API_KEY_HERE",
    },
    "Schedule": {
        "cron": "0 6,18 * * *",
        "runOnStart": True,
    },
    "Cache": {
        "enabled": True,
        "ttl": 86400,
    },
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("flixpatrol-mdblist")

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# ---------------------------------------------------------------------------
# File cache
# ---------------------------------------------------------------------------

class FileCache:
    def __init__(self, cache_dir: Path, ttl: int = 86400, enabled: bool = True):
        self.cache_dir = cache_dir
        self.ttl = ttl
        self.enabled = enabled
        if enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def get(self, key: str):
        if not self.enabled:
            return None
        p = self._path(key)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            if time.time() - data.get("ts", 0) > self.ttl:
                p.unlink(missing_ok=True)
                return None
            return data.get("v")
        except Exception:
            return None

    def set(self, key: str, value):
        if not self.enabled:
            return
        self._path(key).write_text(json.dumps({"ts": time.time(), "v": value}))

    def clear(self):
        if self.cache_dir.exists():
            for f in self.cache_dir.glob("*.json"):
                f.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# FlixPatrol scraper
# ---------------------------------------------------------------------------
#
# The FlixPatrol top-10 page has this HTML structure (verified 2026-05-02):
#
#   <h3>TOP 10 Movies</h3>
#   <table>
#     <tr>
#       <td>1.</td> <td>–</td>
#       <td><a href="/title/apex-2026/">Apex</a></td>
#       <td>4 d</td>
#     </tr>
#     ...
#   </table>
#
#   <h3>TOP 10 TV Shows</h3>
#   <table> ... </table>
#
#   <h3>TOP 10 Kids Movies</h3>
#   <table> ... </table>
#
#   <h3>TOP 10 Kids TV Shows</h3>
#   <table> ... </table>
#
# Strategy: find each h3 heading, determine its section type, then parse
# the first <table> that follows it.


class FlixPatrolScraper:
    def __init__(self, cache: FileCache):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.cache = cache

    def _get(self, url: str) -> Optional[BeautifulSoup]:
        logger.debug(f"GET {url}")
        try:
            r = self.session.get(url, timeout=30)
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except requests.RequestException as e:
            logger.error(f"HTTP error for {url}: {e}")
            return None

    # --- top 10 ---

    def get_top10(self, platform: str, location: str, media_type: str = "both",
                  limit: int = 10, fallback=False, kids: bool = False) -> list[dict]:
        """
        Fetch a top-10 list. media_type is "movies", "shows", or "both".
        Returns list of dicts: {title, url, type, rank}
        """
        url = f"{FLIXPATROL_BASE}/top10/{platform}/{location}"

        # Cache the full page HTML to avoid re-fetching for movies+shows
        cache_key = f"fp:page:{platform}:{location}"
        cached_html = self.cache.get(cache_key)
        if cached_html is not None:
            soup = BeautifulSoup(cached_html, "html.parser")
        else:
            soup = self._get(url)
            if soup:
                self.cache.set(cache_key, str(soup))

        if not soup and fallback and fallback != location:
            logger.info(f"No page for {platform}/{location}, fallback → {fallback}")
            url = f"{FLIXPATROL_BASE}/top10/{platform}/{fallback}"
            soup = self._get(url)

        if not soup:
            return []

        sections = self._parse_sections(soup)
        results = []

        for mtype in _split_types(media_type):
            section_key = (mtype, kids)
            items = sections.get(section_key, [])
            item_type = "movie" if mtype == "movies" else "show"
            for item in items:
                item["type"] = item_type
            results.extend(items)

        return results[:limit]

    def _parse_sections(self, soup: BeautifulSoup) -> dict:
        """
        Parse the page into sections keyed by (type, kids).
        Returns: {("movies", False): [...], ("shows", False): [...], ...}
        """
        sections = {}
        headings = soup.find_all(["h2", "h3"])

        for heading in headings:
            ht = heading.get_text(strip=True).lower()

            section_key = self._classify_heading(ht)
            if not section_key:
                continue

            table = self._find_next_table(heading)
            if not table:
                continue

            items = self._parse_table(table)
            if items:
                sections[section_key] = items
                logger.debug(f"  Section '{ht}' → {len(items)} items")

        return sections

    @staticmethod
    def _classify_heading(text: str) -> Optional[tuple]:
        """
        Classify a heading like "TOP 10 Movies" or "TOP 10 Kids TV Shows"
        into a (type, kids) tuple.
        """
        text = text.strip().lower()

        # Must contain "top" to be a ranking heading
        if "top" not in text:
            return None

        is_kids = "kids" in text

        # Order matters: check "tv shows" before "movies" because
        # "kids tv shows" contains neither "movie" alone
        if "tv show" in text or "tv-show" in text or "shows" in text:
            return ("shows", is_kids)
        if "movie" in text:
            return ("movies", is_kids)

        return None

    @staticmethod
    def _find_next_table(element: Tag) -> Optional[Tag]:
        """Find the first <table> after the given heading."""
        # Walk siblings
        sib = element.find_next_sibling()
        while sib:
            if isinstance(sib, Tag):
                if sib.name == "table":
                    return sib
                tbl = sib.find("table")
                if tbl:
                    return tbl
                # Stop if we hit the next heading
                if sib.name in ("h2", "h3"):
                    break
            sib = sib.find_next_sibling()

        # Broader fallback: next table anywhere in the document after heading
        return element.find_next("table")

    @staticmethod
    def _parse_table(table: Tag) -> list[dict]:
        """Extract title entries from a FlixPatrol ranking table."""
        items = []
        for row in table.find_all("tr"):
            link = row.find("a", href=re.compile(r"/title/"))
            if not link:
                continue
            title = link.get_text(strip=True)
            href = link.get("href", "")
            if not title or not href:
                continue

            full_url = (FLIXPATROL_BASE + href) if href.startswith("/") else href

            rank = len(items) + 1
            cells = row.find_all("td")
            if cells:
                rank_text = cells[0].get_text(strip=True).rstrip(".")
                if rank_text.isdigit():
                    rank = int(rank_text)

            items.append({
                "title": title,
                "url": full_url,
                "rank": rank,
            })
        return items

    # --- popular ---

    def get_popular(self, platform: str, media_type: str = "both",
                    limit: int = 100) -> list[dict]:
        results = []
        for mtype in _split_types(media_type):
            slug = {"movie-db": "movie-database",
                    "tmdb": "the-movie-database"}.get(platform, platform)
            url = f"{FLIXPATROL_BASE}/popular/{mtype}/{slug}"

            cache_key = f"fp:pop:{platform}:{mtype}"
            cached = self.cache.get(cache_key)
            if cached is not None:
                results.extend(cached)
                continue

            soup = self._get(url)
            if not soup:
                continue

            items = []
            for link in soup.find_all("a", href=re.compile(r"/title/")):
                title = link.get_text(strip=True)
                href = link.get("href", "")
                if title and href:
                    full_url = (FLIXPATROL_BASE + href) if href.startswith("/") else href
                    items.append({
                        "title": title, "url": full_url,
                        "type": "movie" if mtype == "movies" else "show",
                        "rank": len(items) + 1,
                    })
            self.cache.set(cache_key, items)
            results.extend(items)

        return results[:limit]

    # --- FlixPatrol title page → year + IMDB ID ---

    def get_title_info(self, title_url: str) -> dict:
        """
        Fetch a FlixPatrol title page and extract:
          - year (int or None)
          - media_type_hint ("movie" or "show" or None)
          - imdb_id (str or None) — if FlixPatrol links to IMDB
        """
        cache_key = f"fp:title:{title_url}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        info = {"year": None, "imdb_id": None, "media_type_hint": None}
        soup = self._get(title_url)
        if not soup:
            return info

        # --- Strategy 1: JSON-LD schema (most reliable) ---
        # FlixPatrol embeds: {"@type":"Movie","name":"Apex","dateCreated":"2026-04-24"}
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(script.string or "")
                if isinstance(ld, dict):
                    # Year from dateCreated
                    dc = ld.get("dateCreated", "")
                    if dc:
                        m = re.match(r"(\d{4})", dc)
                        if m:
                            info["year"] = int(m.group(1))
                    # Type hint
                    schema_type = ld.get("@type", "").lower()
                    if schema_type == "movie":
                        info["media_type_hint"] = "movie"
                    elif schema_type in ("tvseries", "tvshow", "series"):
                        info["media_type_hint"] = "show"
            except (json.JSONDecodeError, TypeError):
                pass

        # --- Strategy 2: Date from page metadata (e.g. "04/24/2026") ---
        if not info["year"]:
            page_text = soup.get_text()
            # Match MM/DD/YYYY pattern used in the metadata bar
            for m in re.finditer(r"\b(\d{2}/\d{2}/(\d{4}))\b", page_text):
                y = int(m.group(2))
                if 1900 <= y <= 2035:
                    info["year"] = y
                    break

        # --- Strategy 3: Bare year in span ---
        if not info["year"]:
            for span in soup.find_all("span"):
                text = span.get_text(strip=True)
                m = re.match(r"^(\d{4})$", text)
                if m:
                    y = int(m.group(1))
                    if 1900 <= y <= 2035:
                        info["year"] = y
                        break

        # --- Strategy 4: Year from URL slug (e.g. /title/apex-2026/) ---
        if not info["year"]:
            m = re.search(r"/title/.*-(\d{4})/?$", title_url)
            if m:
                y = int(m.group(1))
                if 1900 <= y <= 2035:
                    info["year"] = y

        # --- IMDB link (if present) ---
        for a in soup.find_all("a", href=True):
            m = re.search(r"imdb\.com/title/(tt\d+)", a["href"])
            if m:
                info["imdb_id"] = m.group(1)
                break

        logger.debug(f"  Title info for {title_url}: {info}")
        self.cache.set(cache_key, info)
        return info


def _split_types(media_type: str) -> list[str]:
    if media_type == "movies":
        return ["movies"]
    if media_type == "shows":
        return ["shows"]
    return ["movies", "shows"]


# ---------------------------------------------------------------------------
# MDBList API client
# ---------------------------------------------------------------------------

class MDBListClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def _req(self, method: str, path: str, params: dict = None, **kwargs):
        params = params or {}
        params["apikey"] = self.api_key
        url = f"{MDBLIST_API_BASE}{path}"
        logger.debug(f"MDBLIST {method} {path}")
        try:
            r = self.session.request(method, url, params=params, timeout=30, **kwargs)
            r.raise_for_status()
            return r.json() if r.text.strip() else None
        except requests.RequestException as e:
            logger.error(f"MDBList API error ({method} {path}): {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.debug(f"Response body: {e.response.text[:500]}")
            return None

    def get_limits(self) -> Optional[dict]:
        return self._req("GET", "/user")

    def get_my_lists(self) -> list[dict]:
        r = self._req("GET", "/lists/user")
        return r if isinstance(r, list) else []

    def create_list(self, name: str) -> Optional[dict]:
        return self._req("POST", "/lists", json={"name": name})

    def get_list_items(self, list_id: int) -> Optional[dict]:
        return self._req("GET", f"/lists/{list_id}/items")

    def add_item(self, list_id: int, media_type: str, imdb_id: str = None,
                 tmdb_id: int = None) -> Optional[dict]:
        """
        Add a single item to a static list.
        Uses query parameters: movie_imdb, movie_tmdb, show_imdb, show_tmdb
        Matches the CLI pattern: update list-items -a add -i {id} --movie-imdb tt...
        """
        params = {}
        prefix = "movie" if media_type == "movie" else "show"
        if imdb_id:
            params[f"{prefix}_imdb"] = imdb_id
        elif tmdb_id:
            params[f"{prefix}_tmdb"] = tmdb_id
        else:
            return None
        return self._req("POST", f"/lists/{list_id}/items/add", params=params)

    def remove_item(self, list_id: int, media_type: str, imdb_id: str = None,
                    tmdb_id: int = None) -> Optional[dict]:
        """Remove a single item from a static list."""
        params = {}
        prefix = "movie" if media_type == "movie" else "show"
        if imdb_id:
            params[f"{prefix}_imdb"] = imdb_id
        elif tmdb_id:
            params[f"{prefix}_tmdb"] = tmdb_id
        else:
            return None
        return self._req("POST", f"/lists/{list_id}/items/remove", params=params)

    def search(self, query: str, media_type: str = "any") -> list[dict]:
        """
        Search for media on MDBList.
        Tries multiple endpoint patterns since the API has evolved.
        """
        # The new API endpoint is /search/media/{type}
        type_slug = {"movie": "movie", "show": "show"}.get(media_type, "any")
        params = {"s": query}

        # Try new API path: /search/media/{type}
        r = self._req("GET", f"/search/media/{type_slug}", params=params)
        if r is not None:
            if isinstance(r, dict) and "search" in r:
                return r["search"]
            if isinstance(r, list):
                return r
            return []

        # Fallback: try /search with ?m= param (old pattern)
        if media_type in ("movie", "show"):
            params["m"] = media_type
        r = self._req("GET", "/search", params=params)
        if r is not None:
            if isinstance(r, dict) and "search" in r:
                return r["search"]
            if isinstance(r, list):
                return r

        return []


# ---------------------------------------------------------------------------
# TMDB API client (fallback search)
# ---------------------------------------------------------------------------

TMDB_API_BASE = "https://api.themoviedb.org/3"
# TMDB provides a free API key for personal use. This is a read-only key
# used solely for searching titles. Users can override with their own key.
TMDB_DEFAULT_KEY = ""  # Set via TMDB_API_KEY env var


class TMDBClient:
    """Minimal TMDB API client for title search (fallback when MDBList search fails)."""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self.session = requests.Session()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str, media_type: str = "movie",
               year: Optional[int] = None) -> list[dict]:
        if not self.api_key:
            return []

        endpoint = "/search/movie" if media_type == "movie" else "/search/tv"
        params = {"api_key": self.api_key, "query": query, "language": "en-US"}
        if year:
            key = "year" if media_type == "movie" else "first_air_date_year"
            params[key] = year

        try:
            url = f"{TMDB_API_BASE}{endpoint}"
            logger.debug(f"TMDB search: {url} query={query} year={year}")
            r = self.session.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])
            logger.debug(f"TMDB returned {len(results)} results for '{query}'")
            # Normalize to our format
            out = []
            for item in results:
                title = item.get("title") or item.get("name", "")
                yr = None
                rd = item.get("release_date") or item.get("first_air_date", "")
                if rd and len(rd) >= 4:
                    yr = int(rd[:4])
                out.append({
                    "title": title,
                    "year": yr,
                    "type": media_type,
                    "tmdb_id": item.get("id"),
                    "ids": {"tmdb": item.get("id")},
                })
            return out
        except requests.RequestException as e:
            logger.error(f"TMDB search error for '{query}': {e}")
            return []


# ---------------------------------------------------------------------------
# Title matcher
# ---------------------------------------------------------------------------

class TitleMatcher:
    """
    Resolves FlixPatrol titles to IMDB/TMDB IDs.

    Strategy (in order):
      1. Use IMDB ID directly from the FlixPatrol title page (most reliable)
      2. Search TMDB by title+year (requires TMDB_API_KEY env var)
    """

    def __init__(self, mdblist: MDBListClient, cache: FileCache,
                 tmdb: Optional[TMDBClient] = None):
        self.mdb = mdblist
        self.tmdb = tmdb
        self.cache = cache

    def find(self, title: str, title_info: dict, media_type: str) -> Optional[dict]:
        year = title_info.get("year")
        fp_imdb = title_info.get("imdb_id")

        # --- Strategy 1: IMDB ID from FlixPatrol page ---
        if fp_imdb:
            return {"imdb_id": fp_imdb, "title": title, "year": year,
                    "_src": "FlixPatrol"}

        # --- Check cache ---
        cache_key = f"match:v2:{title}:{year}:{media_type}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached if cached != "_MISS_" else None

        # --- Strategy 2: TMDB search ---
        best = None
        if self.tmdb and self.tmdb.available:
            mtype = "movie" if media_type == "movie" else "show"

            # Try with year first (more precise)
            if year:
                results = self.tmdb.search(title, mtype, year)
                best = self._pick(results, title, year, media_type)

            # Retry without year
            if not best:
                results = self.tmdb.search(title, mtype)
                best = self._pick(results, title, year, media_type)

            if best:
                best["_src"] = "TMDB"

        if best:
            self.cache.set(cache_key, best)
        else:
            self.cache.set(cache_key, "_MISS_")
        return best

    def _pick(self, results: list, title: str, year: Optional[int],
              media_type: str) -> Optional[dict]:
        """Pick the best match from search results. Only returns items with IDs."""
        if not results:
            return None

        tl = self._norm(title)

        # Pass 1: exact title + year match
        for item in results:
            it = self._norm(item.get("title", ""))
            iy = item.get("year")
            if it == tl and self._year_ok(year, iy):
                r = self._extract_ids(item)
                if r:
                    return r

        # Pass 2: exact title, ignore year
        for item in results:
            it = self._norm(item.get("title", ""))
            if it == tl:
                r = self._extract_ids(item)
                if r:
                    return r

        # Pass 3: first result with any ID
        for item in results:
            r = self._extract_ids(item)
            if r:
                return r

        return None

    @staticmethod
    def _extract_ids(item: dict) -> Optional[dict]:
        """Extract IDs from a search result. Returns None if no usable ID found."""
        ids = item.get("ids", {})
        imdb = (ids.get("imdbid") or ids.get("imdb")
                or item.get("imdb_id") or item.get("imdb"))
        tmdb = (ids.get("tmdbid") or ids.get("tmdb")
                or item.get("tmdb_id") or item.get("id"))

        # CRITICAL: only return if we have at least one usable ID
        if not imdb and not tmdb:
            return None

        r = {"title": item.get("title", ""), "year": item.get("year")}
        if imdb:
            r["imdb_id"] = str(imdb)
        if tmdb:
            r["tmdb_id"] = int(tmdb) if isinstance(tmdb, (int, float)) else tmdb
        return r

    @staticmethod
    def _norm(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^\w\s]", "", s)
        return re.sub(r"\s+", " ", s)

    @staticmethod
    def _year_ok(want: Optional[int], got: Optional[int]) -> bool:
        if want is None or got is None:
            return True
        return abs(want - got) <= 1


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


def make_top10_name(cfg: dict) -> str:
    parts = [cfg.get("platform", ""), "top 10"]
    t = cfg.get("type", "both")
    if t == "movies":
        parts.append("movies")
    elif t == "shows":
        parts.append("shows")
    parts.append(cfg.get("location", "world"))
    if cfg.get("kids"):
        parts.append("kids")
    return " ".join(parts).title().replace("-", " ")


def make_popular_name(cfg: dict) -> str:
    parts = ["popular", cfg.get("platform", "")]
    t = cfg.get("type", "both")
    if t == "movies":
        parts.append("movies")
    elif t == "shows":
        parts.append("shows")
    return " ".join(parts).title().replace("-", " ")


def find_or_create_list(mdb: MDBListClient, name: str, slug: str) -> Optional[int]:
    for lst in mdb.get_my_lists():
        if lst.get("slug") == slug or lst.get("name", "").lower() == name.lower():
            logger.info(f"  List exists: '{lst.get('name')}' (id={lst['id']})")
            return lst["id"]
    if DRY_RUN:
        logger.info(f"  [DRY RUN] Would create list '{name}'")
        return None
    logger.info(f"  Creating list '{name}' ...")
    result = mdb.create_list(name)
    if result and "id" in result:
        logger.info(f"  Created (id={result['id']})")
        return result["id"]
    time.sleep(1)
    for lst in mdb.get_my_lists():
        if lst.get("name", "").lower() == name.lower():
            return lst["id"]
    logger.error(f"  Failed to create list '{name}'")
    return None


def sync_items(mdb: MDBListClient, list_id: int, items: list[dict], name: str):
    if not items:
        logger.warning(f"  No matched items for '{name}'")
        return

    if DRY_RUN:
        logger.info(f"  [DRY RUN] Would sync {len(items)} items to '{name}'")
        return

    # Clear existing items one by one
    existing = mdb.get_list_items(list_id)
    if existing:
        removed = 0
        for m in existing.get("movies", []):
            imdb = m.get("imdb_id")
            if imdb:
                mdb.remove_item(list_id, "movie", imdb_id=imdb)
                removed += 1
        for s in existing.get("shows", []):
            imdb = s.get("imdb_id")
            if imdb:
                mdb.remove_item(list_id, "show", imdb_id=imdb)
                removed += 1
        if removed:
            logger.info(f"  Removed {removed} old items from '{name}'")

    # Add new items one by one
    added = 0
    failed = 0
    for item in items:
        media_type = item.get("type", "movie")
        imdb_id = item.get("imdb_id")
        tmdb_id = item.get("tmdb_id")
        r = mdb.add_item(list_id, media_type, imdb_id=imdb_id, tmdb_id=tmdb_id)
        if r and r.get("added", {}).get("movies", 0) + r.get("added", {}).get("shows", 0) > 0:
            added += 1
        elif r:
            added += 1  # count as added even without detailed response
        else:
            failed += 1
            logger.warning(f"  Failed to add: {item.get('title', '?')}")

    logger.info(f"  Synced '{name}': {added} added, {failed} failed")


def _entry(item: dict) -> Optional[dict]:
    if "imdb_id" in item:
        return {"imdb_id": item["imdb_id"]}
    if "tmdb_id" in item:
        return {"tmdb_id": item["tmdb_id"]}
    return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        logger.info(f"Writing default config to {CONFIG_FILE}")
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        logger.info("Edit config/default.json and restart the container.")
        sys.exit(0)

    cfg = json.loads(CONFIG_FILE.read_text())

    env_key = os.environ.get("MDBLIST_API_KEY")
    if env_key:
        cfg.setdefault("MDBList", {})["apiKey"] = env_key
    env_cron = os.environ.get("SCHEDULE")
    if env_cron:
        cfg.setdefault("Schedule", {})["cron"] = env_cron

    return cfg


def validate_config(cfg: dict):
    key = cfg.get("MDBList", {}).get("apiKey", "")
    if not key or key == "YOUR_MDBLIST_API_KEY_HERE":
        logger.error(
            "MDBList API key not set! "
            "Set it in config/default.json or via MDBLIST_API_KEY env var. "
            "Get yours at https://mdblist.com/preferences/"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Sync job
# ---------------------------------------------------------------------------

def run_sync(cfg: dict):
    start = time.time()
    logger.info("=" * 55)
    logger.info("Starting sync cycle")
    logger.info("=" * 55)

    if DRY_RUN:
        logger.info("*** DRY RUN – no writes to MDBList ***")

    cache_cfg = cfg.get("Cache", {})
    cache = FileCache(
        CONFIG_DIR / ".cache",
        ttl=cache_cfg.get("ttl", 86400),
        enabled=cache_cfg.get("enabled", True),
    )

    mdb = MDBListClient(cfg["MDBList"]["apiKey"])
    scraper = FlixPatrolScraper(cache)

    # TMDB fallback search (optional, set TMDB_API_KEY env var)
    tmdb_key = os.environ.get("TMDB_API_KEY", "")
    tmdb = TMDBClient(tmdb_key) if tmdb_key else None
    if tmdb and tmdb.available:
        logger.info("TMDB fallback search: enabled")
    else:
        logger.info("TMDB fallback search: disabled (set TMDB_API_KEY to enable)")

    matcher = TitleMatcher(mdb, cache, tmdb)

    limits = mdb.get_limits()
    if limits:
        logger.info(
            f"MDBList API: {limits.get('api_requests_count', 0)}/"
            f"{limits.get('api_requests', 1000)} requests used"
        )

    # --- Top 10 ---
    for t10 in cfg.get("FlixPatrolTop10", []):
        name = t10.get("name") or make_top10_name(t10)
        slug = slugify(name) if t10.get("normalizeName", True) else name
        kids = t10.get("kids", False)
        logger.info(f"\n▶ Top10: {name}")
        logger.info(f"  {t10.get('platform')}/{t10.get('location')} "
                     f"type={t10.get('type', 'both')} limit={t10.get('limit', 10)}"
                     f"{' kids=true' if kids else ''}")

        fp = scraper.get_top10(
            t10["platform"], t10.get("location", "world"),
            t10.get("type", "both"), t10.get("limit", 10),
            t10.get("fallback", False), kids,
        )
        if not fp:
            logger.warning("  No items from FlixPatrol")
            continue
        logger.info(f"  FlixPatrol returned {len(fp)} items")

        matched = _match_all(fp, scraper, matcher)
        if not matched:
            logger.warning("  No items matched")
            continue

        lid = find_or_create_list(mdb, name, slug)
        if lid is not None:
            sync_items(mdb, lid, matched, name)
        elif DRY_RUN:
            sync_items(mdb, 0, matched, name)

    # --- Popular ---
    for pop in cfg.get("FlixPatrolPopular", []):
        name = pop.get("name") or make_popular_name(pop)
        slug = slugify(name) if pop.get("normalizeName", True) else name
        logger.info(f"\n▶ Popular: {name}")

        fp = scraper.get_popular(
            pop["platform"], pop.get("type", "both"), pop.get("limit", 100),
        )
        if not fp:
            logger.warning("  No items from FlixPatrol")
            continue
        logger.info(f"  FlixPatrol returned {len(fp)} items")

        matched = _match_all(fp, scraper, matcher)
        if not matched:
            continue

        lid = find_or_create_list(mdb, name, slug)
        if lid is not None:
            sync_items(mdb, lid, matched, name)
        elif DRY_RUN:
            sync_items(mdb, 0, matched, name)

    elapsed = time.time() - start
    logger.info(f"\n{'=' * 55}")
    logger.info(f"Sync complete in {elapsed:.1f}s")
    logger.info(f"{'=' * 55}")


def _match_all(fp_items: list, scraper: FlixPatrolScraper,
               matcher: TitleMatcher) -> list[dict]:
    matched = []
    for item in fp_items:
        title_info = scraper.get_title_info(item["url"]) if item.get("url") else {}
        media_type = item.get("type", "movie")

        ids = matcher.find(item["title"], title_info, media_type)
        if ids and (ids.get("imdb_id") or ids.get("tmdb_id")):
            ids["type"] = media_type
            matched.append(ids)
            id_str = ids.get("imdb_id") or f"tmdb:{ids.get('tmdb_id')}"
            src = ids.pop("_src", "unknown")
            logger.info(f"  ✓ {item['title']} → {id_str} (via {src})")
        else:
            logger.warning(f"  ✗ {item['title']} – not found")
        time.sleep(0.3)
    return matched


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logger.info("╔═══════════════════════════════════════════════════════╗")
    logger.info("║       FlixPatrol → MDBList Sync  v1.1.0             ║")
    logger.info("╚═══════════════════════════════════════════════════════╝")

    cfg = load_config()
    validate_config(cfg)

    sched_cfg = cfg.get("Schedule", {})
    cron_expr = os.environ.get("SCHEDULE") or sched_cfg.get("cron", "0 6,18 * * *")
    run_on_start = sched_cfg.get("runOnStart", True)

    if os.environ.get("RUN_ONCE", "false").lower() == "true":
        logger.info("RUN_ONCE mode – executing once and exiting")
        run_sync(cfg)
        return

    scheduler = Scheduler(cron_expr)
    logger.info(f"Schedule: {cron_expr}")
    logger.info(f"Next run: {scheduler.next_run_str()}")

    stop = False
    def _signal(sig, frame):
        nonlocal stop
        logger.info("Shutdown signal received – stopping after current cycle")
        stop = True
    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    if run_on_start:
        logger.info("runOnStart=true → running initial sync now")
        run_sync(cfg)
        cfg = load_config()

    while not stop:
        sleep_sec = scheduler.seconds_until_next()
        logger.info(f"Sleeping {_fmt_dur(sleep_sec)} until {scheduler.next_run_str()}")

        deadline = time.time() + sleep_sec
        while time.time() < deadline and not stop:
            time.sleep(min(30, deadline - time.time()))

        if stop:
            break

        cfg = load_config()
        validate_config(cfg)
        run_sync(cfg)
        scheduler.advance()

    logger.info("Exiting cleanly. Goodbye!")


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if sec or not parts:
        parts.append(f"{sec}s")
    return " ".join(parts)


if __name__ == "__main__":
    main()
