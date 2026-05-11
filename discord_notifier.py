"""
discord_notifier.py - Discord 웹훅으로 알람 전송
"""
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Embed 색상 (10진수 RGB)
_COLOR_NEWS = 3447003      # 파랑
_COLOR_SEC_8K = 15105570   # 주황
_COLOR_SEC_10K = 5763719   # 초록
_COLOR_SEC_10Q = 16776960  # 노랑
_COLOR_DEFAULT = 9807270   # 회색

_SEC_COLORS: Dict[str, int] = {
    "8-K": _COLOR_SEC_8K,
    "10-K": _COLOR_SEC_10K,
    "10-Q": _COLOR_SEC_10Q,
}


def _fmt_local(unix_ts: int) -> str:
    if not unix_ts:
        return "N/A"
    try:
        return datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "N/A"


def _fmt_iso_utc(unix_ts: int) -> str:
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _post(webhook_url: str, payload: Dict) -> bool:
    """Discord 웹훅으로 POST 요청을 보냅니다. Rate-limit 자동 재시도."""
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)

        # Rate limit 처리
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1.0)
            logger.warning("Discord Rate Limit — %.1f초 후 재시도", retry_after)
            time.sleep(float(retry_after) + 0.1)
            resp = requests.post(webhook_url, json=payload, timeout=10)

        resp.raise_for_status()
        return True

    except requests.exceptions.RequestException as e:
        logger.error("Discord 웹훅 전송 실패: %s", e)
        return False


def send_news_alert(webhook_url: str, item: Dict[str, Any]) -> bool:
    """Yahoo Finance 뉴스 알람을 Discord로 전송합니다."""
    ticker = item.get("ticker", "")
    title = item.get("title", "제목 없음")[:250]
    link: Optional[str] = item.get("link") or None
    publisher = item.get("publisher", "Unknown")
    publish_time = item.get("publish_time", 0)

    embed: Dict[str, Any] = {
        "title": f"📰  [{ticker}]  {title}",
        "color": _COLOR_NEWS,
        "description": (
            f"**출처:** {publisher}\n"
            f"**시간:** {_fmt_local(publish_time)}"
        ),
        "footer": {"text": f"Stock News Alert  •  {ticker}"},
        "timestamp": _fmt_iso_utc(publish_time),
    }
    if link:
        embed["url"] = link

    payload = {
        "username": "주식 뉴스 봇 📈",
        "embeds": [embed],
    }
    return _post(webhook_url, payload)


def send_sec_alert(webhook_url: str, item: Dict[str, Any]) -> bool:
    """SEC EDGAR 공시 알람을 Discord로 전송합니다."""
    ticker = item.get("ticker", "")
    form_type = item.get("form_type", "")
    filing_date = item.get("filing_date", "")
    description = item.get("description", "")
    link: Optional[str] = item.get("link") or None
    publish_time = item.get("publish_time", 0)

    color = _SEC_COLORS.get(form_type, _COLOR_DEFAULT)

    desc_lines = []
    if description:
        desc_lines.append(f"**내용:** {description}")
    desc_lines.append(f"**공시 양식:** {form_type}")
    desc_lines.append(f"**공시일:** {filing_date}")

    embed: Dict[str, Any] = {
        "title": f"🏛️  [{ticker}]  SEC {form_type} 공시",
        "color": color,
        "description": "\n".join(desc_lines),
        "footer": {"text": f"SEC EDGAR  •  {ticker}"},
        "timestamp": _fmt_iso_utc(publish_time),
    }
    if link:
        embed["url"] = link

    payload = {
        "username": "주식 뉴스 봇 📈",
        "embeds": [embed],
    }
    return _post(webhook_url, payload)
