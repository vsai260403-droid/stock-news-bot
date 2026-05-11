"""
news_fetcher.py - 여러 소스에서 뉴스 수집

지원 소스:
  yahoo      - Yahoo Finance (yfinance, API 키 불필요)
  google_rss - Google News RSS (feedparser, API 키 불필요)
  finnhub    - Finnhub Company News (무료 API 키: https://finnhub.io/register)
"""
import logging
import time
from datetime import date, timedelta
from typing import Any, Dict, List

import requests
import yfinance as yf

logger = logging.getLogger(__name__)


# ── Yahoo Finance ──────────────────────────────────────────────────────────────
def fetch_yahoo_news(ticker: str) -> List[Dict[str, Any]]:
    """Yahoo Finance에서 해당 티커의 최신 뉴스를 가져옵니다."""
    try:
        stock = yf.Ticker(ticker)
        raw_news = stock.news
        if not raw_news:
            return []

        result = []
        for item in raw_news:
            # UUID를 그대로 ID로 사용 (seen_news.json 하위 호환 유지)
            news_id = item.get("uuid") or item.get("link", "")
            if not news_id:
                continue
            result.append({
                "id": news_id,
                "title": item.get("title", "(제목 없음)"),
                "link": item.get("link", ""),
                "publisher": item.get("publisher", "Yahoo Finance"),
                "publish_time": item.get("providerPublishTime", 0),
                "source": "Yahoo Finance",
                "ticker": ticker.upper(),
            })
        return result

    except Exception as e:
        logger.error("[%s] Yahoo Finance 뉴스 실패: %s", ticker, e)
        return []


# ── Google News RSS ────────────────────────────────────────────────────────────
def fetch_google_news_rss(ticker: str) -> List[Dict[str, Any]]:
    """Google News RSS 피드에서 해당 티커 관련 뉴스를 가져옵니다. (API 키 불필요)"""
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser 미설치 → Google News RSS 건너뜀. pip install feedparser")
        return []

    url = (
        f"https://news.google.com/rss/search"
        f"?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        feed = feedparser.parse(url)
        result = []
        for entry in feed.entries[:15]:
            raw_id = entry.get("id") or entry.get("link", "")
            # Google News RSS: entry.source.title 에 출판사 이름이 있음
            source_obj = getattr(entry, "source", None)
            publisher = getattr(source_obj, "title", "Google News")
            published = entry.get("published_parsed")
            ts = int(time.mktime(published)) if published else 0
            result.append({
                "id": f"gnews_{raw_id}",
                "title": entry.get("title", "(제목 없음)"),
                "link": entry.get("link", ""),
                "publisher": publisher,
                "publish_time": ts,
                "source": "Google News",
                "ticker": ticker.upper(),
            })
        return result

    except Exception as e:
        logger.error("[%s] Google News RSS 실패: %s", ticker, e)
        return []


# ── Finnhub ────────────────────────────────────────────────────────────────────
def fetch_finnhub_news(ticker: str, api_key: str) -> List[Dict[str, Any]]:
    """
    Finnhub Company News API에서 최근 7일 뉴스를 가져옵니다.
    무료 API 키 발급: https://finnhub.io/register
    """
    today = date.today().isoformat()
    from_date = (date.today() - timedelta(days=7)).isoformat()
    url = (
        f"https://finnhub.io/api/v1/company-news"
        f"?symbol={ticker}&from={from_date}&to={today}&token={api_key}"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []

        result = []
        for item in data[:15]:
            news_id = str(item.get("id", ""))
            result.append({
                "id": f"finnhub_{news_id}",
                "title": item.get("headline", "(제목 없음)"),
                "link": item.get("url", ""),
                "publisher": item.get("source", "Finnhub"),
                "publish_time": item.get("datetime", 0),
                "source": "Finnhub",
                "ticker": ticker.upper(),
            })
        return result

    except Exception as e:
        logger.error("[%s] Finnhub 뉴스 실패: %s", ticker, e)
        return []


# ── 통합 수집기 ────────────────────────────────────────────────────────────────
def fetch_all_news(ticker: str, config: dict) -> List[Dict[str, Any]]:
    """
    설정된 모든 소스에서 뉴스를 수집하고 ID 기준 중복을 제거합니다.

    config 키:
      news_sources    - 활성화할 소스 목록 (기본: ["yahoo", "google_rss"])
      finnhub_api_key - Finnhub API 키 (옵션)
    """
    sources = config.get("news_sources", ["yahoo", "google_rss"])
    finnhub_key = config.get("finnhub_api_key", "").strip()

    seen_ids: set = set()
    all_items: List[Dict[str, Any]] = []

    def _add(items: List[Dict[str, Any]]) -> None:
        for item in items:
            if item["id"] and item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                all_items.append(item)

    if "yahoo" in sources:
        _add(fetch_yahoo_news(ticker))

    if "google_rss" in sources:
        _add(fetch_google_news_rss(ticker))

    if "finnhub" in sources and finnhub_key:
        _add(fetch_finnhub_news(ticker, finnhub_key))

    return all_items
