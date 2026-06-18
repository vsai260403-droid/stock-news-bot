"""
linkedin_fetcher.py - LinkedIn 페이지 RSS 변환 피드 수집

LinkedIn은 자체 공개 RSS가 거의 없으므로 RSS.app 같은 외부 변환 서비스에서
생성한 RSS/Atom URL을 config.json에 등록해서 사용합니다.
"""
import calendar
import hashlib
import html
import logging
import re
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; StockAlarmBot/1.0)",
    "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml,*/*;q=0.8",
}


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(str(text))
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _feed_entries(feed: Any, source_name: str, account: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for entry in feed.entries[:10]:
        raw_id = entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title", "")
        if not raw_id:
            continue

        published = entry.get("published_parsed") or entry.get("updated_parsed")
        publish_time = int(calendar.timegm(published)) if published else 0
        title = _strip_html(entry.get("title", "")) or "LinkedIn 업데이트"
        summary = _strip_html(entry.get("summary", "") or entry.get("description", ""))
        link = entry.get("link", "")
        stable_id = hashlib.md5(f"{account}|{raw_id}|{link}|{title}".encode("utf-8")).hexdigest()[:20]

        result.append({
            "id": f"linkedin_{stable_id}",
            "account": account,
            "title": title[:300],
            "text": (summary or title)[:1000],
            "link": link,
            "publish_time": publish_time,
            "source": source_name or "LinkedIn",
        })
    return result


def _configured_feeds(config: dict) -> List[Dict[str, str]]:
    feeds = config.get("linkedin_feeds", [])
    if not isinstance(feeds, list):
        return []

    result: List[Dict[str, str]] = []
    for idx, feed in enumerate(feeds, 1):
        if isinstance(feed, str):
            url = feed.strip()
            name = f"LinkedIn {idx}"
            account = name
        elif isinstance(feed, dict):
            url = str(feed.get("url") or "").strip()
            name = str(feed.get("name") or feed.get("account") or f"LinkedIn {idx}").strip()
            account = str(feed.get("account") or name).strip()
        else:
            continue

        if not url:
            continue
        result.append({"url": url, "name": name, "account": account})
    return result


def fetch_linkedin_feed(feed_config: Dict[str, str], timeout: int = 10) -> List[Dict[str, Any]]:
    """단일 LinkedIn RSS 변환 피드를 가져옵니다."""
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser 미설치 → pip install feedparser")
        return []

    url = feed_config.get("url", "")
    name = feed_config.get("name", "LinkedIn")
    account = feed_config.get("account", name)
    if not url:
        return []

    try:
        response = requests.get(url, timeout=timeout, headers=_BROWSER_HEADERS)
        if response.status_code != 200:
            logger.info("LinkedIn RSS [%s] HTTP %d", name, response.status_code)
            return []
        feed = feedparser.parse(response.text)
        if feed.bozo and not feed.entries:
            logger.info("LinkedIn RSS [%s] 파싱 실패 (bozo=%s)", name, feed.bozo_exception)
            return []
        items = _feed_entries(feed, name, account)
        logger.info("LinkedIn RSS [%s] 수집: %d건", name, len(items))
        return items
    except Exception as e:
        logger.info("LinkedIn RSS [%s] 실패: %s", name, e)
        return []


def fetch_all_linkedin_posts(config: dict) -> List[Dict[str, Any]]:
    """config에 등록된 LinkedIn RSS 변환 피드들을 모두 수집합니다."""
    if not config.get("monitor_linkedin", False):
        return []

    result: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for feed_config in _configured_feeds(config):
        for item in fetch_linkedin_feed(feed_config):
            item_id = item.get("id", "")
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            result.append(item)
    return result
