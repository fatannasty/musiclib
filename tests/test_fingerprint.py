import shutil
import subprocess

import pytest

from musiclib.fingerprint import compute_fingerprint, similarity


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
