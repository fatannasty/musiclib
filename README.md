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

The dashboard opens with a bright, light appearance. Use **Dark mode** in the
header to switch themes; your choice is saved in this browser, not on the server.
If browser storage is blocked, the switch still works for the current page and
an unsaved-preference notice appears. The header keeps four collection totals
visible; expand **More library details** for duplicate, issue, and album counts.
Short entrance and hover animations provide feedback without looping decoration.
The dashboard respects your device's reduced-motion setting, including review scrolling.

Album summaries are reconciled from healthy and flagged catalog tracks when the
dashboard starts and before each library-changing operation releases its lock,
including failed or stopped jobs with completed changes. Merges and tag edits
remove obsolete album identities; trash and recovery update album membership and
counts. Reconciliation only updates the database and checks artwork filenames in
member folders: it does not scan audio, change tags, move files, or contact catalogs.
Restarting the updated app also repairs old summaries without a rescan.

Valid MusicBrainz matches and comparisons survive unchanged scans, artwork changes,
and file organization. Changing album membership, titles, or track/disc numbers
clears the affected missing-track comparison; run **Missing Music** again to compare
the current tracks. Changing tagged release IDs also resets the affected release
match. The first reconciliation after upgrading clears old comparisons whose inputs
were not recorded, while retaining known release matches and expected track counts.
CLI commands reconcile on opening the catalog and after checks, cleanup, and organization.

The dashboard keeps a compact Activity bar with readable task names, phase,
file counts, elapsed time, and **Stop job**. Expand **Details, history and results**
for current paths, stop behavior, saved jobs, and paginated results. File-level
results show readable outcomes; raw records are collapsed under **Technical details**.
Close results when finished. Failed jobs have a dismissible notice; dismissing
the notice does not delete the job or its error history. Only one library-changing job
runs at a time. Interrupted jobs are marked for review on restart. The Library tab
supports catalog search and filters; Recovery lists trashed files and restores
them only when the original path is still free.
Job polling preserves the Stop button and keyboard focus rather than rebuilding
the controls. Locked actions explain whether a library operation or catalog
search is busy; browsing, selection, and playback remain available. Background
refreshes do not override a form's own disabled/busy state.

Use **Stop job** in Activity to request a cooperative stop. The job remains
**stopping** and keeps its operation lock until the worker reaches a safe
checkpoint; it is then saved as **cancelled**, with the last processed count.
Queued work can stop before it begins. Current file/album work, network requests,
in-flight decoder workers, and necessary catalog refreshes may finish first.
Waiting decoder tasks are cancelled rather than all being processed. Completed
tag edits and moves are not rolled back; completed trash moves remain recoverable.
Refresh the library and create a fresh preview before retrying. Stopped jobs are
not automatically resumed and do not have a complete result list.

## Listen to tracks

Use **Play** in Library track lists, album pages, Duplicates, or Bad Files to
audition a file before editing or removing it. The player stays open while you
switch tabs. It shows the source format, bitrate, sample rate, bit depth, and
channel count when those are available. Playback never edits the source file.

**Original file** streams the unchanged audio with seeking, using the formats
your browser supports (typically MP3, FLAC, WAV, AAC/M4A, Ogg, and Opus; support
varies by browser and codec). If the browser cannot play it, the player falls
back to **Compatibility MP3**, or you can select that mode yourself. FFmpeg
decodes the app's supported formats, including ALAC, WMA, AIFF, APE, and WavPack,
into a temporary live MP3 stream. FFmpeg is included in the Docker image; local
installs need `ffmpeg` on the server's PATH for compatibility mode.

Compatibility playback is **lossy 320 kbps MP3, 48 kHz stereo**, not a bit-perfect
quality reference. Prefer Original for comparing audio quality; browser/device
mixing and output settings can still affect what you hear. Use the **Position**
slider to jump within a compatibility stream. At most two compatibility streams
can run per server process; switching tracks or choosing **Stop and close** releases the
decoder. Missing, deleted, trashed, out-of-library, and symbolic-link sources
cannot be played. Listening does not replace the **Bad Files** decode checks.

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

**Duplicates** has top-of-page controls for selecting the current page or **all
groups across pages**, plus a selection count and clear action. Confirming
**Trash copies in selected groups** preserves one chosen keeper per group and
leaves unapproved groups alone. File paths and hashes are checked against the
review and again on disk; changed copies or missing keepers block removal.
Errors are listed in Activity. Recoverable duplicate moves use the same verified,
no-overwrite trash mechanism as selected Library trash.

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

[catalog]
album_source = "all"        # all | musicbrainz | itunes | library
ipv4_only = false           # force IPv4 for interactive metadata searches

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

Library uses one active review workspace, with **Select → Preview → Confirm →
Results** progress. Opening another workflow or leaving an unfinished review asks
before discarding its choices. A running review cannot be replaced in place;
you can leave it after confirmation and monitor its job in Activity. Closing a
review does not stop or undo an accepted job. Late responses from a discarded
review cannot replace the current one. Draft choices are not saved across page
reloads; saved job previews can be reopened from Activity, one at a time.

Selection actions appear only when relevant. On an album page, **Edit album
tags** and **Find catalog matches** are the main actions. Open **More album actions**
for numbering, artwork, organization, and duplicate review; **Remove files**
keeps trash separate. In Tracks view, **Catalog lookup** contains **Find album
matches** and **Look up each track**. Select at least two tracks/albums to see
merge actions. Duplicates keeps its manual file picker under **Choose duplicate
files manually**. No tools or navigation tabs have been removed.

Choose **50, 100, 150, or 200 Albums per load**. Scrolling near the bottom appends
the next batch instead of replacing existing albums; **Load more albums** also
works with a keyboard or without automatic scroll detection. Album selections
survive appended batches and search filters until cleared or successfully merged.
Changing the load size resets the displayed list, not the selection.

To combine albums, select their album-card checkboxes and choose **Merge selected
albums** (up to 200 tracks per batch). This works across folders. Enter the target
album name and album artist, then preview the current/new values for every file
and confirm the saved tag review. Those two grouping fields are required for a
merge; other fields remain opt-in. Track artists, titles, numbers, and artwork
are preserved unless explicitly selected. This joins catalog groups through tags;
it does not concatenate audio, delete copies, or move files. In Tracks view,
**Merge selected tracks into an album** supports selections across track pages.
After saving, **Preview organization of updated files** offers a separate,
fixed-target rename review using the album templates.

On an album page, select its tracks and choose **Review album duplicates**.
Review the suggested groups, listen, and choose a keeper for each approved group.
**Trash approved copies** moves only those copies to recoverable trash, with
snapshot and keeper checks. Bad tracks are excluded from duplicate comparison.
The album-scoped comparison does not replace the main Duplicates review list.
After cleanup, **Preview organization of remaining tracks** lets you review exact
destinations before renaming. Tracks view also offers **Review selected duplicates**.

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
Review artwork** to reopen a preview after refreshing. An apply attempt
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
Review organization** on a `rename-preview` job to reopen a saved preview after
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

Preview/apply results remain in Activity. Use **View results → Review trash**
to reopen a preview after refreshing. An apply attempt consumes it;
create a fresh preview to retry failures. Restore moved files from **Recovery**,
which refuses occupied original paths and supports copying across filesystems
when hard links are unavailable. A file restored later cannot be trashed again
using an already-used preview.

In **Recovery**, select up to 200 recoverable entries across pages and choose
**Preview restore selected**. The saved preview lists exact trash files, original
paths, sizes, and ready/blocked reasons without moving files or creating folders.
Choose **Restore reviewed files** and confirm to restore only the saved ready list;
changing checkboxes does not retarget an existing preview.

Occupied paths (including case-only conflicts), duplicate original destinations
within a batch, missing/changed trash files, symbolic links, and unsafe paths are
blocked or skipped. Files are never overwritten or restored under an unexpected
suffix. File bytes, tags, and artwork remain unchanged, and other trash entries
stay untouched. NAS permission failures are reported per file without stopping
the rest; copies across filesystems are verified before the trash source is
removed. Permission checks at preview are advisory.

Preview and apply results remain in Activity. Use **View results → Review restore
preview** to reopen a saved preview after refresh. An apply attempt consumes it;
create a fresh preview to retry failures. Existing single-file **Restore** remains
available. Restored tracks are flagged for review; run **Scan** to refresh their
metadata if needed. Recovery now refreshes restored audio metadata and album
bookkeeping automatically; unreadable files are reported separately from restore
failures, remain restored, and can be checked with **Bad Files**. A catalog/history
failure after a successful move reports the restored path explicitly instead of
moving it back or overwriting anything.

In Library, choose **Lookup**, enter or correct the title and/or artist, then
search. MusicBrainz and iTunes are queried together, and matching albums show Cover Art Archive
or iTunes artwork previews. This works even when a file has no title or artist
tags. Search terms do not modify file tags; only explicitly selected suggestions
are written after confirmation. Tag editing does not embed artwork; use
**Change artwork** to upload a cover or **Cover Art** for automatic downloads.
Lookup failures display the provider error so
network or service problems can be distinguished from an empty search. Interactive
requests use four-second connection/read timeouts. Address/route connection errors
retry once using IPv4; timeouts, rate limits, and HTTP service errors are not
automatically retried. If one provider fails, available matches are shown with a warning instead
of hiding the failure. MusicBrainz requests remain rate limited.

Catalog searches run separately from library changes: you can edit tags, scan,
trash, or organize files while a lookup is running. Only one catalog search or
bulk lookup runs at a time; a second search asks you to wait rather than creating
an unbounded queue. Writes and moves still run one at a time. Activity shows both
background jobs when a bulk lookup and a library operation overlap.

For bulk tagging, select up to 100 Library tracks across pages and choose **Catalog
lookup → Look up each track**. Review the album cards for each file and select the individual
fields to write, then choose **Apply reviewed tags** and confirm the batch.
No fields are selected automatically; changing a candidate clears that track's
field selection. Files without search terms are reported separately and can be
looked up individually. Per-track tagging does not rename or move files.

To retag a whole album at once, select up to 200 tracks and choose **Find album
matches**. Choose **Library + both catalogs**, **Library + MusicBrainz**,
**Library + iTunes**, or **Your library only (offline)**. Only the selected online
providers are contacted; local matches remain first. The offline option makes no
catalog requests. Set a default in **Settings → Album lookup sources and
connections**, or override it for each search. If a catalog fails, use **Search
library only** without losing your album/artist search terms, or use **Edit album
tags** on an album page for manual corrections.

The same settings include **Use IPv4-only catalog connections**, which applies
to interactive album and track metadata searches (including bulk track lookups).
It avoids the initial automatic IPv4/IPv6 attempt when a NAS has broken IPv6
routing. Environment proxy routing and TLS verification are retained; no global
socket or Docker network settings are changed. An unavailable address can also
mean a wider NAS routing or connection-resource problem: changing catalogs cannot
repair a disconnected container. Check the NAS/container network if all online
sources keep failing. These options do not change Cover Art, Missing Music, or
Usenet requests.

Only album-level fields (album, album artist, artist, year, genre,
release ID) can be written to every selected file. Nothing is selected or written
until you choose fields and confirm. Optionally rename just those files with the
current Organize templates after tags are saved. Files moved or changed while
album suggestions were loading are skipped at apply time; run a fresh search
before applying to those files.

On Bad Files, **Trash selected** moves the files you check even if a check has not
just finished. Permission or lock errors are reported per file and do not abort the
rest of the batch. **Trash all bad files** still skips warnings.
Top-of-page **Select all matching files** includes every page under the current
severity/type filters and selects each file once even if it has several issues.
Changing filters clears the selection. Large confirmed selections run as one
background job in bounded batches; the old 200-file per-request limit no longer
blocks this workflow.

### Workflow review and cleanup

The Library now separates catalog lookup, reviewed manual merging, duplicate
cleanup, and organization instead of hiding those operations behind one tagging
label. Track-only controls are hidden on the album grid. Duplicate group rendering,
organization-preview launching, and selection request validation are shared
rather than separately implemented for each entry point.

Recovery and confirmation reviews are deliberate safety features, not redundant
screens. Cover Art handles automatic downloads separately from reviewed uploads;
Missing Music and Usenet are complementary optional workflows, not duplicate
Library views. They remain available rather than being removed without usage
evidence. Potential follow-ups are metadata-edit undo, resuming stopped batches,
reconciling legacy Missing Music summaries after manual regrouping, optional-tab
visibility, and splitting the large dashboard script into modules. None of those
is silently implied by merging albums or stopping a job.

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
.venv/bin/pytest
.venv/bin/ruff check src tests
node --test tests/dashboard.test.cjs
```

The test suite generates its own valid FLAC/MP3/WAV fixtures at the byte level, so
it runs anywhere without binary dependencies.
Dashboard interaction regressions use Node's built-in test runner (no npm install);
Node is only needed to run those development checks, not to serve the dashboard.

## How it handles large libraries

- SQLite catalog (WAL mode) — scans are incremental via size/mtime, re-scans touch
  only changed files
- Tag extraction and hashing run in a process pool across all CPU cores
- Deep decode checks (`doctor --deep`) run ffmpeg in parallel, only on files not
  already validated
- Duplicate search is index-driven (hash index, tag index, duration buckets for
  fingerprints) — no O(n²) scans
