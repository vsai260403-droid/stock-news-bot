"""
AI provider 공통 모듈

우선순위:
1. ChatGPT/Codex OAuth(auth.json)
2. Gemini OpenAI-compatible API
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
DEFAULT_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _decode_jwt(token: str) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except Exception:
        return {}


def _derive_account_id(id_token: str) -> str:
    claims = _decode_jwt(id_token)
    auth = claims.get("https://api.openai.com/auth", {})
    if isinstance(auth, dict):
        return str(auth.get("chatgpt_account_id") or "")
    return ""


def _token_expiring(access_token: str, margin_seconds: int = 300) -> bool:
    claims = _decode_jwt(access_token)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return int(exp) <= int(time.time()) + margin_seconds


def _read_auth(path: str) -> Dict[str, Any]:
    auth_path = Path(os.path.expanduser(path))
    if not auth_path.exists():
        raise FileNotFoundError(f"auth.json 없음: {auth_path}")

    with auth_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("auth.json 형식이 올바르지 않음")

    return data


def _write_auth(path: str, data: Dict[str, Any]) -> None:
    auth_path = Path(os.path.expanduser(path))
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = auth_path.with_suffix(".tmp")

    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    os.chmod(temp_path, 0o600)
    temp_path.replace(auth_path)


def _refresh_tokens(
    auth_path: str,
    auth_data: Dict[str, Any],
    token_url: str,
    client_id: str,
    timeout: float,
) -> Dict[str, Any]:
    tokens = auth_data.get("tokens") or {}
    refresh_token = tokens.get("refresh_token")

    if not refresh_token:
        raise RuntimeError("refresh_token 없음")

    logger.info("[AI/OAuth] access token 갱신 시도")

    response = requests.post(
        token_url,
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "scope": "openid profile email offline_access",
        },
        timeout=timeout,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"OAuth 토큰 갱신 HTTP {response.status_code}: {response.text[:500]}"
        )

    payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError("OAuth 갱신 응답에 access_token 없음")

    tokens["access_token"] = access_token
    tokens["refresh_token"] = payload.get("refresh_token") or refresh_token

    if payload.get("id_token"):
        tokens["id_token"] = payload["id_token"]

    if not tokens.get("account_id") and tokens.get("id_token"):
        tokens["account_id"] = _derive_account_id(tokens["id_token"])

    auth_data["tokens"] = tokens
    auth_data["last_refresh"] = datetime.now(timezone.utc).isoformat()
    _write_auth(auth_path, auth_data)

    logger.info("[AI/OAuth] access token 갱신 성공")
    return auth_data


def _load_oauth_tokens(config: dict) -> Dict[str, str]:
    auth_path = str(
        config.get("openai_oauth_auth_file")
        or DEFAULT_AUTH_PATH
    )
    timeout = float(config.get("openai_oauth_timeout_sec", 45) or 45)
    token_url = str(
        config.get("openai_oauth_token_url")
        or DEFAULT_TOKEN_URL
    )
    client_id = str(
        config.get("openai_oauth_client_id")
        or DEFAULT_CLIENT_ID
    )

    auth_data = _read_auth(auth_path)
    tokens = auth_data.get("tokens") or {}

    access_token = str(tokens.get("access_token") or "")
    refresh_token = str(tokens.get("refresh_token") or "")

    if not access_token or (_token_expiring(access_token) and refresh_token):
        auth_data = _refresh_tokens(
            auth_path,
            auth_data,
            token_url,
            client_id,
            timeout,
        )
        tokens = auth_data.get("tokens") or {}
        access_token = str(tokens.get("access_token") or "")

    id_token = str(tokens.get("id_token") or "")
    account_id = str(tokens.get("account_id") or "")

    if not account_id and id_token:
        account_id = _derive_account_id(id_token)

    if not access_token:
        raise RuntimeError("auth.json에 access_token 없음")
    if not account_id:
        raise RuntimeError("auth.json에 account_id 없음")

    return {
        "access_token": access_token,
        "account_id": account_id,
    }


def _extract_completed_text(event: Dict[str, Any]) -> str:
    response = event.get("response")
    if not isinstance(response, dict):
        return ""

    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text.strip()

    parts = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)

    return "".join(parts).strip()


def chatgpt_oauth_generate(prompt: str, config: dict) -> str:
    tokens = _load_oauth_tokens(config)

    model = str(
        config.get("openai_oauth_model")
        or "gpt-5.4"
    )
    endpoint = str(
        config.get("openai_oauth_endpoint")
        or DEFAULT_CODEX_URL
    )
    timeout = float(config.get("openai_oauth_timeout_sec", 45) or 45)

    payload = {
        "model": model,
        "instructions": "",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt,
                    }
                ],
            }
        ],
        "store": False,
        "stream": True,
    }

    headers = {
        "Authorization": f"Bearer {tokens['access_token']}",
        "chatgpt-account-id": tokens["account_id"],
        "OpenAI-Beta": "responses=experimental",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "codex_cli_rs/0.111.0",
    }

    logger.info("[AI/OAuth] 호출: model=%s", model)

    response = requests.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=timeout,
        stream=True,
    )

    if response.status_code >= 400:
        body = response.text[:1000]
        raise RuntimeError(f"OAuth HTTP {response.status_code}: {body}")

    deltas = []
    completed_text = ""

    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue

        line = raw_line.strip()
        if not line.startswith("data:"):
            continue

        data_text = line[5:].strip()
        if not data_text or data_text == "[DONE]":
            continue

        try:
            event = json.loads(data_text)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")

        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)

        elif event_type in ("response.completed", "response.done"):
            completed_text = _extract_completed_text(event)

        elif event_type in ("response.failed", "error"):
            raise RuntimeError(f"OAuth upstream 오류: {data_text[:1000]}")

    text = "".join(deltas).strip() or completed_text.strip()
    if not text:
        raise RuntimeError("OAuth 응답 본문이 비어 있음")

    logger.info("[AI/OAuth] 성공: model=%s, chars=%d", model, len(text))
    return text


def gemini_generate(prompt: str, config: dict) -> str:
    api_key = str(config.get("gemini_api_key") or "").strip()
    model = str(
        config.get("gemini_summary_model")
        or config.get("gemini_model")
        or "gemini-3.1-flash-lite"
    ).strip()

    if not api_key:
        raise RuntimeError("Gemini API key 없음")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai 패키지 없음") from exc

    logger.info("[AI/Gemini] 호출: model=%s", model)

    client = OpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        max_retries=0,
        timeout=float(config.get("gemini_timeout_sec", 30) or 30),
    )

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )

    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("Gemini 응답 본문이 비어 있음")

    logger.info("[AI/Gemini] 성공: model=%s, chars=%d", model, len(text))
    return text


def ai_generate_with_fallback(
    prompt: str,
    config: dict,
    purpose: str = "요청",
) -> Optional[str]:
    providers = config.get(
        "ai_provider_order",
        ["openai_oauth", "gemini"],
    )

    if not isinstance(providers, list):
        providers = ["openai_oauth", "gemini"]

    last_error: Optional[Exception] = None

    for provider in providers:
        provider = str(provider).strip().lower()

        try:
            if provider == "openai_oauth":
                if not config.get("openai_oauth_enabled", True):
                    logger.info("[AI/%s] OAuth 비활성화 → 건너뜀", purpose)
                    continue

                result = chatgpt_oauth_generate(prompt, config)
                logger.info("[AI/%s] OAuth 사용 성공", purpose)
                return result

            if provider == "gemini":
                result = gemini_generate(prompt, config)
                logger.info("[AI/%s] Gemini fallback 성공", purpose)
                return result

            logger.warning("[AI/%s] 알 수 없는 provider: %s", purpose, provider)

        except Exception as exc:
            last_error = exc
            logger.warning(
                "[AI/%s] %s 실패 → 다음 provider 시도: %s",
                purpose,
                provider,
                exc,
            )

    logger.warning("[AI/%s] 모든 provider 실패: %s", purpose, last_error)
    return None
