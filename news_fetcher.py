"""
news_fetcher.py - 여러 소스에서 뉴스 수집

지원 소스:
  yahoo      - Yahoo Finance RSS (feedparser, API 키 불필요)
  google_rss - Google News RSS (feedparser, API 키 불필요)
  finnhub    - Finnhub Company News (무료 API 키: https://finnhub.io/register)
"""
import calendar
import logging
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


# ── Yahoo Finance RSS ──────────────────────────────────────────────────────────
def fetch_yahoo_news(ticker: str) -> List[Dict[str, Any]]:
    """Yahoo Finance RSS 피드에서 해당 티커의 최신 뉴스를 가져옵니다.
    
    yfinance의 stock.news API 대신 공식 RSS 피드를 사용합니다.
    (yfinance news API는 Yahoo 측 변경으로 인해 불안정)
    """
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser 미설치 → pip install feedparser")
        return []

    url = (
        f"https://feeds.finance.yahoo.com/rss/2.0/headline"
        f"?s={ticker}&region=US&lang=en-US"
    )
    try:
        feed = feedparser.parse(
            url,
            request_headers={"User-Agent": "Mozilla/5.0 (compatible; StockAlarmBot/1.0)"},
        )
        if feed.bozo and not feed.entries:
            logger.warning("[%s] Yahoo Finance RSS 파싱 실패 (bozo=True)", ticker)
            return []

        result = []
        for entry in feed.entries[:15]:
            raw_id = entry.get("id") or entry.get("link", "")
            if not raw_id:
                continue
            published = entry.get("published_parsed")
            ts = int(calendar.timegm(published)) if published else 0
            source_obj = getattr(entry, "source", None)
            publisher = getattr(source_obj, "title", "Yahoo Finance") if source_obj else "Yahoo Finance"
            result.append({
                "id": f"yahoo_rss_{raw_id}",
                "title": entry.get("title", "(제목 없음)"),
                "link": entry.get("link", ""),
                "publisher": publisher,
                "publish_time": ts,
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
            ts = int(calendar.timegm(published)) if published else 0
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

    # ── 시간 필터: 설정된 시간(기본 24시간)보다 오래된 뉴스 제외 ──────────────
    max_age_hours = config.get("news_max_age_hours", 24)
    if max_age_hours > 0:
        cutoff_ts = int(time.time()) - (max_age_hours * 3600)
        filtered = [item for item in all_items if item.get("publish_time", 0) >= cutoff_ts]
        skipped = len(all_items) - len(filtered)
        if skipped > 0:
            logger.debug("[%s] 시간 필터: %d건 제외 (%d시간 이상 경과)", ticker, skipped, max_age_hours)
        return filtered

    return all_items


# ── AI 한글 요약 ───────────────────────────────────────────────────────────────
def ai_summarize_news(title: str, publisher: str, gemini_api_key: str) -> Optional[str]:
    """Google Gemini API로 영문 뉴스 제목을 한국어로 번역·요약합니다.

    실패 시 None 반환 (알람은 정상 전송).
    OpenAI 호환 API 사용 (grpcio 의존성 없음, 라즈베리파이 호환).
    """
    if not gemini_api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning(
            "openai 패키지 미설치 → pip install openai  (AI 요약 비활성화)"
        )
        return None
    try:
        client = OpenAI(
            api_key=gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        prompt = (
            "당신은 주식 투자자를 위한 뉴스 번역·요약 도우미입니다. "
            "영어 뉴스 제목과 출처를 받으면, "
            "한국어로 자연스럽게 번역하고 투자자에게 중요한 핵심 내용을 "
            "1~2문장으로 간결하게 설명해 주세요.\n\n"
            f"출처: {publisher}\n제목: {title}"
        )
        response = client.chat.completions.create(
            model="gemini-2.0-flash",
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("AI 요약 실패: %s", e)
        return None
