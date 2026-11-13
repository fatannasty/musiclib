from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .artwork import Artwork
from .brainz import Brainz
from .config import Config
from .db import Database
from .doctor import Doctor
from .duplicates import DuplicateFinder
from .fingerprint import compute_all, fpcalc_available
from .rename import Renamer
from .scanner import Scanner
from .server.auth import hash_password, valid_password, valid_username
from .usenet import Sabnzbd, SabnzbdError, search_all

app = typer.Typer(
    name="musiclib",
    help="Music library manager: scan, validate, dedupe, organize, fetch art, fill gaps via Usenet.",
    no_args_is_help=True,
)
console = Console()
_config: Optional[Config] = None


def get_config(config_path: Optional[str] = None) -> Config:
    global _config
    if _config is None or config_path:
        _config = Config.load(Path(config_path) if config_path else None)
    return _config


def get_db(config: Config) -> Database:
    db = Database(config.db_path)
    Scanner(config, db).rebuild_albums()
    return db


def human_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}TB"


def human_duration(seconds: float) -> str:
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


CONFIG_OPTION = typer.Option(None, "--config", help="Path to config.toml")
APPLY_OPTION = typer.Option(False, "--apply", help="Execute changes instead of dry-run")
HARD_DELETE_OPTION = typer.Option(
    False, "--hard-delete", help="Permanently delete instead of moving to trash"
)


@app.command()
def init(
    library: str = typer.Option(..., "--library", help="Path to your music library"),
    config_path: Optional[str] = typer.Option(
        None, "--config", help="Where to write the config"
    ),
) -> None:
    """Create a configuration file for a library."""
    config = Config.create(library, Path(config_path) if config_path else None)
    console.print(f"[green]config written to {config.config_path}[/green]")
    console.print(f"library: {config.library}")
    console.print("next: musiclib scan")


@app.command("create-admin")
def create_admin(config_path: Optional[str] = CONFIG_OPTION) -> None:
    """Create the first administrator account for this Musiclib installation."""
    config = get_config(config_path)
    db = Database(config.db_path)
    try:
        if db.user_count():
            console.print("[red]An account already exists; administrator bootstrap is closed.[/red]")
            raise typer.Exit(1)
        username = typer.prompt("Administrator username").strip().lower()
        if not valid_username(username):
            console.print(
                "[red]Use 3–32 letters, numbers, dots, hyphens, or underscores; start with a letter.[/red]"
            )
            raise typer.Exit(1)
        password = typer.prompt(
            "Administrator password", hide_input=True, confirmation_prompt=True
        )
        if not valid_password(password):
            console.print("[red]Use a password between 12 and 128 characters.[/red]")
            raise typer.Exit(1)
        try:
            db.create_user(username, hash_password(password), role="admin")
        except (ValueError, sqlite3.IntegrityError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print("[green]Administrator account created. Sign in through the Musiclib website.[/green]")
    finally:
        db.close()


@app.command()
def scan(
    path: Optional[str] = typer.Option(
        None, "--path", help="Scan this path instead of the whole library"
    ),
    rescan: bool = typer.Option(
        False, "--rescan", help="Re-read files even if size/mtime are unchanged"
    ),
    config_path: Optional[str] = CONFIG_OPTION,
) -> None:
    """Scan the library and catalog every audio file."""
    config = get_config(config_path)
    db = get_db(config)
    scanner = Scanner(config, db)
    root = str(Path(path).expanduser()) if path else config.library
    console.print(f"[bold]scanning {root}[/bold]")
    with Progress(
        SpinnerColumn(), BarColumn(), TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("{task.fields[current]}"), console=console,
    ) as progress:
        total = len(scanner.iter_audio_files(root)) if not rescan else 0
        task = progress.add_task("scan", total=total or None, current="")
        report = scanner.scan(
            root, rescan=rescan,
            progress=lambda done, current: progress.update(
                task, completed=done, current=Path(current).name[:40]
            ),
        )
        progress.update(task, completed=report.scanned)
    table = Table(title="scan report")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("files found", str(report.scanned))
    table.add_row("skipped (unchanged)", str(report.skipped_unchanged))
    table.add_row("new", str(report.added))
    table.add_row("updated", str(report.updated))
    table.add_row("removed from disk", str(report.removed))
    table.add_row("unparseable", str(report.errors))
    console.print(table)
    for bad_path in report.error_paths[:10]:
        console.print(f"  [red]bad: {bad_path}[/red]")
    db.close()


@app.command()
def stats(config_path: Optional[str] = CONFIG_OPTION) -> None:
    """Show library statistics."""
    config = get_config(config_path)
    db = get_db(config)
    s = db.stats()
    table = Table(title="library")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("tracks", f"{s['tracks']:,}")
    table.add_row("albums", f"{s['albums']:,}")
    table.add_row("size on disk", human_bytes(s["bytes"]))
    table.add_row("duration", human_duration(s["duration_seconds"]))
    table.add_row("tracks with embedded art", f"{s['with_art']:,}")
    table.add_row("open issues", str(s["issues"]))
    table.add_row("duplicate groups", str(s["dupe_groups"]))
    table.add_row("albums missing art", str(s["albums_missing_art"]))
    table.add_row("albums missing tracks", str(s["albums_missing_tracks"]))
    console.print(table)
    db.close()


@app.command()
def doctor(
    deep: Optional[bool] = typer.Option(
        None, "--deep/--no-deep", help="Full audio decode check (requires ffmpeg)"
    ),
    apply: bool = APPLY_OPTION,
    hard_delete: bool = HARD_DELETE_OPTION,
    config_path: Optional[str] = CONFIG_OPTION,
) -> None:
    """Find corrupt/broken files. Use --apply to remove them."""
    config = get_config(config_path)
    db = get_db(config)
    doc = Doctor(config, db)
    console.print(f"[bold]checking {len(db.all_tracks())} files[/bold]")
    if deep and not doc.ffmpeg:
        console.print("[yellow]ffmpeg not found; structural checks only (brew install ffmpeg)[/yellow]")
    with Progress(
        SpinnerColumn(), BarColumn(), TextColumn("{task.fields[current]}"), console=console
    ) as progress:
        task = progress.add_task("decode", total=None, current="")
        report = doc.check(
            deep=deep, apply_actions=apply, hard_delete=hard_delete,
            progress=lambda done, current: progress.update(
                task, completed=done, current=Path(current).name[:40]
            ),
        )
    Scanner(config, db).rebuild_albums()
    for result in report.results[:25]:
        color = "red" if result["severity"] == "bad" else "yellow"
        console.print(
            f"[{color}]{result['severity']}: {result['path']} ({', '.join(result['issues'])})[/{color}]"
        )
    if len(report.results) > 25:
        console.print(f"... and {len(report.results) - 25} more")
    console.print(
        f"checked={report.checked} bad={report.bad} suspect={report.suspect}"
    )
    if apply:
        console.print(
            f"[green]{report.trashed} moved to trash, {report.deleted} deleted[/green]"
        )
    else:
        console.print("[yellow]dry-run; pass --apply to trash bad files[/yellow]")
    db.close()


@app.command()
def dupes(
    strategy: Optional[str] = typer.Option(
        None, "--strategy", help="hash|tags|fingerprint"
    ),
    apply: bool = APPLY_OPTION,
    hard_delete: bool = HARD_DELETE_OPTION,
    config_path: Optional[str] = CONFIG_OPTION,
) -> None:
    """Find duplicate tracks and keep the best version."""
    config = get_config(config_path)
    db = get_db(config)
    finder = DuplicateFinder(config, db)
    if strategy == "fingerprint" and not fpcalc_available():
        console.print(
            "[yellow]fpcalc not found (brew install chromaprint); using tags+hash[/yellow]"
        )
        strategy = "tags"
    if strategy == "fingerprint":
        pending = db.tracks_missing_fingerprint()
        if pending:
            console.print(f"[bold]computing {len(pending)} fingerprints[/bold]")
            with Progress(SpinnerColumn(), BarColumn(), console=console) as progress:
                task = progress.add_task("fingerprint", total=len(pending))
                compute_all(
                    config, db, pending,
                    progress=lambda done, _: progress.update(task, completed=done),
                )
    groups = finder.find(strategy=strategy)
    if not groups:
        console.print("[green]no duplicates found[/green]")
        db.close()
        return
    reclaimed = 0
    for group in groups:
        keep = group.keep
        console.print(f"\n[bold]keep:[/bold] {keep.track.path} [dim](score {keep.score})[/dim]")
        for member in group.members:
            if member.action == "loser":
                reclaimed += member.track.size
                console.print(
                    f"[red]dupe:[/red] {member.track.path} [dim](score {member.score})[/dim]"
                )
    console.print(f"\n[bold]{len(groups)} duplicate groups, {human_bytes(reclaimed)} reclaimable[/bold]")
    if apply:
        report = finder.apply(groups, hard_delete=hard_delete)
        Scanner(config, db).rebuild_albums()
        console.print(
            f"[green]{report.trashed} moved to trash, {report.deleted} deleted, "
            f"{human_bytes(report.reclaimed_bytes)} reclaimed[/green]"
        )
        for failure in report.failures:
            console.print(f"Skipped {failure['path']}: {failure['error']}", style="red", markup=False)
    else:
        console.print("[yellow]dry-run; pass --apply to remove losers[/yellow]")
    db.close()


@app.command()
def rename(
    apply: bool = APPLY_OPTION,
    config_path: Optional[str] = CONFIG_OPTION,
) -> None:
    """Reorganize and rename files to match the template."""
    config = get_config(config_path)
    db = get_db(config)
    renamer = Renamer(config, db)
    report = renamer.plan()
    for move in report.moves[:40]:
        console.print(f"[cyan]{move.source}[/cyan]\n  -> [green]{move.target}[/green]")
    if len(report.moves) > 40:
        console.print(f"... and {len(report.moves) - 40} more")
    for path, reason in report.skipped[:10]:
        console.print(f"[yellow]skip: {path} ({reason})[/yellow]")
    console.print(
        f"\n[bold]{len(report.moves)} to move, {report.already_ok} already organized, "
        f"{len(report.skipped)} skipped[/bold]"
    )
    if apply:
        done = renamer.apply(report)
        Scanner(config, db).rebuild_albums()
        console.print(f"[green]{done.moved} files moved[/green]")
        for src, err in done.errors[:10]:
            console.print(f"[red]error: {src}: {err}[/red]")
    else:
        console.print("[yellow]dry-run; pass --apply to move files[/yellow]")
    db.close()


@app.command()
def art(
    apply: bool = APPLY_OPTION,
    replace_small: bool = typer.Option(
        False, "--replace-small", help="Also re-download art that is too small"
    ),
    config_path: Optional[str] = CONFIG_OPTION,
) -> None:
    """Download missing album art and embed it."""
    config = get_config(config_path)
    db = get_db(config)
    artwork = Artwork(config, db)
    brainz = Brainz(config, db)
    albums = artwork.albums_needing_art(replace_small)
    if not albums:
        console.print("[green]all albums have art[/green]")
        db.close()
        return
    console.print(f"[bold]{len(albums)} albums missing art[/bold]")
    if not apply:
        for album in albums:
            console.print(f"  {album.album_artist} - {album.album}")
        console.print("[yellow]dry-run; pass --apply to download[/yellow]")
        db.close()
        return
    for album in albums:
        result = artwork.process_album(album, brainz.resolve_release, replace_small)
        color = {"downloaded": "green", "skipped": "yellow", "failed": "yellow",
                 "none": "yellow", "small": "yellow", "error": "red"}.get(result.status, "white")
        console.print(
            f"[{color}]{result.status}: {result.artist} - {result.album} "
            f"({result.detail})[/{color}]"
        )
    db.close()


@app.command()
def missing(
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-query MusicBrainz even if cached"
    ),
    config_path: Optional[str] = CONFIG_OPTION,
) -> None:
    """Compare albums against MusicBrainz and report missing tracks."""
    config = get_config(config_path)
    db = get_db(config)
    brainz = Brainz(config, db)
    with Progress(SpinnerColumn(), TextColumn("{task.fields[current]}"), console=console) as progress:
        progress.add_task("brainz", total=None, current="")
        gaps = brainz.refresh_albums(force=refresh)
    if not gaps:
        gaps = brainz.albums_with_gaps()
    gaps = [g for g in gaps if g.missing]
    if not gaps:
        console.print("[green]no missing tracks detected[/green]")
        db.close()
        return
    table = Table(title="missing tracks")
    table.add_column("artist")
    table.add_column("album")
    table.add_column("missing")
    for gap in gaps:
        titles = ", ".join(
            f"{t.position}. {t.title}" for t in gap.missing if t.title
        )
        table.add_row(gap.album.album_artist, gap.album.album, titles[:120])
    console.print(table)
    console.print("run [bold]musiclib fill \"artist - album\"[/bold] to fetch via Usenet")
    db.close()


@app.command()
def search(
    query: str = typer.Argument(..., help="Search terms, e.g. 'artist - album'"),
    limit: int = typer.Option(25, "--limit", help="Max results"),
    config_path: Optional[str] = CONFIG_OPTION,
) -> None:
    """Search newznab indexers for music."""
    config = get_config(config_path)
    clients = config.indexer_clients()
    if not clients:
        console.print("[red]no indexers configured with url+api_key in config[/red]")
        raise typer.Exit(1)
    results = search_all(clients, query, limit)
    if not results:
        console.print("[yellow]no results[/yellow]")
        return
    table = Table(title=f"results: {query}")
    table.add_column("idx")
    table.add_column("score", justify="right")
    table.add_column("size", justify="right")
    table.add_column("title")
    for result in results:
        table.add_row(
            result.indexer, str(result.score), human_bytes(result.size), result.title[:80]
        )
    console.print(table)


@app.command()
def fill(
    query: Optional[str] = typer.Argument(None, help="'artist - album' to fetch"),
    all_missing: bool = typer.Option(
        False, "--all", help="Fetch every album with missing tracks"
    ),
    apply: bool = APPLY_OPTION,
    wait: bool = typer.Option(False, "--wait", help="Wait for SABnzbd then import"),
    config_path: Optional[str] = CONFIG_OPTION,
) -> None:
    """Find missing music on Usenet and send it to SABnzbd."""
    config = get_config(config_path)
    db = get_db(config)
    clients = config.indexer_clients()
    if not clients:
        console.print("[red]no indexers configured with url+api_key in config[/red]")
        raise typer.Exit(1)
    if not config.sabnzbd.get("api_key"):
        console.print("[red]sabnzbd api_key not configured[/red]")
        raise typer.Exit(1)
    sab = Sabnzbd(config.sabnzbd["url"], config.sabnzbd["api_key"])
    targets: List[str] = []
    if all_missing:
        brainz = Brainz(config, db)
        gaps = brainz.albums_with_gaps()
        targets = [f"{g.album.album_artist} - {g.album.album}" for g in gaps if g.missing]
    elif query:
        targets = [query]
    else:
        console.print("pass a query or --all")
        raise typer.Exit(2)
    if not targets:
        console.print("[green]nothing to fetch[/green]")
        db.close()
        return
    nzo_ids = []
    for target in targets:
        results = search_all(clients, target, 20)
        results = [r for r in results if r.score > 40]
        if not results:
            console.print(f"[yellow]no usable results for: {target}[/yellow]")
            continue
        best = results[0]
        console.print(
            f"[bold]{target}[/bold] -> {best.title} [dim]({best.indexer}, "
            f"{human_bytes(best.size)})[/dim]"
        )
        if not apply:
            continue
        try:
            nzo_id = sab.add_url(best.url, name=target)
            db.record_download(nzo_id, target, best.url, best.indexer)
            nzo_ids.append((nzo_id, target))
            console.print(f"[green]queued in SABnzbd as {nzo_id}[/green]")
        except SabnzbdError as exc:
            console.print(f"[red]failed: {exc}[/red]")
    if not apply:
        console.print("[yellow]dry-run; pass --apply to send to SABnzbd[/yellow]")
        db.close()
        return
    if wait and nzo_ids:
        console.print("[bold]waiting for SABnzbd to finish...[/bold]")
        for nzo_id, target in nzo_ids:
            while True:
                status = sab.job_status(nzo_id)
                if status in ("completed", "failed"):
                    db.update_download_status(nzo_id, status)
                    console.print(f"{target}: {status}")
                    break
                time.sleep(10)
        complete_dir = config.sabnzbd.get("complete_dir")
        if complete_dir:
            console.print(f"[bold]importing from {complete_dir}[/bold]")
            scanner = Scanner(config, db)
            report = scanner.scan(complete_dir)
            renamer = Renamer(config, db)
            done = renamer.apply(renamer.plan())
            console.print(
                f"[green]imported {report.added + report.updated} files, "
                f"organized {done.moved}[/green]"
            )
    db.close()


@app.command()
def sab(
    config_path: Optional[str] = CONFIG_OPTION,
) -> None:
    """Show SABnzbd queue and recent history."""
    config = get_config(config_path)
    if not config.sabnzbd.get("api_key"):
        console.print("[red]sabnzbd api_key not configured[/red]")
        raise typer.Exit(1)
    sab_client = Sabnzbd(config.sabnzbd["url"], config.sabnzbd["api_key"])
    try:
        console.print(f"SABnzbd version: {sab_client.version()}")
    except SabnzbdError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    queue = sab_client.queue()
    table = Table(title="queue")
    table.add_column("title")
    table.add_column("status")
    table.add_column("size left", justify="right")
    for slot in queue:
        table.add_row(
            slot.get("filename", "?")[:60], slot.get("status", "?"),
            slot.get("sizeleft", "?"),
        )
    console.print(table)
    history = sab_client.history()
    table = Table(title="history")
    table.add_column("title")
    table.add_column("status")
    for slot in history[:15]:
        table.add_row(slot.get("name", "?")[:60], slot.get("status", "?"))
    console.print(table)


@app.command()
def config_show(config_path: Optional[str] = CONFIG_OPTION) -> None:
    """Print the effective configuration."""
    config = get_config(config_path)
    console.print_json(json.dumps(
        {
            "config_path": str(config.config_path),
            "library": config.library,
            "db_path": config.db_path,
            "trash_dir": config.trash_dir,
            "organize": config.organize,
            "doctor": config.doctor,
            "duplicates": config.duplicates,
            "artwork": config.artwork,
            "sabnzbd": {**config.sabnzbd, "api_key": "***" if config.sabnzbd.get("api_key") else ""},
            "indexers": [
                {**i, "api_key": "***" if i.get("api_key") else ""} for i in config.indexers
            ],
        }
    ))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    config_path: Optional[str] = CONFIG_OPTION,
) -> None:
    """Run the web dashboard."""
    import uvicorn

    from .server import create_app

    config = get_config(config_path)
    dashboard = create_app(config)
    console.print(f"[bold]dashboard on http://{host}:{port}[/bold]")
    uvicorn.run(dashboard, host=host, port=port, log_level="warning")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
