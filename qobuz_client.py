"""
Qobuz API client with built-in credential spoofer.

The spoofer extracts app_id and app_secret at runtime by fetching Qobuz's
own login page and parsing the embedded JavaScript bundle — the same
technique used by streamrip (https://github.com/nathom/streamrip).

No manual app_id / app_secret setup required. Only QOBUZ_EMAIL and
QOBUZ_PASSWORD need to be in .env.

Authentication flow:
  1. QobuzSpoofer.fetch() → app_id, secrets
  2. POST /user/login     → user_auth_token
  3. X-App-Id + X-User-Auth-Token headers on all subsequent requests.
"""

import base64
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

import requests
from rich.console import Console

console = Console()

QOBUZ_API = "https://www.qobuz.com/api.json/0.2"
QOBUZ_PLAY = "https://play.qobuz.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


# ---------------------------------------------------------------------------
# Spoofer
# ---------------------------------------------------------------------------

class QobuzSpoofer:
    """
    Extracts Qobuz app_id and secrets from the Qobuz web player JS bundle.
    Ported from streamrip's async implementation to synchronous requests.
    """

    # Regex patterns ported verbatim from streamrip
    _SEED_TZ_RE = (
        r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.ut'
        r"imezone\.(?P<timezone>[a-z]+)\)"
    )
    _INFO_EXTRAS_RE = (
        r'name:"\w+/(?P<timezone>{timezones})",info:"'
        r'(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"'
    )
    _APP_ID_RE = r'production:\{api:\{appId:"(?P<app_id>\d{9})",appSecret:"(\w{32})'
    _BUNDLE_RE = r'<script src="(/resources/\d+\.\d+\.\d+-[a-z]\d{3}/bundle\.js)"></script>'

    def fetch(self) -> tuple[str, list[str]]:
        """
        Return (app_id, secrets_list) by scraping the Qobuz web player.
        Raises RuntimeError if extraction fails.
        """
        session = requests.Session()
        session.headers.update(HEADERS)

        # 1. Fetch login page to find bundle URL
        resp = session.get(f"{QOBUZ_PLAY}/login", timeout=20)
        resp.raise_for_status()
        login_html = resp.text

        bundle_match = re.search(self._BUNDLE_RE, login_html)
        if not bundle_match:
            raise RuntimeError(
                "Could not locate Qobuz JS bundle URL. "
                "The page structure may have changed."
            )
        bundle_path = bundle_match.group(1)

        # 2. Fetch the bundle JS
        resp = session.get(f"{QOBUZ_PLAY}{bundle_path}", timeout=30)
        resp.raise_for_status()
        bundle = resp.text

        # 3. Extract app_id
        id_match = re.search(self._APP_ID_RE, bundle)
        if not id_match:
            raise RuntimeError(
                "Could not extract Qobuz app_id from JS bundle. "
                "The bundle format may have changed."
            )
        app_id = id_match.group("app_id")

        # 4. Extract secrets via seed/timezone pairs
        secrets: OrderedDict[str, list[str]] = OrderedDict()
        for m in re.finditer(self._SEED_TZ_RE, bundle):
            seed, timezone = m.group("seed", "timezone")
            secrets[timezone] = [seed]

        if len(secrets) < 2:
            raise RuntimeError("Could not extract enough seed/timezone pairs from bundle.")

        # Prioritise the second pair (Qobuz JS ternary always evaluates false branch)
        keypairs = list(secrets.items())
        secrets.move_to_end(keypairs[1][0], last=False)

        # 5. Fetch info/extras for each timezone
        info_extras_re = self._INFO_EXTRAS_RE.format(
            timezones="|".join(tz.capitalize() for tz in secrets)
        )
        for m in re.finditer(info_extras_re, bundle):
            timezone, info, extras = m.group("timezone", "info", "extras")
            secrets[timezone.lower()] += [info, extras]

        # 6. Decode each secret: concat seed+info+extras, drop last 44 chars, base64-decode
        decoded: dict[str, str] = {}
        for tz, parts in secrets.items():
            decoded[tz] = base64.standard_b64decode(
                "".join(parts)[:-44]
            ).decode("utf-8")

        secret_list = [v for v in decoded.values() if v]
        if not secret_list:
            raise RuntimeError("Failed to decode any Qobuz secrets from bundle.")

        return app_id, secret_list


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class QobuzTrack:
    id: int
    title: str
    artist: str
    album: str
    duration: int = 0

    def __str__(self) -> str:
        return f"{self.artist} — {self.title} (from {self.album})"


class QobuzAuthError(Exception):
    pass


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class QobuzClient:
    def __init__(self, app_id: str, secrets: list[str]):
        self.app_id = app_id
        self.secrets = secrets
        self.user_auth_token: Optional[str] = None
        self.session = requests.Session()
        self.session.headers.update({"X-App-Id": app_id})

    @classmethod
    def from_spoofer(cls) -> "QobuzClient":
        """
        Auto-fetch app_id and secrets from the Qobuz web player, then
        return a ready-to-use QobuzClient.
        """
        console.print("[dim]Fetching Qobuz app credentials from web player...[/dim]")
        spoofer = QobuzSpoofer()
        app_id, secrets = spoofer.fetch()
        console.print(f"[dim]Got app_id={app_id}, {len(secrets)} secret(s)[/dim]")
        return cls(app_id, secrets)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self, email: str, password: str) -> None:
        """Authenticate and store the user auth token."""
        resp = self.session.post(
            f"{QOBUZ_API}/user/login",
            data={
                "email": email,
                "password": password,
                "app_id": self.app_id,
            },
            timeout=20,
        )
        if resp.status_code == 401:
            raise QobuzAuthError("Invalid Qobuz credentials (email/password).")
        if resp.status_code == 400:
            body = resp.json()
            raise QobuzAuthError(
                f"Qobuz login failed: {body.get('message', resp.text)}"
            )
        resp.raise_for_status()
        data = resp.json()
        self.user_auth_token = data["user_auth_token"]
        self.session.headers.update({"X-User-Auth-Token": self.user_auth_token})
        console.print(
            f"[green]Logged in to Qobuz as {data['user']['display_name']}[/green]"
        )

    def login_with_token(self, user_auth_token: str) -> None:
        """Authenticate using a token captured directly from a browser session.

        Use this when the email/password login flow is broken (Qobuz periodically
        changes their auth mechanism). To get a token:
          1. Log in at play.qobuz.com in your browser
          2. Open DevTools → Network tab → filter for 'user/login'
          3. Copy 'user_auth_token' from the response JSON
          4. Add QOBUZ_AUTH_TOKEN=<value> to your .env file
        """
        self.user_auth_token = user_auth_token
        self.session.headers.update({"X-User-Auth-Token": user_auth_token})
        console.print("[green]Authenticated with Qobuz token.[/green]")

    def verify_auth(self) -> bool:
        """Return True if the current token is valid, False if it has expired."""
        try:
            resp = self.session.get(
                f"{QOBUZ_API}/track/search",
                params={"query": "test", "limit": 1, "offset": 0},
                timeout=10,
            )
            return resp.status_code != 401
        except Exception:
            return False

    def _require_auth(self):
        if not self.user_auth_token:
            raise QobuzAuthError("Not logged in. Call login() first.")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_track(self, query: str, limit: int = 5) -> list[QobuzTrack]:
        """Search for tracks matching query. Returns up to `limit` results."""
        self._require_auth()
        for attempt in range(3):
            try:
                resp = self.session.get(
                    f"{QOBUZ_API}/track/search",
                    params={"query": query, "limit": limit, "offset": 0},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                items = (data.get("tracks") or {}).get("items") or []
                return [self._parse_track(t) for t in items]
            except (requests.exceptions.ReadTimeout, requests.exceptions.HTTPError) as e:
                is_transient = isinstance(e, requests.exceptions.ReadTimeout) or (
                    isinstance(e, requests.exceptions.HTTPError)
                    and e.response is not None
                    and e.response.status_code >= 500
                )
                if not is_transient or attempt == 2:
                    return []  # give up, treat as not found
                time.sleep(5 * (attempt + 1))
        return []

    def _parse_track(self, raw: dict) -> QobuzTrack:
        album = raw.get("album") or {}
        artist = raw.get("performer") or raw.get("artist") or {}
        return QobuzTrack(
            id=raw["id"],
            title=raw.get("title") or "",
            artist=(artist.get("name") or ""),
            album=(album.get("title") or ""),
            duration=raw.get("duration") or 0,
        )

    def find_best_track(self, artist: str, title: str) -> Optional[QobuzTrack]:
        """
        Search Qobuz and return the best match requiring both artist and title
        to roughly match. Returns None rather than a false positive.

        Strategy:
          1. Search "artist title" — check results for artist+title match
          2. Search just "title"   — check results for artist+title match
          3. Give up and return None
        """
        for query in [f"{artist} {title}", title]:
            results = self.search_track(query, limit=8)
            match = self._best_match(results, artist, title)
            if match:
                return match
        return None

    @staticmethod
    def _normalize(s: str) -> str:
        """Normalize Unicode punctuation for fuzzy string comparison."""
        return (
            s.lower()
            .replace("\u2019", "'")   # curly right apostrophe → straight
            .replace("\u2018", "'")   # curly left apostrophe → straight
            .replace("\u201c", '"')   # curly left quote → straight
            .replace("\u201d", '"')   # curly right quote → straight
            .replace("\u2013", "-")   # en dash → hyphen
            .replace("\u2014", "-")   # em dash → hyphen
        )

    @staticmethod
    def _substr_match(a: str, b: str, min_ratio: float = 0.8) -> bool:
        """True if a and b match via substring, but only if the shorter string
        is at least min_ratio of the longer string's length. This prevents
        short strings (e.g. 'Eternal') from falsely matching longer ones
        (e.g. 'Eternal 808') or partial artist names matching unrelated artists."""
        if a == b:
            return True
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if shorter in longer:
            return len(shorter) / len(longer) >= min_ratio
        return False

    def _best_match(
        self, results: list[QobuzTrack], artist: str, title: str
    ) -> Optional[QobuzTrack]:
        """
        Return the first result where both title and artist loosely match.
        Artist matching handles comma-separated credits (e.g. "Artist A, Artist B").
        """
        title_n = self._normalize(title)
        # Split on commas to handle multi-artist credits on NTS tracklists
        artist_parts = [self._normalize(a) for a in artist.split(",") if a.strip()]

        for track in results:
            track_title = self._normalize(track.title)
            track_artist = self._normalize(track.artist)

            title_match = self._substr_match(title_n, track_title)
            artist_match = any(
                self._substr_match(part, track_artist)
                for part in artist_parts
            )

            if title_match and artist_match:
                return track

        return None

    # ------------------------------------------------------------------
    # Playlists
    # ------------------------------------------------------------------

    def create_playlist(
        self,
        name: str,
        description: str = "",
        is_public: bool = False,
        is_collaborative: bool = False,
    ) -> dict:
        """Create a new playlist and return the API response dict."""
        self._require_auth()
        resp = self.session.post(
            f"{QOBUZ_API}/playlist/create",
            data={
                "name": name,
                "description": description,
                "is_public": "1" if is_public else "0",
                "is_collaborative": "1" if is_collaborative else "0",
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def add_tracks_to_playlist(self, playlist_id: int, track_ids: list[int]) -> dict:
        """Add tracks to a playlist in batches of 50."""
        self._require_auth()
        if not track_ids:
            return {}
        last_response: dict = {}
        for i in range(0, len(track_ids), 50):
            batch = track_ids[i : i + 50]
            resp = self.session.post(
                f"{QOBUZ_API}/playlist/addTracks",
                data={
                    "playlist_id": str(playlist_id),
                    "track_ids": ",".join(str(t) for t in batch),
                    "playlist_track_ids": ",".join(str(t) for t in batch),
                },
                timeout=20,
            )
            resp.raise_for_status()
            last_response = resp.json()
        return last_response

    def get_playlist(self, playlist_id: int) -> dict:
        """Fetch playlist metadata."""
        self._require_auth()
        resp = self.session.get(
            f"{QOBUZ_API}/playlist/get",
            params={"playlist_id": playlist_id, "extra": "tracks"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def get_playlist_track_ids(self, playlist_id: int) -> set[int]:
        """Return the set of track IDs already in a playlist."""
        data = self.get_playlist(playlist_id)
        items = (data.get("tracks") or {}).get("items") or []
        return {item["id"] for item in items if "id" in item}

    def get_playlist_tracks(self, playlist_id: int) -> list[dict]:
        """Return all track items in a playlist, handling pagination."""
        self._require_auth()
        items: list[dict] = []
        limit = 50
        offset = 0
        while True:
            resp = self.session.get(
                f"{QOBUZ_API}/playlist/get",
                params={
                    "playlist_id": playlist_id,
                    "extra": "tracks",
                    "limit": limit,
                    "offset": offset,
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            page = (data.get("tracks") or {}).get("items") or []
            items.extend(page)
            total = (data.get("tracks") or {}).get("total", 0)
            offset += limit
            if offset >= total:
                break
        return items

    def delete_tracks_from_playlist(self, playlist_id: int, track_ids: list[int]) -> dict:
        """Remove specific tracks from a playlist by track ID."""
        self._require_auth()
        if not track_ids:
            return {}
        last_response: dict = {}
        for i in range(0, len(track_ids), 50):
            batch = track_ids[i : i + 50]
            resp = self.session.post(
                f"{QOBUZ_API}/playlist/deleteTracks",
                data={
                    "playlist_id": str(playlist_id),
                    "track_ids": ",".join(str(t) for t in batch),
                },
                timeout=20,
            )
            resp.raise_for_status()
            last_response = resp.json()
        return last_response

    def prepend_tracks_to_playlist(self, playlist_id: int, track_ids: list[int]) -> None:
        """Add tracks to the beginning of a playlist.

        Strategy: capture existing track order, delete all existing tracks,
        then add new tracks followed by the original tracks.
        """
        existing_track_ids = [item["id"] for item in self.get_playlist_tracks(playlist_id) if "id" in item]

        if existing_track_ids:
            self.delete_tracks_from_playlist(playlist_id, existing_track_ids)

        self.add_tracks_to_playlist(playlist_id, track_ids)

        if existing_track_ids:
            self.add_tracks_to_playlist(playlist_id, existing_track_ids)
