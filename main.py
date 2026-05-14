"""
main.py - 주식 뉴스 Discord 알람 메인 실행 파일

실행: python main.py
"""
import json
import logging
import os
import time
from typing import Set

import schedule

from discord_bot import start_bot_thread
from discord_notifier import send_news_alert, send_sec_alert, send_tweet_alert
from news_fetcher import fetch_all_news, ai_summarize_news
from sec_fetcher import fetch_sec_filings
from twitter_fetcher import fetch_all_tweets

# ─── 로깅 설정 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("stock_alarm.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ─── 파일 경로 ────────────────────────────────────────────────────────────────
CONFIG_FILE = "config.json"
SEEN_NEWS_FILE = "seen_news.json"
SEEN_SEC_FILE = "seen_sec.json"
SEEN_TWEETS_FILE = "seen_tweets.json"


# ─── 유틸 함수 ────────────────────────────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_seen(filepath: str) -> Set[str]:
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(filepath: str, seen: Set[str]) -> None:
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)


def _validate_config(config: dict) -> bool:
    """필수 설정값 검증."""
    webhook = config.get("discord_webhook_url", "")
    if not webhook or webhook == "YOUR_DISCORD_WEBHOOK_URL_HERE":
        logger.error(
            "Discord webhook URL이 설정되지 않았습니다.\n"
            "  python ticker_manager.py set-webhook <WEBHOOK_URL>"
        )
        return False
    if not config.get("tickers"):
        logger.error(
            "모니터링할 티커가 없습니다.\n"
            "  python ticker_manager.py add AAPL TSLA NVDA"
        )
        return False
    return True


# ─── 뉴스 체크 ────────────────────────────────────────────────────────────────
def check_news(config: dict, seen: Set[str], initial: bool = False) -> int:
    """등록된 모든 소스에서 뉴스를 확인하고 새 항목을 Discord로 전송합니다."""
    webhook_url = config["discord_webhook_url"]
    tickers = config.get("tickers", [])
    openai_api_key = config.get("openai_api_key", "").strip()
    count = 0

    for ticker in tickers:
        items = fetch_all_news(ticker, config)
        for item in items:
            item_id = item["id"]
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            if not initial:
                # AI 한글 요약 (openai_api_key가 설정된 경우에만)
                if openai_api_key:
                    item["ai_summary"] = ai_summarize_news(
                        item.get("title", ""),
                        item.get("publisher", ""),
                        openai_api_key,
                    )
                if send_news_alert(webhook_url, item):
                    count += 1
                    logger.info("[%s] 뉴스 알람 전송: %s", ticker, item["title"][:60])
                    time.sleep(0.5)  # Rate limit 방지

    return count


# ─── SEC 공시 체크 ────────────────────────────────────────────────────────────
def check_sec(config: dict, seen: Set[str], initial: bool = False) -> int:
    """SEC EDGAR 공시를 확인하고 새 항목을 Discord로 전송합니다."""
    if not config.get("monitor_sec_filings", False):
        return 0

    webhook_url = config["discord_webhook_url"]
    tickers = config.get("tickers", [])
    form_types = config.get("sec_form_types", ["8-K"])
    count = 0

    for ticker in tickers:
        items = fetch_sec_filings(ticker, form_types)
        for item in items:
            item_id = item["id"]
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            if not initial:
                if send_sec_alert(webhook_url, item):
                    count += 1
                    logger.info("[%s] SEC 공시 알람 전송: %s", ticker, item["form_type"])
                    time.sleep(0.5)

    return count


# ─── 트위터 체크 ──────────────────────────────────────────────────────────────
def check_tweets(config: dict, seen: Set[str], initial: bool = False) -> int:
    """등록된 트위터 계정에서 새 트윗을 확인하고 Discord로 전송합니다."""
    if not config.get("monitor_twitter", False):
        return 0

    webhook_url = config["discord_webhook_url"]
    tickers = config.get("tickers", [])
    count = 0

    for ticker in tickers:
        items = fetch_all_tweets(ticker, config)
        for item in items:
            item_id = item["id"]
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            if not initial:
                if send_tweet_alert(webhook_url, item):
                    count += 1
                    logger.info(
                        "[%s] 트윗 알람 전송: @%s — %s",
                        ticker,
                        item["username"],
                        item["title"][:60],
                    )
                    time.sleep(0.5)  # Rate limit 방지

    return count


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main() -> None:
    config = load_config()

    if not _validate_config(config):
        return

    seen_news: Set[str] = load_seen(SEEN_NEWS_FILE)
    seen_sec: Set[str] = load_seen(SEEN_SEC_FILE)
    seen_tweets: Set[str] = load_seen(SEEN_TWEETS_FILE)

    news_interval = config.get("check_interval_seconds", 300)
    sec_interval = config.get("sec_check_interval_seconds", 1800)
    twitter_interval = config.get("twitter_check_interval_seconds", 600)
    monitor_sec = config.get("monitor_sec_filings", False)
    monitor_twitter = config.get("monitor_twitter", False)

    # ── Discord 봇 스레드 시작 (선택적) ─────────────────────────────────────
    start_bot_thread(config)

    logger.info("=" * 60)
    logger.info("주식 뉴스 Discord 알람 시스템 시작")
    logger.info("모니터링 티커: %s", ", ".join(config["tickers"]))
    logger.info("뉴스 소스: %s", ", ".join(config.get("news_sources", ["yahoo", "google_rss"])))
    logger.info("뉴스 체크 주기: %d초", news_interval)
    if monitor_sec:
        logger.info(
            "SEC 공시 체크 주기: %d초  (양식: %s)",
            sec_interval,
            ", ".join(config.get("sec_form_types", ["8-K"])),
        )
    if monitor_twitter:
        twitter_accounts = config.get("twitter_accounts", {})
        account_summary = ", ".join(
            f"{t}: {', '.join('@' + u for u in accs)}"
            for t, accs in twitter_accounts.items()
            if t in config.get("tickers", [])
        )
        logger.info("트위터 모니터링: ON  (%s)", account_summary or "계정 없음")
        logger.info("트위터 체크 주기: %d초", twitter_interval)
    logger.info("=" * 60)

    # ── 초기 로드 (기존 항목은 알람 없이 seen 처리) ──────────────────────────
    logger.info("기존 뉴스 초기 로드 중 (알람 없음)...")
    check_news(config, seen_news, initial=True)
    save_seen(SEEN_NEWS_FILE, seen_news)

    if monitor_sec:
        logger.info("기존 SEC 공시 초기 로드 중 (알람 없음)...")
        check_sec(config, seen_sec, initial=True)
        save_seen(SEEN_SEC_FILE, seen_sec)

    if monitor_twitter:
        logger.info("기존 트윗 초기 로드 중 (알람 없음)...")
        check_tweets(config, seen_tweets, initial=True)
        save_seen(SEEN_TWEETS_FILE, seen_tweets)

    logger.info("초기 로드 완료. 새 항목이 생기면 Discord로 알람을 보냅니다.")

    # ── 스케줄러 작업 정의 ────────────────────────────────────────────────────
    def news_job() -> None:
        cfg = load_config()  # 티커 변경 반영을 위해 매번 재로드
        count = check_news(cfg, seen_news)
        save_seen(SEEN_NEWS_FILE, seen_news)
        logger.info("뉴스 체크 완료 — 새 항목: %d건", count)

    def sec_job() -> None:
        cfg = load_config()
        count = check_sec(cfg, seen_sec)
        save_seen(SEEN_SEC_FILE, seen_sec)
        if count > 0:
            logger.info("SEC 공시 체크 완료 — 새 공시: %d건", count)

    def twitter_job() -> None:
        cfg = load_config()
        count = check_tweets(cfg, seen_tweets)
        save_seen(SEEN_TWEETS_FILE, seen_tweets)
        if count > 0:
            logger.info("트위터 체크 완료 — 새 트윗: %d건", count)

    schedule.every(news_interval).seconds.do(news_job)
    if monitor_sec:
        schedule.every(sec_interval).seconds.do(sec_job)
    if monitor_twitter:
        schedule.every(twitter_interval).seconds.do(twitter_job)

    # ── 메인 루프 ─────────────────────────────────────────────────────────────
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
