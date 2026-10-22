from __future__ import annotations

import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

import httpx

from ..models import IndexerResult


def _text(node, tag: str) -> str:
    child = node.find(tag)
    return child.text if child is not None and child.text else ""


def score_result(result: IndexerResult, terms: List[str]) -> int:
    title = result.title.lower()
    score = 0
    for term in terms:
        if term and term in title:
            score += 40
    if 30_000_000 <= result.size <= 2_000_000_000:
        score += 20
    elif result.size > 2_000_000_000:
        score -= 10
    if "flac" in title or "mp3" in title:
        score += 5
    if any(bad in title for bad in ("cumpleta", "discogra", "singles collection")):
        score -= 15
    return score


class NewznabClient:
    def __init__(self, name: str, url: str, api_key: str, categories: str = "3000"):
        self.name = name
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.categories = categories
        self.client = httpx.Client(timeout=25, follow_redirects=True)

    def search(self, query: str, limit: int = 50) -> List[IndexerResult]:
        params = {
            "t": "search",
            "q": query,
            "cat": self.categories,
            "apikey": self.api_key,
            "o": "xml",
        }
        try:
            response = self.client.get(f"{self.url}/api", params=params)
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except (httpx.HTTPError, ET.ParseError, ValueError):
            return []
        items = []
        terms = [t.lower() for t in query.split() if len(t) > 2]
        for node in root.iter("item"):
            title = _text(node, "title")
            link = _text(node, "link") or _text(node, "guid")
            if not title or not link:
                continue
            size = 0
            enclosure = node.find("enclosure")
            if enclosure is not None:
                size = int(enclosure.get("length") or 0)
            for attr in node.iter():
                tag = str(attr.tag).rsplit("}", 1)[-1]
                if tag == "attr" and attr.get("name") == "size":
                    try:
                        size = int(attr.get("value") or 0)
                    except ValueError:
                        pass
            result = IndexerResult(
                indexer=self.name,
                title=title,
                url=link,
                size=size,
                posted=_text(node, "pubDate"),
            )
            result.score = score_result(result, terms)
            items.append(result)
        items.sort(key=lambda r: (-r.score, -r.size))
        return items[:limit]


def search_all(
    indexers: List[Dict[str, str]], query: str, limit: int = 50
) -> List[IndexerResult]:
    clients = [
        NewznabClient(
            name=i["name"],
            url=i["url"],
            api_key=i.get("api_key", ""),
            categories=i.get("categories", "3000"),
        )
        for i in indexers
    ]
    results: List[IndexerResult] = []
    with ThreadPoolExecutor(max_workers=max(1, len(clients))) as pool:
        for client_results in pool.map(lambda c: c.search(query), clients):
            results.extend(client_results)
    results.sort(key=lambda r: (-r.score, -r.size))
    return results[:limit]
