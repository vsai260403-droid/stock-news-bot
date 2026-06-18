"""
news_fetcher.py - 여러 소스에서 뉴스 수집

지원 소스:
  yahoo      - Yahoo Finance RSS (feedparser, API 키 불필요)
  google_rss - Google News RSS (feedparser, API 키 불필요)
  finnhub    - Finnhub Company News (무료 API 키: https://finnhub.io/register)
"""
import calendar
import hashlib
import html
import json
import logging
import re
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from ai_provider import ai_generate_with_fallback

logger = logging.getLogger(__name__)


def _strip_html(text: str) -> str:
    """RSS/HTML 조각을 사람이 읽기 좋은 평문으로 정리합니다."""
    if not text:
        return ""
    text = html.unescape(str(text))
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _entry_summary(entry: Any, title: str = "") -> str:
    """feedparser entry에서 기사 요약/본문 발췌 후보를 추출합니다."""
    candidates: List[str] = []

    for key in ("summary", "description", "subtitle"):
        value = entry.get(key, "")
        if value:
            candidates.append(str(value))

    for content in entry.get("content", []) or []:
        if isinstance(content, dict) and content.get("value"):
            candidates.append(str(content.get("value")))

    title_clean = _strip_html(title).lower()
    for candidate in candidates:
        cleaned = _strip_html(candidate)
        if not cleaned:
            continue
        if title_clean and cleaned.lower() == title_clean:
            continue
        return cleaned[:1200]
    return ""


def normalize_news_title(title: str) -> str:
    """재배포 기사 중복 제거용 제목 정규화."""
    normalized = (title or "").lower().strip()
    normalized = normalized.replace("’", "'").replace("‘", "'")
    normalized = normalized.replace("“", '"').replace("”", '"')
    normalized = normalized.replace("–", "-").replace("—", "-")

    normalized = re.sub(r"^\[[a-z]{1,8}\]\s*", "", normalized)
    normalized = re.sub(
        r"\s+[-|:]\s+[a-z0-9][a-z0-9&.,' /-]{0,45}\."
        r"(com|net|org|io|ai|co|news|finance)\s*$",
        "",
        normalized,
    )

    suffixes = [
        "yahoo finance", "the motley fool", "motley fool",
        "24/7 wall st.", "24/7 wall st", "247 wall st.", "247 wall st", "wall st.", "wall st",
        "barron's", "barrons", "benzinga", "reuters", "marketwatch",
        "investor's business daily", "investors business daily", "seeking alpha", "zacks",
        "globenewswire", "business wire", "pr newswire", "cnbc", "msn", "google news",
        "ap news", "associated press", "morningstar", "investopedia", "kiplinger",
        "nasdaq", "gurufocus", "thestreet", "the street",
    ]
    publisher_pattern = "|".join(re.escape(s) for s in suffixes)
    normalized = re.sub(r"\s+[-|:]\s+(" + publisher_pattern + r")\s*$", "", normalized)
    normalized = re.sub(r"\s+\((" + publisher_pattern + r")\)\s*$", "", normalized)

    normalized = re.sub(
        r"\s+[-|:]\s+[a-z0-9&.,' /-]{2,45}\s+"
        r"(news|finance|wire|journal|times|post|daily|report|reports|media|market|markets|street|st\.?)\.?\s*$",
        "",
        normalized,
    )

    match = re.search(r"\s+[-|:]\s+([a-z0-9&.,' /-]{2,35})$", normalized)
    if match:
        suffix = match.group(1).strip()
        suffix_words = suffix.split()
        looks_like_publisher = (
            len(suffix_words) <= 4
            and not any(ch.isdigit() for ch in suffix)
            and len(normalized[:match.start()].split()) >= 4
        )
        if looks_like_publisher:
            normalized = normalized[:match.start()].strip()

    normalized = normalized.replace("'s", "s")
    normalized = re.sub(r"[^a-z0-9가-힣]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def news_title_hash(title: str) -> str:
    """같은 기사 재배포를 잡기 위한 안정적인 제목 해시 키를 반환합니다."""
    normalized = normalize_news_title(title)
    return "title_norm_" + hashlib.md5(normalized.encode("utf-8")).hexdigest()[:16]


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


_IMPORTANT_NEWS_PATTERNS = [
    (4, "실적/가이던스", r"\b(earnings|quarterly results|financial results|q[1-4].*results|revenue|profit|eps|guidance|preliminary results|raises guidance|cuts guidance)\b"),
    (4, "인수합병", r"\b(merger|acquisition|acquires|acquired by|buyout|takeover|strategic acquisition|definitive agreement)\b"),
    (3, "계약/파트너십", r"\b(partnership|collaboration|contract|agreement|order|award|customer win|supplier deal|license agreement)\b"),
    (3, "제품/승인", r"\b(fda|approval|clearance|clinical trial|phase [123]|drug|patent|product launch|launches|unveils|production|deliveries|shipments)\b"),
    (3, "규제/법적", r"\b(sec|doj|ftc|investigation|lawsuit|settlement|antitrust|probe|recall|regulatory|fine|sanction)\b"),
    (3, "자본정책", r"\b(stock split|dividend|buyback|repurchase|offering|secondary offering|debt offering|notes offering|bankruptcy|chapter 11|delisting)\b"),
    (3, "경영진 변화", r"\b(ceo|cfo|coo|chief executive|chief financial|resigns|steps down|appoints|names .* ceo|names .* cfo)\b"),
    (3, "보안/운영 리스크", r"\b(cyberattack|data breach|outage|plant shutdown|factory shutdown|supply disruption)\b"),
]

_LOW_VALUE_NEWS_PATTERNS = [
    ("보유 지분/주식 매매", r"\b(13f|form 4|insider(?:s)?|institutional investor|hedge fund|asset management|capital management|wealth management|advisors)\b.*\b(buys?|sells?|sold|bought|purchases?|shares acquired|shares sold|stake|position|holdings?)\b"),
    ("보유 지분/주식 매매", r"\b(buys?|sells?|sold|bought|purchases?|disposes?|reduces?|raises?|boosts?|cuts?|trims?)\b.*\b(stake|position|holdings?|shares of|shares in)\b"),
    ("분석가/목표가", r"\b(analyst|analysts|price target|target price|rating|upgrade[sd]?|downgrade[sd]?|initiates coverage|maintains .* rating|brokerage|wall street)\b"),
    ("전망/의견성 기사", r"\b(should you buy|is .* a buy|buy sell or hold|better buy|where will .* stock|where .* stock will|prediction|forecast|could .* stock|will .* stock|what'?s next|upside|downside|bull case|bear case)\b"),
    ("추천/리스트 기사", r"\b(best stocks|top stocks|stocks to buy|stock to buy|watchlist|millionaire-maker|3 stocks|three stocks|2 stocks|two stocks|undervalued stocks)\b"),
]


def _news_filter_text(item: Dict[str, Any]) -> str:
    return "\n".join(
        str(item.get(key, "") or "")
        for key in ("title", "summary", "publisher", "source")
    )


def _importance_score(item: Dict[str, Any]) -> Tuple[int, List[str]]:
    text = _news_filter_text(item).lower()
    score = 0
    signals: List[str] = []
    for points, label, pattern in _IMPORTANT_NEWS_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            score += points
            signals.append(label)
    return score, signals


def _low_value_reason(item: Dict[str, Any]) -> str:
    text = _news_filter_text(item).lower()
    for label, pattern in _LOW_VALUE_NEWS_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return label
    return ""


def _configured_min_importance(config: dict) -> int:
    try:
        return int(config.get("news_importance_min_score", 2))
    except (TypeError, ValueError):
        return 2


def _keyword_list(config: dict, key: str) -> List[str]:
    value = config.get(key, [])
    if not isinstance(value, list):
        return []
    return [str(item).strip().lower() for item in value if str(item).strip()]


def _local_importance_filter(
    items: List[Dict[str, Any]],
    ticker: str,
    config: dict,
) -> List[Dict[str, Any]]:
    """저신호 기사(보유 지분 변동, 전망성 칼럼 등)를 제외합니다."""
    if not config.get("news_importance_filter_enabled", True):
        return items

    min_score = _configured_min_importance(config)
    always_include = _keyword_list(config, "news_always_include_keywords")
    always_exclude = _keyword_list(config, "news_excluded_keywords")
    important: List[Dict[str, Any]] = []

    for item in items:
        title = item.get("title", "")
        text = _news_filter_text(item).lower()

        excluded_keyword = next((kw for kw in always_exclude if kw in text), "")
        if excluded_keyword:
            logger.info(
                "[%s] 뉴스 제외: 사용자 제외 키워드(%s) — %s | source=%s",
                ticker,
                excluded_keyword,
                title[:140],
                item.get("source", ""),
            )
            continue

        included_keyword = next((kw for kw in always_include if kw in text), "")
        if included_keyword:
            important.append(item)
            continue

        low_reason = _low_value_reason(item)
        if low_reason:
            logger.info(
                "[%s] 뉴스 제외: 저신호 뉴스(%s) — %s | source=%s",
                ticker,
                low_reason,
                title[:140],
                item.get("source", ""),
            )
            continue

        score, signals = _importance_score(item)
        if score >= min_score:
            item["importance_score"] = score
            item["importance_signals"] = signals
            important.append(item)
        else:
            logger.info(
                "[%s] 뉴스 제외: 중요도 점수 미달(score=%d/%d) — %s | source=%s",
                ticker,
                score,
                min_score,
                title[:140],
                item.get("source", ""),
            )

    skipped = len(items) - len(important)
    if skipped > 0:
        logger.info("[%s] 중요도 필터: %d건 제외, %d건 통과", ticker, skipped, len(important))
    return important


# ── Gemini 배치 관련성 필터 ──────────────────────────────────────────────────────
def _ai_relevance_filter(
    items: List[Dict[str, Any]],
    ticker: str,
    company_name: str,
    config: dict,
) -> Optional[List[Dict[str, Any]]]:
    """OAuth 우선, Gemini fallback으로 뉴스 관련성을 배치 판단합니다."""
    if not items:
        return items

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
        f"Reply with ONLY a JSON array of the relevant headline numbers. "
        f"Example: [1, 3, 5]\n"
        f"If none are relevant, reply: []"
    )

    raw = ai_generate_with_fallback(
        prompt,
        {
            **config,
            "gemini_request_model": config.get("gemini_relevance_model") or config.get("gemini_model"),
        },
        purpose=f"{ticker} 관련성 필터",
    )
    if not raw:
        return None

    match = re.search(r"\[[\d,\s]*\]", raw)
    if not match:
        logger.warning(
            "[%s] AI 관련성 응답 파싱 실패 → 로컬 필터로 폴백: %s",
            ticker,
            raw[:160],
        )
        return None

    try:
        indices = json.loads(match.group())
    except Exception as exc:
        logger.warning("[%s] AI 관련성 JSON 파싱 실패: %s", ticker, exc)
        return None

    relevant = [
        items[i - 1]
        for i in indices
        if isinstance(i, int) and 1 <= i <= len(items)
    ]

    kept_ids = {id(item) for item in relevant}
    for item in items:
        if id(item) not in kept_ids:
            logger.info(
                "[%s] 뉴스 제외: AI 관련성 필터 탈락 — %s | source=%s",
                ticker,
                item.get("title", "")[:140],
                item.get("source", ""),
            )

    logger.info(
        "[%s] AI 관련성 필터: %d건 제외, %d건 통과",
        ticker,
        len(items) - len(relevant),
        len(relevant),
    )
    return relevant


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
            summary = _entry_summary(entry, title)
            published = entry.get("published_parsed")
            ts = int(calendar.timegm(published)) if published else 0
            source_obj = getattr(entry, "source", None)
            publisher = getattr(source_obj, "title", "Yahoo Finance") if source_obj else "Yahoo Finance"
            result.append({
                "id": f"yahoo_rss_{raw_id}",
                "title": title,
                "summary": summary,
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
            summary = _entry_summary(entry, title)

            result.append({
                "id": f"gnews_{raw_id}",
                "title": title,
                "summary": summary,
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
                "summary": _strip_html(item.get("summary", ""))[:1200],
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

    def _add(items: List[Dict[str, Any]], source_name: str) -> None:
        before = len(all_items)
        for item in items:
            if not item.get("id"):
                logger.info("[%s] 뉴스 제외: item_id 없음 — %s", ticker, item.get("title", "")[:120])
                continue
            th = news_title_hash(item.get("title", ""))
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

    # 관련성 필터: OAuth 우선 → Gemini fallback → 로컬 regex fallback
    if all_items:
        filtered = _ai_relevance_filter(
            all_items,
            ticker,
            company_name,
            config,
        )
        if filtered is None:
            all_items = _local_relevance_filter(
                all_items,
                ticker.upper(),
                company_name,
                reason="all_ai_providers_failed",
            )
        else:
            all_items = filtered

    if all_items:
        all_items = _local_importance_filter(
            all_items,
            ticker.upper(),
            config,
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
    gemini_api_key: str = "",
    gemini_model: Optional[str] = None,
    config: Optional[dict] = None,
    content: str = "",
) -> Optional[str]:
    """OAuth 우선, Gemini fallback으로 뉴스·SEC·트윗을 번역·요약합니다."""
    effective_config = dict(config or {})

    # 기존 호출부와의 호환성 유지
    if gemini_api_key and not effective_config.get("gemini_api_key"):
        effective_config["gemini_api_key"] = gemini_api_key
    if gemini_model and not effective_config.get("gemini_summary_model"):
        effective_config["gemini_summary_model"] = gemini_model

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
        content = _strip_html(content)[:2500]
        content_block = (
            f"\n기사 내용/요약 발췌:\n{content}\n"
            if content
            else "\n기사 내용/요약 발췌: 제공되지 않음\n"
        )
        prompt = (
            "당신은 주식 투자자를 위한 뉴스 번역·요약 도우미입니다. "
            "영어 뉴스 제목, 출처, 기사 내용/요약 발췌를 받으면, "
            "한국어로 자연스럽게 번역하고 투자자에게 중요한 핵심 내용을 간결하게 설명해 주세요. "
            "제공된 내용 밖의 수치나 원인은 추정하지 말고, 내용이 부족하면 본문 확인이 필요하다고 말해 주세요.\n"
            "그 다음 줄바꿈 후, 이 뉴스에 대해 일반 투자자 시각에서 "
            "짧고 재치있는 한마디를 ➡️ 이모지와 함께 한 줄로 추가해 주세요.\n\n"
            f"출처: {publisher}\n제목: {title}{content_block}"
        )

    return ai_generate_with_fallback(
        prompt,
        effective_config,
        purpose="요약",
    )

