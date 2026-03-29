"""
NTS.live scraper — fetches show episodes and their tracklists.

NTS is a Next.js app, so all page data is embedded in a <script id="__NEXT_DATA__">
JSON blob. We parse that instead of relying on fragile HTML selectors.

Public NTS API endpoints:
  GET https://www.nts.live/api/v2/shows/{slug}
  GET https://www.nts.live/api/v2/shows/{slug}/episodes?offset=0&limit=20
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup
from rich.console import Console

console = Console()

NTS_API = "https://www.nts.live/api/v2"
NTS_BASE = "https://www.nts.live"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html,*/*",
}


@dataclass
class Track:
    artist: str
    title: str
    label: str = ""
    start_time: str = ""

    def search_query(self) -> str:
        return f"{self.artist} {self.title}"

    def __str__(self) -> str:
        label_part = f" [{self.label}]" if self.label else ""
        return f"{self.artist} — {self.title}{label_part}"


@dataclass
class Episode:
    slug: str
    name: str
    episode_alias: str
    broadcast: str = ""
    description: str = ""
    tracklist: list[Track] = field(default_factory=list)
    url: str = ""


def _slug_from_url(url: str) -> tuple[str, Optional[str]]:
    """
    Parse an NTS URL and return (show_slug, episode_alias_or_None).

    Accepted forms:
      https://www.nts.live/shows/floating-points
      https://www.nts.live/shows/floating-points/episodes/floating-points-12th-january-2020
    """
    url = url.strip().rstrip("/")
    # Remove query string / fragment
    url = re.split(r"[?#]", url)[0]

    m = re.search(
        r"nts\.live/shows/([^/]+)(?:/episodes/([^/]+))?", url, re.IGNORECASE
    )
    if not m:
        raise ValueError(
            f"Could not parse NTS URL: {url!r}\n"
            "Expected format: https://www.nts.live/shows/<show-slug>[/episodes/<episode>]"
        )
    return m.group(1), m.group(2)


def _parse_tracklist(raw: list[dict]) -> list[Track]:
    tracks = []
    for item in raw:
        artist = (item.get("artist") or "").strip()
        title = (item.get("title") or "").strip()
        if not artist and not title:
            continue
        tracks.append(
            Track(
                artist=artist,
                title=title,
                label=(item.get("label") or "").strip(),
                start_time=(item.get("start_time") or ""),
            )
        )
    return tracks


def _fetch_json(url: str, params: dict = None) -> dict:
    resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _episode_from_api(data: dict) -> Episode:
    """Build an Episode from an NTS API episode object."""
    alias = data.get("episode_alias", "")
    show_alias = data.get("show_alias", "")
    # Tracklist lives at embeds.tracklist.results, not the top-level tracklist key
    raw_tracklist = (
        (data.get("embeds") or {}).get("tracklist", {}).get("results")
        or data.get("tracklist")
        or []
    )
    return Episode(
        slug=show_alias,
        name=data.get("episode_name") or data.get("name") or alias,
        episode_alias=alias,
        broadcast=data.get("broadcast") or data.get("updated") or "",
        description=(data.get("description") or "").strip(),
        tracklist=_parse_tracklist(raw_tracklist),
        url=f"{NTS_BASE}/shows/{show_alias}/episodes/{alias}" if alias else "",
    )


def _scrape_episode_page(url: str) -> list[Track]:
    """
    Fall back to scraping the episode page when the API doesn't return a tracklist.
    NTS uses window._REACT_STATE_ (a <script id="react-state"> tag) to embed all
    page data; the tracklist lives at episode.embeds.tracklist.results or
    episode.tracklist within that state object.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Current NTS site embeds state as window._REACT_STATE_ = {...}
    tag = soup.find("script", {"id": "react-state"})
    if tag and tag.string:
        try:
            import re as _re
            json_str = _re.sub(
                r"^window\._REACT_STATE_\s*=\s*", "", tag.string
            ).rstrip(";")
            state = json.loads(json_str)
            ep = state.get("episode") or {}
            # Prefer embeds path, fall back to flat tracklist key
            raw = (
                (ep.get("embeds") or {}).get("tracklist", {}).get("results")
                or ep.get("tracklist")
                or []
            )
            if raw:
                return _parse_tracklist(raw)
        except (json.JSONDecodeError, Exception):
            pass

    return []


def get_show_info(show_slug: str) -> dict:
    """Return basic show metadata from the NTS API."""
    return _fetch_json(f"{NTS_API}/shows/{show_slug}")


def get_episodes(show_slug: str, limit: int = 20, offset: int = 0) -> list[Episode]:
    """Fetch a page of episodes for a show."""
    data = _fetch_json(
        f"{NTS_API}/shows/{show_slug}/episodes",
        params={"limit": limit, "offset": offset},
    )
    results = data.get("results") or []
    return [_episode_from_api(ep) for ep in results]


def get_all_episodes(show_slug: str, max_episodes: int = 100) -> list[Episode]:
    """Fetch all episodes (up to max_episodes) for a show, paging through the API."""
    episodes = []
    offset = 0
    page_size = 20
    total: Optional[int] = None

    while len(episodes) < max_episodes:
        data = _fetch_json(
            f"{NTS_API}/shows/{show_slug}/episodes",
            params={"limit": page_size, "offset": offset},
        )
        # Read total from metadata on first page (API may cap actual page size)
        if total is None:
            meta = (data.get("metadata") or {}).get("resultset") or {}
            total = meta.get("count")

        results = data.get("results") or []
        if not results:
            break
        episodes.extend(_episode_from_api(ep) for ep in results)

        if total is not None and len(episodes) >= total:
            break
        offset += len(results)
        time.sleep(0.3)  # be polite

    return episodes[:max_episodes]


def get_single_episode(show_slug: str, episode_alias: str) -> Episode:
    """Fetch a single episode by its alias."""
    data = _fetch_json(f"{NTS_API}/shows/{show_slug}/episodes/{episode_alias}")
    return _episode_from_api(data)


def enrich_tracklist(episode: Episode) -> Episode:
    """
    If the episode has no tracklist yet, first try re-fetching via the direct
    episode API endpoint (which includes embeds.tracklist.results with full
    artist data). Falls back to page scraping if that also fails.
    Mutates episode in-place and returns it.
    """
    if episode.tracklist:
        return episode

    # The list API endpoint omits embeds; the single-episode endpoint includes them
    if episode.slug and episode.episode_alias:
        try:
            full = get_single_episode(episode.slug, episode.episode_alias)
            if full.tracklist:
                episode.tracklist = full.tracklist
                return episode
        except Exception:
            pass

    # Last resort: scrape the episode page
    if episode.url:
        episode.tracklist = _scrape_episode_page(episode.url)

    return episode


def resolve_from_url(url: str) -> tuple[str, Optional[str]]:
    """
    Given any NTS URL, return (show_slug, episode_alias_or_None).
    """
    return _slug_from_url(url)
