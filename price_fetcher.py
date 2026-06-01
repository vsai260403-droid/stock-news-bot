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
    """Yahoo chart meta.currentTradingPeriod로 장 상태를 직접 계산합니다."""
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


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest_chart_price(result: Dict[str, Any]) -> Tuple[Optional[float], Optional[int]]:
    """1분봉 chart 데이터에서 가장 최근 non-null close와 timestamp를 반환합니다."""
    timestamps = result.get("timestamp") or []
    quote_list = result.get("indicators", {}).get("quote", [])
    if not quote_list:
        return None, None

    closes = quote_list[0].get("close") or []
    for idx in range(len(closes) - 1, -1, -1):
        price = _to_float(closes[idx])
        if price is None:
            continue
        ts = timestamps[idx] if idx < len(timestamps) else None
        return price, int(ts) if isinstance(ts, (int, float)) else None

    return None, None


def _select_price_and_base(
    ticker: str,
    market_state: str,
    meta: Dict[str, Any],
    latest_price: Optional[float],
) -> Tuple[Optional[float], Optional[float], str, str]:
    """장 상태별 현재가와 기준가를 선택합니다.

    PRE/POST에서는 chartPreviousClose가 전전일 종가처럼 들어오는 경우가 있어
    기준가로 쓰면 잘못된 급등락 알림이 발생합니다.
    """
    regular_price = _to_float(meta.get("regularMarketPrice"))
    pre_price = _to_float(meta.get("preMarketPrice"))
    post_price = _to_float(meta.get("postMarketPrice"))
    chart_prev = _to_float(meta.get("chartPreviousClose"))
    regular_prev = _to_float(meta.get("regularMarketPreviousClose"))
    previous_close = _to_float(meta.get("previousClose"))

    if market_state == "PRE":
        price = pre_price or latest_price or regular_price
        prev_close = regular_price or chart_prev or regular_prev or previous_close
        price_source = "preMarketPrice" if pre_price is not None else ("chart_close" if latest_price is not None else "regularMarketPrice")
        prev_source = "regularMarketPrice" if regular_price is not None else ("chartPreviousClose" if chart_prev is not None else "previousClose")
    elif market_state == "POST":
        price = post_price or latest_price or regular_price
        prev_close = regular_price or chart_prev or regular_prev or previous_close
        price_source = "postMarketPrice" if post_price is not None else ("chart_close" if latest_price is not None else "regularMarketPrice")
        prev_source = "regularMarketPrice" if regular_price is not None else ("chartPreviousClose" if chart_prev is not None else "previousClose")
    elif market_state == "REGULAR":
        price = regular_price or latest_price
        prev_close = chart_prev or regular_prev or previous_close
        price_source = "regularMarketPrice" if regular_price is not None else "chart_close"
        prev_source = "chartPreviousClose" if chart_prev is not None else ("regularMarketPreviousClose" if regular_prev is not None else "previousClose")
    else:
        price = latest_price or regular_price or pre_price or post_price
        prev_close = chart_prev or regular_prev or previous_close or regular_price
        price_source = "chart_close" if latest_price is not None else "regularMarketPrice"
        prev_source = "chartPreviousClose" if chart_prev is not None else ("regularMarketPreviousClose" if regular_prev is not None else "previousClose")

    if market_state in ("PRE", "POST") and regular_price is not None and chart_prev is not None:
        diff_pct = abs(regular_price - chart_prev) / chart_prev * 100 if chart_prev else 0.0
        if diff_pct >= 1.0:
            logger.info(
                "[%s] PRE/POST 기준가 보정: regularMarketPrice=%.4f 사용, chartPreviousClose=%.4f 무시 (diff=%.2f%%)",
                ticker,
                regular_price,
                chart_prev,
                diff_pct,
            )

    return price, prev_close, price_source, prev_source


def fetch_price(ticker: str) -> Optional[Dict[str, Any]]:
    """Yahoo Finance에서 현재 주가 정보를 가져옵니다."""
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
        raw_market_state = meta.get("marketState")
        market_state = raw_market_state or _derive_market_state(meta)
        latest_price, latest_ts = _latest_chart_price(result)

        price, prev_close, price_source, prev_source = _select_price_and_base(
            ticker.upper(), market_state, meta, latest_price
        )
        if price is None:
            logger.warning("[%s] 가격 필드 없음", ticker)
            return None
        if not prev_close:
            logger.warning("[%s] 기준가(prev_close) 필드 없음", ticker)
            return None

        change = price - prev_close
        change_pct = change / prev_close * 100
        timestamp = latest_ts or meta.get("regularMarketTime") or int(time.time())

        logger.info(
            "[%s] price debug: market=%s, price=%.4f(%s), prev_close=%.4f(%s), change=%.2f%%, "
            "regularMarketPrice=%s, preMarketPrice=%s, postMarketPrice=%s, chartPreviousClose=%s",
            ticker.upper(),
            market_state,
            price,
            price_source,
            prev_close,
            prev_source,
            change_pct,
            meta.get("regularMarketPrice"),
            meta.get("preMarketPrice"),
            meta.get("postMarketPrice"),
            meta.get("chartPreviousClose"),
        )

        return {
            "ticker": ticker.upper(),
            "name": meta.get("longName") or meta.get("shortName", ticker),
            "price": price,
            "prev_close": prev_close,
            "change": change,
            "change_pct": change_pct,
            "currency": meta.get("currency", "USD"),
            "timestamp": timestamp,
            "market_state": market_state,
            "raw_market_state": raw_market_state,
            "price_source": price_source,
            "prev_close_source": prev_source,
            "regular_market_price": _to_float(meta.get("regularMarketPrice")),
            "pre_market_price": _to_float(meta.get("preMarketPrice")),
            "post_market_price": _to_float(meta.get("postMarketPrice")),
            "chart_previous_close": _to_float(meta.get("chartPreviousClose")),
        }
    except Exception as e:
        logger.error("[%s] 주가 조회 실패: %s", ticker, e)
        return None


def fetch_earnings(ticker: str) -> Optional[Dict[str, Any]]:
    """Yahoo Finance에서 실적 발표 일정을 가져옵니다."""
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
