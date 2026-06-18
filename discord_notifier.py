"""
discord_notifier.py - Discord 웹훅으로 알람 전송
"""
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# 표시용 시간대: 한국 시간으로 고정
_KST = ZoneInfo("Asia/Seoul")

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
    """본문/필드에 표시할 시간을 KST로 고정합니다."""
    if not unix_ts:
        return "N/A"
    try:
        return datetime.fromtimestamp(unix_ts, tz=_KST).strftime("%Y-%m-%d %H:%M:%S KST")
    except Exception:
        return "N/A"


def _fmt_iso_utc(unix_ts: int) -> str:
    """Discord embed timestamp용 ISO UTC 문자열."""
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _post(webhook_url: str, payload: Dict) -> bool:
    """Discord 웹훅으로 POST 요청을 보냅니다. Rate-limit 자동 재시도."""
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)

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
    ai_summary: Optional[str] = item.get("ai_summary") or None
    summary: str = str(item.get("summary") or "").strip()

    if ai_summary:
        description = (
            f"🤖 **AI 요약 (한국어)**\n{ai_summary}\n\n"
            f"**출처:** {publisher}\n"
            f"**시간:** {_fmt_local(publish_time)}"
        )
    else:
        desc_lines = []
        if summary and summary.lower() != title.lower():
            desc_lines.append(f"**내용:** {summary[:700]}")
        desc_lines.append(f"**출처:** {publisher}")
        desc_lines.append(f"**시간:** {_fmt_local(publish_time)}")
        description = "\n".join(desc_lines)

    fields = []
    if link:
        fields.append({
            "name": "🔗 원문 링크",
            "value": f"[기사 전문 보기]({link})",
            "inline": False,
        })

    embed: Dict[str, Any] = {
        "title": f"📰  [{ticker}]  {title}",
        "color": _COLOR_NEWS,
        "description": description,
        "footer": {"text": f"Stock News Alert  •  {ticker}"},
        "timestamp": _fmt_iso_utc(publish_time),
    }
    if link:
        embed["url"] = link
    if fields:
        embed["fields"] = fields

    payload = {
        "username": "주식 뉴스 봇 📈",
        "embeds": [embed],
    }
    return _post(webhook_url, payload)


def send_tweet_alert(webhook_url: str, item: Dict[str, Any]) -> bool:
    """Twitter/X 트윗 알람을 Discord로 전송합니다."""
    ticker = item.get("ticker", "")
    username = item.get("username", "")
    title = item.get("title", "")[:250]
    text = item.get("text", "")[:500]
    link: Optional[str] = item.get("link") or None
    publish_time = item.get("publish_time", 0)
    ai_summary: Optional[str] = item.get("ai_summary") or None

    tweet_body = text if (text and text != title) else title

    if ai_summary:
        description = f"🤖 **AI 요약 (한국어)**\n{ai_summary}\n\n**원문:** {tweet_body}"
    else:
        description = tweet_body

    embed: Dict[str, Any] = {
        "title": f"🐦  [{ticker}]  @{username}" if ticker else f"🐦  @{username}",
        "color": 1942002,
        "description": description[:1000] if description else "(내용 없음)",
        "fields": [
            {
                "name": "게시 시간",
                "value": _fmt_local(publish_time),
                "inline": True,
            },
            {
                "name": "계정",
                "value": f"[@{username}](https://twitter.com/{username})",
                "inline": True,
            },
        ],
        "footer": {"text": f"Twitter/X Alert  •  {ticker}" if ticker else "Twitter/X Alert"},
        "timestamp": _fmt_iso_utc(publish_time),
    }
    if link:
        embed["url"] = link

    payload = {
        "username": "주식 뉴스 봇 📈",
        "embeds": [embed],
    }
    return _post(webhook_url, payload)


def send_linkedin_alert(webhook_url: str, item: Dict[str, Any]) -> bool:
    """LinkedIn RSS 변환 피드 알람을 Discord로 전송합니다."""
    account = item.get("account", "LinkedIn")
    title = item.get("title", "LinkedIn 업데이트")[:250]
    text = item.get("text", "")[:1000]
    link: Optional[str] = item.get("link") or None
    publish_time = item.get("publish_time", 0)
    ai_summary: Optional[str] = item.get("ai_summary") or None

    if ai_summary:
        description = f"🤖 **AI 요약 (한국어)**\n{ai_summary}\n\n**원문:** {text or title}"
    else:
        description = text or title

    fields = [
        {
            "name": "게시 시간",
            "value": _fmt_local(publish_time),
            "inline": True,
        },
        {
            "name": "페이지",
            "value": account,
            "inline": True,
        },
    ]
    if link:
        fields.append({
            "name": "🔗 원문 링크",
            "value": f"[LinkedIn에서 보기]({link})",
            "inline": False,
        })

    embed: Dict[str, Any] = {
        "title": f"💼  {account}  —  {title}",
        "color": 3447003,
        "description": description[:1200] if description else "(내용 없음)",
        "fields": fields,
        "footer": {"text": "LinkedIn Alert"},
        "timestamp": _fmt_iso_utc(publish_time),
    }
    if link:
        embed["url"] = link

    payload = {
        "username": "주식 뉴스 봇 📈",
        "embeds": [embed],
    }
    return _post(webhook_url, payload)


def send_official_alert(webhook_url: str, item: Dict[str, Any]) -> bool:
    """회사 공식 IR/Newsroom 업데이트 알람을 Discord로 전송합니다."""
    ticker = item.get("ticker", "")
    name = item.get("name", "Official")
    title = item.get("title", "공식 업데이트")[:250]
    summary = str(item.get("summary") or "").strip()
    link: Optional[str] = item.get("link") or None
    publish_time = item.get("publish_time", 0)
    ai_summary: Optional[str] = item.get("ai_summary") or None

    desc_lines = []
    if ai_summary:
        desc_lines.append(f"🤖 **AI 요약 (한국어)**\n{ai_summary}")
    elif summary:
        desc_lines.append(f"**내용:** {summary[:900]}")
    desc_lines.append(f"**출처:** {name}")
    desc_lines.append(f"**시간:** {_fmt_local(publish_time)}")

    fields = []
    if link:
        fields.append({
            "name": "🔗 원문 링크",
            "value": f"[공식 페이지에서 보기]({link})",
            "inline": False,
        })

    title_prefix = f"🏢  [{ticker}]" if ticker else "🏢"
    embed: Dict[str, Any] = {
        "title": f"{title_prefix}  {title}",
        "color": 5763719,
        "description": "\n".join(desc_lines),
        "footer": {"text": f"Official IR/Newsroom  •  {name}"},
        "timestamp": _fmt_iso_utc(publish_time),
    }
    if link:
        embed["url"] = link
    if fields:
        embed["fields"] = fields

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
    ai_summary: Optional[str] = item.get("ai_summary") or None

    color = _SEC_COLORS.get(form_type, _COLOR_DEFAULT)

    desc_lines = []
    if ai_summary:
        desc_lines.append(f"🤖 **AI 요약 (한국어)**\n{ai_summary}\n")
    if description:
        desc_lines.append(f"**내용:** {description}")
    desc_lines.append(f"**공시 양식:** {form_type}")
    desc_lines.append(f"**공시일:** {filing_date}")

    fields = []
    if link:
        fields.append({
            "name": "🔗 원문 링크",
            "value": f"[SEC EDGAR 공시 보기]({link})",
            "inline": False,
        })

    embed: Dict[str, Any] = {
        "title": f"🏛️  [{ticker}]  SEC {form_type} 공시",
        "color": color,
        "description": "\n".join(desc_lines),
        "footer": {"text": f"SEC EDGAR  •  {ticker}"},
        "timestamp": _fmt_iso_utc(publish_time),
    }
    if link:
        embed["url"] = link
    if fields:
        embed["fields"] = fields

    payload = {
        "username": "주식 뉴스 봇 📈",
        "embeds": [embed],
    }
    return _post(webhook_url, payload)


_UP_COMMENTS: Dict[int, list] = {
    1: ["슬슬 움직이네요 👀", "오, 뭔가 시작되는 냄새? 🤔", "관심 가져볼 만하네요!", "살짝 달아오르는 중 🌡️"],
    2: ["이거 심상치 않은데요?! 🔥", "제법 달리는데요!", "눈여겨봐야겠는걸요? 😮", "누가 사고 있는 거죠? 🧐"],
    3: ["🚀 본격 시동 걸렸습니다!", "홀더분들 기분 좋으시겠다 😁", "이거 진짜 가는 건가요?!", "폭등 중... 🔥🔥🔥"],
    4: ["와... 이건 역사에 남을 것 같은데요? ✍️", "지금 못 탄 분들 손 떨리시죠? 😱", "🚀🚀🚀 하늘을 뚫을 기세!", "시간 여행 가능하면 어제 샀을 텐데 😭"],
}

_DOWN_COMMENTS: Dict[int, list] = {
    1: ["으음... 조정인가요? 😅", "살짝 흔들리네요", "손이 떨리기 시작하는 구간 👀", "신경 쓰세요... 😐"],
    2: ["이건 좀 아프다 💀", "손절 고민하시는 분들 계시죠?", "버텨야 하나... 🫠", "천리 길 주의 😨"],
    3: ["🩸 처참하네요...", "이 구간이 진짜 시험대", "저가 매수 기회?? 아니면... 😬", "올라올라... 지하로 하하 😂"],
    4: ["망했다... 😭😭😭", "숫자가 폭삭이네요 🤯", "이제 높은 거 파는 사람 없겠죠", "여기서 나가요 🚶"],
}


def _pick_comment(is_up: bool, level: int) -> str:
    import random
    bank = _UP_COMMENTS if is_up else _DOWN_COMMENTS
    tier = min(level, 4)
    tier = max(tier, 1)
    return random.choice(bank[tier])


def send_price_alert(webhook_url: str, item: Dict[str, Any]) -> bool:
    """주가 급변동 알람을 Discord로 전송합니다."""
    ticker = item["ticker"]
    name = item.get("name", ticker)
    price = item["price"]
    prev_close = item.get("prev_close", 0.0)
    change = item.get("change", 0.0)
    change_pct = item.get("change_pct", 0.0)
    currency = item.get("currency", "USD")
    target_pct = item.get("target_pct")
    threshold = item.get("threshold")

    is_up = change >= 0
    arrow = "📈" if is_up else "📉"
    sign = "+" if is_up else ""
    color = 5763719 if is_up else 15158332

    alert_level = 1
    if target_pct is not None and threshold and threshold > 0:
        alert_level = max(1, int(abs(target_pct) / threshold))

    comment = _pick_comment(is_up, alert_level)

    if target_pct is not None and threshold is not None:
        level_str = f"{'+' if target_pct >= 0 else ''}{target_pct:.0f}% 돌파"
        title = f"{arrow}  [{ticker}]  {level_str}  —  {comment}"
    else:
        title = f"{arrow}  [{ticker}]  주가 급변동 알람  —  {comment}"

    threshold_note = f"\n⚠️ 알람 설정: ±{threshold:.0f}% 단위" if threshold else ""

    embed: Dict[str, Any] = {
        "title": title,
        "color": color,
        "description": (
            f"**{name}**\n\n"
            f"💰 현재가: **{price:,.2f} {currency}**\n"
            f"📊 전일 대비: **{sign}{change_pct:.2f}%** ({sign}{change:,.2f} {currency})\n"
            f"📌 전일 종가: {prev_close:,.2f} {currency}"
            f"{threshold_note}"
        ),
        "footer": {"text": f"Yahoo Finance  •  {ticker}"},
        "timestamp": _fmt_iso_utc(item.get("timestamp", 0)),
    }
    payload = {"username": "주식 뉴스 봇 📈", "embeds": [embed]}
    return _post(webhook_url, payload)
