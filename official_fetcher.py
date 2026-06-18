"""
official_fetcher.py - 회사 공식 IR/Newsroom 페이지 감시

RSS/Atom 피드가 있으면 피드로 읽고, 일반 HTML 페이지면 링크를 추출해서
새 공식 발표/뉴스룸 글을 감지합니다.
"""
import calendar
import hashlib
import html
import logging
import re
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; StockAlarmBot/1.0)",
    "Accept": "text/html,application/rss+xml,application/atom+xml,application/xml,text/xml,*/*;q=0.8",
}

_LINK_HINTS = re.compile(
    r"(news|press|release|releases|investor|investors|ir|events|announcements|blog|posts|articles|updates)",
    re.IGNORECASE,
)
_SKIP_LINKS = re.compile(
    r"(javascript:|mailto:|tel:|#|/privacy|/terms|/cookie|/login|/signup|/careers|/jobs|/contact)",
    re.IGNORECASE,
)


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(str(text))
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _configured_sources(config: dict) -> List[Dict[str, str]]:
    sources = config.get("official_feeds", [])
    if not isinstance(sources, list):
        return []

    result: List[Dict[str, str]] = []
    for idx, source in enumerate(sources, 1):
        if isinstance(source, str):
            url = source.strip()
            name = f"Official {idx}"
            ticker = ""
        elif isinstance(source, dict):
            url = str(source.get("url") or "").strip()
            name = str(source.get("name") or source.get("ticker") or f"Official {idx}").strip()
            ticker = str(source.get("ticker") or "").upper().strip()
        else:
            continue

        if not url:
            continue
        result.append({"url": url, "name": name, "ticker": ticker})
    return result


def _is_feed_response(content_type: str, body: str) -> bool:
    content_type = content_type.lower()
    if "rss" in content_type or "atom" in content_type or "xml" in content_type:
        return True
    head = body[:500].lower()
    return "<rss" in head or "<feed" in head or "<rdf" in head


def _parse_feed(body: str, source: Dict[str, str]) -> List[Dict[str, Any]]:
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser 미설치 -> pip install feedparser")
        return []

    feed = feedparser.parse(body)
    if feed.bozo and not feed.entries:
        logger.info("공식 피드 [%s] 파싱 실패 (bozo=%s)", source.get("name"), feed.bozo_exception)
        return []

    result: List[Dict[str, Any]] = []
    name = source.get("name", "Official")
    ticker = source.get("ticker", "")
    for entry in feed.entries[:10]:
        raw_id = entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title", "")
        if not raw_id:
            continue
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        publish_time = int(calendar.timegm(published)) if published else 0
        title = _strip_html(entry.get("title", "")) or "공식 업데이트"
        summary = _strip_html(entry.get("summary", "") or entry.get("description", ""))
        link = entry.get("link", "")
        stable_id = hashlib.md5(f"{name}|{raw_id}|{link}|{title}".encode("utf-8")).hexdigest()[:20]
        result.append({
            "id": f"official_{stable_id}",
            "ticker": ticker,
            "name": name,
            "title": title[:300],
            "summary": summary[:1000],
            "link": link,
            "publish_time": publish_time,
            "source": "Official",
            "source_type": "rss",
        })
    return result


def _extract_links(body: str, source_url: str, limit: int = 12) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    seen: set = set()
    for match in re.finditer(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", body, flags=re.IGNORECASE | re.DOTALL):
        href = html.unescape(match.group(1)).strip()
        if not href or _SKIP_LINKS.search(href):
            continue
        absolute_url = urljoin(source_url, href)
        parsed = urlparse(absolute_url)
        if parsed.scheme not in ("http", "https"):
            continue

        label = _strip_html(match.group(2))
        path = parsed.path.lower()
        combined = f"{label} {path}"
        if not label or len(label) < 8:
            continue
        if not _LINK_HINTS.search(combined):
            continue
        if absolute_url in seen:
            continue

        seen.add(absolute_url)
        links.append({"title": label[:300], "link": absolute_url})
        if len(links) >= limit:
            break
    return links


def _parse_html(body: str, source: Dict[str, str]) -> List[Dict[str, Any]]:
    name = source.get("name", "Official")
    ticker = source.get("ticker", "")
    source_url = source.get("url", "")
    result: List[Dict[str, Any]] = []
    for link_info in _extract_links(body, source_url):
        title = link_info["title"]
        link = link_info["link"]
        stable_id = hashlib.md5(f"{name}|{link}|{title}".encode("utf-8")).hexdigest()[:20]
        result.append({
            "id": f"official_{stable_id}",
            "ticker": ticker,
            "name": name,
            "title": title,
            "summary": "",
            "link": link,
            "publish_time": 0,
            "source": "Official",
            "source_type": "html",
        })
    return result


def fetch_official_source(source: Dict[str, str], timeout: int = 10) -> List[Dict[str, Any]]:
    url = source.get("url", "")
    name = source.get("name", "Official")
    if not url:
        return []

    try:
        response = requests.get(url, timeout=timeout, headers=_BROWSER_HEADERS)
        if response.status_code != 200:
            logger.info("공식 페이지 [%s] HTTP %d", name, response.status_code)
            return []
        content_type = response.headers.get("content-type", "")
        if _is_feed_response(content_type, response.text):
            items = _parse_feed(response.text, source)
        else:
            items = _parse_html(response.text, source)
        logger.info("공식 페이지 [%s] 수집: %d건", name, len(items))
        return items
    except Exception as e:
        logger.info("공식 페이지 [%s] 실패: %s", name, e)
        return []


def fetch_all_official_posts(config: dict) -> List[Dict[str, Any]]:
    if not config.get("monitor_official", False):
        return []

    result: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for source in _configured_sources(config):
        for item in fetch_official_source(source):
            item_id = item.get("id", "")
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            result.append(item)
    return result
