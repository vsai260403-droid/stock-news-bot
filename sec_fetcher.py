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


# 8-K 항목 코드 → 설명 매핑
_8K_ITEMS = {
    "1.01": "중요 계약 체결",
    "1.02": "중요 계약 종료",
    "1.03": "파산/회생 절차",
    "1.04": "광산 안전 공시",
    "1.05": "중요 사이버보안 사고",
    "2.01": "자산 취득/처분",
    "2.02": "실적 발표 (매출/이익)",
    "2.03": "직접 금융 의무 발생",
    "2.04": "트리거 이벤트 (채무불이행 등)",
    "2.05": "임원 해고/이탈",
    "2.06": "자산 손상",
    "3.01": "상장폐지 통보",
    "3.02": "주식 미등록 판매",
    "3.03": "기존 주주 권리 변경",
    "4.01": "회계법인 변경",
    "4.02": "재무제표 재작성",
    "5.01": "지배주주 변경",
    "5.02": "임원 선임/사임/보상",
    "5.03": "정관 변경",
    "5.04": "임시주주총회 결의",
    "5.05": "주주총회 결의/이사회 결의",
    "5.06": "Shell company 변경",
    "5.07": "주주총회 결과",
    "5.08": "이사회 구성 변경",
    "6.01": "자산담보증권 손실",
    "7.01": "기타 공시 사항 (자발적)",
    "7.02": "재무제표 및 부속서류",
    "8.01": "기타 이벤트",
    "9.01": "재무제표 및 첨부서류",
}


def _describe_8k_items(items_str: str) -> str:
    """8-K 항목 코드 문자열을 사람이 읽기 쉬운 설명으로 변환합니다."""
    if not items_str:
        return ""
    codes = [c.strip() for c in items_str.split(",") if c.strip()]
    labels = [_8K_ITEMS.get(code, f"항목 {code}") for code in codes]
    return ", ".join(labels)


def fetch_filing_text(filing_link: str, max_chars: int = 3000,
                      cik_int: int = 0, accession_clean: str = "") -> str:
    """
    SEC 공시의 실제 본문을 가져옵니다 (AI 요약용).
    8-K의 경우 Exhibit 99.1(보도자료)을 우선 탐색합니다.
    """
    import re as _re

    def _clean_html(html: str) -> str:
        text = _re.sub(r"<[^>]+>", " ", html)
        text = _re.sub(r"[ \t]+", " ", text)
        text = _re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # Exhibit 99.1 탐색 (cik_int + accession_clean 있을 때)
    if cik_int and accession_clean:
        try:
            index_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_int}/{accession_clean}/{accession_clean[:18].replace('', '')}-index.htm"
            )
            # EDGAR 파일 목록 JSON API
            json_url = (
                f"https://data.sec.gov/Archives/edgar/data/"
                f"{cik_int}/{accession_clean}/"
            )
            idx_resp = requests.get(
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                f"&action=getcompany",
                headers=_HEADERS, timeout=10
            )
        except Exception:
            pass

        # 파일 목록 가져오기 — EDGAR 인덱스 JSON
        try:
            idx_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_int}/{accession_clean}/"
            )
            idx_resp = requests.get(idx_url, headers=_HEADERS, timeout=10)
            if idx_resp.status_code == 200:
                # href에서 .htm/.html 파일 목록 추출
                hrefs = _re.findall(r'href="([^"]+\.htm[l]?)"', idx_resp.text, _re.IGNORECASE)
                # Exhibit 99 계열 우선
                exhibit_urls = []
                other_urls = []
                for href in hrefs:
                    name = href.lower()
                    if "ex99" in name or "ex-99" in name or "exhibit99" in name or "press" in name:
                        url = href if href.startswith("http") else f"https://www.sec.gov{href}"
                        exhibit_urls.append(url)
                    elif accession_clean.lower() in name or "8k" in name or "form" in name:
                        url = href if href.startswith("http") else f"https://www.sec.gov{href}"
                        other_urls.append(url)

                for url in (exhibit_urls or other_urls)[:2]:
                    try:
                        doc_resp = requests.get(url, headers=_HEADERS, timeout=12)
                        if doc_resp.status_code == 200:
                            text = _clean_html(doc_resp.text)
                            if len(text) > 200:  # 의미있는 내용 있을 때만
                                logger.debug("SEC Exhibit 가져오기 성공: %s", url)
                                return text[:max_chars]
                    except Exception:
                        continue
        except Exception as e:
            logger.debug("SEC 인덱스 탐색 실패: %s", e)

    # fallback: 원본 링크 직접 읽기
    try:
        resp = requests.get(filing_link, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        text = _clean_html(resp.text)
        # 본문 시작 지점 찾기
        for keyword in ["ITEM ", "Item ", "PRESS RELEASE", "PURSUANT TO", "AGREEMENT", "WHEREAS"]:
            idx = text.find(keyword)
            if 0 < idx < 3000:
                text = text[idx:]
                break
        return text[:max_chars]
    except Exception as e:
        logger.debug("SEC 문서 본문 가져오기 실패 (%s): %s", filing_link, e)
        return ""


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
        items_list = recent.get("items", [])  # 8-K 이벤트 항목 코드 (예: "2.02,7.01")

        result = []
        for i, form in enumerate(forms):
            if form not in form_types:
                continue

            accession = accession_numbers[i] if i < len(accession_numbers) else ""
            filing_date = filing_dates[i] if i < len(filing_dates) else ""
            primary_doc = primary_docs[i] if i < len(primary_docs) else ""
            description = descriptions[i] if i < len(descriptions) else ""
            items_str = items_list[i] if i < len(items_list) else ""

            if not accession:
                continue

            # 8-K 항목 코드를 사람이 읽기 쉬운 설명으로 변환
            if items_str:
                item_labels = _describe_8k_items(items_str)
                if item_labels:
                    description = item_labels

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
                    # AI 요약용 내부 필드
                    "_cik_int": cik_int,
                    "_accession_clean": accession_clean,
                }
            )

            # 최신 20건만 반환
            if len(result) >= 20:
                break

        return result

    except Exception as e:
        logger.error("[%s] SEC EDGAR 공시 가져오기 실패: %s", ticker, e)
        return []


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
