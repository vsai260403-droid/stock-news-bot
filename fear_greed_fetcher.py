"""CNN Fear & Greed Index fetcher."""
import logging
from typing import Any, Dict, Optional

import requests


logger = logging.getLogger(__name__)

FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FEAR_GREED_PAGE_URL = "https://edition.cnn.com/markets/fear-and-greed"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://edition.cnn.com",
    "Referer": FEAR_GREED_PAGE_URL,
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

_RATING_KO = {
    "extreme fear": "극단적 공포",
    "fear": "공포",
    "neutral": "중립",
    "greed": "탐욕",
    "extreme greed": "극단적 탐욕",
}


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _delta(score: Optional[float], previous: Optional[float]) -> Optional[float]:
    if score is None or previous is None:
        return None
    return score - previous


def _commentary(score: Optional[float], rating: str) -> str:
    if score is None:
        return "시장 심리 데이터를 해석할 수 없습니다."
    if score <= 25:
        return "공포가 매우 강합니다. 반등 후보를 보더라도 변동성과 추가 하락 리스크를 같이 확인하세요."
    if score <= 45:
        return "공포 구간입니다. 무리한 추격보다 지지선 회복과 거래량 확인이 중요합니다."
    if score < 55:
        return "중립 구간입니다. 개별 종목 촉매와 차트 신호의 비중을 더 크게 봐도 좋습니다."
    if score < 75:
        return "탐욕 구간입니다. 돌파 매매는 가능하지만 과열 신호와 익절 구간을 같이 보세요."
    return "극단적 탐욕 구간입니다. 좋은 차트라도 추격 진입 리스크와 급반전 가능성을 경계하세요."


def fetch_fear_greed_index(timeout: int = 15) -> Optional[Dict[str, Any]]:
    try:
        response = requests.get(FEAR_GREED_URL, timeout=timeout, headers=_HEADERS)
        if response.status_code != 200:
            logger.warning("CNN Fear & Greed HTTP %s: %s", response.status_code, response.text[:120])
            return None
        payload = response.json()
        current = payload.get("fear_and_greed") or {}
        score = _to_float(current.get("score"))
        rating = str(current.get("rating") or "").strip().lower()
        previous_close = _to_float(current.get("previous_close"))
        previous_week = _to_float(current.get("previous_1_week"))
        previous_month = _to_float(current.get("previous_1_month"))
        previous_year = _to_float(current.get("previous_1_year"))
        if score is None:
            return None
        return {
            "score": score,
            "rating": rating,
            "rating_ko": _RATING_KO.get(rating, rating or "N/A"),
            "timestamp": str(current.get("timestamp") or ""),
            "previous_close": previous_close,
            "previous_1_week": previous_week,
            "previous_1_month": previous_month,
            "previous_1_year": previous_year,
            "delta_close": _delta(score, previous_close),
            "delta_week": _delta(score, previous_week),
            "delta_month": _delta(score, previous_month),
            "delta_year": _delta(score, previous_year),
            "commentary": _commentary(score, rating),
            "source_url": FEAR_GREED_PAGE_URL,
        }
    except Exception as e:
        logger.warning("CNN Fear & Greed 조회 실패: %s", e)
        return None