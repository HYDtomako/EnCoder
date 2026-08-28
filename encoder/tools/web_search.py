"""Web search via Tavily API. 严格继承 tools/base.py 的 Tool 基类.

Uses the stdlib (urllib) to call Tavily's REST endpoint directly, so no new
hard dependency is added. Set TAVILY_API_KEY to enable.
"""

import json
import os
import urllib.request

from .base import Tool

TAVILY_ENDPOINT = "https://app.tavily.com/search"
_MAX_RESULTS = 10


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web via Tavily for up-to-date information not available "
        "in the local workspace. Returns a numbered list of title / URL / "
        "snippet. Use when the answer needs current or external facts."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query",
            },
            "max_results": {
                "type": "integer",
                "description": "Max results to return, default 5, max 10",
            },
        },
        "required": ["query"],
    }

    def execute(self, query: str, max_results: int = 5) -> str:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            return (
                "Error: TAVILY_API_KEY is not set. "
                "Set it in the environment or a .env file, e.g. "
                "TAVILY_API_KEY=tvly-..."
            )

        try:
            body = json.dumps({
                "api_key": api_key,
                "query": query,
                "max_results": max(1, min(max_results, _MAX_RESULTS)),
            }).encode("utf-8")
            req = urllib.request.Request(
                TAVILY_ENDPOINT,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as e:
            return f"Error searching web: {e}"

        results = data.get("results") or []
        if not results:
            return "(no results)"

        out = []
        for i, r in enumerate(results, 1):
            title = r.get("title") or "(untitled)"
            url = r.get("url") or ""
            snippet = (r.get("content") or r.get("snippet") or "")[:300]
            out.append(f"{i}. {title}\n{url}\n{snippet}")
        return "\n\n".join(out)