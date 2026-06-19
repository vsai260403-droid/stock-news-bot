"""Trading-oriented analysis helpers for price-move alerts."""
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from ai_provider import ai_fallback_available, ai_generate_with_fallback


logger = logging.getLogger(__name__)

_POSITIVE_PATTERNS = re.compile(
    r"\b(beat|beats|raises?|raised|growth|profit|record|approval|approved|partnership|contract|order|launch|unveils?|buyback|dividend|guidance raised|upgrade)\b",
    re.IGNORECASE,
)
_NEGATIVE_PATTERNS = re.compile(
    r"\b(miss|misses|cuts?|cut|loss|decline|lawsuit|probe|investigation|offering|dilution|downgrade|recall|delay|bankruptcy|resigns?|halts?)\b",
    re.IGNORECASE,
)
_HIGH_IMPACT_PATTERNS = re.compile(
    r"\b(earnings|guidance|fda|approval|merger|acquisition|offering|bankruptcy|sec|investigation|contract|partnership|buyback|dividend|production|deliveries)\b",
    re.IGNORECASE,
)


def _now() -> int:
    return int(time.time())


def _age_hours(item: Dict[str, Any], now: int) -> Optional[float]:
    publish_time = int(item.get("publish_time", 0) or 0)
    if not publish_time:
        return None
    return max(0.0, (now - publish_time) / 3600)


def _title(item: Dict[str, Any]) -> str:
    return str(item.get("title") or item.get("description") or item.get("text") or "").strip()


def _link(item: Dict[str, Any]) -> str:
    return str(item.get("link") or "").strip()


def _short(text: str, limit: int = 95) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _direction_score(text: str) -> int:
    score = 0
    if _POSITIVE_PATTERNS.search(text):
        score += 1
    if _NEGATIVE_PATTERNS.search(text):
        score -= 1
    return score


def _event_score(text: str, source_type: str) -> int:
    score = 1
    if source_type in ("sec", "official"):
        score += 1
    if _HIGH_IMPACT_PATTERNS.search(text):
        score += 2
    if _NEGATIVE_PATTERNS.search(text) or _POSITIVE_PATTERNS.search(text):
        score += 1
    return min(5, score)


def _append_item(
    result: List[Dict[str, Any]],
    item: Dict[str, Any],
    source_type: str,
    label: str,
    now: int,
    max_age_hours: int,
) -> None:
    title = _title(item)
    if not title:
        return
    age = _age_hours(item, now)
    if age is not None and age > max_age_hours:
        return
    text = " ".join([title, str(item.get("summary") or ""), str(item.get("description") or "")])
    result.append({
        "source_type": source_type,
        "label": label,
        "title": title,
        "link": _link(item),
        "age_hours": age,
        "direction_score": _direction_score(text),
        "event_score": _event_score(text, source_type),
    })


def collect_recent_catalysts(ticker: str, config: dict) -> List[Dict[str, Any]]:
    max_age_hours = int(config.get("price_catalyst_lookback_hours", 24) or 24)
    max_items = int(config.get("price_catalyst_max_items", 8) or 8)
    now = _now()
    result: List[Dict[str, Any]] = []

    try:
        from news_fetcher import fetch_all_news
        for item in fetch_all_news(ticker, config)[:8]:
            _append_item(result, item, "news", "뉴스", now, max_age_hours)
    except Exception as e:
        logger.info("[%s] 가격 원인 분석 뉴스 수집 실패: %s", ticker, e)

    try:
        if config.get("monitor_sec_filings", False):
            from sec_fetcher import fetch_sec_filings, filter_sec_by_age
            forms = config.get("sec_form_types", ["8-K"])
            filings = filter_sec_by_age(fetch_sec_filings(ticker, forms), max(1, int(max_age_hours / 24) + 1))
            for item in filings[:5]:
                _append_item(result, item, "sec", "SEC", now, max_age_hours)
    except Exception as e:
        logger.info("[%s] 가격 원인 분석 SEC 수집 실패: %s", ticker, e)

    try:
        if config.get("monitor_twitter", False):
            from twitter_fetcher import fetch_all_tweets
            for item in fetch_all_tweets(ticker, config)[:6]:
                _append_item(result, item, "twitter", "X/Twitter", now, max_age_hours)
    except Exception as e:
        logger.info("[%s] 가격 원인 분석 트윗 수집 실패: %s", ticker, e)

    try:
        if config.get("monitor_official", False):
            from official_fetcher import fetch_all_official_posts
            official_cfg = dict(config)
            official_cfg["official_feeds"] = [
                feed for feed in config.get("official_feeds", [])
                if not isinstance(feed, dict) or str(feed.get("ticker") or "").upper() in ("", ticker.upper())
            ]
            for item in fetch_all_official_posts(official_cfg)[:5]:
                _append_item(result, item, "official", "공식", now, max_age_hours)
    except Exception as e:
        logger.info("[%s] 가격 원인 분석 공식 페이지 수집 실패: %s", ticker, e)

    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in sorted(result, key=lambda row: (row.get("event_score", 0), -(row.get("age_hours") or 0)), reverse=True):
        key = re.sub(r"[^a-z0-9가-힣]+", " ", item["title"].lower()).strip()[:80]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= max_items:
            break
    return deduped


def _local_signal(price_info: Dict[str, Any], catalysts: List[Dict[str, Any]]) -> Dict[str, Any]:
    change_pct = float(price_info.get("change_pct", 0.0) or 0.0)
    volume_ratio = price_info.get("volume_ratio")
    direction_sum = sum(int(item.get("direction_score", 0) or 0) for item in catalysts)
    event_score = max([int(item.get("event_score", 0) or 0) for item in catalysts] or [0])

    impact = min(5, max(1, int(abs(change_pct) / 3) + (1 if event_score >= 3 else 0)))
    if isinstance(volume_ratio, (int, float)) and volume_ratio >= 3:
        impact = min(5, impact + 1)

    if direction_sum > 0:
        direction = "긍정"
    elif direction_sum < 0:
        direction = "부정"
    else:
        direction = "중립/불명"

    if not catalysts:
        cause = "최근 24시간 내 뚜렷한 뉴스/공시/공식 발표를 찾지 못했습니다. 수급성 움직임 가능성을 확인하세요."
    else:
        top = catalysts[0]
        label = str(top.get("label") or top.get("source_type") or "이벤트")
        cause = f"가장 가까운 촉매 후보는 {label}의 '{_short(top.get('title', ''), 70)}'입니다."

    volume_note = ""
    abnormal_volume = False
    if isinstance(volume_ratio, (int, float)):
        abnormal_volume = volume_ratio >= 2.5
        volume_note = f"평균 대비 {volume_ratio:.1f}배" + (" — 이상거래" if abnormal_volume else "")

    return {
        "impact_score": impact,
        "direction": direction,
        "cause_summary": cause,
        "confidence": "높음" if catalysts and event_score >= 3 else ("중간" if catalysts else "낮음"),
        "volume_note": volume_note,
        "abnormal_volume": abnormal_volume,
    }


def _ai_signal(price_info: Dict[str, Any], catalysts: List[Dict[str, Any]], config: dict) -> Optional[Dict[str, Any]]:
    if not catalysts or not ai_fallback_available(config):
        return None

    catalyst_lines = "\n".join(
        f"- {item['label']}: {item['title']}"
        for item in catalysts[:6]
    )
    prompt = (
        "You are a concise trading alert analyst. Do not give buy/sell instructions. "
        "Analyze the likely reason and market impact of this stock move for a trader.\n"
        "Reply ONLY as JSON with keys: impact_score (1-5 integer), direction (긍정/부정/중립/불명), "
        "cause_summary (Korean, max 160 chars), confidence (높음/중간/낮음), risk_note (Korean, max 120 chars).\n\n"
        f"Ticker: {price_info.get('ticker')}\n"
        f"Move: {float(price_info.get('change_pct', 0.0) or 0.0):.2f}%\n"
        f"Market state: {price_info.get('market_state')}\n"
        f"Volume ratio: {price_info.get('volume_ratio')}\n"
        f"Catalysts:\n{catalyst_lines}"
    )
    raw = ai_generate_with_fallback(prompt, config, purpose=f"{price_info.get('ticker')} 가격 원인 분석")
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
    except Exception:
        return None
    result: Dict[str, Any] = {}
    try:
        result["impact_score"] = max(1, min(5, int(parsed.get("impact_score", 1))))
    except Exception:
        result["impact_score"] = 1
    result["direction"] = str(parsed.get("direction") or "중립/불명")[:20]
    result["cause_summary"] = str(parsed.get("cause_summary") or "")[:220]
    result["confidence"] = str(parsed.get("confidence") or "중간")[:20]
    result["risk_note"] = str(parsed.get("risk_note") or "")[:160]
    return result


def analyze_price_move(price_info: Dict[str, Any], config: dict) -> Dict[str, Any]:
    ticker = str(price_info.get("ticker") or "").upper()
    catalysts = collect_recent_catalysts(ticker, config)
    local = _local_signal(price_info, catalysts)
    ai = _ai_signal(price_info, catalysts, config)
    if ai:
        local.update({key: value for key, value in ai.items() if value})
    local["catalysts"] = catalysts
    return local


def analyze_news_signal(news_item: Dict[str, Any], price_info: Dict[str, Any], config: dict) -> Dict[str, Any]:
    ticker = str(news_item.get("ticker") or price_info.get("ticker") or "").upper()
    now = _now()
    catalysts: List[Dict[str, Any]] = []
    _append_item(
        catalysts,
        news_item,
        "news",
        "뉴스",
        now,
        int(config.get("news_max_age_hours", config.get("price_catalyst_lookback_hours", 24)) or 24),
    )
    enriched_price_info = dict(price_info)
    enriched_price_info["ticker"] = ticker
    local = _local_signal(enriched_price_info, catalysts)
    ai = _ai_signal(enriched_price_info, catalysts, config)
    if ai:
        local.update({key: value for key, value in ai.items() if value})
    local["catalysts"] = catalysts
    return local
