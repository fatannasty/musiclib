from musiclib.models import IndexerResult
from musiclib.usenet.newznab import NewznabClient, score_result, search_all

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
<channel>
<item>
  <title>Artist One - Album A 2001 FLAC</title>
  <link>https://idx.example/nzb/1.nzb</link>
  <pubDate>Mon, 01 Jan 2024</pubDate>
  <enclosure url="https://idx.example/nzb/1.nzb" length="350000000" type="application/x-nzb"/>
</item>
<item>
  <title>Artist One - Album A 2001 MP3 320</title>
  <link>https://idx.example/nzb/2.nzb</link>
  <pubDate>Tue, 02 Jan 2024</pubDate>
  <newznab:attr name="size" value="120000000"/>
</item>
<item>
  <title>Completely Different Thing 1999</title>
  <link>https://idx.example/nzb/3.nzb</link>
  <pubDate>Wed, 03 Jan 2024</pubDate>
  <newznab:attr name="size" value="9000000"/>
</item>
</channel>
</rss>
"""


class FakeResponse:
    def __init__(self, content):
        self.content = content
        self.status_code = 200

    def raise_for_status(self):
        pass


def test_score_result_ranks_matches():
    result = IndexerResult(indexer="i", title="Artist One - Album A 2001 FLAC",
                           url="u", size=350_000_000)
    assert score_result(result, ["artist", "one", "album"]) > 60
    other = IndexerResult(indexer="i", title="Completely Different", url="u", size=5_000)
    assert score_result(other, ["artist", "one", "album"]) < 20


def test_client_search_parses_and_sorts(monkeypatch):
    client = NewznabClient("idx", "https://idx.example", "key")

    def fake_get(url, params=None):
        assert params["t"] == "search"
        assert params["apikey"] == "key"
        return FakeResponse(SAMPLE_XML.encode())

    monkeypatch.setattr(client.client, "get", fake_get)
    results = client.search("artist one album a")
    assert len(results) == 3
    assert results[0].title.startswith("Artist One - Album A 2001 FLAC")
    assert results[0].size == 350_000_000
    assert results[1].size == 120_000_000
    assert results[0].score >= results[1].score >= results[2].score


def test_client_search_handles_http_error(monkeypatch):
    client = NewznabClient("idx", "https://idx.example", "key")

    def fake_get(url, params=None):
        raise ConnectionError("down")

    import httpx

    monkeypatch.setattr(
        client.client, "get",
        lambda url, params=None: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )
    assert client.search("anything") == []


def test_search_all_merges_indexers(monkeypatch):
    results_a = [IndexerResult(indexer="a", title="A result", url="u1", size=300_000_000, score=80)]
    results_b = [IndexerResult(indexer="b", title="B result", url="u2", size=300_000_000, score=100)]
    monkeypatch.setattr(
        NewznabClient, "search", lambda self, q, limit=50: results_a if self.name == "a" else results_b
    )
    merged = search_all([{"name": "a", "url": "x", "api_key": "k"},
                         {"name": "b", "url": "y", "api_key": "k"}], "query")
    assert [r.indexer for r in merged] == ["b", "a"]
