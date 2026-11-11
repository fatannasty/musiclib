import json
import shutil
import subprocess

import pytest

from musiclib.fingerprint import compute_encoded_fingerprint, compute_fingerprint, similarity


def test_similarity_identical():
    fp = ",".join(str(w) for w in (0x12345678, 0x9ABCDEF0, 0x0F0F0F0F, 0xA5A5A5A5,
                                   0x13579BDF, 0x2468ACE0, 0xFFFFFFFF, 0x00000000))
    assert similarity(fp, fp) == 1.0


def test_similarity_different():
    fp_a = ",".join(str(w) for w in (0x00000000,) * 16)
    fp_b = ",".join(str(w) for w in (0xFFFFFFFF,) * 16)
    assert similarity(fp_a, fp_b) == 0.0


def test_similarity_short_fingerprints_rejected():
    assert similarity("1,2,3", "1,2,3") == 0.0


def test_similarity_aligns_small_leading_audio_offsets():
    words = [str((index * 2654435761) & 0xFFFFFFFF) for index in range(120)]
    assert similarity(",".join(words), ",".join(["93828746"] * 8 + words)) == 1.0
    assert similarity(",".join(["93828746"] * 8 + words), ",".join(words)) == 1.0


def test_repeated_silence_fingerprints_are_not_duplicate_evidence():
    assert similarity(",".join(["123456"] * 120), ",".join(["123456"] * 120)) == 0.0


def test_compute_fingerprint_without_fpcalc(tmp_path):
    target = tmp_path / "a.flac"
    target.write_bytes(b"x")
    from musiclib import fingerprint as fp_mod

    if fp_mod.fpcalc_available() is None:
        result = compute_fingerprint(str(target))
        assert result["fingerprint"] is None
    else:
        assert True


def test_compute_fingerprint_parses_output(tmp_path, monkeypatch):
    target = tmp_path / "a.flac"
    target.write_bytes(b"x")
    monkeypatch.setattr("musiclib.fingerprint.fpcalc_available", lambda: "/usr/bin/fpcalc")
    output = "DURATION=180\nFINGERPRINT=1,2,4294967295,0,7\n"

    class FakeProc:
        returncode = 0
        stdout = output
        stderr = ""

    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: FakeProc()
    )
    result = compute_fingerprint(str(target))
    assert result["fingerprint"] == "1,2,4294967295,0,7"
    assert result["duration"] == 180.0


@pytest.mark.skipif(not shutil.which("fpcalc") or not shutil.which("ffmpeg"),
                    reason="Real audio comparison needs fpcalc and FFmpeg")
def test_real_audio_transcode_and_leading_silence_match_but_other_audio_does_not(tmp_path):
    source, copy, other = (tmp_path / name for name in ("source.flac", "copy.mp3", "other.flac"))
    for path, base in ((source, 220), (other, 773)):
        expression = (
            f"0.5*sin(2*PI*({base}+110*floor(mod(t,5)))*t)"
            f"+0.3*sin(2*PI*({base + 110}+55*floor(mod(t,7)))*t)"
        )
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        f"aevalsrc='{expression}':s=44100:d=24", "-y", str(path)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-af", "adelay=300:all=1",
                    "-b:a", "192k", "-y", str(copy)], check=True)
    original, encoded, different = (compute_fingerprint(str(path)) for path in (source, copy, other))
    assert original["fingerprint"] and encoded["fingerprint"] and different["fingerprint"]
    assert similarity(original["fingerprint"], encoded["fingerprint"], minimum=.9) >= .9
    assert similarity(original["fingerprint"], different["fingerprint"], minimum=.9) < .9
    result = compute_encoded_fingerprint(str(source))
    assert isinstance(result["fingerprint"], str) and result["fingerprint"]
    assert "," not in result["fingerprint"]
    assert result["duration"] == 24


def test_encoded_fingerprint_uses_compressed_format_and_full_duration(monkeypatch):
    calls = []
    monkeypatch.setattr("musiclib.fingerprint.fpcalc_available", lambda: "/usr/bin/fpcalc")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "duration": 245.9, "fingerprint": "AQAA_encoded",
        }))

    monkeypatch.setattr(subprocess, "run", run)
    assert compute_encoded_fingerprint("/music/song.flac") == {
        "duration": 245, "fingerprint": "AQAA_encoded",
    }
    assert calls == [(["/usr/bin/fpcalc", "-json", "-length", "120", "/music/song.flac"],
                     {"capture_output": True, "text": True, "timeout": 120, "check": False})]


@pytest.mark.skipif(not shutil.which("fpcalc") or not shutil.which("ffmpeg"),
                    reason="Real audio identification needs fpcalc and FFmpeg")
def test_encoded_fingerprint_keeps_full_duration_beyond_analysis_window(tmp_path):
    source = tmp_path / "long.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=11025:duration=130",
                    "-y", str(source)], check=True)
    result = compute_encoded_fingerprint(str(source))
    assert result["duration"] == 130
    assert result["fingerprint"] and "," not in result["fingerprint"]


@pytest.mark.parametrize("payload", [
    None, [], {}, {"fingerprint": "", "duration": 120},
    {"fingerprint": "encoded", "duration": True},
    {"fingerprint": "encoded", "duration": float("inf")},
    {"fingerprint": "encoded", "duration": 0},
])
def test_encoded_fingerprint_rejects_unusable_output(monkeypatch, payload):
    monkeypatch.setattr("musiclib.fingerprint.fpcalc_available", lambda: "/usr/bin/fpcalc")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, stdout=json.dumps(payload)))
    with pytest.raises(RuntimeError, match="fpcalc returned"):
        compute_encoded_fingerprint("/music/song.flac")


@pytest.mark.parametrize("failure,exception,message", [
    ("missing", ValueError, "needs fpcalc"),
    ("timeout", RuntimeError, "timed out"),
    ("decode", RuntimeError, "could not decode"),
    ("output", RuntimeError, "invalid fingerprint"),
])
def test_encoded_fingerprint_reports_failures(monkeypatch, failure, exception, message):
    monkeypatch.setattr("musiclib.fingerprint.fpcalc_available",
                        lambda: None if failure == "missing" else "/usr/bin/fpcalc")

    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("fpcalc", 120)
        return subprocess.CompletedProcess([], 1 if failure == "decode" else 0, stdout="not json")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(exception, match=message):
        compute_encoded_fingerprint("/music/song.flac")
