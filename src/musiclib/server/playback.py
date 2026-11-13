from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any, Dict

import anyio
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.types import Receive, Scope, Send

from ..config import Config
from ..db import Database
from ..fsops import is_inside
from ..models import Track, audio_format

logger = logging.getLogger(__name__)

MEDIA_TYPES = {
    "mp3": "audio/mpeg", "flac": "audio/flac", "mp4": "audio/mp4",
    "alac": "audio/mp4", "aac": "audio/aac", "ogg": "audio/ogg",
    "opus": "audio/ogg", "wav": "audio/wav", "aiff": "audio/aiff",
    "wma": "audio/x-ms-wma", "ape": "audio/x-ape", "wv": "audio/x-wavpack",
}
DEMUXERS = {
    "mp3": "mp3", "flac": "flac", "mp4": "mov", "alac": "mov",
    "aac": "aac", "ogg": "ogg", "opus": "ogg", "wav": "wav",
    "aiff": "aiff", "wma": "asf", "ape": "ape", "wv": "wv",
}


class _PlaybackResponse(StreamingResponse):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    await self.background()


class Playback:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.slots = BoundedSemaphore(2)

    def format_inventory(self) -> Dict[str, Any]:
        ffmpeg_available = shutil.which("ffmpeg") is not None
        return {
            "ffmpeg_available": ffmpeg_available,
            "formats": [
                {
                    **item,
                    "supported": item["format"] in MEDIA_TYPES,
                    "compatibility_playback": (
                        item["format"] in DEMUXERS and ffmpeg_available
                    ),
                }
                for item in self.db.audio_format_counts()
            ],
        }

    def source(self, track_id: int) -> Track:
        track = self.db.get_track(track_id)
        if track is None or track.status == "deleted":
            raise HTTPException(404, "Track is not available for playback.")
        path = Path(track.path).absolute()
        try:
            unsafe = (
                not is_inside(str(path), self.config.library)
                or is_inside(str(path), self.config.trash_dir)
                or any(part.is_symlink() for part in (path, *path.parents))
            )
        except (OSError, RuntimeError) as exc:
            raise HTTPException(403, "Track path cannot be safely accessed.") from exc
        if unsafe or audio_format(str(path)) not in MEDIA_TYPES:
            raise HTTPException(403, "Only audio files inside the library can be played.")
        if not path.is_file():
            raise HTTPException(404, "Audio file is missing.")
        return track

    def info(self, track_id: int) -> Dict[str, Any]:
        track = self.source(track_id)
        return {
            "id": track.id, "title": track.title or Path(track.path).name,
            "artist": track.artist, "album": track.album,
            "cover_url": f"/api/tracks/{track.id}/cover" if track.has_embedded_art else None,
            "format": track.format or audio_format(track.path),
            "duration": track.duration, "bitrate": track.bitrate,
            "sample_rate": track.sample_rate, "bit_depth": track.bit_depth,
            "channels": track.channels,
            "media_type": MEDIA_TYPES[audio_format(track.path)],
            "original_url": f"/api/tracks/{track.id}/audio",
            "compatible_url": f"/api/tracks/{track.id}/audio?mode=compatible",
            "compatible_available": shutil.which("ffmpeg") is not None,
        }

    async def compatible(self, track: Track, start: float) -> StreamingResponse:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise HTTPException(503, "Compatibility playback needs FFmpeg installed on the server.")
        if not self.slots.acquire(blocking=False):
            raise HTTPException(503, "Two compatibility streams are already playing. Stop one and retry.")
        process = None
        errors = None
        closed = False

        async def close() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            with anyio.CancelScope(shield=True):
                try:
                    if process is not None:
                        if process.returncode is None:
                            try:
                                process.terminate()
                            except ProcessLookupError:
                                pass
                        try:
                            await asyncio.wait_for(process.communicate(), timeout=2)
                        except asyncio.TimeoutError:
                            if process.returncode is None:
                                try:
                                    process.kill()
                                except ProcessLookupError:
                                    pass
                            await process.communicate()
                finally:
                    if errors is not None:
                        errors.close()
                    self.slots.release()

        def error_detail() -> str:
            errors.seek(0)
            return errors.read(2000).decode("utf-8", errors="replace").strip()

        try:
            errors = tempfile.TemporaryFile()
            process = await asyncio.create_subprocess_exec(
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
                "-threads", "1", "-filter_threads", "1", "-protocol_whitelist", "file,pipe",
                "-f", DEMUXERS[audio_format(track.path)], "-ss", str(start), "-i", track.path,
                "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
                "-ac", "2", "-ar", "48000", "-c:a", "libmp3lame", "-b:a", "320k",
                "-threads", "1", "-f", "mp3", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=errors,
            )
            first = await asyncio.wait_for(process.stdout.read(65536), timeout=15)
            if not first:
                await asyncio.wait_for(process.wait(), timeout=2)
                logger.warning("Cannot play track %s: %s", track.id, error_detail())
                raise HTTPException(422, "FFmpeg could not decode this track. Check it in Bad Files.")
        except asyncio.TimeoutError as exc:
            await close()
            raise HTTPException(504, "Audio decoding timed out. Check this file in Bad Files.") from exc
        except OSError as exc:
            await close()
            logger.exception("Could not start compatibility playback for track %s", track.id)
            raise HTTPException(503, "Could not start FFmpeg for playback.") from exc
        except BaseException:
            await close()
            raise

        async def stream():
            try:
                yield first
                while True:
                    chunk = await asyncio.wait_for(process.stdout.read(65536), timeout=30)
                    if not chunk:
                        break
                    yield chunk
                await process.wait()
                if process.returncode:
                    logger.warning("Playback failed for track %s: %s", track.id, error_detail())
                    raise RuntimeError("Audio decoding failed; check the file in Bad Files.")
            except asyncio.TimeoutError as exc:
                logger.warning("Playback stalled for track %s", track.id)
                raise RuntimeError("Audio decoding timed out.") from exc
            finally:
                await close()

        return _PlaybackResponse(
            stream(), media_type="audio/mpeg",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            background=BackgroundTask(close),
        )
