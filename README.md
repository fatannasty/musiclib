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

Tabs: Overview (stats widgets, scan, issues), Duplicates (review and re-pick keepers),
Bad Files (doctor, including selected-file trash with per-file errors), Library (album grid from the scanned catalog, album pages, and track search; tag suggestions
field by field; nothing is selected or written until you choose fields and confirm),
Organize (rename preview/apply), Cover Art, Missing Music (MusicBrainz gaps), Usenet
(indexer search + SABnzbd queue).

Library opens on an **Albums** grid grouped from scanned tags (artist, year, track
count, and cover when a MusicBrainz ID or embedded art exists). Open an album to
see its tracks, already selected so you can edit tags or **Find catalog matches**. Switch to **Tracks**
for the file table and per-track lookup.

On an album page, **Edit album tags** lets you correct up to 200 selected tracks
without a catalog lookup. Check only the fields you want to replace (album, album
artist, year, genre, track artist, or MusicBrainz release ID), then **Preview
changes** to see each file's current and proposed values. No fields are checked
automatically. Mixed values are shown as empty; unchecked fields remain intact.
Blank values cannot clear tags. Changing the form invalidates the preview.
Choose **Confirm and save tags** and confirm the batch to write it. Titles,
track numbers, and filenames stay unchanged; changing track artist explicitly
replaces the artist on every selected track, including compilations.

Manual edits use saved reviews: files moved or changed since preview are skipped,
and per-file permission errors do not stop the remaining files. Results are saved
in Activity. After saving, the album grid refreshes to reflect the new tags.

For individual corrections, choose **Edit track** beside a track on an album page.
This edits only that file, regardless of the album checkboxes. Opt in to title,
track artist, track number, or disc number changes, then **Preview changes** to see
the current and proposed values. Track and disc numbers must be whole numbers
from 1 to 9999; existing totals (such as the `12` in `3/12`) are retained and shown
in the preview. Unchecked tags, other tracks, artwork, and filenames stay unchanged.
Blank values cannot clear tags. **Confirm and save tags** requires confirmation;
changing the form invalidates its preview. No catalog lookup or automatic numbering
is performed. Files changed or moved since preview are refused, including changes
detected while preparing the temporary edit. File-health findings remain intact;
only missing-tag warnings are refreshed after a tag correction. Use **Preview
organization** separately if you want filenames to reflect corrected titles/numbers.

For a sequence of corrections, select up to 200 album tracks and choose **Renumber
selected**. Arrange the selected list with **Up/Down**, choose a starting track
number, and optionally check **Change disc number on all selected tracks**.
**Preview changes** shows each file's current and proposed numbers, including
existing totals. Changing the starting number, optional disc, or order invalidates
the preview; **Confirm and save tags** requires confirmation before any writes.

The sequence follows your arranged list, not filename sorting, and continues
across selected discs. Select one disc at a time to restart numbering. Numbers
must remain between 1 and 9999. Existing track/disc totals are preserved, not
recalculated; unselected files, titles, artists, artwork, and filenames stay
unchanged. Unavailable files retain their sequence slot rather than shifting
the rest. Moved/changed files are skipped at apply time, per-file errors remain
in Activity, and there is no catalog lookup or automatic organization.

On an album page, **Change artwork** lets you upload a JPEG or PNG cover for up
to 200 selected tracks. Images must be no larger than 8 MiB or 25 million pixels;
damaged or animated images are rejected. **Preview artwork** shows the uploaded
image, dimensions, an example of current embedded art, and the exact ready or
blocked files. Previewing saves the image in the app database for review but does
not modify music files. Selecting another image invalidates the displayed preview.

Choose **Apply reviewed artwork** and confirm to replace the embedded front/main
cover in only those ready files. Additional pictures, text tags, filenames, and
folder images stay unchanged. Files are edited through a temporary copy, and the
saved cover is verified before replacing the original. Changed or moved files are
skipped; per-file failures do not stop the rest. Existing file-health findings are
preserved. Supported formats are MP3, FLAC,
M4A/MP4 (including ALAC), Ogg Vorbis, Opus, WAV, and AIFF.

Artwork previews and apply results are saved in Activity. Use **View results →
Review artwork preview** to reopen a preview after refreshing. An apply attempt
consumes its preview; create a fresh preview to retry failed files. Embedded art
takes priority over catalog artwork on album cards, and updated covers refresh
without requiring a manual browser-cache clear.

For safe album organization, select up to 200 tracks on an album page and choose
**Preview organization**. The saved preview lists current and exact proposed paths,
using the album and multidisc templates in Settings (even for loose root files).
Previewing does not move files, create folders, or change track records. Duplicate
targets, existing files (including case-only conflicts), and detected folder
permission problems are blocked with reasons; files are not silently given suffixes.
Permission checks are advisory because NAS ACLs or mount conditions can still
reject an operation when it runs.

Choose **Apply ready moves** and confirm to move only the ready files in that
preview. Paths, tags/hashes, templates, permissions, and target occupancy are
checked again before moving. Changed files and new conflicts are reported without
stopping other files. Existing targets are never overwritten, including if another
process creates a target during the move. Tags, artwork, and empty folders are left
untouched. Preview and apply results remain in Activity; use **View results →
Review rename preview** on a `rename-preview` job to reopen a saved preview after
refreshing. Create a fresh preview to retry failed files.

To remove unwanted album tracks, select up to 200 on an album page and choose
**Trash selected**. This creates a saved preview showing exact source/trash paths,
sizes, and ready/blocked reasons without moving music files or creating folders.
Only the saved ready list is used; changing the album checkboxes does not retarget
an existing preview. Choose **Move reviewed files to trash** and confirm to move
those files, never permanently delete them. Unselected tracks, tags, embedded art,
folder images, and empty source folders stay unchanged.

Trash copies use separate per-preview folders under the configured trash directory,
with no overwriting. The destination bytes and original source are checked before
removal, and Recovery history is saved before the source is removed. Missing,
outside-library, already-trashed, or symbolic-link sources are blocked. Bad files
can be selected even when they have no scan hash; the preview hashes their current
bytes. Files moved or changed since preview are skipped; permission failures do
not stop the remaining files. NAS permission checks at preview are advisory.
If a catalog update fails after a move, the file remains in Recovery and the
reported error asks you to run Scan.

Preview/apply results remain in Activity. Use **View results → Review trash
preview** to reopen a preview after refreshing. An apply attempt consumes it;
create a fresh preview to retry failures. Restore moved files from **Recovery**,
which refuses occupied original paths and supports copying across filesystems
when hard links are unavailable. A file restored later cannot be trashed again
using an already-used preview.

In Library, choose **Lookup**, enter or correct the title and/or artist, then
search. MusicBrainz and iTunes are queried together, and matching albums show Cover Art Archive
or iTunes artwork previews. This works even when a file has no title or artist
tags. Search terms do not modify file tags; only explicitly selected suggestions
are written after confirmation. Tag editing does not embed artwork; use
**Change artwork** to upload a cover or **Cover Art** for automatic downloads.
Lookup failures display the provider error so
network or service problems can be distinguished from an empty search. Interactive
requests use four-second connection/read timeouts without a long automatic retry
sequence. If one provider fails, available matches are shown with a warning instead
of hiding the failure. MusicBrainz requests remain rate limited.

Catalog searches run separately from library changes: you can edit tags, scan,
trash, or organize files while a lookup is running. Only one catalog search or
bulk lookup runs at a time; a second search asks you to wait rather than creating
an unbounded queue. Writes and moves still run one at a time. Activity shows both
background jobs when a bulk lookup and a library operation overlap.

For bulk tagging, select up to 100 Library tracks across pages and choose **Look up
selected tracks**. Review the album cards for each file and select the individual
fields to write, then choose **Apply reviewed tags** and confirm the batch.
No fields are selected automatically; changing a candidate clears that track's
field selection. Files without search terms are reported separately and can be
looked up individually. Per-track tagging does not rename or move files.

To retag a whole album at once, select up to 200 tracks and choose **Tag selected
as one album**. Suggestions prefer albums already in your library, then MusicBrainz
and iTunes. Only album-level fields (album, album artist, artist, year, genre,
release ID) can be written to every selected file. Nothing is selected or written
until you choose fields and confirm. Optionally rename just those files with the
current Organize templates after tags are saved. Files moved or changed while
album suggestions were loading are skipped at apply time; run a fresh search
before applying to those files.

On Bad Files, **Trash selected** moves the files you check even if a check has not
just finished. Permission or lock errors are reported per file and do not abort the
rest of the batch. **Trash all bad files** still skips warnings.

Bulk lookups and writes run in Activity with per-file progress and saved results.
Use **View results → Review suggestions** on a completed `metadata-lookup` job to
reopen its suggestions after navigating away or refreshing. Files are applied
individually: failed or changed files are reported without undoing successful
updates, and files changed since lookup must be scanned and looked up again.

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
