"""
Service mode: periodically check configured NTS shows and prepend any newly-
broadcast tracks to their corresponding Qobuz playlists.

Config file format (JSON):

    {
      "interval_hours": 24,
      "shows": [
        {
          "url": "https://www.nts.live/shows/floating-points",
          "playlist_id": 12345678,
          "last_updated": "2026-01-01T00:00:00Z"
        },
        {
          "show": "charlie-bones",
          "playlist_id": 87654321,
          "last_updated": "2026-01-01T00:00:00Z",
          "is_public": false
        }
      ]
    }

On each run, for every show:
  1. Fetch episodes broadcast after `last_updated`
  2. Enrich their tracklists and collect all unique tracks (newest first)
  3. Search Qobuz for each track
  4. Prepend found tracks to `playlist_id`, splitting into overflow playlists
     when the 2000-track Qobuz limit is reached
  5. Persist the updated `last_updated` and `playlist_id` (the latter only
     changes when a split occurs — the last-created playlist becomes the new
     target for future prepends)
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console

import nts_scraper as nts
from qobuz_client import QobuzClient

console = Console()

DEFAULT_INTERVAL_HOURS = 24


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(path: Path, config: dict) -> None:
    """Atomically write the config back to disk."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (or plain date). Returns None if unparseable."""
    if not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_show_slug(show_entry: dict) -> str:
    if show_entry.get("show"):
        return show_entry["show"].strip("/")
    if show_entry.get("url"):
        return nts.resolve_from_url(show_entry["url"])[0]
    raise ValueError(f"Show entry missing 'show' or 'url': {show_entry}")


# ---------------------------------------------------------------------------
# Episode / track collection
# ---------------------------------------------------------------------------

def collect_new_tracks(
    show_slug: str, last_updated: Optional[datetime]
) -> tuple[list[nts.Track], Optional[datetime]]:
    """
    Fetch episodes broadcast after `last_updated`, enrich their tracklists,
    and return (tracks_newest_first, latest_broadcast_seen).

    Tracks are deduplicated across episodes and returned newest-first so that
    prepending preserves broadcast order (newest episode's tracks end up at
    the very top of the playlist).
    """
    all_new: list[nts.Episode] = []
    latest_broadcast: Optional[datetime] = None
    offset = 0
    page_size = 20
    stop = False

    while not stop:
        page = nts.get_episodes(show_slug, limit=page_size, offset=offset)
        if not page:
            break
        for ep in page:
            ep_dt = _parse_iso(ep.broadcast)
            if ep_dt and (latest_broadcast is None or ep_dt > latest_broadcast):
                latest_broadcast = ep_dt
            if last_updated and ep_dt and ep_dt <= last_updated:
                stop = True
                break
            if ep_dt is None:
                # Undated episodes can't be compared — skip them rather than
                # reprocessing on every run.
                continue
            all_new.append(ep)
        if stop or len(page) < page_size:
            break
        offset += page_size
        time.sleep(0.3)

    if not all_new:
        return [], latest_broadcast

    console.print(f"[dim]{len(all_new)} new episode(s) to process.[/dim]")

    # Sort oldest-first so we walk through history in chronological order,
    # then reverse the final track list so prepending preserves chronology.
    def ep_sort_key(ep: nts.Episode):
        return _parse_iso(ep.broadcast) or datetime.min.replace(tzinfo=timezone.utc)

    all_new.sort(key=ep_sort_key)

    # Local import avoids a circular dependency at module load time.
    from main import _track_key

    seen_keys: set[tuple] = set()
    oldest_first: list[nts.Track] = []
    for ep in all_new:
        nts.enrich_tracklist(ep)
        for t in ep.tracklist or []:
            key = _track_key(t.artist, t.title)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            oldest_first.append(t)
        time.sleep(0.2)

    return list(reversed(oldest_first)), latest_broadcast


# ---------------------------------------------------------------------------
# Per-show processing
# ---------------------------------------------------------------------------

def process_show(client: QobuzClient, show_entry: dict) -> None:
    """
    Process a single show entry from the config. Mutates `show_entry` in
    place with the updated `last_updated` and (if a split occurred) the new
    `playlist_id`.
    """
    from main import add_to_existing_playlist, search_and_match

    show_slug = _resolve_show_slug(show_entry)
    playlist_id = show_entry.get("playlist_id")
    if not playlist_id:
        console.print(f"[red]Show entry {show_slug!r} missing playlist_id — skipping.[/red]")
        return

    last_updated_raw = show_entry.get("last_updated")
    last_updated = _parse_iso(last_updated_raw or "")
    if last_updated_raw and last_updated is None:
        console.print(
            f"[red]Show entry {show_slug!r} has unparseable last_updated "
            f"{last_updated_raw!r} — skipping.[/red]"
        )
        return
    if last_updated is None:
        console.print(
            f"[yellow]Show entry {show_slug!r} has no last_updated — skipping. "
            f"Set it to a start date (e.g. '2020-01-01T00:00:00Z') to backfill.[/yellow]"
        )
        return

    console.print(
        f"\n[bold cyan]{show_slug}[/bold cyan] — playlist [bold]{playlist_id}[/bold], "
        f"last updated {last_updated.isoformat()}"
    )

    new_tracks, latest_broadcast = collect_new_tracks(show_slug, last_updated)

    if not new_tracks:
        console.print("[dim]No new tracks since last run.[/dim]")
        if latest_broadcast and latest_broadcast > last_updated:
            show_entry["last_updated"] = latest_broadcast.isoformat()
        return

    console.print(f"[green]{len(new_tracks)} new track(s) to prepend.[/green]")

    track_ids, not_found = search_and_match(client, new_tracks)
    if not track_ids:
        console.print("[yellow]No tracks matched on Qobuz.[/yellow]")
        if latest_broadcast and latest_broadcast > last_updated:
            show_entry["last_updated"] = latest_broadcast.isoformat()
        return

    # Dedup Qobuz IDs (one Qobuz track may match multiple NTS entries)
    track_ids = list(dict.fromkeys(track_ids))

    result = add_to_existing_playlist(
        client,
        playlist_id,
        track_ids,
        prepend=True,
        is_public=bool(show_entry.get("is_public", False)),
    )

    # If overflow created new playlist(s), roll the config's playlist_id
    # forward to the last-created one so subsequent runs prepend there.
    playlists = result.get("playlists") or []
    if len(playlists) > 1:
        new_primary = playlists[-1][0]
        console.print(
            f"[yellow]Playlist overflowed — primary for next run is now "
            f"[bold]{new_primary}[/bold].[/yellow]"
        )
        show_entry["playlist_id"] = new_primary

    if latest_broadcast and latest_broadcast > last_updated:
        show_entry["last_updated"] = latest_broadcast.isoformat()


# ---------------------------------------------------------------------------
# Service entry points
# ---------------------------------------------------------------------------

def run_once(config_path: Path) -> None:
    """Process every show in the config exactly once."""
    from main import load_qobuz_client, login_qobuz

    config = load_config(config_path)
    shows = config.get("shows") or []
    if not shows:
        console.print("[yellow]No shows configured.[/yellow]")
        return

    client = load_qobuz_client()
    login_qobuz(client)

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    console.print(f"[dim]Service run started at {started}[/dim]")

    for entry in shows:
        try:
            process_show(client, entry)
        except Exception as e:
            console.print(f"[red]Error processing {entry}: {e}[/red]")
        # Persist progress after each show so a crash mid-run doesn't lose
        # the work we've already committed to Qobuz.
        save_config(config_path, config)

    console.print("\n[green]Service run complete.[/green]")


def run_loop(config_path: Path) -> None:
    """Run forever, sleeping `interval_hours` between runs."""
    while True:
        try:
            run_once(config_path)
        except Exception as e:
            console.print(f"[red]Run failed: {e}[/red]")

        # Re-read interval so the user can tweak it between runs.
        try:
            interval_hours = float(
                load_config(config_path).get("interval_hours") or DEFAULT_INTERVAL_HOURS
            )
        except Exception:
            interval_hours = DEFAULT_INTERVAL_HOURS

        sleep_seconds = interval_hours * 3600
        next_run_ts = datetime.now(timezone.utc).timestamp() + sleep_seconds
        next_run = datetime.fromtimestamp(next_run_ts, timezone.utc).isoformat(timespec="seconds")
        console.print(f"\n[dim]Sleeping {interval_hours}h. Next run at {next_run}.[/dim]")
        time.sleep(sleep_seconds)
