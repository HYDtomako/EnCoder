"""Tests for the web tools (web_search / web_fetch).

All network calls are monkeypatched; nothing touches the real internet or
Tavily API.
"""

import json

from corecoder.tools import get_tool
from corecoder.tools.web_fetch import WebFetchTool
from corecoder.tools.web_search import WebSearchTool


class _FakeResp:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int = -1):
        return self._payload


def _boom(*args, **kwargs):
    raise OSError("network down")


# --- web_search ---

def test_web_search_requires_api_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    r = WebSearchTool().execute("what is tavily")
    assert "TAVILY_API_KEY" in r


def test_web_search_formats_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    captured = {}

    def fake_urlopen(req, timeout=20):
        body = json.loads(req.data.decode("utf-8"))
        captured.update(body)
        payload = json.dumps({
            "results": [
                {"title": "Tavily Docs", "url": "https://docs.example/t",
                 "snippet": "an API for web search"},
                {"title": "No Snippet", "url": "https://empty.example"},
            ]
        }).encode("utf-8")
        return _FakeResp(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    r = WebSearchTool().execute("tavily", max_results=3)

    assert captured["query"] == "tavily"
    assert captured["max_results"] == 3
    assert "Tavily Docs" in r and "https://docs.example/t" in r
    assert "an API for web search" in r


def test_web_search_caps_max_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    captured = {}

    def fake_urlopen(req, timeout=20):
        captured.update(json.loads(req.data.decode("utf-8")))
        return _FakeResp(json.dumps({"results": []}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    r = WebSearchTool().execute("q", max_results=99)
    assert captured["max_results"] == 10
    assert "(no results)" in r


def test_web_search_network_error(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    r = WebSearchTool().execute("anything")
    assert r.startswith("Error searching web")


# --- web_fetch ---

def test_web_fetch_strips_html(monkeypatch):
    html = (
        b"<html><head><title>T</title><style>.x{display:none}</style></head>"
        b"<body><script>alert(1)</script><h1>Hello</h1>"
        b"<p>World &amp; more</p></body></html>"
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=20: _FakeResp(html))
    r = WebFetchTool().execute("https://example.com/page")

    assert "Hello" in r and "World & more" in r
    assert "alert" not in r and ".x{" not in r


def test_web_fetch_truncates(monkeypatch):
    html = (b"<p>" + b"word " * 600 + b"</p>")
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=20: _FakeResp(html))
    r = WebFetchTool().execute("https://example.com/", max_chars=500)
    assert "... (truncated)" in r
    # max_chars floor is 500 -> at most that plus the marker line
    assert len(r) <= 500 + len("\n... (truncated)") + 1


def test_web_fetch_rejects_non_http():
    r = WebFetchTool().execute("ftp://example.com/file")
    assert "only http/https" in r


def test_web_fetch_error(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    r = WebFetchTool().execute("https://example.com/")
    assert r.startswith("Error fetching")


def test_web_tools_registered_and_have_schema():
    for name in ("web_search", "web_fetch"):
        t = get_tool(name)
        assert t is not None
        s = t.schema()
        assert s["type"] == "function"
        assert "function" in s and "name" in s["function"]
        assert s["function"]["name"] == name
        assert s["function"]["description"].strip()  # LLM needs a real description
        assert s["function"]["parameters"]["type"] == "object"


def test_web_tools_are_tool_subclasses():
    from corecoder.tools.base import Tool
    for name in ("web_search", "web_fetch"):
        assert isinstance(get_tool(name), Tool)