#!/usr/bin/env python3
"""
nts-qobuz: Scan an NTS.live show and create a Qobuz playlist from its tracklist.

Usage examples:
  # Single episode
  python main.py --url "https://www.nts.live/shows/floating-points/episodes/floating-points-12th-january-2020"

  # Most recent episode of a show
  python main.py --show floating-points

  # Interactive: pick from the last N episodes
  python main.py --show floating-points --pick

  # List available episodes without creating a playlist
  python main.py --show floating-points --list

  # Add to an existing playlist
  python main.py --url "..." --add-to 12345678

  # Add to the beginning of an existing playlist
  python main.py --url "..." --add-to 12345678 --prepend

  # Dry run: show what would be added without creating the playlist
  python main.py --url "..." --dry-run
"""

import argparse
import os
import sys
import time

# Force UTF-8 output on Windows so track/artist names with non-Latin
# characters don't crash the console (cp1252 is the default there).
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

import nts_scraper as nts
from qobuz_client import QobuzAuthError, QobuzClient, QobuzSpoofer

console = Console()
load_dotenv()

QOBUZ_PLAYLIST_LIMIT = 2000


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_qobuz_client() -> QobuzClient:
    try:
        return QobuzClient.from_spoofer()
    except Exception as e:
        console.print(f"[red]Failed to auto-fetch Qobuz credentials:[/red] {e}")
        sys.exit(1)


def _run_get_token() -> str:
    """Launch get_token.py and return the newly saved token."""
    import subprocess
    get_token_script = os.path.join(os.path.dirname(__file__), "get_token.py")
    console.print("[yellow]Launching browser to capture a fresh Qobuz token...[/yellow]")
    result = subprocess.run([sys.executable, get_token_script])
    if result.returncode != 0:
        console.print("[red]Token capture failed. Exiting.[/red]")
        sys.exit(1)
    from dotenv import load_dotenv
    load_dotenv(override=True)
    return os.getenv("QOBUZ_AUTH_TOKEN", "").strip()


def login_qobuz(client: QobuzClient) -> None:
    token = os.getenv("QOBUZ_AUTH_TOKEN", "").strip()
    if token:
        client.login_with_token(token)
        if not client.verify_auth():
            console.print("[yellow]Token has expired — refreshing...[/yellow]")
            token = _run_get_token()
            if not token:
                console.print("[red]No token captured. Exiting.[/red]")
                sys.exit(1)
            client.login_with_token(token)
        return
    email = os.getenv("QOBUZ_EMAIL", "").strip()
    password = os.getenv("QOBUZ_PASSWORD", "").strip()
    if not email:
        email = Prompt.ask("Qobuz email")
    if not password:
        password = Prompt.ask("Qobuz password", password=True)
    try:
        client.login(email, password)
    except QobuzAuthError as e:
        console.print(f"[red]Auth error:[/red] {e}")
        sys.exit(1)


def service_login_qobuz(client: QobuzClient) -> None:
    """Headless login for service mode.

    Never prompts and never spawns a browser. Raises QobuzAuthError if no
    valid credentials are available so the caller can notify and exit
    cleanly (systemd / cron will retry on the next scheduled run).
    """
    token = os.getenv("QOBUZ_AUTH_TOKEN", "").strip()
    if token:
        client.login_with_token(token)
        if not client.verify_auth():
            raise QobuzAuthError(
                "QOBUZ_AUTH_TOKEN has expired. Re-capture it via get_token.py "
                "on a desktop machine and update .env, then restart the service."
            )
        return
    email = os.getenv("QOBUZ_EMAIL", "").strip()
    password = os.getenv("QOBUZ_PASSWORD", "").strip()
    if not email or not password:
        raise QobuzAuthError(
            "No Qobuz credentials configured. Set QOBUZ_AUTH_TOKEN (preferred) "
            "or QOBUZ_EMAIL + QOBUZ_PASSWORD in .env."
        )
    client.login(email, password)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def print_episode_header(episode: nts.Episode) -> None:
    console.print(
        Panel(
            f"[bold]{episode.name}[/bold]\n"
            f"[dim]{episode.broadcast}[/dim]\n\n"
            f"{episode.description or '(no description)'}",
            title=f"[cyan]NTS Episode[/cyan]",
            expand=False,
        )
    )


def print_tracklist(tracks: list[nts.Track]) -> None:
    if not tracks:
        console.print("[yellow]No tracklist found for this episode.[/yellow]")
        return
    table = Table(title="Tracklist", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Artist", style="bold")
    table.add_column("Title")
    table.add_column("Label", style="dim")
    for i, t in enumerate(tracks, 1):
        table.add_row(str(i), t.artist, t.title, t.label)
    console.print(table)


def print_episodes_table(episodes: list[nts.Episode]) -> None:
    table = Table(title="Episodes", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Name")
    table.add_column("Broadcast", style="dim")
    table.add_column("Tracks", justify="right")
    for i, ep in enumerate(episodes, 1):
        track_count = str(len(ep.tracklist)) if ep.tracklist else "?"
        table.add_row(str(i), ep.name, ep.broadcast, track_count)
    console.print(table)


# ---------------------------------------------------------------------------
# Core flow
# ---------------------------------------------------------------------------

def resolve_episode(args) -> nts.Episode:
    """Determine which episode to use based on CLI args."""
    if args.url:
        show_slug, episode_alias = nts.resolve_from_url(args.url)
        if episode_alias:
            console.print(f"Fetching episode [bold]{episode_alias}[/bold]...")
            episode = nts.get_single_episode(show_slug, episode_alias)
        else:
            # URL points to show root — use latest episode
            console.print(f"Fetching latest episode for show [bold]{show_slug}[/bold]...")
            episodes = nts.get_episodes(show_slug, limit=1)
            if not episodes:
                console.print("[red]No episodes found.[/red]")
                sys.exit(1)
            episode = episodes[0]
    elif args.show:
        show_slug = args.show.strip("/")
        if args.pick:
            n = args.pick_count if hasattr(args, "pick_count") else 10
            console.print(f"Fetching last {n} episodes for [bold]{show_slug}[/bold]...")
            episodes = nts.get_episodes(show_slug, limit=n)
            if not episodes:
                console.print("[red]No episodes found.[/red]")
                sys.exit(1)
            print_episodes_table(episodes)
            idx = IntPrompt.ask(
                "Pick an episode number", default=1
            )
            episode = episodes[max(0, idx - 1)]
        else:
            console.print(f"Fetching latest episode for [bold]{show_slug}[/bold]...")
            episodes = nts.get_episodes(show_slug, limit=1)
            if not episodes:
                console.print("[red]No episodes found.[/red]")
                sys.exit(1)
            episode = episodes[0]
    else:
        console.print("[red]Provide --url or --show.[/red]")
        sys.exit(1)

    # Enrich tracklist if needed (scrape episode page)
    if not episode.tracklist:
        console.print("[dim]No tracklist in API response, scraping episode page...[/dim]")
        nts.enrich_tracklist(episode)

    return episode


def search_and_match(
    client: QobuzClient, tracks: list[nts.Track]
) -> tuple[list[int], list[nts.Track]]:
    """
    Search Qobuz for each NTS track.
    Returns (found_track_ids, unfound_tracks).
    """
    found_ids: list[int] = []
    not_found: list[nts.Track] = []

    results_table = Table(title="Search Results", show_lines=False)
    results_table.add_column("NTS Track", style="bold")
    results_table.add_column("Qobuz Match")
    results_table.add_column("Status", justify="center")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Searching Qobuz...", total=len(tracks))

        for track in tracks:
            progress.update(task, description=f"Searching: {track}")
            match = client.find_best_track(track.artist, track.title)
            if match:
                found_ids.append(match.id)
                results_table.add_row(
                    str(track),
                    str(match),
                    "[green]✓[/green]",
                )
            else:
                not_found.append(track)
                results_table.add_row(
                    str(track),
                    "[dim]—[/dim]",
                    "[red]✗[/red]",
                )
            progress.advance(task)
            time.sleep(0.15)  # rate limiting

    console.print(results_table)
    console.print(
        f"\n[green]{len(found_ids)} found[/green] / "
        f"[red]{len(not_found)} not found[/red] out of {len(tracks)} tracks"
    )
    return found_ids, not_found


def add_to_existing_playlist(
    client: QobuzClient,
    playlist_id: int,
    track_ids: list[int],
    prepend: bool = False,
    is_public: bool = False,
) -> dict:
    """Add tracks to an existing playlist, splitting overflow into new playlists.

    Returns a dict with:
      added       - tracks added to the primary playlist
      overflow    - tracks that went into overflow playlist(s)
      dupes       - tracks skipped as already present
      playlists   - list of (id, name, count) tuples, primary first
    """
    console.print(f"[dim]Fetching existing tracks in playlist {playlist_id}...[/dim]")
    data = client.get_playlist(playlist_id)
    playlist_name = data.get("name", str(playlist_id))
    # get_playlist only returns the first page of tracks, so use the
    # paginated helper for an accurate count and full dedup set.
    existing_items = client.get_playlist_tracks(playlist_id)
    existing_ids = {item["id"] for item in existing_items if "id" in item}
    current_count = len(existing_items)

    new_ids = [tid for tid in track_ids if tid not in existing_ids]
    dupes = len(track_ids) - len(new_ids)
    if dupes:
        console.print(f"[dim]Skipping {dupes} track(s) already in the playlist.[/dim]")

    if not new_ids:
        return {"added": 0, "overflow": 0, "dupes": dupes, "playlists": [(playlist_id, playlist_name, current_count)]}

    slots = QOBUZ_PLAYLIST_LIMIT - current_count
    fits = new_ids[:slots]
    overflow_ids = new_ids[slots:]
    result_playlists = []

    if fits:
        console.print(f"\nAdding {len(fits)} tracks to playlist [bold]{playlist_id}[/bold]...")
        if prepend:
            client.prepend_tracks_to_playlist(playlist_id, fits)
        else:
            client.add_tracks_to_playlist(playlist_id, fits)
        result_playlists.append((playlist_id, playlist_name, len(fits)))

    if overflow_ids:
        console.print(
            f"\n[yellow]{len(overflow_ids)} track(s) overflow the {QOBUZ_PLAYLIST_LIMIT}-track limit "
            f"— creating overflow playlist(s).[/yellow]"
        )
        parts = [overflow_ids[i:i + QOBUZ_PLAYLIST_LIMIT] for i in range(0, len(overflow_ids), QOBUZ_PLAYLIST_LIMIT)]
        for i, part in enumerate(parts, 2):
            name = f"{playlist_name} ({i})"
            console.print(f"\nCreating overflow playlist [bold]{name!r}[/bold]...")
            pl = client.create_playlist(name, is_public=is_public)
            pid = pl["id"]
            client.add_tracks_to_playlist(pid, part)
            result_playlists.append((pid, name, len(part)))

    return {"added": len(fits), "overflow": len(overflow_ids), "dupes": dupes, "playlists": result_playlists}


def create_qobuz_playlist(
    client: QobuzClient,
    episode: nts.Episode,
    track_ids: list[int],
    is_public: bool,
    custom_name: str = "",
) -> dict:
    show_slug = episode.slug or "NTS"
    broadcast = episode.broadcast[:10] if episode.broadcast else ""
    default_name = f"NTS – {episode.name}"
    if broadcast:
        default_name += f" ({broadcast})"

    name = custom_name or default_name
    description = (
        f"Playlist generated from NTS show: {episode.url}\n\n"
        f"{episode.description or ''}"
    ).strip()

    console.print(f"\nCreating playlist [bold]{name!r}[/bold]...")
    playlist = client.create_playlist(name, description=description, is_public=is_public)
    playlist_id = playlist["id"]

    console.print(f"Adding {len(track_ids)} tracks...")
    client.add_tracks_to_playlist(playlist_id, track_ids)

    return playlist


# ---------------------------------------------------------------------------
# All-episodes mega-playlist
# ---------------------------------------------------------------------------

def _track_key(artist: str, title: str) -> tuple:
    n = QobuzClient._normalize
    return (n(artist), n(title))


def cmd_all_episodes(args) -> None:
    # 1. Resolve show slug
    if args.url:
        show_slug = nts.resolve_from_url(args.url)[0]
    elif args.show:
        show_slug = args.show.strip("/")
    else:
        console.print("[red]Provide --url or --show.[/red]")
        sys.exit(1)

    # 2. Show info for playlist name
    show_info = nts.get_show_info(show_slug)
    show_name = show_info.get("name") or show_slug

    # 3. Fetch all episodes
    console.print(f"Fetching all episodes for [bold]{show_name}[/bold]...")
    episodes = nts.get_all_episodes(show_slug, max_episodes=9999)
    if not episodes:
        console.print("[red]No episodes found.[/red]")
        sys.exit(1)
    console.print(f"Found [bold]{len(episodes)}[/bold] episodes.")

    # 4. Enrich each episode's tracklist
    seen_keys: set[tuple] = set()
    all_tracks: list[nts.Track] = []
    duplicate_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Fetching tracklists...", total=len(episodes))
        for ep in episodes:
            progress.update(task, description=f"Fetching: {ep.name}")
            nts.enrich_tracklist(ep)
            for track in ep.tracklist or []:
                key = _track_key(track.artist, track.title)
                if key in seen_keys:
                    duplicate_count += 1
                else:
                    seen_keys.add(key)
                    all_tracks.append(track)
            progress.advance(task)
            time.sleep(0.2)

    # 5. Summary
    total_raw = len(all_tracks) + duplicate_count
    console.print(
        f"\nEpisodes: [bold]{len(episodes)}[/bold] | "
        f"Total tracks: [bold]{total_raw}[/bold] | "
        f"Unique: [bold]{len(all_tracks)}[/bold] | "
        f"Duplicates removed: [bold]{duplicate_count}[/bold]"
    )

    if not all_tracks:
        console.print("\n[yellow]No tracks found across any episode.[/yellow]")
        sys.exit(0)

    # 6. Confirm
    if not args.no_confirm and not args.dry_run:
        if not Confirm.ask(
            f"\nSearch Qobuz for {len(all_tracks)} unique tracks and create a playlist?"
        ):
            console.print("Aborted.")
            return

    # 7. Auth + search
    client = load_qobuz_client()
    login_qobuz(client)

    track_ids, not_found = search_and_match(client, all_tracks)

    if not track_ids:
        console.print("\n[yellow]No tracks found on Qobuz. Playlist not created.[/yellow]")
        return

    # 8. Deduplicate Qobuz IDs (same Qobuz track may match multiple NTS entries)
    track_ids = list(dict.fromkeys(track_ids))

    if args.dry_run:
        console.print("\n[dim]--dry-run: skipping playlist creation.[/dim]")
        return

    # 9. Create/update playlist(s) — Qobuz caps at 2000 tracks per playlist
    if args.add_to:
        result = add_to_existing_playlist(
            client, args.add_to, track_ids,
            prepend=args.prepend, is_public=args.public,
        )
        if not result["added"] and not result["overflow"]:
            console.print("\n[yellow]All tracks are already in that playlist. Nothing added.[/yellow]")
            return
        playlist_lines = "\n".join(
            f"  https://play.qobuz.com/playlist/{pid}  ({count} tracks)"
            for pid, _name, count in result["playlists"]
        )
        console.print(
            Panel(
                f"[green bold]Tracks added![/green bold]\n\n"
                f"Tracks added: {result['added'] + result['overflow']}\n"
                f"Episodes processed: {len(episodes)}\n"
                + (f"Not found on Qobuz: {len(not_found)}\n" if not_found else "")
                + f"\n{playlist_lines}",
                title="Done",
                border_style="green",
            )
        )
    else:
        base_name = args.playlist_name or f"NTS \u2013 {show_name} (Complete)"
        description = f"All-episodes mega-playlist generated from NTS show: https://www.nts.live/shows/{show_slug}"
        parts = [track_ids[i:i + QOBUZ_PLAYLIST_LIMIT] for i in range(0, len(track_ids), QOBUZ_PLAYLIST_LIMIT)]
        multi = len(parts) > 1
        if multi:
            console.print(f"\n[yellow]{len(track_ids)} tracks exceeds Qobuz's 2000-track limit — splitting into {len(parts)} playlists.[/yellow]")

        created_playlists = []
        for i, part_ids in enumerate(parts, 1):
            name = f"{base_name} – Part {i}" if multi else base_name
            console.print(f"\nCreating playlist [bold]{name!r}[/bold]...")
            playlist = client.create_playlist(name, description=description, is_public=args.public)
            playlist_id = playlist["id"]
            console.print(f"Adding {len(part_ids)} tracks in batches...")
            client.add_tracks_to_playlist(playlist_id, part_ids)
            created_playlists.append((name, playlist_id, len(part_ids)))

        # 10. Done panel
        playlist_lines = "\n".join(
            f"  Part {i}: {name}  ({count} tracks)\n"
            f"  https://play.qobuz.com/playlist/{pid}"
            for i, (name, pid, count) in enumerate(created_playlists, 1)
        ) if multi else (
            f"Name:  {created_playlists[0][0]}\n"
            f"ID:    {created_playlists[0][1]}\n"
            f"Tracks added: {created_playlists[0][2]}\n"
            f"\nView at: https://play.qobuz.com/playlist/{created_playlists[0][1]}"
        )
        console.print(
            Panel(
                f"[green bold]{'Playlists' if multi else 'Playlist'} created![/green bold]\n\n"
                + playlist_lines + "\n"
                + f"\nEpisodes processed: {len(episodes)}\n"
                + (f"Not found on Qobuz: {len(not_found)}" if not_found else ""),
                title="Done",
                border_style="green",
            )
        )

    if not_found:
        console.print("\n[yellow]Tracks not found on Qobuz:[/yellow]")
        for t in not_found:
            console.print(f"  [dim]•[/dim] {t}")


# ---------------------------------------------------------------------------
# List-only mode
# ---------------------------------------------------------------------------

def cmd_list(args) -> None:
    show_slug = args.show.strip("/") if args.show else nts.resolve_from_url(args.url)[0]
    n = getattr(args, "list_count", 20)
    console.print(f"Fetching episodes for [bold]{show_slug}[/bold]...")
    episodes = nts.get_all_episodes(show_slug, max_episodes=n)
    if not episodes:
        console.print("[red]No episodes found.[/red]")
        return
    print_episodes_table(episodes)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nts-qobuz",
        description="Create Qobuz playlists from NTS.live show tracklists.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    source = p.add_mutually_exclusive_group()
    source.add_argument(
        "--url",
        metavar="URL",
        help="Full URL of an NTS show or episode.",
    )
    source.add_argument(
        "--show",
        metavar="SLUG",
        help="NTS show slug (e.g. floating-points).",
    )

    p.add_argument(
        "--pick",
        action="store_true",
        help="Interactively pick from recent episodes (requires --show).",
    )
    p.add_argument(
        "--pick-count",
        type=int,
        default=10,
        metavar="N",
        help="How many recent episodes to list when using --pick (default: 10).",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List available episodes without creating a playlist.",
    )
    p.add_argument(
        "--list-count",
        type=int,
        default=20,
        metavar="N",
        help="How many episodes to list with --list (default: 20).",
    )
    p.add_argument(
        "--playlist-name",
        metavar="NAME",
        default="",
        help="Custom playlist name (auto-generated if not set).",
    )
    p.add_argument(
        "--public",
        action="store_true",
        help="Make the Qobuz playlist public (private by default).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Search tracks and show results without creating the playlist.",
    )
    p.add_argument(
        "--no-confirm",
        action="store_true",
        help="Skip confirmation prompts.",
    )
    p.add_argument(
        "--all-episodes",
        action="store_true",
        help="Collect tracks from every episode of the show and create a single mega-playlist.",
    )
    p.add_argument(
        "--add-to",
        metavar="PLAYLIST_ID",
        type=int,
        default=None,
        help="Add tracks to an existing Qobuz playlist instead of creating a new one.",
    )
    p.add_argument(
        "--prepend",
        action="store_true",
        help="When using --add-to, insert new tracks at the beginning of the playlist.",
    )
    p.add_argument(
        "--service",
        action="store_true",
        help="Run in service mode: process every show in the config file once and exit.",
    )
    p.add_argument(
        "--loop",
        action="store_true",
        help="With --service, run continuously, sleeping interval_hours between runs.",
    )
    p.add_argument(
        "--config",
        metavar="PATH",
        default="service_config.json",
        help="Path to service-mode config file (default: service_config.json).",
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.url and not args.show and not args.service:
        parser.print_help()
        sys.exit(0)

    console.print(
        Panel.fit(
            "[bold cyan]NTS → Qobuz Playlist Generator[/bold cyan]",
            border_style="cyan",
        )
    )

    # Service mode: process every show in the config file
    if args.service:
        from pathlib import Path

        import service

        config_path = Path(args.config)
        if not config_path.exists():
            console.print(f"[red]Config file not found:[/red] {config_path}")
            sys.exit(1)
        if args.loop:
            service.run_loop(config_path)
        else:
            service.run_once(config_path)
        return

    # All-episodes mega-playlist
    if args.all_episodes:
        cmd_all_episodes(args)
        return

    # List-only mode (no Qobuz auth needed)
    if args.list:
        cmd_list(args)
        return

    # Resolve episode + tracklist
    episode = resolve_episode(args)
    print_episode_header(episode)
    print_tracklist(episode.tracklist)

    if not episode.tracklist:
        console.print(
            "\n[yellow]This episode has no tracklist on NTS. "
            "Nothing to add to Qobuz.[/yellow]"
        )
        sys.exit(0)

    # Confirm before hitting Qobuz
    if not args.no_confirm and not args.dry_run:
        if not Confirm.ask(
            f"\nSearch Qobuz for {len(episode.tracklist)} tracks and create a playlist?"
        ):
            console.print("Aborted.")
            return

    # Qobuz auth + search
    client = load_qobuz_client()
    login_qobuz(client)

    track_ids, not_found = search_and_match(client, episode.tracklist)

    if not track_ids:
        console.print("\n[yellow]No tracks found on Qobuz. Playlist not created.[/yellow]")
        return

    if args.dry_run:
        console.print("\n[dim]--dry-run: skipping playlist creation.[/dim]")
        return

    if args.add_to:
        result = add_to_existing_playlist(
            client, args.add_to, track_ids,
            prepend=args.prepend, is_public=args.public,
        )
        if not result["added"] and not result["overflow"]:
            console.print("\n[yellow]All tracks are already in that playlist. Nothing added.[/yellow]")
            return
        playlist_lines = "\n".join(
            f"  https://play.qobuz.com/playlist/{pid}  ({count} tracks)"
            for pid, _name, count in result["playlists"]
        )
        console.print(
            Panel(
                f"[green bold]Tracks added![/green bold]\n\n"
                f"Tracks added: {result['added'] + result['overflow']}\n"
                + (f"Not found on Qobuz: {len(not_found)}\n" if not_found else "")
                + f"\n{playlist_lines}",
                title="Done",
                border_style="green",
            )
        )
    else:
        playlist = create_qobuz_playlist(
            client,
            episode,
            track_ids,
            is_public=args.public,
            custom_name=args.playlist_name,
        )
        playlist_id = playlist.get("id")
        console.print(
            Panel(
                f"[green bold]Playlist created![/green bold]\n\n"
                f"Name:  {playlist.get('name')}\n"
                f"ID:    {playlist_id}\n"
                f"Tracks added: {len(track_ids)}\n"
                + (f"Not found on Qobuz: {len(not_found)}\n" if not_found else "")
                + f"\nView at: https://play.qobuz.com/playlist/{playlist_id}",
                title="Done",
                border_style="green",
            )
        )

    if not_found:
        console.print("\n[yellow]Tracks not found on Qobuz:[/yellow]")
        for t in not_found:
            console.print(f"  [dim]•[/dim] {t}")


if __name__ == "__main__":
    main()
