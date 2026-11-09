# musiclib

A music library manager for large collections: scans and catalogs every audio file,
finds and removes broken files, deduplicates (keeping the best version), renames and
reorganizes everything into a clean structure, downloads missing cover art, detects
missing album tracks against MusicBrainz, and fetches replacements from Usenet via
SABnzbd.

Ships with both a CLI and a local web dashboard.

## Requirements

- Python 3.9+
- Optional but recommended: `ffmpeg` (deep corrupt-file detection), `chromaprint`
  (`fpcalc`, audio-fingerprint duplicate detection)

```bash
brew install ffmpeg chromaprint
```

## Install

```bash
cd musiclib
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/musiclib --help
```

## Quick start

```bash
musiclib init --library /path/to/your/music   # writes ~/.config/musiclib/config.toml
musiclib scan                                 # catalog the library (incremental)
musiclib stats                                # overview
```

Recommended workflow, in order:

```bash
musiclib doctor --apply      # trash corrupt/broken files (dry-run without --apply)
musiclib dupes --apply       # remove duplicates, keep the best version
musiclib rename --apply      # reorganize into Artist/Album (Year)/NN - Title.ext
musiclib art --apply         # download + embed missing cover art
musiclib missing             # find gaps in albums via MusicBrainz
musiclib fill "Artist - Album" --apply --wait   # fetch via Usenet into SABnzbd
```

Safety model: every destructive command is a dry-run until you pass `--apply`, and
deletions move files to `<library>/.musiclib/trash/YYYY-MM/` (use `--hard-delete` to
bypass). Every removal is recorded in the database (`deletions` table).

Files placed directly in the library root are organized into
`Singles/<Genre>/Artist - Title.ext`; files without a genre use `Unknown Genre`.
Set `organize.root_singles = false` in the config to disable this rule.

The dashboard keeps a persistent Activity panel with exact phase and file counts,
elapsed time, job history, and paginated results. Only one library-changing job
runs at a time. Interrupted jobs are marked for review on restart. The Library tab
supports catalog search and filters; Recovery lists trashed files and restores
them only when the original path is still free.

## Duplicate detection

Three passes, merged with union-find:

1. **Exact** — identical SHA-256 file hash
2. **Tags** — same normalized artist/album/title with duration within tolerance
3. **Fingerprint** — chromaprint audio similarity (needs `fpcalc`), for files with
   missing or unreliable tags

Best version is chosen by a quality score: lossless beats lossy, higher
bitrate/sample-rate/bit-depth wins, complete tags and embedded art add up, broken
files sink. Ties prefer the shorter, shallower path. In the web dashboard you can
override the keeper per group before applying.

## Configuration

`~/.config/musiclib/config.toml` (or `musiclib.toml` in the working directory, or
`--config`/`MUSICLIB_CONFIG`):

```toml
library = "/path/to/your/music"
db_path = "~/.local/share/musiclib/library.db"

[organize]
template = "{albumartist}/{album} ({year})/{track:02d} - {title}.{ext}"
template_multidisc = "{albumartist}/{album} ({year})/CD{disc}/{track:02d} - {title}.{ext}"

[duplicates]
duration_tolerance = 5
strategy = "tags"            # hash | tags | fingerprint

[brainz]
contact = "you@example.com"  # MusicBrainz requires a real contact

[sabnzbd]
url = "http://localhost:8080"
api_key = "your-sabnzbd-api-key"
complete_dir = "/Downloads/sabnzbd/complete"

[[indexers]]                 # any newznab-compatible indexer
name = "drunkenslug"
url = "https://drunkenslug.com"
api_key = "your-indexer-api-key"
categories = "3000"
```

## Usenet flow

1. `musiclib missing` compares your albums to MusicBrainz and lists missing tracks
2. `musiclib fill --all --apply` searches your indexers, sends the best NZB for each
   incomplete album to SABnzbd (SABnzbd handles PAR repair and extraction)
3. `--wait` polls SABnzbd until downloads finish, then imports and organizes the
   new files into your library automatically

Manual search: `musiclib search "artist - album"` then `musiclib fill "artist - album"`.

## Web dashboard

```bash
musiclib serve               # http://127.0.0.1:8000
```

Tabs: Overview (stats, scan, issues), Duplicates (review and re-pick keepers),
Bad Files (doctor), Library (browse tracks and review MusicBrainz tag suggestions
field by field; nothing is selected or written until you choose fields and confirm),
Organize (rename preview/apply), Cover Art, Missing Music (MusicBrainz gaps), Usenet
(indexer search + SABnzbd queue).

In Library, choose **Lookup**, enter or correct the title and/or artist, then
choose **Search MusicBrainz**. This works even when a file has no title or artist
tags. Search terms do not modify file tags; only explicitly selected suggestions
are written after confirmation. Lookup failures display the provider error so
network or service problems can be distinguished from an empty search.

### LAN access

Run the dashboard on all interfaces so any device on your network can use it:

```bash
musiclib serve --host 0.0.0.0 --port 8000
```

Then open `http://<your-mac-ip>:8000` from a phone, tablet, or PC on the same
network (find your IP with `ipconfig getifaddr en0`). macOS may prompt once to
allow incoming connections — click Allow.

### Keeping it in sync across machines

The repo auto-syncs: a launchd agent (`~/Library/LaunchAgents/com.musiclib.autosync.plist`)
commits and pushes changes on this Mac every 60 seconds. To set up another PC:

```bash
git clone https://github.com/<you>/musiclib.git
cd musiclib
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/musiclib init --library /path/to/music/on/that/pc
```

The auto-sync script pulls with `--rebase` before pushing, so edits from
different machines merge cleanly.

### NAS / home-server deployment (recommended)

For a music collection on a NAS, run musiclib as a Docker container on the NAS
itself — always on, accessible from any device in the house via the browser
(including Windows PCs), with ffmpeg and chromaprint included:

```bash
git clone https://github.com/<you>/musiclib.git && cd musiclib
MUSIC_DIR=/volume1/music DATA_DIR=/volume1/docker/musiclib docker compose up -d --build
```

Then open `http://<nas-ip>:8000`. Full guide: [docs/DEPLOY-NAS.md](docs/DEPLOY-NAS.md).

## CLI reference

| Command | Purpose |
|---|---|
| `init` | create config for a library |
| `scan [--path] [--rescan]` | catalog audio files (parallel, incremental) |
| `stats` | library statistics |
| `doctor [--apply] [--no-deep]` | find/trash broken files |
| `dupes [--apply] [--strategy]` | find/remove duplicates |
| `rename [--apply]` | reorganize and rename |
| `art [--apply] [--replace-small]` | fetch/embed cover art |
| `missing [--refresh]` | MusicBrainz gap detection |
| `search QUERY` | search newznab indexers |
| `fill QUERY|--all [--apply] [--wait]` | send NZBs to SABnzbd, optionally import |
| `sab` | SABnzbd queue/history |
| `serve` | run the web dashboard |
| `config show` | print effective config (secrets redacted) |

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # 54 tests, no network or ffmpeg required
.venv/bin/ruff check src tests
```

The test suite generates its own valid FLAC/MP3/WAV fixtures at the byte level, so
it runs anywhere without binary dependencies.

## How it handles large libraries

- SQLite catalog (WAL mode) — scans are incremental via size/mtime, re-scans touch
  only changed files
- Tag extraction and hashing run in a process pool across all CPU cores
- Deep decode checks (`doctor --deep`) run ffmpeg in parallel, only on files not
  already validated
- Duplicate search is index-driven (hash index, tag index, duration buckets for
  fingerprints) — no O(n²) scans
