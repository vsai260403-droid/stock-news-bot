"""
twitter_fetcher.py - Nitter RSS를 이용한 트위터/X 타임라인 수집

Twitter API 키 불필요. 공개 Nitter 인스턴스의 RSS 피드를 사용합니다.
Nitter는 Twitter의 오픈소스 대체 프론트엔드입니다.

참고: 인스턴스 가용성은 변동될 수 있습니다.
  https://github.com/zedeus/nitter/wiki/Instances
"""
import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 기본 Nitter 인스턴스 목록 (순서대로 시도, 실패 시 다음으로)
DEFAULT_NITTER_INSTANCES: List[str] = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.catsarch.com",
    "https://nitter.unixfox.eu",
    "https://nitter.1d4.us",
]

# 잘 알려진 티커별 트위터 계정 기본값
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


def _try_fetch_rss(
    instance: str, username: str, timeout: int = 8
) -> List[Dict[str, Any]]:
    """특정 Nitter 인스턴스에서 RSS를 가져옵니다."""
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser 미설치 → pip install feedparser")
        return []

    url = f"{instance.rstrip('/')}/{username}/rss"
    try:
        feed = feedparser.parse(
            url,
            request_headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; StockAlarmBot/1.0)"
                )
            },
        )
        # bozo=True + entries 없으면 파싱 실패
        if feed.bozo and not feed.entries:
            return []

        result: List[Dict[str, Any]] = []
        for entry in feed.entries[:10]:
            raw_id = entry.get("id") or entry.get("link", "")
            if not raw_id:
                continue

            published = entry.get("published_parsed")
            ts = int(time.mktime(published)) if published else 0

            title = _strip_html(entry.get("title", ""))
            summary = _strip_html(entry.get("summary", ""))
            # title이 summary보다 보통 짧음; 내용은 summary 사용
            text = summary if summary else title

            result.append(
                {
                    "id": f"tweet_{username}_{raw_id}",
                    "username": username,
                    "title": title[:300],
                    "text": text[:500],
                    "link": entry.get("link", ""),
                    "publish_time": ts,
                    "source": "Twitter/X",
                }
            )
        return result

    except Exception as e:
        logger.debug("Nitter [%s] @%s 실패: %s", instance, username, e)
        return []


def fetch_twitter_timeline(
    username: str,
    nitter_instances: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    여러 Nitter 인스턴스를 순차적으로 시도하여 트위터 타임라인을 가져옵니다.
    모든 인스턴스 실패 시 빈 리스트를 반환합니다.
    """
    instances = nitter_instances or DEFAULT_NITTER_INSTANCES
    for instance in instances:
        result = _try_fetch_rss(instance, username)
        if result:
            logger.debug(
                "[@%s] %d개 트윗 수집 (%s)", username, len(result), instance
            )
            return result
        time.sleep(0.3)  # 다음 인스턴스 시도 전 잠시 대기

    logger.warning("[@%s] 모든 Nitter 인스턴스에서 수집 실패", username)
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
        "nitter_instances", DEFAULT_NITTER_INSTANCES
    )
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
