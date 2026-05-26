"""
price_fetcher.py - Yahoo Finance에서 주가 및 실적 일정 수집

API 키 불필요.
"""
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StockAlarmBot/1.0)"}


def _derive_market_state(meta: Dict[str, Any], now_ts: Optional[int] = None) -> str:
    """Yahoo chart meta.currentTradingPeriod로 장 상태를 직접 계산합니다.

    Yahoo chart API는 meta.marketState를 None/UNKNOWN으로 주는 경우가 많습니다.
    대신 currentTradingPeriod의 pre/regular/post start/end Unix timestamp를 보고
    현재 시간이 어느 구간에 속하는지 계산합니다.
    """
    now_ts = int(now_ts or time.time())
    periods = meta.get("currentTradingPeriod") or {}

    for key, label in (("pre", "PRE"), ("regular", "REGULAR"), ("post", "POST")):
        period = periods.get(key) or {}
        start = period.get("start")
        end = period.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            if int(start) <= now_ts < int(end):
                return label

    return "CLOSED"


def _latest_chart_price(result: Dict[str, Any]) -> Tuple[Optional[float], Optional[int]]:
    """1분봉 chart 데이터에서 가장 최근 non-null close와 timestamp를 반환합니다.

    includePrePost=true일 때 meta.regularMarketPrice는 정규장 마지막 가격일 수 있어
    프리장/애프터장 현재가로 부정확할 수 있습니다. 따라서 quote.close 배열의
    마지막 유효값을 우선 사용합니다.
    """
    timestamps = result.get("timestamp") or []
    quote_list = result.get("indicators", {}).get("quote", [])
    if not quote_list:
        return None, None

    closes = quote_list[0].get("close") or []
    for idx in range(len(closes) - 1, -1, -1):
        close = closes[idx]
        if close is None:
            continue
        try:
            price = float(close)
        except (TypeError, ValueError):
            continue
        ts = timestamps[idx] if idx < len(timestamps) else None
        return price, int(ts) if isinstance(ts, (int, float)) else None

    return None, None


def fetch_price(ticker: str) -> Optional[Dict[str, Any]]:
    """Yahoo Finance에서 현재 주가 정보를 가져옵니다.

    반환 키:
        ticker, name, price, prev_close, change, change_pct, currency,
        timestamp, market_state
    """
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?range=1d&interval=1m&includePrePost=true"
    )
    try:
        resp = requests.get(url, timeout=10, headers=_HEADERS)
        if resp.status_code != 200:
            logger.warning("[%s] Yahoo chart 응답 오류: HTTP %s", ticker, resp.status_code)
            return None

        results = resp.json().get("chart", {}).get("result", [])
        if not results:
            logger.warning("[%s] Yahoo chart 결과 없음", ticker)
            return None

        result = results[0]
        meta = result.get("meta", {})

        latest_price, latest_ts = _latest_chart_price(result)
        meta_price = meta.get("regularMarketPrice")
        price = latest_price if latest_price is not None else meta_price
        if price is None:
            logger.warning("[%s] chart close/regularMarketPrice 없음", ticker)
            return None

        prev_close = (
            meta.get("chartPreviousClose")
            or meta.get("regularMarketPreviousClose")
            or meta.get("previousClose")
        )
        change = (price - prev_close) if prev_close else 0.0
        change_pct = (change / prev_close * 100) if prev_close else 0.0

        raw_market_state = meta.get("marketState")
        market_state = raw_market_state or _derive_market_state(meta)
        timestamp = latest_ts or meta.get("regularMarketTime") or int(time.time())

        if latest_price is not None and meta_price is not None:
            logger.debug(
                "[%s] price source=chart_close %.4f (regularMarketPrice %.4f)",
                ticker, price, float(meta_price),
            )

        return {
            "ticker": ticker.upper(),
            "name": meta.get("longName") or meta.get("shortName", ticker),
            "price": price,
            "prev_close": prev_close or 0.0,
            "change": change,
            "change_pct": change_pct,
            "currency": meta.get("currency", "USD"),
            "timestamp": timestamp,
            "market_state": market_state,  # PRE / REGULAR / POST / CLOSED / UNKNOWN
            "raw_market_state": raw_market_state,
            "price_source": "chart_close" if latest_price is not None else "regularMarketPrice",
            "regular_market_price": meta_price,
        }
    except Exception as e:
        logger.error("[%s] 주가 조회 실패: %s", ticker, e)
        return None


def fetch_earnings(ticker: str) -> Optional[Dict[str, Any]]:
    """Yahoo Finance에서 실적 발표 일정을 가져옵니다.

    반환 키:
        ticker, earnings_dates (unix ts 리스트), earnings_avg/low/high (EPS),
        revenue_avg, exdividend_date, dividend_date
    """
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
        f"?modules=calendarEvents"
    )
    try:
        resp = requests.get(url, timeout=8, headers=_HEADERS)
        if resp.status_code != 200:
            return None
        qs = resp.json().get("quoteSummary", {})
        results: List[dict] = qs.get("result") or []
        if not results:
            return None
        calendar = results[0].get("calendarEvents", {})
        earnings = calendar.get("earnings", {})

        raw_dates = earnings.get("earningsDate", [])
        earnings_dates = [d.get("raw") for d in raw_dates if d.get("raw")]

        return {
            "ticker": ticker.upper(),
            "earnings_dates": earnings_dates,
            "earnings_avg": earnings.get("earningsAverage", {}).get("fmt"),
            "earnings_low": earnings.get("earningsLow", {}).get("fmt"),
            "earnings_high": earnings.get("earningsHigh", {}).get("fmt"),
            "revenue_avg": earnings.get("revenueAverage", {}).get("fmt"),
            "exdividend_date": calendar.get("exDividendDate", {}).get("raw"),
            "dividend_date": calendar.get("dividendDate", {}).get("raw"),
        }
    except Exception as e:
        logger.error("[%s] 실적 발표 일정 조회 실패: %s", ticker, e)
        return None
