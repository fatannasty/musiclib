from __future__ import annotations

import base64
from typing import Any, Callable, Dict, List
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .artwork import Artwork
from .artwork_review import MAX_IMAGE_BYTES, validate_cover
from .catalog_http import catalog_error, catalog_get
from .config import Config
from .db import Database
from .metadata_review import MetadataReview

_IMAGE_HOSTS = {
    "iTunes": ("mzstatic.com",),
    "Deezer": ("dzcdn.net",),
}


def _trusted_cover_url(source: str, url: Any) -> bool:
    if not isinstance(source, str) or not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    sources = set(source.split(" + "))
    if "MusicBrainz" in sources and host == "coverartarchive.org":
        return True
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for provider in sources for suffix in _IMAGE_HOSTS.get(provider, ())
    )


def discover_artwork_batch(
    config: Config,
    db: Database,
    albums: List[Dict[str, Any]],
    progress: Callable[..., None],
    check_cancelled: Callable[[], None],
) -> Dict[str, Any]:
    reviewer = MetadataReview(config, db)
    results = []
    for index, album in enumerate(albums, start=1):
        check_cancelled()
        artist, title = album["album_artist"], album["album"]
        item: Dict[str, Any] = {
            "album_id": album["id"], "album_artist": artist, "album": title,
            "suggestions": [], "status": "no-matches",
        }
        try:
            tracks = [
                dict(track) for status in ("ok", "suspect")
                for track in db.album_tracks(artist, title, status=status)
            ]
            if not tracks:
                raise ValueError("Album is no longer available in the library.")
            if len(tracks) > 200:
                raise ValueError("This album has more than 200 tracks; discover artwork in smaller album batches.")
            suggestion = reviewer.suggest_album(
                [track["id"] for track in tracks], album=title, artist=artist, source="all",
            )
            item["tracks"] = suggestion["tracks"]
            item["warnings"] = suggestion["warnings"]
            item["suggestions"] = [
                candidate for candidate in suggestion["suggestions"]
                if candidate.get("cover_url") and _trusted_cover_url(
                    candidate.get("source", ""), candidate.get("cover_url"),
                )
            ]
            item["status"] = "suggestions" if item["suggestions"] else "no-matches"
        except (ValueError, RuntimeError, OSError) as exc:
            item.update(status="error", error=str(exc), warnings=[])
        results.append(item)
        progress("Finding catalog artwork", index, len(albums), f"{artist} — {title}")
    return {
        "albums": results,
        "candidates": sum(len(item["suggestions"]) for item in results),
        "source": "MusicBrainz, iTunes, and Deezer",
    }


def download_candidate_cover(candidate: Dict[str, Any], config: Config, db: Database) -> bytes:
    source = candidate.get("source")
    url = candidate.get("cover_url")
    fields = candidate.get("fields") or {}
    if not _trusted_cover_url(source, url):
        raise ValueError("The artwork source URL is not allowed.")
    if urlsplit(url).hostname == "coverartarchive.org":
        release_id = fields.get("mbid_release")
        try:
            UUID(str(release_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("The MusicBrainz suggestion has no valid release ID.") from exc
        data = Artwork(config, db).download_front(str(release_id))
        if not data:
            raise ValueError("The Cover Art Archive has no usable front cover for this release.")
    else:
        try:
            response = catalog_get(
                url,
                headers={"User-Agent": "musiclib/0.1", "Accept": "image/jpeg,image/png"},
                timeout=10,
                follow_redirects=False,
                ipv4_only=bool(config.catalog.get("ipv4_only", False)),
            )
            if response.is_redirect:
                raise ValueError("This artwork source requires a redirect; choose another suggestion.")
            response.raise_for_status()
            data = response.content
        except (httpx.HTTPError, OSError) as exc:
            raise ValueError(f"{source} cover download failed: {catalog_error(exc)}") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("The catalog cover exceeds the 8 MiB artwork limit.")
    validate_cover(base64.b64encode(data).decode("ascii"))
    return data
