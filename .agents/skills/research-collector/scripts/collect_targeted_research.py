#!/usr/bin/env python3
"""Collect targeted web evidence for a research_patch.

Input is a targeted_research_request JSON file. The script performs best-effort
web search, fetches seed/result pages, extracts readable text snippets, and
writes a structured research_patch JSON plus optional Markdown preview.

This script collects evidence. It does not decide final topics or write prose.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, unquote, urlparse
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

LOW_QUALITY_PATTERNS = [
    re.compile(r"No Results for", re.I),
    re.compile(r"Reach Us Now", re.I),
    re.compile(r"federally insured", re.I),
    re.compile(r"Digital Asset Center", re.I),
]


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "section", "article"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = html.unescape(data).strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        self.parts.append(text)

    @property
    def title(self) -> str:
        return normalize_space(" ".join(self.title_parts))[:200]

    @property
    def text(self) -> str:
        return normalize_space("\n".join(self.parts))


@dataclass
class Page:
    url: str
    title: str
    text: str
    ok: bool
    error: str | None = None


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str


def normalize_space(value: str) -> str:
    value = re.sub(r"\r\n?", "\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def is_low_quality_text(value: str) -> bool:
    text = normalize_space(value)
    if not text:
        return True
    return any(pattern.search(text) for pattern in LOW_QUALITY_PATTERNS)


def relevance_terms(value: str) -> list[str]:
    terms: list[str] = []
    for chunk in re.split(r"[\s，。、“”‘’：:；;？！]+", value):
        for part in re.split(r"(?:为什么|为何|怎么|如何|背后|是否|能否|有没有|是不是)", chunk):
            part = part.strip()
            if len(part) >= 2 and part not in terms:
                terms.append(part)
    return terms[:12]


def is_relevant_hit(hit: SearchHit, query: str) -> bool:
    haystack = f"{hit.title}\n{hit.snippet}\n{hit.url}".lower()
    terms = [term.lower() for term in relevance_terms(query)]
    if not terms:
        return True
    return any(term in haystack for term in terms)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_url(url: str, timeout: int, max_chars: int) -> Page:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_chars * 4)
            content_type = resp.headers.get("content-type", "")
            charset = "utf-8"
            match = re.search(r"charset=([\w.-]+)", content_type, re.I)
            if match:
                charset = match.group(1)
            decoded = raw.decode(charset, errors="replace")
    except Exception as exc:  # noqa: BLE001 - collector should report and continue
        return Page(url=url, title="", text="", ok=False, error=str(exc))

    extractor = TextExtractor()
    try:
        extractor.feed(decoded)
    except Exception:
        pass
    text = extractor.text[:max_chars]
    title = extractor.title or infer_title_from_url(url)
    if is_low_quality_text(f"{title}\n{text}"):
        return Page(url=url, title=title, text="", ok=False, error="low_quality_or_no_result_page")
    return Page(url=url, title=title, text=text, ok=bool(text))


def fetch_raw(url: str, timeout: int, max_chars: int) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read(max_chars)
        content_type = resp.headers.get("content-type", "")
        charset = "utf-8"
        match = re.search(r"charset=([\w.-]+)", content_type, re.I)
        if match:
            charset = match.group(1)
        return raw.decode(charset, errors="replace")


def infer_title_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = unquote(parsed.path.rstrip("/").split("/")[-1])
    return name or parsed.netloc or url


def clean_html_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return normalize_space(html.unescape(value))


def is_collectable_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    blocked_hosts = {
        "www.bing.com",
        "bing.com",
        "r.bing.com",
        "th.bing.com",
        "go.microsoft.com",
        "login.live.com",
    }
    if host in blocked_hosts:
        return False
    if any(host.endswith("." + blocked) for blocked in blocked_hosts):
        return False
    if re.search(r"\.(css|js|png|jpg|jpeg|gif|webp|svg|ico|woff2?|ttf|map)(\?|$)", parsed.path, re.I):
        return False
    if "/search?" in parsed.path:
        return False
    return parsed.scheme in {"http", "https"}


def passes_domain_limits(url: str, limits: dict[str, Any]) -> bool:
    host = urlparse(url).netloc.lower()
    allowed = [str(x).lower() for x in limits.get("allowed_domains", []) or []]
    blocked = [str(x).lower() for x in limits.get("blocked_domains", []) or []]
    if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
        return False
    if blocked and any(host == d or host.endswith("." + d) for d in blocked):
        return False
    return True


def search_bing(query: str, max_results: int, timeout: int) -> list[SearchHit]:
    url = "https://www.bing.com/search?q=" + quote_plus(query)
    try:
        raw = fetch_raw(url, timeout=timeout, max_chars=180000)
    except Exception:
        return []
    hits: list[SearchHit] = []
    seen: set[str] = set()

    blocks = re.findall(r'<li class="b_algo".*?</li>', raw, flags=re.I | re.S)
    for block in blocks:
        href_match = re.search(r'<a[^>]+href="(https?://[^"#]+)"[^>]*>(.*?)</a>', block, flags=re.I | re.S)
        if not href_match:
            continue
        link = html.unescape(href_match.group(1))
        if not is_collectable_url(link) or link in seen:
            continue
        title = clean_html_text(href_match.group(2)) or infer_title_from_url(link)
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, flags=re.I | re.S)
        snippet = clean_html_text(snippet_match.group(1)) if snippet_match else ""
        seen.add(link)
        if is_low_quality_text(f"{title}\n{snippet}"):
            continue
        hits.append(SearchHit(url=link, title=title, snippet=snippet))
        if len(hits) >= max_results:
            return hits

    links = re.findall(r'href="(https?://[^"#]+)"', raw)
    for link in links:
        link = html.unescape(link)
        if not is_collectable_url(link):
            continue
        if link in seen:
            continue
        seen.add(link)
        hits.append(SearchHit(url=link, title=infer_title_from_url(link), snippet=""))
        if len(hits) >= max_results:
            break
    return hits


def search_ddgs(query: str, max_results: int, limits: dict[str, Any]) -> list[SearchHit]:
    try:
        from ddgs import DDGS  # type: ignore
    except Exception:
        return []

    hits: list[SearchHit] = []
    try:
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=max_results)
            for item in results:
                url = str(item.get("href") or item.get("url") or "").strip()
                if not url or not is_collectable_url(url) or not passes_domain_limits(url, limits):
                    continue
                title = str(item.get("title") or infer_title_from_url(url))
                snippet = str(item.get("body") or item.get("snippet") or "")
                if is_low_quality_text(f"{title}\n{snippet}"):
                    continue
                hits.append(SearchHit(url=url, title=title, snippet=snippet))
    except Exception:
        return []
    return hits


def search_web(
    query: str,
    max_results: int,
    timeout: int,
    backend: str,
    limits: dict[str, Any],
) -> tuple[str, list[SearchHit]]:
    if backend in {"auto", "ddgs"}:
        hits = [hit for hit in search_ddgs(query, max_results=max_results, limits=limits) if is_relevant_hit(hit, query)]
        if hits or backend == "ddgs":
            return "ddgs", hits
    if backend in {"auto", "bing"}:
        hits = [hit for hit in search_bing(query, max_results=max_results, timeout=timeout) if is_relevant_hit(hit, query)]
        return "bing", hits
    return backend, []


def request_seed_urls(request: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("seed_urls", "urls"):
        for item in request.get(key, []) or []:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict) and item.get("url"):
                urls.append(str(item["url"]))
    return urls


def build_queries(request: dict[str, Any]) -> list[str]:
    queries = [str(q) for q in request.get("search_queries", []) or [] if str(q).strip()]
    task_object = str(request.get("task_object") or "").strip()
    if not queries:
        for question in request.get("target_questions", []) or []:
            question = str(question).strip()
            if question:
                queries.append((task_object + " " + question).strip())
    if not queries and task_object:
        missing = str(request.get("missing_decision") or "").strip()
        queries.append((task_object + " " + missing).strip())
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        if query and query not in seen:
            seen.add(query)
            deduped.append(query)
    return deduped


def keywords_for_question(question: str, task_object: str) -> list[str]:
    chunks = re.split(r"[，。、“”‘’：:；;？！\s]+", question)
    terms = []
    if task_object:
        terms.append(task_object)
        for chunk in re.split(r"(?:为什么|为何|怎么|如何|背后|是否|能否|有没有|是不是)", task_object):
            chunk = chunk.strip()
            if len(chunk) >= 2 and chunk not in terms:
                terms.append(chunk)
    for chunk in chunks:
        chunk = chunk.strip()
        if len(chunk) >= 2 and chunk not in terms:
            terms.append(chunk)
    return terms[:10]


def find_snippets(text: str, terms: list[str], limit: int) -> list[str]:
    snippets: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if not term:
            continue
        start = text.find(term)
        if start < 0:
            continue
        left = max(0, start - 90)
        right = min(len(text), start + len(term) + 180)
        snippet = normalize_space(text[left:right]).replace("\n", " ")
        if snippet and snippet not in seen:
            seen.add(snippet)
            snippets.append(snippet)
        if len(snippets) >= limit:
            break
    return snippets


def collect(request: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    limits = request.get("collection_limit") or {}
    max_results = int(limits.get("max_search_results_per_query", args.max_results))
    max_pages = int(limits.get("max_pages", args.max_pages))
    max_chars = int(limits.get("max_chars_per_page", args.max_chars))
    timeout = int(limits.get("timeout_seconds", args.timeout))

    queries = build_queries(request)
    urls = request_seed_urls(request)
    search_hit_pages: list[Page] = []
    search_log: list[dict[str, Any]] = []

    for query in queries:
        backend_used, hits = search_web(
            query,
            max_results=max_results,
            timeout=timeout,
            backend=args.search_backend,
            limits=limits,
        )
        search_log.append(
            {
                "query": query,
                "backend": backend_used,
                "results": [
                    {"url": hit.url, "title": hit.title, "snippet": hit.snippet}
                    for hit in hits
                ],
            }
        )
        for hit in hits:
            urls.append(hit.url)
            if hit.snippet and not is_low_quality_text(f"{hit.title}\n{hit.snippet}"):
                search_hit_pages.append(
                    Page(
                        url=hit.url,
                        title=hit.title,
                        text=hit.snippet,
                        ok=True,
                        error=None,
                    )
                )

    deduped_urls: list[str] = []
    seen_urls: set[str] = set()
    for url in urls:
        if not is_collectable_url(url) or not passes_domain_limits(url, limits):
            continue
        if url not in seen_urls:
            seen_urls.add(url)
            deduped_urls.append(url)
        if len(deduped_urls) >= max_pages:
            break

    pages: list[Page] = search_hit_pages[:]
    for url in deduped_urls:
        pages.append(fetch_url(url, timeout=timeout, max_chars=max_chars))
        if args.delay:
            time.sleep(args.delay)

    task_object = str(request.get("task_object") or "")
    evidence_by_question: list[dict[str, Any]] = []
    for question in request.get("target_questions", []) or []:
        terms = keywords_for_question(str(question), task_object)
        evidence: list[dict[str, Any]] = []
        for page in pages:
            if not page.ok:
                continue
            snippets = find_snippets(page.text, terms, limit=2)
            if snippets:
                evidence.append(
                    {
                        "source_title": page.title,
                        "url": page.url,
                        "snippets": snippets,
                    }
                )
            if len(evidence) >= args.evidence_per_question:
                break
        evidence_by_question.append({"question": question, "evidence": evidence})

    sources = [
        {
            "title": page.title,
            "url": page.url,
            "ok": page.ok,
            "error": page.error,
            "text_preview": page.text[: args.preview_chars] if page.ok else "",
        }
        for page in pages
    ]

    return {
        "task_object": request.get("task_object"),
        "mode": "targeted_supplement",
        "triggered_by": request.get("triggered_by"),
        "request_id": request.get("request_id"),
        "missing_decision": request.get("missing_decision"),
        "target_questions": request.get("target_questions", []),
        "research_patch": {
            "new_findings": [],
            "evidence_by_question": evidence_by_question,
            "usable_for_decision": [],
            "still_missing": [],
            "sources": sources,
            "search_log": search_log,
            "collector_note": "Script collected evidence snippets only. Agent synthesis must fill new_findings, usable_for_decision, and still_missing.",
        },
        "merge_instruction": "append_only，不覆盖原 research_pack",
        "return_to": request.get("triggered_by") or "topic-planner",
        "next_allowed_steps": [
            "由 agent 综合 research_patch",
            "交回上游阶段重新判断",
            "不得直接生成选题、大纲或正文",
        ],
    }


def write_markdown(path: Path, patch: dict[str, Any]) -> None:
    lines = [
        "# Targeted Research Patch",
        "",
        f"- task_object: {patch.get('task_object')}",
        f"- request_id: {patch.get('request_id')}",
        f"- missing_decision: {patch.get('missing_decision')}",
        "",
        "## Evidence By Question",
        "",
    ]
    for item in patch.get("research_patch", {}).get("evidence_by_question", []):
        lines.append(f"### {item.get('question')}")
        for evidence in item.get("evidence", []):
            lines.append(f"- {evidence.get('source_title')}  ")
            lines.append(f"  {evidence.get('url')}")
            for snippet in evidence.get("snippets", []):
                lines.append(f"  - {snippet}")
        lines.append("")
    lines.append("## Sources")
    for source in patch.get("research_patch", {}).get("sources", []):
        status = "ok" if source.get("ok") else f"failed: {source.get('error')}"
        lines.append(f"- {source.get('title') or source.get('url')} ({status})")
        lines.append(f"  {source.get('url')}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path, help="targeted_research_request JSON")
    parser.add_argument("--out", required=True, type=Path, help="output research_patch JSON")
    parser.add_argument("--out-md", type=Path, help="optional Markdown preview path")
    parser.add_argument("--max-results", type=int, default=5)
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--max-chars", type=int, default=50000)
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--search-backend", choices=["auto", "ddgs", "bing", "none"], default="auto")
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--evidence-per-question", type=int, default=6)
    parser.add_argument("--preview-chars", type=int, default=1200)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    request = load_json(args.request)
    patch = collect(request, args)
    write_json(args.out, patch)
    if args.out_md:
        write_markdown(args.out_md, patch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
