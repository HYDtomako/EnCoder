"""Fetch a web page and return its readable text. 严格继承 tools/base.py.

Uses urllib for the request and stdlib html.parser to strip markup, so no new
dependency is added. Script/style blocks are dropped entirely.
"""

import urllib.request
from html.parser import HTMLParser

from .base import Tool

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_SKIP_TAGS = {"script", "style"}
_DEFAULT_MAX_CHARS = 4000


class _TextExtractor(HTMLParser):
    """Collect visible text; skip script/style body and all markup."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return "\n".join(parser.parts)


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "Fetch a web link / URL and return its readable text content. "
        "Use to read a specific page the user points you at."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The web link to read",
            },
            "max_chars": {
                "type": "integer",
                "description": "Max chars of text to return, default 4000",
            },
        },
        "required": ["url"],
    }

    def execute(self, url: str, max_chars: int = _DEFAULT_MAX_CHARS) -> str:
        if not url.startswith(("http://", "https://")):
            return f"Error: unsupported URL '{url}' (only http/https)"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read(200_000)  # cap the download to protect context
            text = _html_to_text(raw.decode("utf-8", errors="replace"))
        except Exception as e:
            return f"Error fetching {url}: {e}"

        # collapse blank runs
        lines = [ln for ln in text.splitlines() if ln.strip()]
        text = "\n".join(lines)
        if not text:
            return "(empty page)"

        max_chars = max(500, max_chars)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated)"
        return text