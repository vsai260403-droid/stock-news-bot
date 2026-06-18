"""Helpers for finding official IR/Newsroom sources for stock tickers."""
import json
import logging
import re
from typing import Dict, Optional
from urllib.parse import urlparse

from ai_provider import ai_generate_with_fallback
from app_state import DEFAULT_GEMINI_MODEL


logger = logging.getLogger(__name__)

KNOWN_OFFICIAL_SOURCES: Dict[str, Dict[str, str]] = {
    "AAPL": {"ticker": "AAPL", "name": "Apple", "url": "https://www.apple.com/newsroom/"},
    "TSLA": {"ticker": "TSLA", "name": "Tesla", "url": "https://ir.tesla.com/press"},
    "NVDA": {"ticker": "NVDA", "name": "NVIDIA", "url": "https://nvidianews.nvidia.com/news"},
    "MSFT": {"ticker": "MSFT", "name": "Microsoft", "url": "https://news.microsoft.com/"},
    "GOOG": {"ticker": "GOOG", "name": "Alphabet", "url": "https://abc.xyz/investor/news/"},
    "GOOGL": {"ticker": "GOOGL", "name": "Alphabet", "url": "https://abc.xyz/investor/news/"},
    "AMZN": {"ticker": "AMZN", "name": "Amazon", "url": "https://ir.aboutamazon.com/news-release/news-release-details/default.aspx"},
    "META": {"ticker": "META", "name": "Meta", "url": "https://investor.fb.com/investor-news/default.aspx"},
}

_URL_HINTS = re.compile(
    r"(investor|investors|ir|newsroom|news|press|release|releases|media|company|blog|rss|feed|feeds|atom)",
    re.IGNORECASE,
)
_BLOCKED_HOSTS = re.compile(
    r"(google\.|bing\.|yahoo\.|twitter\.|x\.com|facebook\.|linkedin\.|wikipedia\.|sec\.gov|nasdaq\.|nyse\.|marketwatch\.|reuters\.|bloomberg\.)",
    re.IGNORECASE,
)


def _normal_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _is_probable_official_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if parsed.scheme not in ("http", "https") or not host:
        return False
    if _BLOCKED_HOSTS.search(host):
        return False
    return bool(_URL_HINTS.search(f"{host} {parsed.path}"))


def _extract_json_object(text: str) -> Optional[dict]:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def official_source_exists(feeds: list, ticker: str, url: str = "") -> bool:
    ticker = ticker.upper().strip()
    target_url = _normal_url(url)
    for feed in feeds:
        if isinstance(feed, dict):
            existing_ticker = str(feed.get("ticker") or "").upper().strip()
            existing_url = _normal_url(str(feed.get("url") or ""))
        else:
            existing_ticker = ""
            existing_url = _normal_url(str(feed))
        if ticker and existing_ticker == ticker:
            return True
        if target_url and existing_url == target_url:
            return True
    return False


def append_official_source(config: dict, source: Dict[str, str]) -> bool:
    ticker = str(source.get("ticker") or "").upper().strip()
    name = str(source.get("name") or ticker or "Official").strip()
    url = str(source.get("url") or "").strip()
    if not ticker or not url or not _is_probable_official_url(url):
        return False

    feeds: list = config.setdefault("official_feeds", [])
    if official_source_exists(feeds, ticker, url):
        return False

    feeds.append({"ticker": ticker, "name": name, "url": url})
    config["monitor_official"] = True
    return True


def remove_official_sources_for_ticker(config: dict, ticker: str) -> int:
    ticker = ticker.upper().strip()
    feeds = config.get("official_feeds", [])
    if not isinstance(feeds, list) or not ticker:
        return 0

    kept = []
    removed_count = 0
    for feed in feeds:
        feed_ticker = str(feed.get("ticker") or "").upper().strip() if isinstance(feed, dict) else ""
        if feed_ticker == ticker:
            removed_count += 1
        else:
            kept.append(feed)
    if removed_count:
        config["official_feeds"] = kept
    return removed_count


def ai_find_official_source(
    ticker: str,
    gemini_api_key: str,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
    config: Optional[dict] = None,
) -> Optional[Dict[str, str]]:
    """GPT OAuth 우선, Gemini fallback으로 공식 IR/Newsroom URL을 찾습니다."""
    ticker = ticker.upper().strip()
    if ticker in KNOWN_OFFICIAL_SOURCES:
        return dict(KNOWN_OFFICIAL_SOURCES[ticker])

    effective_config = dict(config or {})
    if gemini_api_key and not effective_config.get("gemini_api_key"):
        effective_config["gemini_api_key"] = gemini_api_key
    effective_config["gemini_request_model"] = gemini_model

    prompt = (
        f"미국 상장 주식 티커 '{ticker}' 회사의 공식 IR 또는 Newsroom 소스를 찾아주세요.\n"
        "가장 좋은 것은 회사가 직접 운영하는 RSS/Atom 피드 URL이고, 없으면 회사 공식 investor relations, newsroom, press releases 페이지 URL입니다.\n"
        "SEC, Yahoo Finance, Nasdaq, NYSE, MarketWatch, Reuters, Bloomberg, Wikipedia, LinkedIn, X/Twitter 같은 제3자/소셜 사이트는 제외하세요.\n"
        "반드시 설명 없이 아래 JSON 객체 하나로만 답하세요. 모르면 url을 NONE으로 주세요.\n"
        '{"ticker":"TICKER","name":"Company Name","url":"https://official-company-page.example/news"}'
    )
    text = ai_generate_with_fallback(
        prompt,
        effective_config,
        purpose=f"{ticker} 공식 IR/Newsroom 탐색",
    )
    logger.info("[AI] %s 공식 페이지 응답: %s", ticker, text)
    if not text:
        return None

    parsed = _extract_json_object(text)
    if not parsed:
        return None

    url = str(parsed.get("url") or "").strip()
    if not url or url.upper() == "NONE" or not _is_probable_official_url(url):
        return None

    name = str(parsed.get("name") or ticker).strip()
    return {"ticker": ticker, "name": name, "url": url}


gemini_find_official_source = ai_find_official_source
_gemini_find_official_source = ai_find_official_source