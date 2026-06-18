"""
ticker_manager.py - 티커 및 설정 관리 CLI

사용법:
  python ticker_manager.py list
  python ticker_manager.py add AAPL MSFT GOOGL
  python ticker_manager.py remove TSLA
  python ticker_manager.py set-webhook https://discord.com/api/webhooks/...
  python ticker_manager.py set-interval 300
  python ticker_manager.py set-gemini-key AIza...

  [트위터 관련]
  python ticker_manager.py twitter-list
  python ticker_manager.py twitter-add TSLA elonmusk Tesla
  python ticker_manager.py twitter-remove TSLA elonmusk
  python ticker_manager.py twitter-on
  python ticker_manager.py twitter-off
"""
import logging
import sys
from typing import List

from app_state import DEFAULT_GEMINI_MODEL, load_config as _load_app_config, save_config as _save_app_config
from twitter_fetcher import KNOWN_ACCOUNTS
from twitter_account_finder import _gemini_find_twitter_accounts

logger = logging.getLogger(__name__)

def _gemini_model(config: dict, specific_key: str = "") -> str:
    """config.json에서 Gemini 모델명을 읽습니다."""
    if specific_key:
        value = str(config.get(specific_key, "") or "").strip()
        if value:
            return value
    return (
        str(config.get("gemini_model", "") or "").strip()
        or DEFAULT_GEMINI_MODEL
    )


def load_config() -> dict:
    return _load_app_config()


def save_config(config: dict) -> None:
    _save_app_config(config)
    print(f"✅ config.json 저장 완료")


# ─── 서브커맨드 ───────────────────────────────────────────────────────────────
def cmd_list(config: dict) -> None:
    webhook = config.get("discord_webhook_url", "")
    webhook_status = "(설정됨)" if webhook and webhook != "YOUR_DISCORD_WEBHOOK_URL_HERE" else "⚠️  (미설정)"
    tickers = config.get("tickers", [])

    print("\n┌─── 현재 설정 ───────────────────────────────────────┐")
    print(f"│  Discord Webhook : {webhook_status}")
    print(f"│  뉴스 체크 주기  : {config.get('check_interval_seconds', 300)}초")
    print(f"│  Gemini 기본 모델: {config.get('gemini_model', DEFAULT_GEMINI_MODEL)}")
    print(f"│  Gemini 관련성   : {config.get('gemini_relevance_model', config.get('gemini_model', DEFAULT_GEMINI_MODEL))}")
    print(f"│  Gemini 요약     : {config.get('gemini_summary_model', config.get('gemini_model', DEFAULT_GEMINI_MODEL))}")
    print(f"│  Gemini 트위터   : {config.get('gemini_twitter_model', config.get('gemini_model', DEFAULT_GEMINI_MODEL))}")
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
    twitter_model = _gemini_model(config, "gemini_twitter_model")
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
            print(f"  ✅ {t} — 추가됨  (Gemini로 트위터 계정 탐색 중... model={twitter_model})")
            accounts = _gemini_find_twitter_accounts(t, gemini_api_key, twitter_model)
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


def cmd_set_gemini_model(config: dict, model: str) -> None:
    model = model.strip()
    if not model:
        print("⚠️  모델명이 비어있습니다.")
        sys.exit(1)
    config["gemini_model"] = model
    config.setdefault("gemini_relevance_model", model)
    config.setdefault("gemini_summary_model", model)
    config.setdefault("gemini_twitter_model", model)
    save_config(config)
    print(f"Gemini 기본 모델 설정 완료: {model}")


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


def cmd_news_filter(config: dict, mode: str = "") -> None:
    current_on = config.get("news_importance_filter_enabled", True)
    current_score = int(config.get("news_importance_min_score", 2) or 2)

    if not mode:
        print(f"뉴스 중요도 필터: {'ON' if current_on else 'OFF'} (강도 {current_score})")
        print("사용법: python ticker_manager.py news-filter on|off|loose|normal|strict")
        return

    mode = mode.lower().strip()
    if mode in ("on", "enable", "enabled"):
        config["news_importance_filter_enabled"] = True
        print(f"뉴스 중요도 필터: ON (강도 {current_score})")
    elif mode in ("off", "disable", "disabled"):
        config["news_importance_filter_enabled"] = False
        print("뉴스 중요도 필터: OFF")
    elif mode in ("loose", "low", "1"):
        config["news_importance_filter_enabled"] = True
        config["news_importance_min_score"] = 1
        print("뉴스 중요도 필터: 느슨하게 (강도 1)")
    elif mode in ("normal", "medium", "2"):
        config["news_importance_filter_enabled"] = True
        config["news_importance_min_score"] = 2
        print("뉴스 중요도 필터: 보통 (강도 2)")
    elif mode in ("strict", "high", "3"):
        config["news_importance_filter_enabled"] = True
        config["news_importance_min_score"] = 3
        print("뉴스 중요도 필터: 엄격하게 (강도 3)")
    else:
        print("사용법: python ticker_manager.py news-filter on|off|loose|normal|strict")
        sys.exit(1)

    save_config(config)


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
            accounts.pop(ticker, None)
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

    elif command == "news-filter":
        cmd_news_filter(config, args[1] if len(args) >= 2 else "")

    elif command == "set-gemini-key":
        if len(args) < 2:
            print("사용법: python ticker_manager.py set-gemini-key AIza...")
            sys.exit(1)
        cmd_set_gemini_key(config, args[1])

    elif command == "set-gemini-model":
        if len(args) < 2:
            print("사용법: python ticker_manager.py set-gemini-model gemini-3.1-flash-lite")
            sys.exit(1)
        cmd_set_gemini_model(config, args[1])

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
