from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import Config
from .db import Database
from .fsops import delete_file, trash_file
from .models import LOSSLESS_FORMATS, Track

_PUNCT_TABLE = str.maketrans("", "", string.punctuation)
_REMOVE_FEAT_RE = re.compile(r"\s*(?:feat|ft|featuring)\.?\s+.*$", re.IGNORECASE)


def normalize(text: Optional[str]) -> str:
    if not text:
        return ""
    text = _REMOVE_FEAT_RE.sub("", text)
    text = text.lower().translate(_PUNCT_TABLE)
    return " ".join(text.split())


def score_track(track: Track) -> int:
    fmt = (track.format or "").lower()
    bitrate = track.bitrate or 0
    if fmt in LOSSLESS_FORMATS:
        score = 10000
        score += (track.bit_depth or 16) * 10
        score += (track.sample_rate or 44100) // 100
    else:
        score = min(bitrate, 320) if fmt == "mp3" else min(bitrate, 1000)
    for value in (track.artist, track.album, track.title, track.year):
        if value:
            score += 5
    if track.track_no:
        score += 5
    if track.has_embedded_art:
        score += 10
    if track.mbid_release:
        score += 5
    if track.title is None or track.album is None:
        score -= 1000
    for issue in track.issues:
        if issue.severity == "bad":
            score -= 100000
        else:
            score -= 100
    return score


def _duration_close(a: Optional[float], b: Optional[float], tolerance: float) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tolerance


class _UnionFind:
    def __init__(self) -> None:
        self._parent: Dict[int, int] = {}

    def add(self, item: int) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: int) -> int:
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def groups(self) -> List[List[int]]:
        buckets: Dict[int, List[int]] = {}
        for item in self._parent:
            buckets.setdefault(self.find(item), []).append(item)
        return [sorted(members) for members in buckets.values() if len(members) > 1]


@dataclass
class DupeMember:
    track: Track
    score: int
    action: str = "loser"


@dataclass
class DupeGroup:
    key: str
    members: List[DupeMember] = field(default_factory=list)
    keep: Optional[DupeMember] = None
    group_id: Optional[int] = None


@dataclass
class DupeReport:
    groups: List[DupeGroup] = field(default_factory=list)
    trashed: int = 0
    deleted: int = 0
    reclaimed_bytes: int = 0


def pick_keep(members: List[DupeMember]) -> DupeMember:
    def sort_key(member: DupeMember) -> Tuple:
        track = member.track
        depth = track.path.count("/")
        return (-member.score, depth, len(track.path), track.path.lower())
    ordered = sorted(members, key=sort_key)
    return ordered[0]


class DuplicateFinder:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.tolerance = float(config.duplicates.get("duration_tolerance", 5))

    def find(
        self, strategy: Optional[str] = None, fingerprint_pass: Optional[bool] = None
    ) -> List[DupeGroup]:
        strategy = (strategy or self.config.duplicates.get("strategy", "tags")).lower()
        tracks = [t for t in self.db.all_tracks() if t.status in ("ok", "suspect")]
        for track in tracks:
            score = score_track(track)
            if score != track.quality_score:
                self.db.update_track_columns(track.id, quality_score=score)
                track.quality_score = score
        union = _UnionFind()
        for track in tracks:
            union.add(track.id)

        by_hash: Dict[str, List[Track]] = {}
        for track in tracks:
            if track.file_hash:
                by_hash.setdefault(track.file_hash, []).append(track)
                if len(by_hash[track.file_hash]) > 1:
                    union.union(by_hash[track.file_hash][0].id, track.id)

        by_tags: Dict[Tuple[str, str, str], List[Track]] = {}
        for track in tracks:
            if track.title and track.album:
                key = (
                    normalize(track.album_artist or track.artist),
                    normalize(track.album),
                    normalize(track.title),
                )
                by_tags.setdefault(key, []).append(track)
        for group in by_tags.values():
            group.sort(key=lambda t: (t.duration or 0))
            clusters: List[List[Track]] = []
            for track in group:
                placed = False
                for cluster in clusters:
                    if _duration_close(
                        cluster[0].duration, track.duration, self.tolerance
                    ) or _duration_close(
                        cluster[-1].duration, track.duration, self.tolerance
                    ):
                        cluster.append(track)
                        placed = True
                        break
                if not placed:
                    clusters.append([track])
            for cluster in clusters:
                for other in cluster[1:]:
                    union.union(cluster[0].id, other.id)

        if fingerprint_pass is None:
            fingerprint_pass = strategy == "fingerprint"
        if fingerprint_pass:
            from .fingerprint import similarity

            buckets: Dict[int, List[Track]] = {}
            for track in tracks:
                if track.fingerprint and track.duration:
                    buckets.setdefault(round(track.duration / 4), []).append(track)
            for bucket in buckets.values():
                if len(bucket) > 60:
                    continue
                for i, a in enumerate(bucket):
                    for b in bucket[i + 1:]:
                        if similarity(a.fingerprint, b.fingerprint) >= 0.85:
                            union.union(a.id, b.id)

        return self._build_groups(union, tracks)

    def _build_groups(
        self, union: _UnionFind, tracks: List[Track]
    ) -> List[DupeGroup]:
        by_id = {t.id: t for t in tracks}
        groups: List[DupeGroup] = []
        for member_ids in union.groups():
            members = [
                DupeMember(by_id[tid], by_id[tid].quality_score) for tid in member_ids
            ]
            keep = pick_keep(members)
            keep.action = "keep"
            key = f"keep:{keep.track.id}"
            group = DupeGroup(key=key, members=members, keep=keep)
            groups.append(group)
        self._persist(groups)
        return groups

    def _persist(self, groups: List[DupeGroup]) -> None:
        self.db.execute("DELETE FROM dupe_members")
        self.db.execute("DELETE FROM dupe_groups")
        for group in groups:
            cur = self.db.execute(
                "INSERT INTO dupe_groups (key, keep_id, resolved, created_at) "
                "VALUES (?,?,0,datetime('now'))",
                (group.key, group.keep.track.id if group.keep else None),
            )
            group.group_id = cur.lastrowid
            self.db.executemany(
                "INSERT INTO dupe_members (group_id, track_id) VALUES (?,?)",
                [(group.group_id, m.track.id) for m in group.members],
            )

    def persisted_groups(self) -> List[DupeGroup]:
        tracks = {track.id: track for track in self.db.all_tracks() if track.id is not None}
        rows = self.db.query(
            "SELECT g.id AS group_id, g.keep_id, m.track_id FROM dupe_groups g "
            "JOIN dupe_members m ON m.group_id=g.id WHERE g.resolved=0 "
            "ORDER BY g.id"
        )
        groups: Dict[int, DupeGroup] = {}
        for row in rows:
            track = tracks.get(row["track_id"])
            if track is None or track.status not in ("ok", "suspect"):
                continue
            group = groups.setdefault(
                row["group_id"], DupeGroup(key=f"persisted:{row['group_id']}", group_id=row["group_id"])
            )
            member = DupeMember(track, track.quality_score)
            group.members.append(member)
            if track.id == row["keep_id"]:
                member.action = "keep"
                group.keep = member
        result = []
        for group in groups.values():
            if len(group.members) < 2:
                continue
            if group.keep is None:
                group.keep = pick_keep(group.members)
                group.keep.action = "keep"
            result.append(group)
        return result

    def manual_group(self, track_ids: List[int], keep_id: int) -> DupeGroup:
        selected = set(track_ids)
        if len(selected) < 2:
            raise ValueError("select at least two tracks")
        tracks = {
            track.id: track
            for track in self.db.all_tracks()
            if track.id in selected and track.status in ("ok", "suspect")
        }
        if len(tracks) != len(selected):
            raise ValueError("one or more selected tracks are unavailable")
        if keep_id not in tracks:
            raise ValueError("choose one of the selected tracks to keep")
        members = [DupeMember(track, score_track(track)) for track in tracks.values()]
        keep = next(member for member in members if member.track.id == keep_id)
        keep.action = "keep"
        return DupeGroup(key=f"manual:{keep_id}", members=members, keep=keep)

    def apply(
        self,
        groups: List[DupeGroup],
        hard_delete: bool = False,
        keep_ids: Optional[Dict[int, int]] = None,
    ) -> DupeReport:
        keep_ids = keep_ids or {}
        report = DupeReport(groups=groups)
        for group in groups:
            keep_id = keep_ids.get(group.group_id) if group.group_id else None
            for member in group.members:
                if keep_id is not None:
                    member.action = "keep" if member.track.id == keep_id else "loser"
                elif member.action == "keep":
                    continue
                if member.action != "loser":
                    continue
                track = member.track
                try:
                    if hard_delete:
                        delete_file(track.path)
                        self.db.record_deletion(
                            track.path, track.size, track.file_hash,
                            f"duplicate of {group.keep.track.path if group.keep else '?'}",
                            None,
                        )
                        report.deleted += 1
                    else:
                        dest = trash_file(track.path, self.config.trash_dir)
                        self.db.record_deletion(
                            track.path, track.size, track.file_hash,
                            f"duplicate of {group.keep.track.path if group.keep else '?'}",
                            dest,
                        )
                        report.trashed += 1
                    report.reclaimed_bytes += track.size
                    self.db.update_track_columns(track.id, status="deleted")
                except FileNotFoundError:
                    self.db.update_track_columns(track.id, status="deleted")
            if group.group_id:
                final_keep = next(
                    (m.track.id for m in group.members if m.action == "keep"), None
                )
                self.db.execute(
                    "UPDATE dupe_groups SET resolved=1, keep_id=? WHERE id=?",
                    (final_keep, group.group_id),
                )
        return report
