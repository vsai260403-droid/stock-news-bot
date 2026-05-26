"""
discord_bot.py - Discord Bot을 통한 티커 관리 명령어

【사전 준비】
  1. https://discord.com/developers/applications 에서 Application 생성
  2. Bot 메뉴 → "Message Content Intent" 활성화
  3. OAuth2 → URL Generator → bot 선택 → Send Messages + Read Message History 권한 선택
  4. 생성된 초대 URL로 봇을 서버에 초대
  5. config.json 의 discord_bot_token 에 Bot Token 입력

【명령어】
  !add AAPL MSFT      — 티커 추가 (Gemini로 트위터 계정 자동 탐색)
  !remove TSLA        — 티커 제거 (트위터 계정도 삭제)
  !list               — 등록된 티커 목록
  !status             — 시스템 상태
  !help               — 도움말

  !twitter-on         — 트위터 알람 활성화
  !twitter-off        — 트위터 알람 비활성화
  !twitter-list       — 티커별 등록 트위터 계정 확인
  !twitter-add TSLA @elonmusk @Tesla  — 티커 연동 계정 추가
  !twitter-remove TSLA @elonmusk      — 티커 연동 계정 제거
  !twitter-follow @elonmusk @nvidia   — 티커 없이 계정 단독 추가
  !twitter-unfollow @elonmusk         — 단독 계정 제거
  !twitter-follows                    — 단독 팔로우 계정 목록
  !twitter-unfollow-all               — 단독 팔로우 계정 전체 제거

  !set-gemini-key AIza...  — Gemini API 키 (트위터 자동 탐색)
  !set-openai-key sk-...   — OpenAI API 키 (AI 요약)
  !set-interval 300        — 뉴스 체크 주기 (초)
  !set-webhook URL         — Discord Webhook URL
"""
import json
import logging
import os
import hashlib
import time
import threading
from typing import Optional

import requests

from twitter_fetcher import KNOWN_ACCOUNTS
from ticker_manager import _gemini_find_twitter_accounts

logger = logging.getLogger(__name__)

CONFIG_FILE = "config.json"
SEEN_NEWS_FILE = "seen_news.json"


def _seed_seen_news_for_tickers(tickers: list, config: dict) -> None:
    """새로 추가된 티커의 현재 뉴스를 seen에 등록합니다 (알람 없이).
    
    !add 직후 호출하여 기존 오래된 기사가 새 기사로 전송되는 것을 방지합니다.
    """
    try:
        from news_fetcher import fetch_all_news
        import hashlib as _hashlib

        def _title_hash(title: str) -> str:
            return "title_" + _hashlib.md5(title.lower().strip().encode("utf-8")).hexdigest()[:16]

        # 기존 seen 로드
        seen: dict = {}
        if os.path.exists(SEEN_NEWS_FILE):
            try:
                with open(SEEN_NEWS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    seen = data
                elif isinstance(data, list):
                    seen = {k: int(time.time()) for k in data}
            except Exception:
                pass

        now = int(time.time())
        count = 0
        for ticker in tickers:
            items = fetch_all_news(ticker, config)
            for item in items:
                item_id = item.get("id", "")
                if item_id and item_id not in seen:
                    seen[item_id] = now
                    count += 1
                th = _title_hash(item.get("title", ""))
                if th and th not in seen:
                    seen[th] = now

        # 저장
        with open(SEEN_NEWS_FILE, "w", encoding="utf-8") as f:
            json.dump(seen, f, indent=2)

        if count > 0:
            logger.info("새 티커 %s: 기존 뉴스 %d건 seen 등록 (알람 없이)", tickers, count)
    except Exception as e:
        logger.warning("seen 뉴스 시드 실패: %s", e)


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

    # ── !price-check ──────────────────────────────────────────────────────────
    @bot.command(name="price-check")
    async def cmd_price_check(ctx):
        """모든 티커의 가격 변동 알람 체크를 수동으로 실행합니다."""
        import asyncio
        import importlib
        # main.py의 check_prices와 load_config를 동적으로 import
        main_mod = importlib.import_module("main")
        check_prices = getattr(main_mod, "check_prices")
        load_config = getattr(main_mod, "load_config")
        loop = asyncio.get_event_loop()
        cfg = await loop.run_in_executor(None, load_config)
        count = await loop.run_in_executor(None, check_prices, cfg)
        if count > 0:
            await ctx.send(f"✅ 가격 변동 알람 체크 완료! (알람 전송: {count}건)")
        else:
            await ctx.send("가격 변동 알람 체크 완료 (전송된 알람 없음)")

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
            # 트위터 계정 자동 등록 (기존 설정은 덮어쓰지 않음)
            if t not in twitter_accounts:
                if t in KNOWN_ACCOUNTS:
                    twitter_accounts[t] = KNOWN_ACCOUNTS[t]
                else:
                    # Gemini로 트위터 계정 탐색
                    gemini_key = cfg.get("gemini_api_key", "").strip()
                    if gemini_key:
                        await ctx.send(f"🔎 **{t}** Gemini로 트위터 계정 탐색 중...")
                        accounts = await loop.run_in_executor(
                            None, _gemini_find_twitter_accounts, t, gemini_key
                        )
                        if accounts:
                            twitter_accounts[t] = accounts

        if added:
            _save_config(cfg)
            # 새로 추가된 티커의 현재 뉴스를 seen에 등록 (첫 알람 폭탄 방지)
            await loop.run_in_executor(None, _seed_seen_news_for_tickers, added, cfg)

        lines = []
        for t in invalid:
            lines.append(f"❌ **{t}** — Yahoo Finance에서 찾을 수 없는 티커")
        for t in added:
            accs = cfg.get("twitter_accounts", {}).get(t, [])
            acc_str = f"  (트위터: {', '.join('@' + a for a in accs)})" if accs else "  (트위터 계정 없음 — `!twitter-add {t} @계정`으로 수동 등록)".format(t=t)
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
        twitter_accounts: dict = cfg.get("twitter_accounts", {})
        removed, not_found = [], []

        for raw in tickers:
            t = raw.upper().strip()
            if t in current:
                current.remove(t)
                # 트위터 계정도 같이 삭제
                if t in twitter_accounts:
                    del twitter_accounts[t]
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
        global_accs = accounts.get("_GLOBAL_", [])
        if registered or global_accs:
            parts = [
                f"{t}({', '.join('@'+a for a in accounts[t])})"
                for t in registered
            ]
            if global_accs:
                parts.append(f"전용({', '.join('@'+a for a in global_accs)})")
            await ctx.send(
                f"🐦 트위터 알람 **ON**\n"
                f"모니터링 계정: " + ", ".join(parts)
            )
        else:
            await ctx.send(
                "🐦 트위터 알람 **ON**\n"
                "⚠️ 등록된 트위터 계정이 없습니다. `!twitter-add TSLA @elonmusk` 또는 `!twitter-follow @account` 으로 추가하세요."
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
            if ticker == "_GLOBAL_":
                continue  # 아래 별도 표시
            mark = " *(티커 미등록)*" if ticker not in tickers else ""
            acc_str = ", ".join(f"[@{a}](https://twitter.com/{a})" for a in accs)
            lines.append(f"• **{ticker}**{mark}: {acc_str}")
        global_accs = accounts.get("_GLOBAL_", [])
        if global_accs:
            acc_str = ", ".join(f"[@{a}](https://twitter.com/{a})" for a in global_accs)
            lines.append(f"• **[전용]** (티커 없음): {acc_str}")
        await ctx.send("\n".join(lines))

    # ── !twitter-follow (티커 없는 전용 계정 추가) ───────────────────────────
    @bot.command(name="twitter-follow")
    async def cmd_twitter_follow(ctx, *usernames):
        if not usernames:
            await ctx.send("사용법: `!twitter-follow @elonmusk @nvidia`")
            return
        cfg = _load_config()
        accounts: dict = cfg.setdefault("twitter_accounts", {})
        existing: list = accounts.setdefault("_GLOBAL_", [])
        added = []
        for raw in usernames:
            username = raw.lstrip("@").strip()
            if not username:
                continue
            if username not in existing:
                existing.append(username)
                added.append(username)
        if added:
            _save_config(cfg)
            await ctx.send(
                f"✅ 전용 트위터 계정 추가: {', '.join('@'+a for a in added)}\n"
                f"(티커 연동 없음 — 트윗 자체 알람)"
            )
        else:
            await ctx.send("⚠️ 추가할 새 계정이 없습니다. (이미 등록됨)")

    # ── !twitter-unfollow (티커 없는 전용 계정 제거) ──────────────────────────
    @bot.command(name="twitter-unfollow")
    async def cmd_twitter_unfollow(ctx, *usernames):
        if not usernames:
            await ctx.send("사용법: `!twitter-unfollow @elonmusk`")
            return
        cfg = _load_config()
        accounts: dict = cfg.get("twitter_accounts", {})
        existing: list = accounts.get("_GLOBAL_", [])
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
                accounts.pop("_GLOBAL_", None)
            _save_config(cfg)
        lines = []
        if removed:
            lines.append(f"🗑️ 전용 트위터 계정 제거: {', '.join('@'+a for a in removed)}")
        if not_found:
            lines.append(f"⚠️ 등록되지 않은 계정: {', '.join('@'+a for a in not_found)}")
        await ctx.send("\n".join(lines) or "제거할 계정이 없습니다.")

    # ── !twitter-test (Nitter 수집 테스트) ───────────────────────────────────
    @bot.command(name="twitter-test")
    async def cmd_twitter_test(ctx, username: str = ""):
        if not username:
            await ctx.send("사용법: `!twitter-test @elonmusk`")
            return
        username = username.lstrip("@").strip()
        await ctx.send(f"🔍 **@{username}** Nitter 수집 테스트 중...")

        from twitter_fetcher import _ALL_NITTER_INSTANCES, _try_fetch_rss, get_healthy_instances
        import asyncio

        cfg = _load_config()
        # config에 직접 지정된 경우 그것 사용, 없으면 자동 체크 목록
        custom = cfg.get("nitter_instances")
        instances = custom or _ALL_NITTER_INSTANCES
        lines = []
        found = 0

        for inst in instances:
            result = await asyncio.get_event_loop().run_in_executor(
                None, _try_fetch_rss, inst, username
            )
            if result:
                latest = result[0].get("title", "")[:60]
                lines.append(f"✅ `{inst}`  →  {len(result)}개  (최신: {latest}...)")
                found += len(result)
                break
            else:
                lines.append(f"❌ `{inst}`  →  실패")

        if found:
            lines.append(f"\n총 {found}개 트윗 수집 성공")
        else:
            lines.append(f"\n⚠️ 모든 Nitter 인스턴스에서 **@{username}** 수집 실패\n"
                         "Nitter 인스턴스가 모두 다운됐거나 계정명이 잘못됐을 수 있습니다.")
        await ctx.send("\n".join(lines))

    # ── !twitter-follows (전용 계정 목록 조회) ────────────────────────────────
    @bot.command(name="twitter-follows")
    async def cmd_twitter_follows(ctx):
        cfg = _load_config()
        global_accs: list = cfg.get("twitter_accounts", {}).get("_GLOBAL_", [])
        tw_on = cfg.get("monitor_twitter", False)
        status_str = "🟢 ON" if tw_on else "🔴 OFF"
        if not global_accs:
            await ctx.send(
                f"🐦 전용 팔로우 계정: {status_str}\n"
                "등록된 계정 없음. `!twitter-follow @elonmusk` 으로 추가하세요."
            )
            return
        acc_lines = "\n".join(
            f"{i+1}. [@{a}](https://twitter.com/{a})" for i, a in enumerate(global_accs)
        )
        await ctx.send(
            f"🐦 전용 팔로우 계정 ({len(global_accs)}개) — 알람: {status_str}\n"
            f"{acc_lines}\n"
            f"전체 제거: `!twitter-unfollow-all`"
        )

    # ── !twitter-unfollow-all (전용 계정 전체 제거) ───────────────────────────
    @bot.command(name="twitter-unfollow-all")
    async def cmd_twitter_unfollow_all(ctx):
        cfg = _load_config()
        accounts: dict = cfg.get("twitter_accounts", {})
        global_accs: list = list(accounts.get("_GLOBAL_", []))
        if not global_accs:
            await ctx.send("⚠️ 제거할 전용 계정이 없습니다.")
            return
        accounts.pop("_GLOBAL_", None)
        _save_config(cfg)
        await ctx.send(
            f"🗑️ 전용 팔로우 계정 전체 제거 ({len(global_accs)}개):\n"
            + ", ".join(f"@{a}" for a in global_accs)
        )

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

    # ── !price ────────────────────────────────────────────────────────────────
    @bot.command(name="price")
    async def cmd_price(ctx, ticker: str = ""):
        if not ticker:
            await ctx.send("사용법: `!price AAPL`")
            return
        ticker = ticker.upper().strip()
        import asyncio
        loop = asyncio.get_event_loop()
        from price_fetcher import fetch_price as _fetch_price
        info = await loop.run_in_executor(None, _fetch_price, ticker)
        if not info:
            await ctx.send(f"❌ **{ticker}** — 주가 조회 실패 (티커 확인 필요)")
            return
        is_up = info["change"] >= 0
        arrow = "📈" if is_up else "📉"
        sign = "+" if is_up else ""
        currency = info.get("currency", "USD")
        from datetime import datetime
        ts = info.get("timestamp", 0)
        time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "N/A"
        msg = (
            f"{arrow} **[{ticker}]** {info.get('name', ticker)}\n"
            f"💰 현재가: **{info['price']:,.2f} {currency}**\n"
            f"📊 전일 대비: **{sign}{info['change_pct']:.2f}%** ({sign}{info['change']:,.2f} {currency})\n"
            f"📌 전일 종가: {info['prev_close']:,.2f} {currency}\n"
            f"🕐 {time_str}"
        )
        await ctx.send(msg)

    # ── !earnings ─────────────────────────────────────────────────────────────
    @bot.command(name="earnings")
    async def cmd_earnings(ctx, ticker: str = ""):
        if not ticker:
            await ctx.send("사용법: `!earnings AAPL`")
            return
        ticker = ticker.upper().strip()
        import asyncio
        loop = asyncio.get_event_loop()
        from price_fetcher import fetch_earnings as _fetch_earnings
        info = await loop.run_in_executor(None, _fetch_earnings, ticker)
        if not info:
            await ctx.send(f"❌ **{ticker}** — 실적 발표 일정 조회 실패")
            return
        from datetime import datetime
        lines = [f"📅 **[{ticker}]** 실적 발표 일정\n"]
        dates = info.get("earnings_dates", [])
        if dates:
            for ts in dates:
                dt_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                lines.append(f"• 실적 발표일: **{dt_str}**")
        else:
            lines.append("• 실적 발표일: 정보 없음")
        if info.get("earnings_avg"):
            lines.append(f"• EPS 예상: {info['earnings_avg']} (범위: {info.get('earnings_low','?')} ~ {info.get('earnings_high','?')})")
        if info.get("revenue_avg"):
            lines.append(f"• 매출 예상: {info['revenue_avg']}")
        ex_div = info.get("exdividend_date")
        if ex_div:
            lines.append(f"• 배당락일: {datetime.fromtimestamp(ex_div).strftime('%Y-%m-%d')}")
        div_date = info.get("dividend_date")
        if div_date:
            lines.append(f"• 배당 지급일: {datetime.fromtimestamp(div_date).strftime('%Y-%m-%d')}")
        await ctx.send("\n".join(lines))

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

    # ── !set-gemini-key ───────────────────────────────────────────────────────
    @bot.command(name="set-gemini-key")
    async def cmd_set_gemini_key(ctx, api_key: str = ""):
        if not api_key:
            await ctx.send("사용법: `!set-gemini-key AIza...`\n⚠️ 이 명령어는 DM으로 보내는 것을 권장합니다.")
            return
        cfg = _load_config()
        cfg["gemini_api_key"] = api_key
        _save_config(cfg)
        try:
            await ctx.message.delete()
        except Exception:
            pass
        await ctx.send("✅ Gemini API 키 저장 완료! 트위터 계정 자동 탐색이 활성화됩니다.")

    # ── !set-interval ─────────────────────────────────────────────────────────
    @bot.command(name="set-interval")
    async def cmd_set_interval(ctx, seconds: str = ""):
        if not seconds:
            await ctx.send("사용법: `!set-interval 300` (초 단위)")
            return
        try:
            val = int(seconds)
            if val < 60:
                await ctx.send("❌ 최소 60초 이상이어야 합니다.")
                return
        except ValueError:
            await ctx.send("❌ 숫자를 입력하세요.")
            return
        cfg = _load_config()
        cfg["check_interval_seconds"] = val
        _save_config(cfg)
        await ctx.send(f"✅ 뉴스 체크 주기: **{val}초**로 변경됨 (봇 재시작 후 적용)")

    # ── !set-sec-interval ─────────────────────────────────────────────────────
    @bot.command(name="set-sec-interval")
    async def cmd_set_sec_interval(ctx, seconds: str = ""):
        if not seconds:
            cfg = _load_config()
            current = cfg.get("sec_check_interval_seconds", 1800)
            await ctx.send(f"현재 SEC 체크 주기: **{current}초** ({current//60}분)\n사용법: `!set-sec-interval 1800`")
            return
        try:
            val = int(seconds)
            if val < 60:
                await ctx.send("❌ 최소 60초 이상이어야 합니다.")
                return
        except ValueError:
            await ctx.send("❌ 숫자를 입력하세요.")
            return
        cfg = _load_config()
        cfg["sec_check_interval_seconds"] = val
        _save_config(cfg)
        await ctx.send(f"✅ SEC 공시 체크 주기: **{val}초** ({val//60}분)로 변경됨 (봇 재시작 후 적용)")

    # ── !set-twitter-interval ─────────────────────────────────────────────────
    @bot.command(name="set-twitter-interval")
    async def cmd_set_twitter_interval(ctx, seconds: str = ""):
        if not seconds:
            cfg = _load_config()
            current = cfg.get("twitter_check_interval_seconds", 600)
            await ctx.send(f"현재 트위터 체크 주기: **{current}초** ({current//60}분)\n사용법: `!set-twitter-interval 600`")
            return
        try:
            val = int(seconds)
            if val < 60:
                await ctx.send("❌ 최소 60초 이상이어야 합니다.")
                return
        except ValueError:
            await ctx.send("❌ 숫자를 입력하세요.")
            return
        cfg = _load_config()
        cfg["twitter_check_interval_seconds"] = val
        _save_config(cfg)
        await ctx.send(f"✅ 트위터 체크 주기: **{val}초** ({val//60}분)로 변경됨 (봇 재시작 후 적용)")

    # ── !set-price-alert ──────────────────────────────────────────────────
    @bot.command(name="set-price-alert")
    async def cmd_set_price_alert(ctx, pct: str = ""):
        if not pct:
            cfg = _load_config()
            current = cfg.get("price_alert_threshold_pct", 5.0)
            monitor = cfg.get("monitor_price", True)
            status = "🟢 활성" if monitor else "🔴 비활성"
            await ctx.send(
                f"현재 주가 알람: {status}\n"
                f"임계값: **±{current:.1f}%** \u2014 이 배수마다 알람\n\n"
                f"설정: `!set-price-alert 5` / `!price-alert-on` / `!price-alert-off`"
            )
            return
        try:
            val = float(pct.rstrip("%"))
            if val <= 0 or val > 50:
                await ctx.send("❌ 1~50 사이 값을 입력하세요.")
                return
        except ValueError:
            await ctx.send("❌ 숫자를 입력하세요. 예: `!set-price-alert 5`")
            return
        cfg = _load_config()
        cfg["price_alert_threshold_pct"] = val
        cfg["monitor_price"] = True
        _save_config(cfg)
        levels = ", ".join(f"±{val*i:.0f}%" for i in range(1, 6))
        await ctx.send(
            f"✅ 주가 알람 임계값: **±{val:.1f}%**\n"
            f"알람 발생 구간: {levels} ... (제한 없음)\n"
            f"(상승/하락 각각 독립 추적)"
        )

    # ── !price-alert-on / !price-alert-off ─────────────────────────────────
    @bot.command(name="price-alert-on")
    async def cmd_price_alert_on(ctx):
        cfg = _load_config()
        cfg["monitor_price"] = True
        _save_config(cfg)
        threshold = cfg.get("price_alert_threshold_pct", 5.0)
        await ctx.send(f"🟢 주가 변동 알람 **활성** (임계값: ±{threshold:.1f}%)")

    @bot.command(name="price-alert-off")
    async def cmd_price_alert_off(ctx):
        cfg = _load_config()
        cfg["monitor_price"] = False
        _save_config(cfg)
        await ctx.send("🔴 주가 변동 알람 **비활성**")

    # ── !set-webhook ──────────────────────────────────────────────────────────
    @bot.command(name="set-webhook")
    async def cmd_set_webhook(ctx, url: str = ""):
        if not url:
            await ctx.send("사용법: `!set-webhook https://discord.com/api/webhooks/...`")
            return
        if not url.startswith("https://discord.com/api/webhooks/"):
            await ctx.send("❌ Discord Webhook URL 형식이 올바르지 않습니다.")
            return
        cfg = _load_config()
        cfg["discord_webhook_url"] = url
        _save_config(cfg)
        try:
            await ctx.message.delete()
        except Exception:
            pass
        await ctx.send("✅ Discord Webhook URL 저장 완료!")

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
        gemini_key = cfg.get("gemini_api_key", "").strip()
        gemini_str = "✅ 활성화" if gemini_key else "❌ 키 없음 (`!set-gemini-key`)"
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
            f"• 트위터 자동 탐색 (Gemini): {gemini_str}\n"
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
            "`!add AAPL TSLA` — 티커 추가 (트위터 계정 자동 탐색)\n"
            "`!remove TSLA` — 티커 제거\n"
            "`!list` — 등록된 티커 목록\n"
            "`!status` — 시스템 상태 확인\n"
            "\n"
            "**[ 조회 ]**\n"
            "`!price AAPL` — 현재 주가 조회\n"
            "`!earnings AAPL` — 실적 발표 일정 조회\n"
            "\n"
            "**[ 주가 알람 ]**\n"
            "`!set-price-alert 5` — 임계값 설정 (5% 단위마다 알람, 인자 없으면 현재 설정 확인)\n"
            "`!price-alert-on` — 주가 변동 알람 활성화\n"
            "`!price-alert-off` — 주가 변동 알람 비활성화\n"
            "\n"
            "**[ 트위터 관리 ]**\n"
            "`!twitter-on` — 트위터 알람 활성화\n"
            "`!twitter-off` — 트위터 알람 비활성화\n"
            "`!twitter-list` — 티커별 트위터 계정 목록\n"
            "`!twitter-add TSLA @elonmusk @Tesla` — 티커 연동 계정 추가\n"
            "`!twitter-remove TSLA @elonmusk` — 티커 연동 계정 제거\n"
            "`!twitter-follow @elonmusk` — 티커 없이 계정 단독 추가\n"
            "`!twitter-unfollow @elonmusk` — 단독 계정 제거\n"
            "`!twitter-follows` — 단독 팔로우 계정 목록\n"
            "`!twitter-unfollow-all` — 단독 팔로우 계정 전체 제거\n"
            "`!twitter-test @elonmusk` — Nitter 수집 테스트\n"
            "\n"
            "**[ 설정 ]**\n"
            "`!set-gemini-key AIza...` — Gemini API 키 (트위터 자동 탐색용, DM 권장)\n"
            "`!set-openai-key sk-...` — OpenAI API 키 (AI 요약용, DM 권장)\n"
            "`!set-interval 300` — 뉴스 체크 주기 (초)\n"
            "`!set-sec-interval 1800` — SEC 공시 체크 주기 (초)\n"
            "`!set-twitter-interval 600` — 트위터 체크 주기 (초)\n"
            "`!set-webhook URL` — Discord Webhook URL (DM 권장)\n"
            "\n"
            "`!help` — 이 도움말"
        )
        await ctx.send(msg)

    # ── !test-price-alert (관리자 전용) ───────────────────────────────────────
    @bot.command(name="test-price-alert")
    async def cmd_test_price_alert(ctx, ticker: str = "", percent: str = ""):
        """관리자 전용: 특정 티커와 변동률로 가격 알람을 테스트합니다."""
        if not getattr(ctx.author, "guild_permissions", None) or not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ 이 명령어는 관리자만 사용할 수 있습니다.")
            return
        if not ticker or not percent:
            await ctx.send("사용법: `!test-price-alert TSLA 7.4` (티커, 퍼센트)")
            return
        ticker = ticker.upper().strip()
        try:
            pct = float(percent)
        except ValueError:
            await ctx.send("❌ 퍼센트는 숫자로 입력하세요. 예: `!test-price-alert TSLA 7.4`")
            return
        import asyncio
        loop = asyncio.get_event_loop()
        from price_fetcher import fetch_price as _fetch_price
        info = await loop.run_in_executor(None, _fetch_price, ticker)
        if not info:
            await ctx.send(f"❌ **{ticker}** — 주가 조회 실패 (티커 확인 필요)")
            return
        cfg = _load_config()
        webhook_url = cfg.get("discord_webhook_url")
        threshold = cfg.get("price_alert_threshold_pct", 5.0)
        alert_level = max(1, int(abs(pct) / threshold)) if threshold > 0 else 1
        alert_info = dict(info)
        alert_info["alert_level"] = alert_level
        alert_info["target_pct"] = pct
        alert_info["threshold"] = threshold
        from discord_notifier import send_price_alert as _send_price_alert
        ok = await loop.run_in_executor(None, _send_price_alert, webhook_url, alert_info)
        if ok:
            await ctx.send(f"✅ 테스트 주가 알람 전송 완료! (티커: {ticker}, {pct:+.2f}%)")
        else:
            await ctx.send("❌ 알람 전송 실패 (웹훅 설정 또는 네트워크 오류)")

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
