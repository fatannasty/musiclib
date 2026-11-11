import errno

import httpx
import pytest

import musiclib.catalog_http as catalog


@pytest.mark.parametrize("wrapped", [False, True])
def test_unavailable_address_retries_once_over_ipv4(monkeypatch, wrapped):
    calls = []
    failure = OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
    if wrapped:
        outer = httpx.ConnectError("connect failed")
        outer.__cause__ = failure
        failure = outer

    def automatic(url, **kwargs):
        calls.append(("auto", url, kwargs))
        raise failure

    def ipv4(url, **kwargs):
        calls.append(("ipv4", url, kwargs))
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(catalog.httpx, "get", automatic)
    monkeypatch.setattr(catalog, "_ipv4_get", ipv4)
    response = catalog.catalog_get(
        "https://itunes.apple.com/search", params={"term": "Artist Album"}, timeout=4.0,
    )
    assert response.status_code == 200
    assert [call[0] for call in calls] == ["auto", "ipv4"]
    assert calls[0][1:] == calls[1][1:]


def test_failed_ipv4_recovery_is_bounded_and_actionable(monkeypatch):
    calls = []

    def fail(url, **kwargs):
        calls.append(url)
        raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")

    monkeypatch.setattr(catalog.httpx, "get", fail)
    monkeypatch.setattr(catalog, "_ipv4_get", fail)
    with pytest.raises(OSError) as exc:
        catalog.catalog_get("https://musicbrainz.org/ws/2/release/", timeout=4.0)
    assert len(calls) == 2
    message = catalog.catalog_error(exc.value)
    assert "NAS/container" in message
    assert "IPv4-only" in message
    assert "offline" in message


@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout("too slow"), httpx.ConnectError("connection refused"),
    OSError(errno.EACCES, "permission denied"),
])
def test_unrelated_failures_are_not_retried(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    def no_retry(*args, **kwargs):
        pytest.fail("Only address/route connection failures should trigger IPv4 recovery")

    monkeypatch.setattr(catalog.httpx, "get", fail)
    monkeypatch.setattr(catalog, "_ipv4_get", no_retry)
    with pytest.raises(type(failure)):
        catalog.catalog_get("https://itunes.apple.com/search")


@pytest.mark.parametrize("status", [429, 503])
def test_http_errors_do_not_trigger_connection_retries(monkeypatch, status):
    response = httpx.Response(status, request=httpx.Request("GET", "https://musicbrainz.org"))
    monkeypatch.setattr(catalog.httpx, "get", lambda *args, **kwargs: response)

    def no_retry(*args, **kwargs):
        pytest.fail("HTTP failures must not trigger extra requests")

    monkeypatch.setattr(catalog, "_ipv4_get", no_retry)
    assert catalog.catalog_get("https://musicbrainz.org") is response


def test_forced_ipv4_never_uses_automatic_transport(monkeypatch):
    def no_automatic(*args, **kwargs):
        pytest.fail("IPv4-only requests must not use the automatic transport")

    response = httpx.Response(200)
    monkeypatch.setattr(catalog.httpx, "get", no_automatic)
    monkeypatch.setattr(catalog, "_ipv4_get", lambda *args, **kwargs: response)
    assert catalog.catalog_get("https://musicbrainz.org", ipv4_only=True) is response


@pytest.mark.parametrize("bypass", [False, True])
def test_ipv4_transport_preserves_proxy_routing_and_request_options(monkeypatch, bypass):
    observed = {}
    transport = object()

    def create_transport(**kwargs):
        observed["transport"] = kwargs
        return transport

    class Client:
        def __init__(self, **kwargs):
            observed["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, url, **kwargs):
            observed["method"] = method
            observed["request"] = (url, kwargs)
            return httpx.Response(200)

    monkeypatch.setattr(catalog, "getproxies", lambda: {"https": "http://proxy.test:8080"})
    monkeypatch.setattr(catalog, "proxy_bypass", lambda host: bypass)
    monkeypatch.setattr(catalog.httpx, "HTTPTransport", create_transport)
    monkeypatch.setattr(catalog.httpx, "Client", Client)
    response = catalog.catalog_get(
        "https://itunes.apple.com/search", ipv4_only=True,
        params={"term": "Album"}, headers={"User-Agent": "musiclib/test"}, timeout=4.0,
    )
    assert response.status_code == 200
    assert observed["transport"] == {
        "local_address": "0.0.0.0", "proxy": None if bypass else "http://proxy.test:8080", "retries": 0,
    }
    assert observed["client"] == {"transport": transport, "trust_env": False}
    assert observed["method"] == "GET"
    assert observed["request"] == ("https://itunes.apple.com/search", {
        "params": {"term": "Album"}, "headers": {"User-Agent": "musiclib/test"}, "timeout": 4.0,
    })


@pytest.mark.parametrize("forced", [False, True])
def test_catalog_post_preserves_form_body_during_ipv4_recovery(monkeypatch, forced):
    calls = []

    def automatic(url, **kwargs):
        calls.append(("auto", url, kwargs))
        raise OSError(errno.ENETUNREACH, "Network is unreachable")

    def ipv4(url, **kwargs):
        calls.append(("ipv4", url, kwargs))
        return httpx.Response(200, json={"status": "ok", "results": []})

    monkeypatch.setattr(catalog.httpx, "post", automatic)
    monkeypatch.setattr(catalog, "_ipv4_post", ipv4)
    data = {"client": "test-key", "fingerprint": "encoded", "duration": "180"}
    catalog.catalog_post("https://api.acoustid.org/v2/lookup", data=data, timeout=10,
                         ipv4_only=forced)
    assert [call[0] for call in calls] == (["ipv4"] if forced else ["auto", "ipv4"])
    assert all(call[1:] == ("https://api.acoustid.org/v2/lookup", {"data": data, "timeout": 10})
               for call in calls)


def test_ipv4_post_uses_post_method_and_preserves_form_body(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200)

    monkeypatch.setattr(catalog, "_ipv4_request", request)
    catalog.catalog_post("https://api.acoustid.org/v2/lookup", data={"client": "test"},
                         ipv4_only=True)
    assert calls == [("POST", "https://api.acoustid.org/v2/lookup", {"data": {"client": "test"}})]


def test_catalog_post_does_not_retry_timeouts(monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append(args)
        raise httpx.ReadTimeout("too slow")

    monkeypatch.setattr(catalog.httpx, "post", timeout)
    monkeypatch.setattr(catalog, "_ipv4_post", timeout)
    with pytest.raises(httpx.ReadTimeout):
        catalog.catalog_post("https://api.acoustid.org/v2/lookup", data={"client": "test"})
    assert len(calls) == 1
