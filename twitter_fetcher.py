"""
twitter_fetcher.py - 트위터/X 타임라인 수집

1순위: Nitter 인스턴스
2순위: RSSHub 공개 인스턴스

Twitter API 키 불필요.
"""
import calendar
import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_RSSHUB_INSTANCES: List[str] = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://hub.slarker.me",
    "https://rsshub.privacyredirect.com",
]

_NITTER_INSTANCES: List[str] = [
    "https://nitter.net",
    "https://xcancel.com",
    "https://nitter.space",
    "https://nuku.trabun.org",
    "https://lightbrd.com",
    "https://nitter.privacyredirect.com",
    "https://nitter.kareem.one",
    "https://nitter.poast.org",
    "https://nitter.catsarch.com",
    "https://nitter.tiekoetter.com",
    "https://nitter.privacydev.net",
    "https://nitter.unixfox.eu",
    "https://nitter.1d4.us",
]

_ALL_NITTER_INSTANCES = _NITTER_INSTANCES

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


def get_healthy_instances() -> List[str]:
    """사용할 인스턴스 목록을 반환합니다. Nitter 우선, 그 다음 RSSHub."""
    return list(_NITTER_INSTANCES) + list(_RSSHUB_INSTANCES)


KNOWN_ACCOUNTS: Dict[str, List[str]] = {
    "TSLA": ["Tesla", "elonmusk"],
    "AAPL": ["Apple", "tim_cook"],
    "NVDA": ["nvidia", "JensenHuang"],
    "MSFT": ["Microsoft", "satyanadella"],
    "GOOGL": ["Google", "sundarpichai"],
    "GOOG": ["Google", "sundarpichai"],
    "AMZN": ["Amazon", "JeffBezos"],
    "META": ["Meta", "zuck"],
    "NFLX": ["netflix"],
    "AMD": ["AMD"],
    "INTC": ["intel"],
    "BABA": ["alibaba"],
    "TSM": ["tsmc"],
    "JPM": ["jpmorgan"],
    "BAC": ["BankofAmerica"],
    "COIN": ["coinbase"],
    "HOOD": ["RobinhoodApp"],
}


def _strip_html(text: str) -> str:
    """HTML 태그 및 엔티티를 제거합니다."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_rsshub_instance(instance: str) -> bool:
    """인스턴스가 RSSHub인지 URL 패턴으로 판단합니다."""
    return "rsshub" in instance.lower() or "hub.slarker" in instance.lower()


def _parse_feed_entries(feed: Any, username: str) -> List[Dict[str, Any]]:
    """feedparser 결과를 공통 포맷으로 변환합니다."""
    result: List[Dict[str, Any]] = []
    for entry in feed.entries[:10]:
        raw_id = entry.get("id") or entry.get("link", "")
        if not raw_id:
            continue

        published = entry.get("published_parsed")
        # feedparser의 published_parsed는 UTC struct_time으로 취급해야 합니다.
        # time.mktime()은 서버 로컬 타임존으로 해석하므로 시간이 밀릴 수 있습니다.
        ts = int(calendar.timegm(published)) if published else 0

        title = _strip_html(entry.get("title", ""))
        summary = _strip_html(entry.get("summary", ""))
        text = summary if summary else title
        result.append({
            "id": f"tweet_{username}_{raw_id}",
            "username": username,
            "title": title[:300],
            "text": text[:500],
            "link": entry.get("link", ""),
            "publish_time": ts,
            "source": "Twitter/X",
        })
    return result


def _try_fetch_rss_nitter(instance: str, username: str, timeout: int = 8) -> List[Dict[str, Any]]:
    """Nitter 인스턴스에서 RSS를 가져옵니다."""
    try:
        import feedparser
        import requests as _req
    except ImportError:
        logger.warning("feedparser 또는 requests 미설치")
        return []

    url = f"{instance.rstrip('/')}/{username}/rss"
    try:
        r = _req.get(url, timeout=timeout, headers=_BROWSER_HEADERS)
        if r.status_code != 200:
            logger.info("Nitter [%s] @%s HTTP %d", instance, username, r.status_code)
            return []
        feed = feedparser.parse(r.text)
        if feed.bozo and not feed.entries:
            logger.info("Nitter [%s] @%s 파싱 실패 (bozo=%s)", instance, username, feed.bozo_exception)
            return []
        entries = _parse_feed_entries(feed, username)
        if not entries:
            logger.info("Nitter [%s] @%s 트윗 0개", instance, username)
        return entries
    except Exception as e:
        logger.info("Nitter [%s] @%s 실패: %s", instance, username, e)
        return []


def _try_fetch_rss_rsshub(instance: str, username: str, timeout: int = 8) -> List[Dict[str, Any]]:
    """RSSHub 인스턴스에서 RSS를 가져옵니다."""
    try:
        import feedparser
        import requests as _req
    except ImportError:
        logger.warning("feedparser 또는 requests 미설치")
        return []

    url = f"{instance.rstrip('/')}/twitter/user/{username}"
    try:
        r = _req.get(url, timeout=timeout, headers=_BROWSER_HEADERS)
        if r.status_code != 200:
            logger.info("RSSHub [%s] @%s HTTP %d", instance, username, r.status_code)
            return []
        feed = feedparser.parse(r.text)
        if feed.bozo and not feed.entries:
            logger.info("RSSHub [%s] @%s 파싱 실패 (bozo=%s)", instance, username, feed.bozo_exception)
            return []
        entries = _parse_feed_entries(feed, username)
        if not entries:
            logger.info("RSSHub [%s] @%s 트윗 0개", instance, username)
        return entries
    except Exception as e:
        logger.info("RSSHub [%s] @%s 실패: %s", instance, username, e)
        return []


def _try_fetch_rss(instance: str, username: str, timeout: int = 8) -> List[Dict[str, Any]]:
    """Nitter URL 패턴으로 RSS를 가져옵니다 (probe_instance 호환용)."""
    return _try_fetch_rss_nitter(instance, username, timeout)


def probe_instance(instance: str, username: str, timeout: int = 8) -> Dict[str, Any]:
    """HTTP 수준에서 인스턴스 상태를 상세 진단합니다 (!twitter-test용)."""
    import feedparser as _fp
    import requests as _req

    is_rsshub = _is_rsshub_instance(instance)
    if is_rsshub:
        url = f"{instance.rstrip('/')}/twitter/user/{username}"
    else:
        url = f"{instance.rstrip('/')}/{username}/rss"

    result: Dict[str, Any] = {
        "url": url,
        "type": "RSSHub" if is_rsshub else "Nitter",
        "http_status": None,
        "content_type": None,
        "entries": 0,
        "bozo": None,
        "error": None,
    }
    try:
        r = _req.get(url, timeout=timeout, headers=_BROWSER_HEADERS)
        result["http_status"] = r.status_code
        result["content_type"] = r.headers.get("content-type", "")[:60]
        if r.status_code != 200:
            return result
        feed = _fp.parse(r.text)
        result["bozo"] = feed.bozo
        result["entries"] = len(feed.entries)
    except Exception as e:
        result["error"] = str(e)[:120]
    return result


def fetch_twitter_timeline(
    username: str,
    nitter_instances: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """RSSHub → Nitter 순으로 시도하여 트위터 타임라인을 가져옵니다."""
    instances = nitter_instances or get_healthy_instances()

    for instance in instances:
        if _is_rsshub_instance(instance):
            result = _try_fetch_rss_rsshub(instance, username)
        else:
            result = _try_fetch_rss_nitter(instance, username)
        if result:
            logger.info("[@%s] %d개 트윗 수집 성공 (%s)", username, len(result), instance)
            return result
        time.sleep(0.3)

    logger.warning("[@%s] 모든 인스턴스에서 수집 실패", username)
    return []


def fetch_all_tweets(ticker: str, config: dict) -> List[Dict[str, Any]]:
    """config에 설정된 트위터 계정들에서 해당 티커의 트윗을 모두 가져옵니다."""
    if not config.get("monitor_twitter", False):
        return []

    twitter_accounts: Dict[str, List[str]] = config.get("twitter_accounts", {})
    usernames: List[str] = twitter_accounts.get(ticker.upper(), [])
    if not usernames:
        return []

    result: List[Dict[str, Any]] = []
    for username in usernames:
        tweets = fetch_twitter_timeline(username)
        for tweet in tweets:
            tweet["ticker"] = ticker.upper()
        result.extend(tweets)
        if len(usernames) > 1:
            time.sleep(0.5)

    # 시간 필터: 최근 트윗만 알림 대상으로 유지합니다.
    # 기본값을 6시간으로 줄여서 전날 트윗이 새 알림처럼 오는 상황을 줄입니다.
    max_age_hours = config.get("tweet_max_age_hours", 6)
    if max_age_hours > 0:
        cutoff_ts = int(time.time()) - (max_age_hours * 3600)
        before = len(result)
        result = [t for t in result if t.get("publish_time", 0) >= cutoff_ts]
        skipped = before - len(result)
        if skipped > 0:
            logger.info("[%s] 트윗 시간 필터: %d건 제외 (%d시간 이상 경과)", ticker, skipped, max_age_hours)

    return result
