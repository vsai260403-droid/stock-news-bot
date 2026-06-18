"""Helpers for finding official Twitter/X accounts for stock tickers."""
import logging
from typing import List, Optional

from app_state import DEFAULT_GEMINI_MODEL


logger = logging.getLogger(__name__)


def gemini_find_twitter_accounts(
    ticker: str,
    gemini_api_key: str,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
) -> Optional[List[str]]:
    """Gemini에게 티커의 공식 트위터 계정을 물어봅니다."""
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("Gemini 트위터 탐색 실패: openai 라이브러리 미설치")
        return None

    try:
        client = OpenAI(
            api_key=gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            max_retries=0,
        )
        prompt = (
            f"주식 티커 '{ticker}'의 공식 트위터(X) 계정 사용자명(username)을 알려주세요.\n"
            f"주식 티커 '{ticker}'의 미국 상장회사 공식 X 계정을 찾아라. "
            "암호화폐/코인/블록체인 프로젝트 계정은 제외하라. "
            "가능하면 회사명, 거래소, 산업을 기준으로 판단하라.\n"
            "회사 공식 계정과 CEO/창립자/주요 임원의 개인 계정을 포함해서 최대 3개까지만 알려주세요.\n"
            "반드시 아래 형식으로만 답하세요 (설명 없이 콤마로 구분된 username만):\n"
            "username1,username2,username3\n\n"
            "존재하지 않거나 모르면 NONE 이라고만 답하세요."
        )
        logger.info("[Gemini] %s 트위터 계정 탐색 요청: model=%s", ticker, gemini_model)
        response = client.chat.completions.create(
            model=gemini_model,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content.strip()
        logger.info("[Gemini] %s 응답: %s", ticker, text)
        if text.upper() == "NONE" or not text:
            logger.info("[Gemini] %s 트위터 계정 없음 (NONE 응답)", ticker)
            return None
        accounts = [account.strip().lstrip("@") for account in text.split(",") if account.strip()]
        logger.info("[Gemini] %s 파싱 결과: %s", ticker, accounts)
        return accounts if accounts else None
    except Exception as e:
        logger.warning("Gemini 트위터 계정 탐색 실패 [%s, model=%s]: %s", ticker, gemini_model, e)
        return None


_gemini_find_twitter_accounts = gemini_find_twitter_accounts