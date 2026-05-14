"""
sec_fetcher.py - SEC EDGAR 공시(8-K, 10-K, 10-Q 등) 가져오기

SEC EDGAR JSON API를 사용합니다 (API 키 불필요).
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_CIK_CACHE_FILE = Path("ticker_cik_cache.json")
_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_HEADERS = {"User-Agent": "StockAlarm/1.0 stockalarm@example.com"}

# 메모리 캐시 (프로세스 재시작 시 초기화됨)
_cik_map: Dict[str, str] = {}


def _load_cik_map() -> Dict[str, str]:
    """SEC EDGAR 티커→CIK 매핑을 파일 캐시 또는 SEC 사이트에서 로드합니다."""
    global _cik_map
    if _cik_map:
        return _cik_map

    # 파일 캐시 확인
    if _CIK_CACHE_FILE.exists():
        try:
            with open(_CIK_CACHE_FILE, "r", encoding="utf-8") as f:
                _cik_map = json.load(f)
            logger.info("CIK 맵 캐시 로드 완료 (%d개)", len(_cik_map))
            return _cik_map
        except Exception as e:
            logger.warning("CIK 캐시 파일 읽기 실패: %s", e)

    # SEC에서 다운로드
    logger.info("SEC EDGAR에서 CIK 맵 다운로드 중...")
    try:
        resp = requests.get(_SEC_TICKERS_URL, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        _cik_map = {
            v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()
        }
        with open(_CIK_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cik_map, f)
        logger.info("CIK 맵 다운로드 완료 (%d개)", len(_cik_map))
        return _cik_map
    except Exception as e:
        logger.error("SEC CIK 맵 다운로드 실패: %s", e)
        return {}


def _get_cik(ticker: str) -> Optional[str]:
    return _load_cik_map().get(ticker.upper())


def fetch_sec_filings(ticker: str, form_types: List[str]) -> List[Dict[str, Any]]:
    """
    SEC EDGAR에서 해당 티커의 최신 공시를 가져옵니다.

    Args:
        ticker:     주식 티커 (예: 'AAPL')
        form_types: 가져올 공시 종류 (예: ['8-K', '10-K', '10-Q'])

    Returns:
        각 공시 항목은 아래 키를 포함하는 dict:
          id, title, link, publisher, publish_time, source, ticker,
          form_type, filing_date, description
    """
    cik = _get_cik(ticker)
    if not cik:
        logger.warning("[%s] CIK를 찾을 수 없음 — SEC 공시 건너뜀", ticker)
        return []

    cik_int = int(cik)
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()

        recent = data.get("filings", {}).get("recent", {})
        accession_numbers = recent.get("accessionNumber", [])
        forms = recent.get("form", [])
        filing_dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])
        descriptions = recent.get("primaryDocDescription", [])

        result = []
        for i, form in enumerate(forms):
            if form not in form_types:
                continue

            accession = accession_numbers[i] if i < len(accession_numbers) else ""
            filing_date = filing_dates[i] if i < len(filing_dates) else ""
            primary_doc = primary_docs[i] if i < len(primary_docs) else ""
            description = descriptions[i] if i < len(descriptions) else ""

            if not accession:
                continue

            accession_clean = accession.replace("-", "")
            if primary_doc:
                filing_link = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik_int}/{accession_clean}/{primary_doc}"
                )
            else:
                filing_link = (
                    f"https://www.sec.gov/cgi-bin/browse-edgar"
                    f"?action=getcompany&CIK={cik}&type={form}"
                    f"&dateb=&owner=include&count=5"
                )

            try:
                ts = int(datetime.strptime(filing_date, "%Y-%m-%d").timestamp())
            except Exception:
                ts = 0

            result.append(
                {
                    "id": accession,
                    "title": f"[SEC {form}] {ticker} {description or form} ({filing_date})",
                    "link": filing_link,
                    "publisher": "SEC EDGAR",
                    "publish_time": ts,
                    "source": "SEC EDGAR",
                    "ticker": ticker.upper(),
                    "form_type": form,
                    "filing_date": filing_date,
                    "description": description,
                }
            )

            # 최신 20건만 반환
            if len(result) >= 20:
                break

        # ── 날짜 필터: sec_max_age_days 이상 지난 공시 제외 ──────────────────
        return result


def filter_sec_by_age(items: List[Dict[str, Any]], max_age_days: int) -> List[Dict[str, Any]]:
    """max_age_days보다 오래된 SEC 공시를 제외합니다."""
    if max_age_days <= 0:
        return items
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    filtered = [item for item in items if item.get("filing_date", "") >= cutoff]
    skipped = len(items) - len(filtered)
    if skipped > 0:
        logger.debug("SEC 날짜 필터: %d건 제외 (%d일 이상 경과)", skipped, max_age_days)
    return filtered

    except Exception as e:
        logger.error("[%s] SEC EDGAR 공시 가져오기 실패: %s", ticker, e)
        return []
