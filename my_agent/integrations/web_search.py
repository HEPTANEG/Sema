import json
import os
import urllib.error
import urllib.request










TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
TAVILY_TIMEOUT_SECONDS = 30
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_LIMIT = 10
MAX_EXTRACT_CHARS = 12000


def tavily_timeout_message():
    return (
        f"Tavily web search timed out after {TAVILY_TIMEOUT_SECONDS} seconds. "
        "Please retry or narrow the query."
    )


def _coerce_max_results(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESULTS
    return max(1, min(parsed, MAX_RESULTS_LIMIT))


def tavily_search(query, max_results=DEFAULT_MAX_RESULTS):
    from runtime.setup_config import value
    api_key = value("search", "api_key")
    if not api_key:
        raise RuntimeError("Missing TAVILY_API_KEY environment variable.")

    query = str(query or "").strip()
    if not query:
        raise RuntimeError("web_search requires a non-empty query.")

    payload = {
        "query": query,
        "search_depth": "basic",
        "include_answer": True,
        "include_raw_content": False,
        "max_results": _coerce_max_results(max_results),
    }
    request = urllib.request.Request(
        TAVILY_SEARCH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:

        with urllib.request.urlopen(request, timeout=TAVILY_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Tavily search error {error.code}: {message}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach Tavily search API: {error.reason}") from error
    except TimeoutError as error:
        raise RuntimeError(tavily_timeout_message()) from error


def tavily_extract(url):
    from runtime.setup_config import value
    api_key = value("search", "api_key")
    if not api_key:
        raise RuntimeError("Missing TAVILY_API_KEY environment variable.")

    url = str(url or "").strip()
    if not url:
        raise RuntimeError("web_fetch requires a non-empty url.")

    payload = {
        "urls": [url],
        "extract_depth": "basic",
        "include_images": False,
    }
    request = urllib.request.Request(
        TAVILY_EXTRACT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:

        with urllib.request.urlopen(request, timeout=TAVILY_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Tavily extract error {error.code}: {message}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach Tavily extract API: {error.reason}") from error
    except TimeoutError as error:
        raise RuntimeError(tavily_timeout_message()) from error


def format_web_search_result(result):
    query = result.get("query") or ""
    answer = result.get("answer") or ""
    results = result.get("results") or []

    lines = [f"Web search results for: {query}".rstrip()]
    if answer:
        lines.append("")
        lines.append(f"Answer: {answer}")

    if not results:
        lines.append("")
        lines.append("No web search results.")
        return "\n".join(lines)

    lines.append("")
    lines.append("Sources:")
    for index, item in enumerate(results, start=1):
        title = item.get("title") or "(untitled)"
        url = item.get("url") or ""
        content = " ".join(str(item.get("content") or "").split())
        score = item.get("score")
        score_text = f" score={score}" if score is not None else ""
        lines.append(f"{index}. {title}{score_text}")
        if url:
            lines.append(f"   URL: {url}")
        if content:
            lines.append(f"   Summary: {content}")
    return "\n".join(lines)


def format_web_fetch_result(result):
    results = result.get("results") or []
    failed = result.get("failed_results") or []

    if not results:
        lines = ["Web fetch returned no readable content."]
        for item in failed:
            url = item.get("url") or ""
            error = item.get("error") or item.get("message") or ""
            lines.append(f"- Failed: {url} {error}".rstrip())
        return "\n".join(lines)

    item = results[0]
    url = item.get("url") or ""
    raw_content = item.get("raw_content") or item.get("content") or ""
    content = str(raw_content).strip()
    clipped = content[:MAX_EXTRACT_CHARS]
    clipped_note = "\n\n[Content truncated.]" if len(content) > MAX_EXTRACT_CHARS else ""

    lines = [
        "Web page content:",
        f"URL: {url}",
        f"chars: {len(content)}",
        "",
        clipped + clipped_note,
    ]
    return "\n".join(lines).rstrip()
