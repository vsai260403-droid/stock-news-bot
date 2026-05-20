"""
price_fetcher.py - Yahoo Finance에서 주가 및 실적 일정 수집

API 키 불필요.
"""
import logging
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StockAlarmBot/1.0)"}


def fetch_price(ticker: str) -> Optional[Dict[str, Any]]:
    """Yahoo Finance에서 현재 주가 정보를 가져옵니다.

    반환 키:
        ticker, name, price, prev_close, change, change_pct, currency, timestamp
    """
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1d&range=1d"
    )
    try:
        resp = requests.get(url, timeout=8, headers=_HEADERS)
        if resp.status_code != 200:
            return None
        results = resp.json().get("chart", {}).get("result", [])
        if not results:
            return None
        meta = results[0].get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None:
            return None
        prev_close = (
            meta.get("chartPreviousClose")
            or meta.get("regularMarketPreviousClose")
            or meta.get("previousClose")
        )
        change = (price - prev_close) if prev_close else 0.0
        change_pct = (change / prev_close * 100) if prev_close else 0.0
        return {
            "ticker": ticker.upper(),
            "name": meta.get("longName") or meta.get("shortName", ticker),
            "price": price,
            "prev_close": prev_close or 0.0,
            "change": change,
            "change_pct": change_pct,
            "currency": meta.get("currency", "USD"),
            "timestamp": meta.get("regularMarketTime", int(time.time())),
            "market_state": meta.get("marketState", "UNKNOWN"),  # REGULAR / PRE / POST / CLOSED
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
