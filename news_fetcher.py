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
def fetch_google_news_rss(ticker: str, company_name: str = "") -> List[Dict[str, Any]]:
    """Google News RSS 피드에서 해당 티커 관련 뉴스를 가져옵니다. (API 키 불필요)
    
    company_name이 제공되면 회사명을 포함한 더 정확한 검색을 수행합니다.
    """
    try:
        import feedparser
        from urllib.parse import quote
    except ImportError:
        logger.warning("feedparser 미설치 → Google News RSS 건너뜀. pip install feedparser")
        return []

    # 검색 쿼리: 회사명이 있으면 "회사명 ticker stock", 없으면 "ticker stock"
    if company_name and company_name.lower() != ticker.lower():
        # 회사명 첫 단어 + ticker 조합으로 검색 (너무 긴 회사명 방지)
        short_name = company_name.split()[0] if company_name else ticker
        query = quote(f'"{ticker}" stock')
    else:
        query = quote(f"{ticker} stock")

    url = (
        f"https://news.google.com/rss/search"
        f"?q={query}&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        feed = feedparser.parse(url)
        result = []
        ticker_upper = ticker.upper()
        for entry in feed.entries[:15]:
            raw_id = entry.get("id") or entry.get("link", "")
            # Google News RSS: entry.source.title 에 출판사 이름이 있음
            source_obj = getattr(entry, "source", None)
            publisher = getattr(source_obj, "title", "Google News")
            published = entry.get("published_parsed")
            ts = int(calendar.timegm(published)) if published else 0
            title = entry.get("title", "(제목 없음)")

            # 관련성 필터: 제목에 ticker가 포함되지 않고 회사명도 없으면 스킵
            title_lower = title.lower()
            ticker_in_title = ticker_upper.lower() in title_lower
            name_in_title = (
                company_name.split()[0].lower() in title_lower
                if company_name
                else False
            )
            if not ticker_in_title and not name_in_title:
                logger.debug("[%s] Google News 관련성 낮아 스킵: %s", ticker, title[:60])
                continue

            result.append({
                "id": f"gnews_{raw_id}",
                "title": title,
                "link": entry.get("link", ""),
                "publisher": publisher,
                "publish_time": ts,
                "source": "Google News",
                "ticker": ticker_upper,
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

    # Yahoo Finance에서 회사명을 가져와 Google News 검색 정확도 향상
    company_name = ""
    if "google_rss" in sources:
        try:
            from price_fetcher import fetch_price
            info = fetch_price(ticker)
            if info:
                company_name = info.get("name", "")
        except Exception:
            pass

    seen_ids: set = set()
    seen_title_hashes: set = set()
    all_items: List[Dict[str, Any]] = []

    def _title_hash(title: str) -> str:
        import hashlib
        return hashlib.md5(title.lower().strip().encode("utf-8")).hexdigest()[:16]

    def _add(items: List[Dict[str, Any]]) -> None:
        for item in items:
            if not item["id"]:
                continue
            th = _title_hash(item.get("title", ""))
            if item["id"] in seen_ids or th in seen_title_hashes:
                continue
            seen_ids.add(item["id"])
            seen_title_hashes.add(th)
            all_items.append(item)

    if "yahoo" in sources:
        _add(fetch_yahoo_news(ticker))

    if "google_rss" in sources:
        _add(fetch_google_news_rss(ticker, company_name))

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
            max_retries=0,  # 자동 재시도 비활성화 (직접 제어)
            timeout=30.0,
        )
        prompt = (
            "당신은 주식 투자자를 위한 뉴스 번역·요약 도우미입니다. "
            "영어 뉴스 제목과 출처를 받으면, "
            "한국어로 자연스럽게 번역하고 투자자에게 중요한 핵심 내용을 "
            "간결하게 설명해 주세요.무슨 내용인지 충분히 요약되어서 설명되어야해요\n"
            "그 다음 줄바꿈 후, 이 뉴스에 대해 일반 투자자 시각에서 "
            "짧고 재치있는 한마디를 ➡️ 이모지와 함께 한 줄로 추가해 주세요. "
            "예: ➡️ \"실적 발표 앞두고 긴장되는 구간이네요 😅\"\n\n"
            f"출처: {publisher}\n제목: {title}"
        )
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = client.chat.completions.create(
                    model="gemini-3.1-flash-lite",
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                err_str = str(e)
                is_503 = "503" in err_str or "Service Unavailable" in err_str or "high demand" in err_str.lower()
                if is_503 and attempt < max_retries:
                    logger.warning("AI 요약 503 에러 (시도 %d/%d) — 30초 후 재시도: %s", attempt, max_retries, e)
                    time.sleep(30)
                else:
                    logger.warning("AI 요약 실패 (시도 %d/%d): %s", attempt, max_retries, e)
                    return None
    except Exception as e:
        logger.warning("AI 요약 클라이언트 생성 실패: %s", e)
        return None
