"""
discord_bot.py - Discord Bot을 통한 티커 관리 명령어

【사전 준비】
  1. https://discord.com/developers/applications 에서 Application 생성
  2. Bot 메뉴 → "Message Content Intent" 활성화
  3. OAuth2 → URL Generator → bot 선택 → Send Messages + Read Message History 권한 선택
  4. 생성된 초대 URL로 봇을 서버에 초대
  5. config.json 의 discord_bot_token 에 Bot Token 입력

【명령어】
  !add AAPL MSFT      — 티커 추가 (알려진 티커는 트위터 계정 자동 등록)
  !remove TSLA        — 티커 제거
  !list               — 등록된 티커 목록
  !status             — 시스템 상태
  !help               — 도움말

  !twitter-on         — 트위터 알람 활성화
  !twitter-off        — 트위터 알람 비활성화
  !twitter-list       — 티커별 등록 트위터 계정 확인
  !twitter-add TSLA @elonmusk @Tesla  — 계정 추가
  !twitter-remove TSLA @elonmusk      — 계정 제거
"""
import json
import logging
import threading
from typing import Optional

import requests

from twitter_fetcher import KNOWN_ACCOUNTS

logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"


def _validate_ticker(ticker: str) -> bool:
    """Yahoo Finance API로 티커가 실제로 존재하는지 확인합니다."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
        resp = requests.get(
            url,
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0 (compatible; StockAlarmBot/1.0)"},
        )
        if resp.status_code != 200:
            return False
        result = resp.json().get("chart", {}).get("result")
        return bool(result)
    except Exception:
        return False


def _load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _make_bot(prefix: str):
    """discord.py Bot 인스턴스와 커맨드를 생성합니다."""
    try:
        import discord
        from discord.ext import commands
    except ImportError:
        logger.error(
            "discord.py 가 설치되어 있지 않습니다. pip install discord.py"
        )
        return None

    intents = discord.Intents.default()
    intents.message_content = True  # Developer Portal 에서 반드시 활성화 필요

    bot = commands.Bot(command_prefix=prefix, intents=intents, help_command=None)

    # ── 이벤트 ────────────────────────────────────────────────────────────────
    @bot.event
    async def on_ready():
        logger.info("Discord Bot 로그인 완료: %s (ID: %s)", bot.user.name, bot.user.id)

    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return  # 모르는 명령어는 무시
        logger.warning("봇 명령어 오류 [%s]: %s", ctx.command, error)

    # ── !add ──────────────────────────────────────────────────────────────────
    @bot.command(name="add")
    async def cmd_add(ctx, *tickers):
        if not tickers:
            await ctx.send("사용법: `!add AAPL TSLA NVDA`")
            return

        cfg = _load_config()
        current_set = set(t.upper() for t in cfg.get("tickers", []))
        added, already = [], []

        twitter_accounts: dict = cfg.setdefault("twitter_accounts", {})
        invalid = []
        await ctx.send(f"🔍 티커 확인 중... ({', '.join(t.upper() for t in tickers)})")
        import asyncio
        loop = asyncio.get_event_loop()
        for raw in tickers:
            t = raw.upper().strip()
            if not t.isalpha() or len(t) > 10:
                invalid.append(t)
                continue
            if t in current_set:
                already.append(t)
                continue
            # Yahoo Finance에서 실제 존재 여부 확인 (blocking → executor)
            exists = await loop.run_in_executor(None, _validate_ticker, t)
            if not exists:
                invalid.append(t)
                continue
            cfg["tickers"].append(t)
            current_set.add(t)
            added.append(t)
            # 알려진 티커면 트위터 계정 자동 등록 (기존 설정은 덮어쓰지 않음)
            if t not in twitter_accounts and t in KNOWN_ACCOUNTS:
                twitter_accounts[t] = KNOWN_ACCOUNTS[t]

        if added:
            _save_config(cfg)

        lines = []
        for t in invalid:
            lines.append(f"❌ **{t}** — Yahoo Finance에서 찾을 수 없는 티커")
        for t in added:
            accs = cfg.get("twitter_accounts", {}).get(t, [])
            acc_str = f"  (트위터 자동: {', '.join('@' + a for a in accs)})" if accs else ""
            lines.append(f"✅ **{t}** 추가됨{acc_str}")
        for t in already:
            lines.append(f"⚠️ **{t}** 이미 등록됨")
        await ctx.send("\n".join(lines) or "추가할 유효한 티커가 없습니다.")

    # ── !remove ───────────────────────────────────────────────────────────────
    @bot.command(name="remove", aliases=["rm", "del"])
    async def cmd_remove(ctx, *tickers):
        if not tickers:
            await ctx.send("사용법: `!remove TSLA AAPL`")
            return

        cfg = _load_config()
        current: list = cfg.get("tickers", [])
        removed, not_found = [], []

        for raw in tickers:
            t = raw.upper().strip()
            if t in current:
                current.remove(t)
                removed.append(t)
            else:
                not_found.append(t)

        if removed:
            cfg["tickers"] = current
            _save_config(cfg)

        lines = []
        if removed:
            lines.append(f"🗑️ 제거됨: **{', '.join(removed)}**")
        if not_found:
            lines.append(f"⚠️ 등록되지 않음: {', '.join(not_found)}")
        await ctx.send("\n".join(lines) or "제거할 티커가 없습니다.")

    # ── !list ─────────────────────────────────────────────────────────────────
    @bot.command(name="list", aliases=["ls", "tickers"])
    async def cmd_list(ctx):
        cfg = _load_config()
        tickers = cfg.get("tickers", [])
        if not tickers:
            await ctx.send("등록된 티커가 없습니다. `!add AAPL` 로 추가하세요.")
            return
        chunk = " | ".join(f"**{t}**" for t in tickers)
        await ctx.send(f"📋 모니터링 중인 티커 ({len(tickers)}개)\n{chunk}")

    # ── !twitter-on ───────────────────────────────────────────────────────────
    @bot.command(name="twitter-on")
    async def cmd_twitter_on(ctx):
        cfg = _load_config()
        cfg["monitor_twitter"] = True
        _save_config(cfg)
        accounts = cfg.get("twitter_accounts", {})
        tickers = cfg.get("tickers", [])
        registered = [t for t in tickers if t in accounts and accounts[t]]
        if registered:
            await ctx.send(
                f"🐦 트위터 알람 **ON**\n"
                f"모니터링 계정: " +
                ", ".join(
                    f"{t}({', '.join('@'+a for a in accounts[t])})"
                    for t in registered
                )
            )
        else:
            await ctx.send(
                "🐦 트위터 알람 **ON**\n"
                "⚠️ 등록된 트위터 계정이 없습니다. `!twitter-add TSLA @elonmusk` 으로 추가하세요."
            )

    # ── !twitter-off ──────────────────────────────────────────────────────────
    @bot.command(name="twitter-off")
    async def cmd_twitter_off(ctx):
        cfg = _load_config()
        cfg["monitor_twitter"] = False
        _save_config(cfg)
        await ctx.send("🔕 트위터 알람 **OFF**")

    # ── !twitter-list ─────────────────────────────────────────────────────────
    @bot.command(name="twitter-list")
    async def cmd_twitter_list(ctx):
        cfg = _load_config()
        accounts: dict = cfg.get("twitter_accounts", {})
        tickers = cfg.get("tickers", [])
        tw_on = cfg.get("monitor_twitter", False)
        status_str = "🟢 ON" if tw_on else "🔴 OFF"

        if not accounts:
            await ctx.send(
                f"🐦 트위터 알람: {status_str}\n"
                "등록된 계정 없음. `!twitter-add TSLA @elonmusk @Tesla` 으로 추가하세요."
            )
            return

        lines = [f"🐦 트위터 알람: {status_str}"]
        for ticker, accs in sorted(accounts.items()):
            mark = " *(티커 미등록)*" if ticker not in tickers else ""
            acc_str = ", ".join(f"[@{a}](https://twitter.com/{a})" for a in accs)
            lines.append(f"• **{ticker}**{mark}: {acc_str}")
        await ctx.send("\n".join(lines))

    # ── !twitter-add ──────────────────────────────────────────────────────────
    @bot.command(name="twitter-add")
    async def cmd_twitter_add(ctx, ticker: str = "", *usernames):
        if not ticker or not usernames:
            await ctx.send("사용법: `!twitter-add TSLA @elonmusk @Tesla`")
            return

        ticker = ticker.upper().strip()
        cfg = _load_config()
        accounts: dict = cfg.setdefault("twitter_accounts", {})
        existing: list = accounts.setdefault(ticker, [])
        added = []

        for raw in usernames:
            username = raw.lstrip("@").strip()
            if not username:
                continue
            if username in existing:
                pass
            else:
                existing.append(username)
                added.append(username)

        if added:
            _save_config(cfg)
            await ctx.send(
                f"✅ **{ticker}** 트위터 계정 추가: {', '.join('@'+a for a in added)}"
            )
        else:
            await ctx.send(f"⚠️ 추가할 새 계정이 없습니다. (이미 등록됨)")

    # ── !twitter-remove ───────────────────────────────────────────────────────
    @bot.command(name="twitter-remove")
    async def cmd_twitter_remove(ctx, ticker: str = "", *usernames):
        if not ticker or not usernames:
            await ctx.send("사용법: `!twitter-remove TSLA @elonmusk`")
            return

        ticker = ticker.upper().strip()
        cfg = _load_config()
        accounts: dict = cfg.get("twitter_accounts", {})
        existing: list = accounts.get(ticker, [])
        removed, not_found = [], []

        for raw in usernames:
            username = raw.lstrip("@").strip()
            if username in existing:
                existing.remove(username)
                removed.append(username)
            else:
                not_found.append(username)

        if removed:
            if not existing:
                accounts.pop(ticker, None)
            _save_config(cfg)

        lines = []
        if removed:
            lines.append(f"🗑️ **{ticker}** 트위터 계정 제거: {', '.join('@'+a for a in removed)}")
        if not_found:
            lines.append(f"⚠️ 등록되지 않은 계정: {', '.join('@'+a for a in not_found)}")
        await ctx.send("\n".join(lines) or "제거할 계정이 없습니다.")

    # ── !set-openai-key ───────────────────────────────────────────────────────
    @bot.command(name="set-openai-key")
    async def cmd_set_openai_key(ctx, api_key: str = ""):
        if not api_key:
            await ctx.send("사용법: `!set-openai-key sk-...`\n⚠️ 이 명령어는 DM으로 보내는 것을 권장합니다.")
            return
        if not api_key.startswith("sk-"):
            await ctx.send("❌ OpenAI API 키는 `sk-`로 시작해야 합니다.")
            return
        cfg = _load_config()
        cfg["openai_api_key"] = api_key
        _save_config(cfg)
        # 키 노출 방지를 위해 메시지 삭제 시도
        try:
            await ctx.message.delete()
        except Exception:
            pass
        await ctx.send("✅ OpenAI API 키 저장 완료! AI 한글 요약이 활성화됩니다.")

    # ── !status ───────────────────────────────────────────────────────────────
    @bot.command(name="status")
    async def cmd_status(ctx):
        cfg = _load_config()
        tickers = cfg.get("tickers", [])
        news_interval = cfg.get("check_interval_seconds", 300)
        sec_interval = cfg.get("sec_check_interval_seconds", 1800)
        monitor_sec = cfg.get("monitor_sec_filings", False)
        monitor_twitter = cfg.get("monitor_twitter", False)
        sources = cfg.get("news_sources", ["yahoo", "google_rss"])
        finnhub_key = cfg.get("finnhub_api_key", "").strip()
        twitter_accounts: dict = cfg.get("twitter_accounts", {})

        finnhub_str = "✅ 활성화" if finnhub_key else "❌ API 키 없음"
        openai_key = cfg.get("openai_api_key", "").strip()
        ai_str = "✅ 활성화" if openai_key else "❌ 키 없음 (`!set-openai-key sk-...`)"
        sec_str = f"ON ({sec_interval}초)" if monitor_sec else "OFF"
        twitter_str = "🟢 ON" if monitor_twitter else "🔴 OFF"

        ticker_lines = []
        for t in tickers:
            accs = twitter_accounts.get(t, [])
            acc_str = f" [{', '.join('@'+a for a in accs)}]" if accs else ""
            ticker_lines.append(f"`{t}`{acc_str}")
        ticker_str = ", ".join(ticker_lines) if ticker_lines else "없음"

        msg = (
            "**📊 시스템 상태**\n"
            f"• 모니터링 티커 ({len(tickers)}개): {ticker_str}\n"
            f"• 뉴스 체크 주기: {news_interval}초\n"
            f"• 뉴스 소스: {', '.join(sources)}\n"
            f"• Finnhub: {finnhub_str}\n"
            f"• AI 한글 요약: {ai_str}\n"
            f"• SEC 공시 감시: {sec_str}\n"
            f"• 트위터 알람: {twitter_str}"
        )
        await ctx.send(msg)

    # ── !help ─────────────────────────────────────────────────────────────────
    @bot.command(name="help")
    async def cmd_help(ctx):
        msg = (
            "**📈 주식 알람 봇 명령어**\n"
            "\n"
            "**[ 티커 관리 ]**\n"
            "`!add AAPL TSLA` — 티커 추가 (알려진 티커는 트위터 자동 등록)\n"
            "`!remove TSLA` — 티커 제거\n"
            "`!list` — 등록된 티커 목록\n"
            "`!status` — 시스템 상태 확인\n"
            "\n"
            "**[ 트위터 관리 ]**\n"
            "`!twitter-on` — 트위터 알람 활성화\n"
            "`!twitter-off` — 트위터 알람 비활성화\n"
            "`!twitter-list` — 티커별 트위터 계정 목록\n"
            "`!twitter-add TSLA @elonmusk @Tesla` — 계정 추가\n"
            "`!twitter-remove TSLA @elonmusk` — 계정 제거\n"
            "\n"
            "**[ AI 요약 ]**\n"
            "`!set-openai-key sk-...` — OpenAI API 키 설정 (DM 권장)\n"
            "\n"
            "`!help` — 이 도움말"
        )
        await ctx.send(msg)

    return bot


def start_bot_thread(config: dict) -> Optional[threading.Thread]:
    """
    Discord 봇을 별도 데몬 스레드에서 실행합니다.
    discord_bot_token 이 설정되지 않은 경우 None 을 반환하고 건너뜁니다.
    """
    token = config.get("discord_bot_token", "").strip()
    if not token or token == "YOUR_DISCORD_BOT_TOKEN_HERE":
        logger.info("discord_bot_token 미설정 → 디스코드 명령어 기능 비활성화")
        return None

    prefix = config.get("discord_command_prefix", "!")

    def _run():
        import asyncio
        import ssl
        import certifi

        # aiohttp 가 내부에서 ssl.create_default_context() 를 직접 호출하므로
        # certifi 인증서를 로드하도록 전역 패치
        _orig_ctx = ssl.create_default_context
        def _patched_ctx(*args, **kwargs):
            ctx = _orig_ctx(*args, **kwargs)
            ctx.load_verify_locations(certifi.where())
            return ctx
        ssl.create_default_context = _patched_ctx

        bot = _make_bot(prefix)
        if bot is None:
            return
        try:
            bot.run(token, log_handler=None)
        except Exception as exc:
            logger.error("Discord 봇 실행 오류: %s", exc)

    thread = threading.Thread(target=_run, daemon=True, name="DiscordBotThread")
    thread.start()
    logger.info("Discord 봇 스레드 시작됨 (prefix='%s')", prefix)
    return thread
