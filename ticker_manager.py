"""
ticker_manager.py - 티커 및 설정 관리 CLI

사용법:
  python ticker_manager.py list
  python ticker_manager.py add AAPL MSFT GOOGL
  python ticker_manager.py remove TSLA
  python ticker_manager.py set-webhook https://discord.com/api/webhooks/...
  python ticker_manager.py set-interval 300
"""
import json
import os
import sys
from typing import List

CONFIG_FILE = "config.json"

DEFAULT_CONFIG = {
    "discord_webhook_url": "YOUR_DISCORD_WEBHOOK_URL_HERE",
    "tickers": [],
    "check_interval_seconds": 300,
    "monitor_sec_filings": True,
    "sec_form_types": ["8-K"],
    "sec_check_interval_seconds": 1800,
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
    print(f"│  등록 티커 ({len(tickers)}개) :")
    if tickers:
        for i, t in enumerate(tickers, 1):
            print(f"│    {i:2d}. {t}")
    else:
        print("│    (없음)")
    print("└─────────────────────────────────────────────────────┘\n")


def cmd_add(config: dict, tickers: List[str]) -> None:
    existing = set(config.setdefault("tickers", []))
    added: List[str] = []
    for t in tickers:
        t = t.upper().strip()
        if not t:
            continue
        if t in existing:
            print(f"  ⚠️  {t} — 이미 등록됨")
        else:
            config["tickers"].append(t)
            existing.add(t)
            added.append(t)
            print(f"  ✅ {t} — 추가됨")
    if added:
        save_config(config)


def cmd_remove(config: dict, tickers: List[str]) -> None:
    existing: List[str] = config.setdefault("tickers", [])
    removed: List[str] = []
    for t in tickers:
        t = t.upper().strip()
        if t not in existing:
            print(f"  ⚠️  {t} — 등록되지 않음")
        else:
            existing.remove(t)
            removed.append(t)
            print(f"  🗑️  {t} — 제거됨")
    if removed:
        save_config(config)


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

    else:
        print(f"알 수 없는 명령어: {command}")
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
