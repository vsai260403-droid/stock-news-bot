"""
news_fetcher.py - 여러 소스에서 뉴스 수집

지원 소스:
  yahoo      - Yahoo Finance RSS (feedparser, API 키 불필요)
  google_rss - Google News RSS (feedparser, API 키 불필요)
  finnhub    - Finnhub Company News (무료 API 키: https://finnhub.io/register)
"""
import calendar
import json
import logging
import re
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


# ── 관련성 필터 ───────────────────────────────────────────────────────────────
def _is_ticker_relevant(title: str, ticker: str, company_name: str = "") -> bool:
    """뉴스 제목이 해당 티커와 관련 있는지 확인합니다.

    티커 기호, 티커별 별칭, 회사명 핵심 단어가 제목에 포함되면 관련 있음으로 판단합니다.
    Gemini가 429/실패했을 때 마지막 안전장치로 사용합니다.
    """
    if not title:
        return False

    title_lower = title.lower()
    ticker_upper = ticker.upper()
    ticker_lower = ticker.lower()

    # 1. 티커가 단어 경계로 포함 ($AAPL, AAPL:, AAPL stock 등)
    if re.search(r"(?<![a-zA-Z])" + re.escape(ticker_lower) + r"(?![a-zA-Z])", title_lower):
        return True

    # 2. 티커별 별칭/핵심 키워드
    aliases = {
        "SOFI": ["sofi", "sofi technologies", "anthony noto"],
        "JOBY": ["joby", "joby aviation"],
        "ATOM": ["atomera", "mears silicon"],
        "BMNR": ["bmnr", "bitmine", "bitmine immersion", "tom lee"],
        "MSTR": ["mstr", "microstrategy", "strategy", "michael saylor", "saylor"],
    }
    for kw in aliases.get(ticker_upper, []):
        if kw in title_lower:
            return True

    # 3. 회사명에서 5글자 이상 핵심 단어 추출 후 제목 매칭
    if company_name:
        stop_words = {
            "inc", "corp", "ltd", "llc", "co", "the", "and", "of", "a", "an",
            "class", "holdings", "group", "international", "services", "systems",
            "technologies", "technology", "incorporated", "company", "corporation",
        }
        key_words = [
            w.strip(".,&()-").lower()
            for w in company_name.split()
            if len(w.strip(".,&()-")) >= 5 and w.strip(".,&()-").lower() not in stop_words
        ]
        for kw in key_words[:3]:
            if kw and kw in title_lower:
                return True

    return False


def _local_relevance_filter(
    items: List[Dict[str, Any]],
    ticker: str,
    company_name: str = "",
    reason: str = "regex",
) -> List[Dict[str, Any]]:
    """로컬 관련성 필터. 탈락 이유를 로그에 남깁니다."""
    relevant: List[Dict[str, Any]] = []
    for item in items:
        title = item.get("title", "")
        if _is_ticker_relevant(title, ticker.upper(), company_name):
            relevant.append(item)
        else:
            logger.info(
                "[%s] 뉴스 제외: 관련성 필터 탈락 (%s) — %s | source=%s",
                ticker,
                reason,
                title[:140],
                item.get("source", ""),
            )

    skipped = len(items) - len(relevant)
    if skipped > 0:
        logger.info("[%s] 로컬 관련성 필터: %d건 제외, %d건 통과", ticker, skipped, len(relevant))
    return relevant


# ── Gemini 배치 관련성 필터 ──────────────────────────────────────────────────────
def _gemini_relevance_filter(
    items: List[Dict[str, Any]],
    ticker: str,
    company_name: str,
    gemini_api_key: str,
    gemini_model: str,
) -> Optional[List[Dict[str, Any]]]:
    """Gemini로 뉴스 관련성을 일괄 판단합니다 (배치 처리 — API 1회 호출).

    성공하면 관련 기사 리스트를 반환합니다.
    실패하면 None을 반환하여 호출부에서 로컬 필터로 안전하게 폴백합니다.
    """
    if not items or not gemini_api_key:
        return items

    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("[%s] openai 패키지 없음 → Gemini 관련성 필터 생략, 로컬 필터로 폴백", ticker)
        return None

    titles = [item.get("title", "") for item in items]
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    company_info = f" / {company_name}" if company_name else ""

    prompt = (
        f"You are a strict stock news relevance filter.\n"
        f"Target company — Ticker: {ticker}{company_info}\n\n"
        f"Review each headline and decide if it is DIRECTLY about this specific company.\n\n"
        f"INCLUDE if the headline:\n"
        f"- Mentions the company by name or ticker symbol\n"
        f"- Covers earnings, revenue, guidance, products, or services of this company\n"
        f"- Mentions CEO/executives of this company\n"
        f"- Covers M&A, partnerships, lawsuits, or regulatory actions involving this company\n\n"
        f"EXCLUDE if the headline:\n"
        f"- Is general market or macroeconomic news\n"
        f"- Covers a sector/industry without specifically mentioning this company\n"
        f"- Is about competitors only, without directly involving {ticker}\n"
        f"- Is a listicle/roundup where this company is not the main focus\n\n"
        f"Headlines:\n{numbered}\n\n"
        f"Reply with ONLY a JSON array of the relevant headline numbers. Example: [1, 3, 5]\n"
        f"If none are relevant, reply: []"
    )

    try:
        logger.info("[%s] Gemini 관련성 필터 호출: model=%s, items=%d", ticker, gemini_model, len(items))
        client = OpenAI(
            api_key=gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            max_retries=0,
            timeout=20.0,
        )
        response = client.chat.completions.create(
            model=gemini_model,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()
        match = re.search(r"\[[\d,\s]*\]", raw)
        if not match:
            logger.warning("[%s] Gemini 관련성 응답 파싱 실패 → 로컬 필터로 폴백: %s", ticker, raw[:120])
            return None

        indices = json.loads(match.group())
        relevant = [
            items[i - 1]
            for i in indices
            if isinstance(i, int) and 1 <= i <= len(items)
        ]

        kept_ids = {id(item) for item in relevant}
        for item in items:
            if id(item) not in kept_ids:
                logger.info(
                    "[%s] 뉴스 제외: Gemini 관련성 필터 탈락 — %s | source=%s",
                    ticker,
                    item.get("title", "")[:140],
                    item.get("source", ""),
                )

        skipped = len(items) - len(relevant)
        if skipped > 0:
            logger.info("[%s] Gemini 관련성 필터: %d건 제외, %d건 통과", ticker, skipped, len(relevant))
        return relevant

    except Exception as e:
        logger.warning("[%s] Gemini 관련성 필터 실패 → 로컬 필터로 폴백: %s", ticker, e)
        return None


# ── Yahoo Finance RSS ──────────────────────────────────────────────────────────
def fetch_yahoo_news(ticker: str, company_name: str = "") -> List[Dict[str, Any]]:
    """Yahoo Finance RSS 피드에서 해당 티커의 최신 뉴스를 가져옵니다."""
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
                logger.info("[%s] Yahoo 뉴스 제외: item_id 없음 — %s", ticker, entry.get("title", "")[:120])
                continue
            title = entry.get("title", "(제목 없음)")
            published = entry.get("published_parsed")
            ts = int(calendar.timegm(published)) if published else 0
            source_obj = getattr(entry, "source", None)
            publisher = getattr(source_obj, "title", "Yahoo Finance") if source_obj else "Yahoo Finance"
            result.append({
                "id": f"yahoo_rss_{raw_id}",
                "title": title,
                "link": entry.get("link", ""),
                "publisher": publisher,
                "publish_time": ts,
                "source": "Yahoo Finance",
                "ticker": ticker.upper(),
            })
        logger.info("[%s] Yahoo 뉴스 수집: %d건", ticker, len(result))
        return result

    except Exception as e:
        logger.error("[%s] Yahoo Finance 뉴스 실패: %s", ticker, e)
        return []


# ── Google News RSS ────────────────────────────────────────────────────────────
def fetch_google_news_rss(ticker: str, company_name: str = "") -> List[Dict[str, Any]]:
    """Google News RSS 피드에서 해당 티커 관련 뉴스를 가져옵니다. (API 키 불필요)"""
    try:
        import feedparser
        from urllib.parse import quote
    except ImportError:
        logger.warning("feedparser 미설치 → Google News RSS 건너뜀. pip install feedparser")
        return []

    if company_name and company_name.lower() != ticker.lower():
        query = quote(f'"{ticker}" stock OR "{company_name}"')
    else:
        query = quote(f'"{ticker}" stock')

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
            if not raw_id:
                logger.info("[%s] Google 뉴스 제외: item_id 없음 — %s", ticker, entry.get("title", "")[:120])
                continue
            source_obj = getattr(entry, "source", None)
            publisher = getattr(source_obj, "title", "Google News")
            published = entry.get("published_parsed")
            ts = int(calendar.timegm(published)) if published else 0
            title = entry.get("title", "(제목 없음)")

            result.append({
                "id": f"gnews_{raw_id}",
                "title": title,
                "link": entry.get("link", ""),
                "publisher": publisher,
                "publish_time": ts,
                "source": "Google News",
                "ticker": ticker_upper,
            })
        logger.info("[%s] Google 뉴스 수집: %d건", ticker, len(result))
        return result

    except Exception as e:
        logger.error("[%s] Google News RSS 실패: %s", ticker, e)
        return []


# ── Finnhub ────────────────────────────────────────────────────────────────────
def fetch_finnhub_news(ticker: str, api_key: str) -> List[Dict[str, Any]]:
    """Finnhub Company News API에서 최근 7일 뉴스를 가져옵니다."""
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
            logger.info("[%s] Finnhub 뉴스 제외: 응답이 list 아님", ticker)
            return []

        result = []
        for item in data[:15]:
            news_id = str(item.get("id", ""))
            if not news_id:
                logger.info("[%s] Finnhub 뉴스 제외: item_id 없음 — %s", ticker, item.get("headline", "")[:120])
                continue
            result.append({
                "id": f"finnhub_{news_id}",
                "title": item.get("headline", "(제목 없음)"),
                "link": item.get("url", ""),
                "publisher": item.get("source", "Finnhub"),
                "publish_time": item.get("datetime", 0),
                "source": "Finnhub",
                "ticker": ticker.upper(),
            })
        logger.info("[%s] Finnhub 뉴스 수집: %d건", ticker, len(result))
        return result

    except Exception as e:
        logger.error("[%s] Finnhub 뉴스 실패: %s", ticker, e)
        return []


# ── 통합 수집기 ────────────────────────────────────────────────────────────────
def fetch_all_news(ticker: str, config: dict) -> List[Dict[str, Any]]:
    """설정된 모든 소스에서 뉴스를 수집하고 ID 기준 중복을 제거합니다."""
    sources = config.get("news_sources", ["yahoo", "google_rss"])
    finnhub_key = config.get("finnhub_api_key", "").strip()

    company_name = ""
    if "google_rss" in sources or "yahoo" in sources:
        try:
            from price_fetcher import fetch_price
            info = fetch_price(ticker)
            if info:
                company_name = info.get("name", "")
        except Exception as e:
            logger.info("[%s] 회사명 조회 실패: %s", ticker, e)

    logger.info("[%s] 뉴스 수집 시작: sources=%s, company_name=%s", ticker, sources, company_name or "N/A")

    seen_ids: set = set()
    seen_title_hashes: set = set()
    all_items: List[Dict[str, Any]] = []

    def _title_hash(title: str) -> str:
        import hashlib
        return hashlib.md5(title.lower().strip().encode("utf-8")).hexdigest()[:16]

    def _add(items: List[Dict[str, Any]], source_name: str) -> None:
        before = len(all_items)
        for item in items:
            if not item.get("id"):
                logger.info("[%s] 뉴스 제외: item_id 없음 — %s", ticker, item.get("title", "")[:120])
                continue
            th = _title_hash(item.get("title", ""))
            if item["id"] in seen_ids:
                logger.info("[%s] 뉴스 제외: 수집 중복 id — %s", ticker, item.get("title", "")[:120])
                continue
            if th in seen_title_hashes:
                logger.info("[%s] 뉴스 제외: 수집 중복 제목 — %s", ticker, item.get("title", "")[:120])
                continue
            seen_ids.add(item["id"])
            seen_title_hashes.add(th)
            all_items.append(item)
        logger.info("[%s] %s 추가 결과: %d건", ticker, source_name, len(all_items) - before)

    if "yahoo" in sources:
        _add(fetch_yahoo_news(ticker, company_name), "yahoo")

    if "google_rss" in sources:
        _add(fetch_google_news_rss(ticker, company_name), "google_rss")

    if "finnhub" in sources and finnhub_key:
        _add(fetch_finnhub_news(ticker, finnhub_key), "finnhub")

    logger.info("[%s] 뉴스 수집 합계: %d건", ticker, len(all_items))

    # 관련성 필터: Gemini 성공 시 Gemini 결과 사용, 실패/429 시 로컬 필터로 폴백
    gemini_key = config.get("gemini_api_key", "").strip()
    relevance_model = (
        config.get("gemini_relevance_model")
        or config.get("gemini_model")
        or "gemini-3.1-flash-lite"
    )
    if all_items:
        if gemini_key:
            filtered = _gemini_relevance_filter(
                all_items, ticker, company_name, gemini_key, relevance_model
            )
            if filtered is None:
                all_items = _local_relevance_filter(
                    all_items, ticker.upper(), company_name, reason="gemini_failed_fallback"
                )
            else:
                all_items = filtered
        else:
            all_items = _local_relevance_filter(
                all_items, ticker.upper(), company_name, reason="no_gemini_key"
            )

    # 시간 필터: 설정된 시간(기본 24시간)보다 오래된 뉴스 제외
    max_age_hours = config.get("news_max_age_hours", 24)
    if max_age_hours > 0:
        cutoff_ts = int(time.time()) - (max_age_hours * 3600)
        filtered = []
        for item in all_items:
            publish_time = int(item.get("publish_time", 0) or 0)
            if publish_time >= cutoff_ts:
                filtered.append(item)
            else:
                logger.info(
                    "[%s] 뉴스 제외: 시간 필터 탈락 (%d시간 초과) — %s | publish_time=%s",
                    ticker,
                    max_age_hours,
                    item.get("title", "")[:140],
                    publish_time,
                )
        logger.info("[%s] 뉴스 최종 통과: %d건", ticker, len(filtered))
        return filtered

    logger.info("[%s] 뉴스 최종 통과: %d건", ticker, len(all_items))
    return all_items


# ── AI 한글 요약 ───────────────────────────────────────────────────────────────
def ai_summarize_news(
    title: str,
    publisher: str,
    gemini_api_key: str,
    gemini_model: Optional[str] = None,
) -> Optional[str]:
    """Google Gemini API로 영문 뉴스 제목을 한국어로 번역·요약합니다."""
    if not gemini_api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai 패키지 미설치 → pip install openai  (AI 요약 비활성화)")
        return None

    model = gemini_model or "gemini-3.1-flash-lite"
    try:
        client = OpenAI(
            api_key=gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            max_retries=0,
            timeout=30.0,
        )
        if "\n\n" in title and len(title) > 200:
            prompt = (
                "당신은 주식 투자자를 위한 SEC 공시 요약 도우미입니다. "
                "아래는 SEC EDGAR 공시 문서의 실제 본문입니다. "
                "투자자에게 중요한 핵심 수치(매출, 순이익, EPS, 가이던스 등)와 "
                "주요 이벤트를 한국어로 간결하게 요약해주세요. "
                "일반론이 아닌 이 문서의 구체적인 내용을 요약해야 합니다.\n"
                "그 다음 줄바꿈 후, 투자자 시각에서 짧고 재치있는 한마디를 "
                "➡️ 이모지와 함께 한 줄로 추가해 주세요.\n\n"
                f"출처: {publisher}\n{title}"
            )
        else:
            prompt = (
                "당신은 주식 투자자를 위한 뉴스 번역·요약 도우미입니다. "
                "영어 뉴스 제목과 출처를 받으면, "
                "한국어로 자연스럽게 번역하고 투자자에게 중요한 핵심 내용을 "
                "간결하게 설명해 주세요. 무슨 내용인지 충분히 요약되어서 설명되어야해요\n"
                "그 다음 줄바꿈 후, 이 뉴스에 대해 일반 투자자 시각에서 "
                "짧고 재치있는 한마디를 ➡️ 이모지와 함께 한 줄로 추가해 주세요. "
                "예: ➡️ \"실적 발표 앞두고 긴장되는 구간이네요 😅\"\n\n"
                f"출처: {publisher}\n제목: {title}"
            )

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                logger.info("AI 요약 호출: model=%s, publisher=%s", model, publisher)
                response = client.chat.completions.create(
                    model=model,
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
                    logger.warning("AI 요약 실패 (시도 %d/%d, model=%s): %s", attempt, max_retries, model, e)
                    return None
    except Exception as e:
        logger.warning("AI 요약 클라이언트 생성 실패: %s", e)
        return None
