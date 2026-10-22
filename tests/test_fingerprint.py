import subprocess

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
