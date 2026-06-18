"""Helpers for finding official Twitter/X accounts for stock tickers."""
import logging
from typing import List, Optional

from ai_provider import ai_generate_with_fallback
from app_state import DEFAULT_GEMINI_MODEL


logger = logging.getLogger(__name__)


def ai_find_twitter_accounts(
    ticker: str,
    gemini_api_key: str,
    gemini_model: str = DEFAULT_GEMINI_MODEL,
    config: Optional[dict] = None,
) -> Optional[List[str]]:
    """GPT OAuth 우선, Gemini fallback으로 티커의 공식 Twitter/X 계정을 찾습니다."""
    effective_config = dict(config or {})
    if gemini_api_key and not effective_config.get("gemini_api_key"):
        effective_config["gemini_api_key"] = gemini_api_key
    effective_config["gemini_request_model"] = gemini_model

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
    text = ai_generate_with_fallback(
        prompt,
        effective_config,
        purpose=f"{ticker} 트위터 계정 탐색",
    )
    logger.info("[AI] %s 트위터 계정 응답: %s", ticker, text)
    if not text or text.strip().upper() == "NONE":
        return None

    accounts = [account.strip().lstrip("@") for account in text.split(",") if account.strip()]
    accounts = [account for account in accounts if account and account.upper() != "NONE"]
    logger.info("[AI] %s 트위터 계정 파싱 결과: %s", ticker, accounts)
    return accounts[:3] if accounts else None


gemini_find_twitter_accounts = ai_find_twitter_accounts
_gemini_find_twitter_accounts = ai_find_twitter_accounts