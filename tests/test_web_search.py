import json

import pytest

from integrations import web_search


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.data).encode("utf-8")


def test_tavily_search_sends_minimal_ai_ready_request(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"query": "Sema", "answer": "answer", "results": []})

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(web_search.urllib.request, "urlopen", fake_urlopen)

    result = web_search.tavily_search("Sema", max_results=3)

    assert result["answer"] == "answer"
    assert captured["url"] == web_search.TAVILY_SEARCH_URL
    assert captured["timeout"] == web_search.TAVILY_TIMEOUT_SECONDS
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"] == {
        "query": "Sema",
        "search_depth": "basic",
        "include_answer": True,
        "include_raw_content": False,
        "max_results": 3,
    }


def test_tavily_search_requires_api_key(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="Missing TAVILY_API_KEY"):
        web_search.tavily_search("query")


def test_tavily_search_wraps_timeout(monkeypatch):
    def timeout_urlopen(_request, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(web_search.urllib.request, "urlopen", timeout_urlopen)

    with pytest.raises(RuntimeError, match="Tavily web search timed out"):
        web_search.tavily_search("query")


def test_tavily_extract_sends_single_url_request(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"results": [{"url": "https://example.com", "raw_content": "Page text."}]})

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(web_search.urllib.request, "urlopen", fake_urlopen)

    result = web_search.tavily_extract("https://example.com")

    assert result["results"][0]["raw_content"] == "Page text."
    assert captured["url"] == web_search.TAVILY_EXTRACT_URL
    assert captured["timeout"] == web_search.TAVILY_TIMEOUT_SECONDS
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"] == {
        "urls": ["https://example.com"],
        "extract_depth": "basic",
        "include_images": False,
    }


def test_format_web_search_result_includes_answer_and_sources():
    formatted = web_search.format_web_search_result(
        {
            "query": "Sema",
            "answer": "Sema is a CLI project.",
            "results": [
                {
                    "title": "Sema Docs",
                    "url": "https://example.com/sema",
                    "content": "Documentation for Sema.",
                    "score": 0.9,
                }
            ],
        }
    )

    assert "Answer: Sema is a CLI project." in formatted
    assert "1. Sema Docs score=0.9" in formatted
    assert "URL: https://example.com/sema" in formatted
    assert "Summary: Documentation for Sema." in formatted


def test_format_web_fetch_result_clips_page_content():
    formatted = web_search.format_web_fetch_result(
        {
            "results": [
                {
                    "url": "https://example.com/docs",
                    "raw_content": "A" * (web_search.MAX_EXTRACT_CHARS + 5),
                }
            ]
        }
    )

    assert "Web page content:" in formatted
    assert "URL: https://example.com/docs" in formatted
    assert f"chars: {web_search.MAX_EXTRACT_CHARS + 5}" in formatted
    assert "[Content truncated.]" in formatted


def test_format_web_fetch_result_reports_failed_extract():
    formatted = web_search.format_web_fetch_result(
        {
            "results": [],
            "failed_results": [
                {
                    "url": "https://example.com/missing",
                    "error": "blocked",
                }
            ],
        }
    )

    assert "no readable content" in formatted
    assert "https://example.com/missing" in formatted
    assert "blocked" in formatted
