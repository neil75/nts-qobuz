# nts-qobuz

Scan an NTS.live show, grab its tracklist, search Qobuz, and create a playlist — all from the terminal.

## Setup

```bash
cd nts-qobuz
pip install -r requirements.txt
cp .env.example .env
# edit .env with your credentials
```

### Credentials

You need:

| Variable | Where to get it |
|---|---|
| `QOBUZ_EMAIL` | Your Qobuz account email |
| `QOBUZ_PASSWORD` | Your Qobuz account password |
| `QOBUZ_APP_ID` | Qobuz app ID — see note below |
| `QOBUZ_APP_SECRET` | Qobuz app secret — see note below |

**Getting `QOBUZ_APP_ID` / `QOBUZ_APP_SECRET`:** Qobuz requires an application registration to use their API. You can either:
- Register at the [Qobuz partner programme](https://www.qobuz.com/us-en/studio/partners) (for commercial use)
- Use the credentials from an open-source Qobuz client you already use (e.g. Sublime Music, or those documented publicly in projects like `qobuz-dl`)

## Usage

```bash
# Latest episode of a show
python main.py --show floating-points

# Specific episode (paste URL from NTS)
python main.py --url "https://www.nts.live/shows/floating-points/episodes/floating-points-12th-january-2020"

# Interactively pick from the last 10 episodes
python main.py --show floating-points --pick

# List episodes without creating a playlist
python main.py --show floating-points --list

# Dry run — search tracks but don't create the playlist
python main.py --show floating-points --dry-run

# Custom playlist name, make it public
python main.py --show floating-points --playlist-name "Floating Points Jan 2020" --public

# Skip confirmation prompts
python main.py --url "..." --no-confirm
```

## Service mode

Service mode reads a JSON config listing NTS shows and their corresponding
Qobuz playlists, then on each run fetches every episode broadcast since the
last run, prepends any newly-found tracks to the playlist, and persists the
updated `last_updated` timestamp.

```bash
# Copy the example and edit it with your shows + playlist IDs
cp service_config.example.json service_config.json

# Run once (suitable for cron / systemd timers)
python main.py --service

# Run forever, sleeping interval_hours between runs
python main.py --service --loop

# Custom config path
python main.py --service --config /etc/nts-qobuz/shows.json
```

### Config format

```json
{
  "interval_hours": 24,
  "shows": [
    {
      "url": "https://www.nts.live/shows/floating-points",
      "playlist_id": 12345678,
      "last_updated": "2026-01-01T00:00:00Z"
    }
  ]
}
```

Each show entry needs:

| Field | Required | Notes |
|---|---|---|
| `url` *or* `show` | yes | Full NTS URL or just the show slug |
| `playlist_id` | yes | The Qobuz playlist to prepend new tracks to |
| `last_updated` | yes | ISO-8601 timestamp; episodes broadcast after this are processed. Set to a far-back date to backfill. |
| `is_public` | no | Whether overflow playlists should be public (default: false) |

### Splitting and playlist IDs

Tracks are prepended in chronological order so the newest episode's tracks
end up at the very top of the playlist. When the 2000-track Qobuz limit is
reached the run "splits" — overflow tracks go into a new playlist named
`{original} (2)` (and `(3)`, etc.). When that happens, the config's
`playlist_id` is automatically rolled forward to the most recently created
playlist so subsequent runs prepend to the new one.

After each show is processed the config file is rewritten in-place, so a
crash mid-run won't lose committed work.

## How it works

1. **NTS scrape**: Fetches tracklist data from the NTS public API (`nts.live/api/v2`). If the API doesn't include a tracklist, falls back to parsing the embedded Next.js `__NEXT_DATA__` JSON from the episode page.
2. **Qobuz search**: Searches Qobuz for each `artist + title` pair. Uses a title-match heuristic to pick the best result.
3. **Playlist creation**: Creates a private (or public) Qobuz playlist and adds all found tracks.

Tracks not available on Qobuz are listed at the end so you know what's missing.

## Notes

- Not all NTS episodes have tracklists — it depends on the show host.
- Qobuz catalogue coverage varies; expect some tracks to be missing.
- The app is polite to both APIs (small delays between requests).
