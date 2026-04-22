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
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console

import nts_scraper as nts
from notifier import Notifier
from qobuz_client import QobuzAuthError, QobuzClient

console = Console()

DEFAULT_INTERVAL_HOURS = 24


# ---------------------------------------------------------------------------
# Run summary (used to build notifications)
# ---------------------------------------------------------------------------

@dataclass
class ShowResult:
    slug: str
    episodes: int = 0
    tracks_found: int = 0
    tracks_added: int = 0
    not_found: int = 0
    split_to: Optional[int] = None
    error: Optional[str] = None
    skipped: bool = False


@dataclass
class RunSummary:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    shows: list[ShowResult] = field(default_factory=list)
    fatal_error: Optional[str] = None

    @property
    def has_changes(self) -> bool:
        return any(s.tracks_added > 0 for s in self.shows)

    @property
    def has_errors(self) -> bool:
        return self.fatal_error is not None or any(s.error for s in self.shows)

    def level(self) -> str:
        if self.fatal_error or any(s.error for s in self.shows):
            return "error"
        if self.has_changes:
            return "info"
        return "info"

    def title(self) -> str:
        if self.fatal_error:
            return "nts-qobuz: run failed"
        if any(s.error for s in self.shows):
            return "nts-qobuz: run finished with errors"
        if self.has_changes:
            total = sum(s.tracks_added for s in self.shows)
            return f"nts-qobuz: added {total} track(s)"
        return "nts-qobuz: no new tracks"

    def body(self) -> str:
        lines = [f"Started: {self.started_at.isoformat(timespec='seconds')}"]
        if self.fatal_error:
            lines.append(f"\nFATAL: {self.fatal_error}")
            return "\n".join(lines)
        for s in self.shows:
            lines.append("")
            lines.append(f"{s.slug}:")
            if s.skipped:
                lines.append(f"  skipped ({s.error or 'no reason given'})")
                continue
            if s.error:
                lines.append(f"  ERROR: {s.error}")
                continue
            lines.append(f"  episodes processed: {s.episodes}")
            lines.append(f"  tracks added:       {s.tracks_added}")
            if s.not_found:
                lines.append(f"  not found on Qobuz: {s.not_found}")
            if s.split_to is not None:
                lines.append(f"  playlist split → new primary: {s.split_to}")
        return "\n".join(lines)


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
) -> tuple[list[nts.Track], int, Optional[datetime]]:
    """
    Fetch episodes broadcast after `last_updated`, enrich their tracklists,
    and return (tracks_newest_first, episode_count, latest_broadcast_seen).

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
        return [], 0, latest_broadcast

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

    return list(reversed(oldest_first)), len(all_new), latest_broadcast


# ---------------------------------------------------------------------------
# Per-show processing
# ---------------------------------------------------------------------------

def process_show(client: QobuzClient, show_entry: dict) -> ShowResult:
    """
    Process a single show entry from the config. Mutates `show_entry` in
    place with the updated `last_updated` and (if a split occurred) the new
    `playlist_id`. Returns a ShowResult describing what happened.
    """
    from main import add_to_existing_playlist, search_and_match

    try:
        show_slug = _resolve_show_slug(show_entry)
    except ValueError as e:
        return ShowResult(slug=str(show_entry), error=str(e), skipped=True)

    result = ShowResult(slug=show_slug)

    playlist_id = show_entry.get("playlist_id")
    if not playlist_id:
        result.skipped = True
        result.error = "missing playlist_id"
        console.print(f"[red]{show_slug}: missing playlist_id — skipping.[/red]")
        return result

    last_updated_raw = show_entry.get("last_updated")
    last_updated = _parse_iso(last_updated_raw or "")
    if last_updated_raw and last_updated is None:
        result.skipped = True
        result.error = f"unparseable last_updated {last_updated_raw!r}"
        console.print(f"[red]{show_slug}: {result.error} — skipping.[/red]")
        return result
    if last_updated is None:
        result.skipped = True
        result.error = "no last_updated set"
        console.print(
            f"[yellow]{show_slug}: no last_updated — skipping. "
            f"Set it to a start date (e.g. '2020-01-01T00:00:00Z') to backfill.[/yellow]"
        )
        return result

    console.print(
        f"\n[bold cyan]{show_slug}[/bold cyan] — playlist [bold]{playlist_id}[/bold], "
        f"last updated {last_updated.isoformat()}"
    )

    new_tracks, episode_count, latest_broadcast = collect_new_tracks(show_slug, last_updated)

    if not new_tracks:
        console.print("[dim]No new tracks since last run.[/dim]")
        if latest_broadcast and latest_broadcast > last_updated:
            show_entry["last_updated"] = latest_broadcast.isoformat()
        return result

    result.episodes = episode_count
    result.tracks_found = len(new_tracks)
    console.print(f"[green]{len(new_tracks)} new track(s) from {episode_count} episode(s) to prepend.[/green]")

    track_ids, not_found = search_and_match(client, new_tracks)
    result.not_found = len(not_found)

    if not track_ids:
        console.print("[yellow]No tracks matched on Qobuz.[/yellow]")
        if latest_broadcast and latest_broadcast > last_updated:
            show_entry["last_updated"] = latest_broadcast.isoformat()
        return result

    # Dedup Qobuz IDs (one Qobuz track may match multiple NTS entries)
    track_ids = list(dict.fromkeys(track_ids))

    add_result = add_to_existing_playlist(
        client,
        playlist_id,
        track_ids,
        prepend=True,
        is_public=bool(show_entry.get("is_public", False)),
    )

    result.tracks_added = add_result.get("added", 0) + add_result.get("overflow", 0)
    # Best-effort estimate of how many distinct episodes contributed: collect
    # returns tracks across episodes, so episodes count is just len(all_new)
    # already logged by collect_new_tracks; we don't store it here.

    # If overflow created new playlist(s), roll the config's playlist_id
    # forward to the last-created one so subsequent runs prepend there.
    playlists = add_result.get("playlists") or []
    if len(playlists) > 1:
        new_primary = playlists[-1][0]
        console.print(
            f"[yellow]Playlist overflowed — primary for next run is now "
            f"[bold]{new_primary}[/bold].[/yellow]"
        )
        show_entry["playlist_id"] = new_primary
        result.split_to = new_primary

    if latest_broadcast and latest_broadcast > last_updated:
        show_entry["last_updated"] = latest_broadcast.isoformat()
    return result


# ---------------------------------------------------------------------------
# Service entry points
# ---------------------------------------------------------------------------

def _send_summary(notifier: Notifier, summary: RunSummary) -> None:
    if not notifier.enabled:
        return
    level = "error" if summary.has_errors else "info"
    if not notifier.should_send(level, summary.has_changes):
        return
    notifier.notify(summary.title(), summary.body(), level=level)


def run_once(config_path: Path) -> RunSummary:
    """Process every show in the config exactly once. Returns the run summary."""
    from main import load_qobuz_client, service_login_qobuz

    summary = RunSummary()
    config = load_config(config_path)
    notifier = Notifier(config.get("notifications"))

    shows = config.get("shows") or []
    if not shows:
        console.print("[yellow]No shows configured.[/yellow]")
        return summary

    started = summary.started_at.isoformat(timespec="seconds")
    console.print(f"[dim]Service run started at {started}[/dim]")

    # Authenticate. In service mode we never spawn a browser; on auth
    # failure we notify and bail so cron/systemd will retry next time.
    try:
        client = load_qobuz_client()
        service_login_qobuz(client)
    except QobuzAuthError as e:
        summary.fatal_error = f"Qobuz auth failed: {e}"
        console.print(f"[red]{summary.fatal_error}[/red]")
        _send_summary(notifier, summary)
        return summary
    except Exception as e:
        summary.fatal_error = f"Failed to initialise Qobuz client: {e}"
        console.print(f"[red]{summary.fatal_error}[/red]")
        _send_summary(notifier, summary)
        return summary

    for entry in shows:
        try:
            result = process_show(client, entry)
        except QobuzAuthError as e:
            # Token may have expired mid-run — abort the rest of the run.
            summary.fatal_error = f"Qobuz auth failed mid-run: {e}"
            console.print(f"[red]{summary.fatal_error}[/red]")
            save_config(config_path, config)
            _send_summary(notifier, summary)
            return summary
        except Exception as e:
            console.print(f"[red]Error processing {entry}: {e}[/red]")
            console.print(traceback.format_exc())
            slug = entry.get("show") or entry.get("url") or str(entry)
            result = ShowResult(slug=slug, error=str(e))
        summary.shows.append(result)
        # Persist progress after each show so a crash mid-run doesn't lose
        # the work we've already committed to Qobuz.
        save_config(config_path, config)

    console.print("\n[green]Service run complete.[/green]")
    _send_summary(notifier, summary)
    return summary


def run_loop(config_path: Path) -> None:
    """Run forever, sleeping `interval_hours` between runs."""
    while True:
        try:
            run_once(config_path)
        except Exception as e:
            console.print(f"[red]Run crashed: {e}[/red]")
            console.print(traceback.format_exc())
            # Best-effort notification of the crash.
            try:
                notifier = Notifier(load_config(config_path).get("notifications"))
                notifier.notify(
                    "nts-qobuz: run crashed",
                    f"Unexpected exception:\n{e}\n\n{traceback.format_exc()}",
                    level="error",
                )
            except Exception:
                pass

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
