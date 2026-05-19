"""
ticker_manager.py - 티커 및 설정 관리 CLI

사용법:
  python ticker_manager.py list
  python ticker_manager.py add AAPL MSFT GOOGL
  python ticker_manager.py remove TSLA
  python ticker_manager.py set-webhook https://discord.com/api/webhooks/...
  python ticker_manager.py set-interval 300
  python ticker_manager.py set-openai-key sk-...

  [트위터 관련]
  python ticker_manager.py twitter-list
  python ticker_manager.py twitter-add TSLA elonmusk Tesla
  python ticker_manager.py twitter-remove TSLA elonmusk
  python ticker_manager.py twitter-on
  python ticker_manager.py twitter-off
"""
import json
import logging
import os
import sys
from typing import List, Optional

from twitter_fetcher import KNOWN_ACCOUNTS

logger = logging.getLogger(__name__)


def _gemini_find_twitter_accounts(ticker: str, gemini_api_key: str) -> Optional[List[str]]:
    """Gemini에게 티커의 공식 트위터 계정을 물어봅니다.
    
    반환: 계정명 리스트 (예: ['nvidia', 'JensenHuang']) 또는 None(실패 시)
    OpenAI 호환 API 사용 (grpcio 의존성 없음, 라즈베리파이 호환).
    """
    try:
        from openai import OpenAI
    except ImportError:
        return None
    try:
        client = OpenAI(
            api_key=gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        prompt = (
            f"주식 티커 '{ticker}'의 공식 트위터(X) 계정 사용자명(username)을 알려주세요.\n"
            "회사 공식 계정과 주요 임원 계정을 포함해서 최대 3개까지만 알려주세요.\n"
            "반드시 아래 형식으로만 답하세요 (설명 없이 콤마로 구분된 username만):\n"
            "username1,username2,username3\n\n"
            "존재하지 않거나 모르면 NONE 이라고만 답하세요."
        )
        response = client.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content.strip()
        if text.upper() == "NONE" or not text:
            return None
        accounts = [a.strip().lstrip("@") for a in text.split(",") if a.strip()]
        return accounts if accounts else None
    except Exception as e:
        logger.warning("Gemini 트위터 계정 탐색 실패 [%s]: %s", ticker, e)
        return None

CONFIG_FILE = "config.json"

DEFAULT_CONFIG = {
    "discord_webhook_url": "YOUR_DISCORD_WEBHOOK_URL_HERE",
    "tickers": [],
    "check_interval_seconds": 300,
    "monitor_sec_filings": True,
    "sec_form_types": ["8-K"],
    "sec_check_interval_seconds": 1800,
    "monitor_twitter": False,
    "twitter_check_interval_seconds": 600,
    "twitter_accounts": {},
    "nitter_instances": [
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
        "https://nitter.catsarch.com",
        "https://nitter.unixfox.eu",
        "https://nitter.1d4.us",
    ],
}


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"✅ config.json 저장 완료")


# ─── 서브커맨드 ───────────────────────────────────────────────────────────────

def cmd_list(config: dict) -> None:
    webhook = config.get("discord_webhook_url", "")
    webhook_status = "(설정됨)" if webhook and webhook != "YOUR_DISCORD_WEBHOOK_URL_HERE" else "⚠️  (미설정)"
    tickers = config.get("tickers", [])

    print("\n┌─── 현재 설정 ───────────────────────────────────────┐")
    print(f"│  Discord Webhook : {webhook_status}")
    print(f"│  뉴스 체크 주기  : {config.get('check_interval_seconds', 300)}초")
    sec = config.get("monitor_sec_filings", False)
    print(f"│  SEC 공시 모니터 : {'ON  (' + ', '.join(config.get('sec_form_types', [])) + ')' if sec else 'OFF'}")
    if sec:
        print(f"│  SEC 체크 주기   : {config.get('sec_check_interval_seconds', 1800)}초")
    tw = config.get("monitor_twitter", False)
    print(f"│  Twitter 모니터 : {'ON' if tw else 'OFF'}")
    if tw:
        print(f"│  Twitter 체크주기: {config.get('twitter_check_interval_seconds', 600)}초")
    print(f"│  등록 티커 ({len(tickers)}개) :")
    if tickers:
        twitter_accounts = config.get("twitter_accounts", {})
        for i, t in enumerate(tickers, 1):
            accs = twitter_accounts.get(t, [])
            acc_str = f"  [트윗터: {', '.join('@' + a for a in accs)}]" if accs else ""
            print(f"│    {i:2d}. {t}{acc_str}")
    else:
        print("│    (없음)")
    print("└─────────────────────────────────────────────────────┘\n")


def cmd_add(config: dict, tickers: List[str]) -> None:
    existing = set(config.setdefault("tickers", []))
    twitter_accounts: dict = config.setdefault("twitter_accounts", {})
    gemini_api_key = config.get("gemini_api_key", "").strip()
    added: List[str] = []
    for t in tickers:
        t = t.upper().strip()
        if not t:
            continue
        if t in existing:
            print(f"  ⚠️  {t} — 이미 등록됨")
            continue
        config["tickers"].append(t)
        existing.add(t)
        added.append(t)

        # 트위터 계정 자동 설정 (이미 직접 설정한 경우 덮어쓰지 않음)
        if t in twitter_accounts:
            print(f"  ✅ {t} — 추가됨")
            continue

        # 1순위: KNOWN_ACCOUNTS 하드코딩
        if t in KNOWN_ACCOUNTS:
            twitter_accounts[t] = KNOWN_ACCOUNTS[t]
            accs_str = ", ".join("@" + a for a in KNOWN_ACCOUNTS[t])
            print(f"  ✅ {t} — 추가됨  (트위터 자동: {accs_str})")
        # 2순위: Gemini로 탐색
        elif gemini_api_key:
            print(f"  ✅ {t} — 추가됨  (Gemini로 트위터 계정 탐색 중...)")
            accounts = _gemini_find_twitter_accounts(t, gemini_api_key)
            if accounts:
                twitter_accounts[t] = accounts
                accs_str = ", ".join("@" + a for a in accounts)
                print(f"       Gemini 탐색 완료: {accs_str}")
            else:
                print(f"       Gemini 탐색 결과 없음 — 수동 등록: python ticker_manager.py twitter-add {t} @account")
        else:
            print(f"  ✅ {t} — 추가됨  (트위터 계정 미등록)")

    if added:
        save_config(config)


def cmd_remove(config: dict, tickers: List[str]) -> None:
    existing: List[str] = config.setdefault("tickers", [])
    twitter_accounts: dict = config.get("twitter_accounts", {})
    removed: List[str] = []
    for t in tickers:
        t = t.upper().strip()
        if t not in existing:
            print(f"  ⚠️  {t} — 등록되지 않음")
        else:
            existing.remove(t)
            # 트위터 계정도 같이 삭제
            if t in twitter_accounts:
                del twitter_accounts[t]
            removed.append(t)
            print(f"  🗑️  {t} — 제거됨 (트위터 계정도 삭제)")
    if removed:
        save_config(config)


def cmd_set_gemini_key(config: dict, api_key: str) -> None:
    api_key = api_key.strip()
    if not api_key:
        print("⚠️  API 키가 비어있습니다.")
        sys.exit(1)
    config["gemini_api_key"] = api_key
    save_config(config)
    print("Gemini API 키 설정 완료! AI 한글 요약 및 트위터 계정 자동 탐색 기능이 활성화됩니다.")


def cmd_set_webhook(config: dict, url: str) -> None:
    url = url.strip()
    if not url.startswith("https://discord.com/api/webhooks/"):
        print("⚠️  올바른 Discord Webhook URL 형식이 아닙니다.")
        print("   예: https://discord.com/api/webhooks/123456/abcdef...")
        sys.exit(1)
    config["discord_webhook_url"] = url
    save_config(config)
    print("Discord Webhook URL 설정 완료!")


def cmd_set_interval(config: dict, seconds_str: str) -> None:
    try:
        seconds = int(seconds_str)
        if seconds < 60:
            print("⚠️  최소 60초 이상 설정해주세요.")
            sys.exit(1)
        config["check_interval_seconds"] = seconds
        save_config(config)
        print(f"뉴스 체크 주기: {seconds}초로 설정됨")
    except ValueError:
        print(f"⚠️  숫자를 입력해주세요: {seconds_str}")
        sys.exit(1)


# ─── Twitter 관리 명령어 ──────────────────────────────────────────────────────────────

def cmd_twitter_list(config: dict) -> None:
    """티커별 등록된 Twitter 계정 목록 출력."""
    tw_on = config.get("monitor_twitter", False)
    accounts: dict = config.get("twitter_accounts", {})
    tickers = config.get("tickers", [])

    print(f"\n트위터 모니터링: {'ON' if tw_on else 'OFF  (활성화: twitter-on)'}")
    print("─" * 45)
    if not accounts:
        print("  등록된 트윗터 계정이 없습니다.")
        print("  예) python ticker_manager.py twitter-add TSLA elonmusk Tesla")
    else:
        for ticker, accs in sorted(accounts.items()):
            mark = " (티커 없음)" if ticker not in tickers else ""
            print(f"  {ticker}{mark}:")
            for acc in accs:
                print(f"    @{acc}  →  https://twitter.com/{acc}")
    print()


def cmd_twitter_add(config: dict, ticker: str, usernames: List[str]) -> None:
    """티커에 Twitter 계정 추가."""
    ticker = ticker.upper().strip()
    accounts: dict = config.setdefault("twitter_accounts", {})
    existing: List[str] = accounts.setdefault(ticker, [])
    added: List[str] = []

    for username in usernames:
        username = username.lstrip("@").strip()
        if not username:
            continue
        if username in existing:
            print(f"  ⚠️  @{username} — [{ticker}]에 이미 등록됨")
        else:
            existing.append(username)
            added.append(username)
            print(f"  ✅ @{username} — [{ticker}]에 추가됨")

    if added:
        save_config(config)


def cmd_twitter_remove(config: dict, ticker: str, usernames: List[str]) -> None:
    """티커에서 Twitter 계정 제거."""
    ticker = ticker.upper().strip()
    accounts: dict = config.get("twitter_accounts", {})
    existing: List[str] = accounts.get(ticker, [])
    removed: List[str] = []

    for username in usernames:
        username = username.lstrip("@").strip()
        if username not in existing:
            print(f"  ⚠️  @{username} — [{ticker}]에 없음")
        else:
            existing.remove(username)
            removed.append(username)
            print(f"  🗑️  @{username} — [{ticker}]에서 제거됨")

    if removed:
        if not existing:
            accounts.pop(ticker, None)  # 계정이 비면 키도 제거
        save_config(config)


def cmd_twitter_on(config: dict) -> None:
    config["monitor_twitter"] = True
    save_config(config)
    print("트위터 모니터링: ON")
    if not config.get("twitter_accounts"):
        print("⚠️  등록된 트윗터 계정이 없습니다.")
        print("   예) python ticker_manager.py twitter-add TSLA elonmusk Tesla")


def cmd_twitter_off(config: dict) -> None:
    config["monitor_twitter"] = False
    save_config(config)
    print("트위터 모니터링: OFF")


# ─── 메인 ─────────────────────────────────────────────────────────────────────

def print_usage() -> None:
    print(__doc__)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print_usage()
        sys.exit(1)

    config = load_config()
    command = args[0].lower()

    if command == "list":
        cmd_list(config)

    elif command == "add":
        if len(args) < 2:
            print("사용법: python ticker_manager.py add TICKER [TICKER2 ...]")
            sys.exit(1)
        cmd_add(config, args[1:])

    elif command == "remove":
        if len(args) < 2:
            print("사용법: python ticker_manager.py remove TICKER [TICKER2 ...]")
            sys.exit(1)
        cmd_remove(config, args[1:])

    elif command == "set-webhook":
        if len(args) < 2:
            print("사용법: python ticker_manager.py set-webhook WEBHOOK_URL")
            sys.exit(1)
        cmd_set_webhook(config, args[1])

    elif command == "set-interval":
        if len(args) < 2:
            print("사용법: python ticker_manager.py set-interval 300")
            sys.exit(1)
        cmd_set_interval(config, args[1])

    elif command == "set-gemini-key":
        if len(args) < 2:
            print("사용법: python ticker_manager.py set-gemini-key AIza...")
            sys.exit(1)
        cmd_set_gemini_key(config, args[1])

    elif command == "twitter-list":
        cmd_twitter_list(config)

    elif command == "twitter-add":
        if len(args) < 3:
            print("사용법: python ticker_manager.py twitter-add TICKER @account1 @account2 ...")
            sys.exit(1)
        cmd_twitter_add(config, args[1], args[2:])

    elif command == "twitter-remove":
        if len(args) < 3:
            print("사용법: python ticker_manager.py twitter-remove TICKER @account1 ...")
            sys.exit(1)
        cmd_twitter_remove(config, args[1], args[2:])

    elif command == "twitter-on":
        cmd_twitter_on(config)

    elif command == "twitter-off":
        cmd_twitter_off(config)

    else:
        print(f"알 수 없는 명령어: {command}")
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
