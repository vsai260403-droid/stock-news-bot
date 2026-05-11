"""
discord_bot.py - Discord Bot을 통한 티커 관리 명령어

【사전 준비】
  1. https://discord.com/developers/applications 에서 Application 생성
  2. Bot 메뉴 → "Message Content Intent" 활성화
  3. OAuth2 → URL Generator → bot 선택 → Send Messages + Read Message History 권한 선택
  4. 생성된 초대 URL로 봇을 서버에 초대
  5. config.json 의 discord_bot_token 에 Bot Token 입력

【명령어】
  !add AAPL MSFT    — 티커 추가
  !remove TSLA      — 티커 제거
  !list             — 등록된 티커 목록
  !status           — 시스템 상태
  !help             — 도움말
"""
import json
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"


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

        for raw in tickers:
            t = raw.upper().strip()
            if not t.isalpha() or len(t) > 10:
                continue
            if t in current_set:
                already.append(t)
            else:
                cfg["tickers"].append(t)
                current_set.add(t)
                added.append(t)

        if added:
            _save_config(cfg)

        lines = []
        if added:
            lines.append(f"✅ 추가됨: **{', '.join(added)}**")
        if already:
            lines.append(f"⚠️ 이미 등록됨: {', '.join(already)}")
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

    # ── !status ───────────────────────────────────────────────────────────────
    @bot.command(name="status")
    async def cmd_status(ctx):
        cfg = _load_config()
        tickers = cfg.get("tickers", [])
        news_interval = cfg.get("check_interval_seconds", 300)
        sec_interval = cfg.get("sec_check_interval_seconds", 1800)
        monitor_sec = cfg.get("monitor_sec_filings", False)
        sources = cfg.get("news_sources", ["yahoo", "google_rss"])
        finnhub_key = cfg.get("finnhub_api_key", "").strip()

        finnhub_str = "✅ 활성화" if finnhub_key else "❌ API 키 없음"
        sec_str = f"ON ({sec_interval}초)" if monitor_sec else "OFF"
        ticker_str = ", ".join(tickers) if tickers else "없음"

        msg = (
            "**📊 시스템 상태**\n"
            f"• 모니터링 티커 ({len(tickers)}개): {ticker_str}\n"
            f"• 뉴스 체크 주기: {news_interval}초\n"
            f"• 뉴스 소스: {', '.join(sources)}\n"
            f"• Finnhub: {finnhub_str}\n"
            f"• SEC 공시 감시: {sec_str}"
        )
        await ctx.send(msg)

    # ── !help ─────────────────────────────────────────────────────────────────
    @bot.command(name="help")
    async def cmd_help(ctx):
        msg = (
            "**📈 주식 알람 봇 명령어**\n"
            "`!add AAPL TSLA` — 티커 추가\n"
            "`!remove TSLA` — 티커 제거\n"
            "`!list` — 등록된 티커 목록\n"
            "`!status` — 시스템 상태 확인\n"
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
