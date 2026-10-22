import pytest

from musiclib.usenet.sabnzbd import Sabnzbd, SabnzbdError


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _make_sab(monkeypatch, payloads):
    sab = Sabnzbd("http://sab:8080", "key123")
    calls = []

    def fake_get(url, params=None):
        calls.append(params)
        mode = params["mode"]
        if mode in payloads:
            return FakeResponse(payloads[mode])
        raise AssertionError(f"unexpected mode {mode}")

    monkeypatch.setattr(sab.client, "get", fake_get)
    return sab, calls


def test_version_and_queue(monkeypatch):
    payloads = {
        "version": {"version": "4.2.0"},
        "queue": {"queue": {"slots": [
            {"nzo_id": "nzo1", "filename": "Artist - Album", "status": "Downloading",
             "sizeleft": "100 MB"},
        ]}},
        "history": {"history": {"slots": [
            {"nzo_id": "nzo0", "name": "Old Album", "status": "Completed"},
        ]}},
    }
    sab, calls = _make_sab(monkeypatch, payloads)
    assert sab.version() == "4.2.0"
    queue = sab.queue()
    assert queue[0]["nzo_id"] == "nzo1"
    history = sab.history()
    assert history[0]["status"] == "Completed"
    assert sab.job_status("nzo1") == "downloading"
    assert sab.job_status("nzo0") == "completed"
    assert sab.job_status("missing") is None
    assert all(c["apikey"] == "key123" for c in calls)


def test_add_url(monkeypatch):
    payloads = {
        "addurl": {"status": True, "nzo_ids": ["nzo42"]},
    }
    sab, calls = _make_sab(monkeypatch, payloads)
    nzo_id = sab.add_url("https://idx/1.nzb", name="Artist - Album")
    assert nzo_id == "nzo42"
    assert calls[0]["name"] == "https://idx/1.nzb"
    assert calls[0]["nzbname"] == "Artist - Album"


def test_add_url_error(monkeypatch):
    payloads = {"addurl": {"status": False, "error": "api key wrong"}}
    sab, _ = _make_sab(monkeypatch, payloads)
    with pytest.raises(SabnzbdError, match="api key wrong"):
        sab.add_url("https://idx/1.nzb")


def test_missing_api_key():
    sab = Sabnzbd("http://sab:8080", "")
    with pytest.raises(SabnzbdError, match="api_key"):
        sab.version()


def test_add_file_posts_multipart(monkeypatch):
    sab = Sabnzbd("http://sab:8080", "key")
    posts = []

    def fake_post(url, params=None, files=None):
        posts.append((params, files))
        return FakeResponse({"status": True, "nzo_ids": ["nzo9"]})

    monkeypatch.setattr(sab.client, "post", fake_post)
    import pathlib

    nzb = pathlib.Path("/tmp/fake.nzb")
    nzb.write_bytes(b"<nzb></nzb>")
    try:
        assert sab.add_file(str(nzb)) == "nzo9"
        assert posts[0][1]["nzbfile"][0] == "fake.nzb"
    finally:
        nzb.unlink()
