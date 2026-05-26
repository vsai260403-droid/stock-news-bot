"""
twitter_fetcher.py - 트위터/X 타임라인 수집

1순위: RSSHub 공개 인스턴스 (Nitter보다 VPS IP 차단률 낙음)
2순위: Nitter 인스턴스 (fallback)

Twitter API 키 불필요.
인스턴스 목록 1시간마다 자동 상태 체크 후 살아있는 것만 사용.
"""
import logging
import re
import time
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── RSSHub 인스턴스 ────────────────────────────────────────────────
# RSS URL 패턴: {base}/twitter/user/{username}
_RSSHUB_INSTANCES: List[str] = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://hub.slarker.me",
    "https://rsshub.privacyredirect.com",
]

# ── Nitter 인스턴스 (fallback) ─────────────────────────────────────────
# RSS URL 패턴: {base}/{username}/rss
_NITTER_INSTANCES: List[str] = [
    "https://xcancel.com",
    "https://nitter.space",
    "https://nuku.trabun.org",
    "https://lightbrd.com",
    "https://nitter.net",
    "https://nitter.privacyredirect.com",
    "https://nitter.kareem.one",
    "https://nitter.poast.org",
    "https://nitter.catsarch.com",
    "https://nitter.tiekoetter.com",
    "https://nitter.privacydev.net",
    "https://nitter.unixfox.eu",
    "https://nitter.1d4.us",
]

_ALL_NITTER_INSTANCES = _NITTER_INSTANCES  # 하위 호환

# 브라우저 헤더 (보트 차단 회피)
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

# 런타임 상태 체크 캐시
_healthy_instances: List[str] = []
_last_health_check: float = 0.0
_HEALTH_CHECK_INTERVAL: float = 3600.0
_health_lock = threading.Lock()

# 브라우저 헤더 (봇 차단 회피) — 모든 함수에서 사용하므로 여기 정의
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


def _check_instance_health(instance: str, timeout: int = 6) -> bool:
    """인스턴스가 RSS 응답을 주는지 확인합니다 (elonmusk 계정으로 테스트)."""
    try:
        import requests as _req
        url = f"{instance.rstrip('/')}/elonmusk/rss"
        r = _req.get(url, timeout=timeout, headers=_BROWSER_HEADERS)
        return r.status_code == 200 and "rss" in r.headers.get("content-type", "").lower()
    except Exception:
        return False


def _check_rsshub_health(instance: str, timeout: int = 6) -> bool:
    """RSSHub 인스턴스가 응답을 주는지 확인합니다."""
    try:
        import requests as _req
        url = f"{instance.rstrip('/')}/twitter/user/elonmusk"
        r = _req.get(url, timeout=timeout, headers=_BROWSER_HEADERS)
        return r.status_code == 200
    except Exception:
        return False


def _refresh_healthy_instances() -> List[str]:
    """전체 후보 목록을 병렬 체크해 살아있는 인스턴스 목록을 갱신합니다."""
    import concurrent.futures

    all_candidates = [
        ("rsshub", inst) for inst in _RSSHUB_INSTANCES
    ] + [
        ("nitter", inst) for inst in _NITTER_INSTANCES
    ]
    logger.info("[Twitter] 인스턴스 상태 체크 중 (RSSHub %d개 + Nitter %d개)...",
                len(_RSSHUB_INSTANCES), len(_NITTER_INSTANCES))

    alive_rsshub, alive_nitter = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(
                _check_rsshub_health if kind == "rsshub" else _check_instance_health,
                inst
            ): (kind, inst)
            for kind, inst in all_candidates
        }
        for future in concurrent.futures.as_completed(futures):
            kind, inst = futures[future]
            try:
                if future.result():
                    if kind == "rsshub":
                        alive_rsshub.append(inst)
                    else:
                        alive_nitter.append(inst)
            except Exception:
                pass

    # 원래 순서 유지, RSSHub 먼저
    alive_rsshub = [i for i in _RSSHUB_INSTANCES if i in alive_rsshub]
    alive_nitter = [i for i in _NITTER_INSTANCES if i in alive_nitter]
    result = alive_rsshub + alive_nitter
    logger.info("[Twitter] 사용 가능: RSSHub %d개 %s | Nitter %d개 %s",
                len(alive_rsshub), alive_rsshub or "(없음)",
                len(alive_nitter), alive_nitter or "(없음)")
    return result


def get_healthy_instances() -> List[str]:
    """살아있는 인스턴스 목록을 반환합니다. 1시간마다 자동 갱신."""
    global _healthy_instances, _last_health_check
    now = time.time()
    with _health_lock:
        if now - _last_health_check >= _HEALTH_CHECK_INTERVAL or not _healthy_instances:
            _healthy_instances = _refresh_healthy_instances()
            _last_health_check = now
            if not _healthy_instances:
                # 모두 실패 시 전체 목록 폴백
                logger.warning("[Twitter] 모든 인스턴스 다운 — 전체 목록으로 폴백")
                _healthy_instances = list(_RSSHUB_INSTANCES) + list(_NITTER_INSTANCES)
    return _healthy_instances


KNOWN_ACCOUNTS: Dict[str, List[str]] = {
    "TSLA": ["Tesla", "elonmusk"],
    "AAPL": ["Apple", "tim_cook"],
    "NVDA": ["nvidia", "JensenHuang"],
    "MSFT": ["Microsoft", "satyanadella"],
    "GOOGL": ["Google", "sundarpichai"],
    "GOOG":  ["Google", "sundarpichai"],
    "AMZN":  ["Amazon", "JeffBezos"],
    "META":  ["Meta", "zuck"],
    "NFLX":  ["netflix"],
    "AMD":   ["AMD"],
    "INTC":  ["intel"],
    "BABA":  ["alibaba"],
    "TSM":   ["tsmc"],
    "JPM":   ["jpmorgan"],
    "BAC":   ["BankofAmerica"],
    "COIN":  ["coinbase"],
    "HOOD":  ["RobinhoodApp"],
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


def _try_fetch_rss_nitter(
    instance: str, username: str, timeout: int = 8
) -> List[Dict[str, Any]]:
    """Nitter 인스턴스에서 RSS를 가져옵니다."""
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser 미설치 → pip install feedparser")
        return []

    url = f"{instance.rstrip('/')}/{username}/rss"
    try:
        feed = feedparser.parse(url, request_headers=_BROWSER_HEADERS)
        if feed.bozo and not feed.entries:
            return []
        return _parse_feed_entries(feed, username)
    except Exception as e:
        logger.info("Nitter [%s] @%s 실패: %s", instance, username, e)
        return []


def _try_fetch_rss_rsshub(
    instance: str, username: str, timeout: int = 8
) -> List[Dict[str, Any]]:
    """RSSHub 인스턴스에서 RSS를 가져옵니다."""
    try:
        import feedparser
    except ImportError:
        return []

    url = f"{instance.rstrip('/')}/twitter/user/{username}"
    try:
        feed = feedparser.parse(url, request_headers=_BROWSER_HEADERS)
        if feed.bozo and not feed.entries:
            return []
        return _parse_feed_entries(feed, username)
    except Exception as e:
        logger.info("RSSHub [%s] @%s 실패: %s", instance, username, e)
        return []


def _try_fetch_rss(instance: str, username: str, timeout: int = 8) -> List[Dict[str, Any]]:
    """Nitter URL 패턴으로 RSS를 가져옵니다 (probe_instance 호환용)."""
    return _try_fetch_rss_nitter(instance, username, timeout)


def _parse_feed_entries(feed: Any, username: str) -> List[Dict[str, Any]]:
    """feedparser 결과를 공통 포맷으로 변환합니다."""
    result: List[Dict[str, Any]] = []
    for entry in feed.entries[:10]:
        raw_id = entry.get("id") or entry.get("link", "")
        if not raw_id:
            continue
        published = entry.get("published_parsed")
        ts = int(time.mktime(published)) if published else 0
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


def probe_instance(instance: str, username: str, timeout: int = 8) -> Dict[str, Any]:
    """HTTP 수준에서 인스턴스 상태를 상세 진단합니다 (!twitter-test용)."""
    import requests as _req
    import feedparser as _fp

    # RSSHub vs Nitter URL 패턴 자동 판단 (URL에 rsshub 포함 여부로 판단)
    is_rsshub = "rsshub" in instance.lower() or "hub.slarker" in instance.lower()
    if is_rsshub:
        url = f"{instance.rstrip('/')}/twitter/user/{username}"
    else:
        url = f"{instance.rstrip('/')}/{username}/rss"

    result: Dict[str, Any] = {"url": url, "type": "RSSHub" if is_rsshub else "Nitter",
                               "http_status": None, "content_type": None,
                               "entries": 0, "bozo": None, "error": None}
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
    """
    RSSHub → Nitter 순으로 시도하여 트위터 타임라인을 가져옵니다.
    모든 인스턴스 실패 시 빈 리스트를 반환합니다.
    """
    instances = nitter_instances or get_healthy_instances()

    for instance in instances:
        if instance in _RSSHUB_INSTANCES:
            result = _try_fetch_rss_rsshub(instance, username)
        else:
            result = _try_fetch_rss_nitter(instance, username)
        if result:
            logger.debug("[@%s] %d개 트윗 수집 (%s)", username, len(result), instance)
            return result
        time.sleep(0.3)

    logger.warning("[@%s] 모든 인스턴스에서 수집 실패", username)
    global _last_health_check
    _last_health_check = 0.0  # 다음 체크에서 목록 강제 갱신
    return []


def fetch_all_tweets(ticker: str, config: dict) -> List[Dict[str, Any]]:
    """
    config에 설정된 트위터 계정들에서 해당 티커의 트윗을 모두 가져옵니다.
    monitor_twitter: false 이면 즉시 빈 리스트 반환.
    """
    if not config.get("monitor_twitter", False):
        return []

    twitter_accounts: Dict[str, List[str]] = config.get("twitter_accounts", {})
    usernames: List[str] = twitter_accounts.get(ticker.upper(), [])
    if not usernames:
        return []

    nitter_instances: List[str] = config.get(
        "nitter_instances"
    ) or get_healthy_instances()
    result: List[Dict[str, Any]] = []

    for username in usernames:
        tweets = fetch_twitter_timeline(username, nitter_instances)
        for tweet in tweets:
            tweet["ticker"] = ticker.upper()
        result.extend(tweets)
        if len(usernames) > 1:
            time.sleep(0.5)  # 인스턴스 부하 분산

    # 시간 필터: tweet_max_age_hours 이상 지난 트윗 제외 (기본 24시간)
    max_age_hours = config.get("tweet_max_age_hours", 24)
    if max_age_hours > 0:
        cutoff_ts = int(time.time()) - (max_age_hours * 3600)
        before = len(result)
        result = [t for t in result if t.get("publish_time", 0) >= cutoff_ts]
        skipped = before - len(result)
        if skipped > 0:
            logger.debug("[%s] 트윗 시간 필터: %d건 제외 (%d시간 이상 경과)", ticker, skipped, max_age_hours)

    return result
