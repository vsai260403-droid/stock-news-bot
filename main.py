"""
main.py - 주식 뉴스 Discord 알람 메인 실행 파일

실행: python main.py
"""
import json
import logging
import os
import hashlib
import time
import schedule

from datetime import date
from typing import Dict, Set

from discord_bot import start_bot_thread
from discord_notifier import send_news_alert, send_sec_alert, send_tweet_alert, send_price_alert
from news_fetcher import fetch_all_news, ai_summarize_news
from price_fetcher import fetch_price
from sec_fetcher import fetch_sec_filings, filter_sec_by_age
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
SEEN_SEC_TICKERS_FILE = "seen_sec_tickers.json"  # 초기화된 SEC 티커 추적
SEEN_TWEETS_FILE = "seen_tweets.json"


# ─── 유틸 함수 ────────────────────────────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_seen(filepath: str) -> Set[str]:
    """seen 파일 로드. dict 형식이면 7일 초과 항목 자동 제거."""
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
        elif isinstance(data, dict):
            cutoff = int(time.time()) - (7 * 24 * 3600)
            return {k for k, v in data.items() if isinstance(v, (int, float)) and v >= cutoff}
    return set()


def save_seen(filepath: str, seen: Set[str]) -> None:
    """seen 파일 저장. {id: timestamp} 형식으로 저장, 7일 초과 항목 자동 제거."""
    now = int(time.time())
    cutoff = now - (7 * 24 * 3600)
    # 기존 타임스탬프 로드
    ts_map: Dict[str, int] = {}
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                ts_map = data
        except Exception:
            pass
    # 새 항목은 현재 시각, 기존 항목은 원래 시각 유지, 7일 초과 항목 제거
    result: Dict[str, int] = {}
    for item_id in seen:
        ts = ts_map.get(item_id, now)
        if ts >= cutoff:
            result[item_id] = ts
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)


def _title_hash(title: str) -> str:
    """뉴스 제목의 해시 키를 반환합니다 (ID가 바뀌어도 동일 기사 중복 전송 방지)."""
    normalized = title.lower().strip()
    return "title_" + hashlib.md5(normalized.encode("utf-8")).hexdigest()[:16]


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


# ─── 주가 변동 체크 ───────────────────────────────────────────────────────────
# "TICKER_YYYYMMDD_up" 또는 "_down" 키 → 해당 날 트리거된 최대 레벨
_price_levels: Dict[str, int] = {}


def check_prices(config: dict) -> int:
    """등록 티커의 주가를 조회하고, 임계값 배수마다 새 레벨에서 Discord 알람을 보냅니다.

    동작 방식:
      threshold=5% 이면 5%/10%/15%... 돌파 시마다 알람.
      공배포(15% 돌파 후 10%로 다시 하락)는 알람 안 보냄.
      날짜가 바뀌면 레벨 자동 초기화.

    Yahoo Finance의 marketState가 UNKNOWN으로 자주 들어오므로,
    장 상태(REGULAR/PRE/POST/CLOSED/UNKNOWN)는 알림 차단 조건으로 사용하지 않는다.
    """
    if not config.get("monitor_price", True):
        logger.info("주가 변동 알람 비활성화 상태 — 체크 생략")
        return 0

    webhook_url = config["discord_webhook_url"]
    tickers = config.get("tickers", [])
    threshold = float(config.get("price_alert_threshold_pct", 5.0) or 5.0)
    today = date.today().strftime("%Y%m%d")
    count = 0
    checked = 0

    for ticker in tickers:
        info = fetch_price(ticker)
        if not info:
            logger.warning("[%s] 주가 조회 실패", ticker)
            continue

        checked += 1
        change_pct = float(info.get("change_pct", 0.0) or 0.0)
        market_state = info.get("market_state", "UNKNOWN")
        direction = "up" if change_pct >= 0 else "down"
        current_level = int(abs(change_pct) / threshold) if threshold > 0 else 0

        if current_level == 0:
            logger.info(
                "[%s] 주가 체크: %.2f%% / 임계값 %.2f%% 미달 (market=%s)",
                ticker, change_pct, threshold, market_state,
            )
            continue

        level_key = f"{ticker}_{today}_{direction}"
        max_alerted = _price_levels.get(level_key, 0)

        if current_level <= max_alerted:
            logger.info(
                "[%s] 주가 체크: %.2f%% 레벨%d, 이미 레벨%d까지 알림 완료 (market=%s)",
                ticker, change_pct, current_level, max_alerted, market_state,
            )
            continue

        # 장 상태와 무관하게 새로 돌파한 레벨마다 알림
        for lvl in range(max_alerted + 1, current_level + 1):
            target_pct = lvl * threshold * (1 if direction == "up" else -1)
            alert_info = dict(info)
            alert_info["alert_level"] = lvl
            alert_info["target_pct"] = target_pct
            alert_info["threshold"] = threshold
            if send_price_alert(webhook_url, alert_info):
                _price_levels[level_key] = lvl
                count += 1
                logger.info(
                    "[%s] 주가 레벨%d 알람 전송: %.2f%% 돌파 (현재 %.2f%%, 가격 %.2f, market=%s)",
                    ticker, lvl, target_pct, change_pct, info["price"], market_state,
                )
                time.sleep(0.5)
            else:
                logger.warning("[%s] 주가 레벨%d Discord 전송 실패", ticker, lvl)
        _price_levels[level_key] = current_level

    logger.info("주가 체크 완료 — 조회 %d개, 새 알람 %d건", checked, count)
    return count


# ─── 뉴스 체크 ────────────────────────────────────────────────────────────────
def check_news(config: dict, seen: Set[str], initial: bool = False) -> int:
    """등록된 모든 소스에서 뉴스를 확인하고 새 항목을 Discord로 전송합니다."""
    webhook_url = config["discord_webhook_url"]
    tickers = config.get("tickers", [])
    gemini_api_key = config.get("gemini_api_key", "").strip()
    count = 0

    for ticker in tickers:
        items = fetch_all_news(ticker, config)
        for item in items:
            item_id = item["id"]
            title_hash = _title_hash(item.get("title", ""))
            # ID 또는 제목 해시로 중복 체크 (RSS ID가 바뀌어도 동일 기사 재전송 방지)
            if not item_id:
                continue
            already_seen = item_id in seen or title_hash in seen
            # ID와 title_hash 모두 등록 (RSS가 ID를 재생성해도 같은 기사 재전송 방지)
            seen.add(item_id)
            seen.add(title_hash)
            if already_seen:
                continue
            if not initial:
                # AI 한글 요약 (gemini_api_key가 설정된 경우에만)
                if gemini_api_key:
                    item["ai_summary"] = ai_summarize_news(
                        item.get("title", ""),
                        item.get("publisher", ""),
                        gemini_api_key,
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
    max_age_days = config.get("sec_max_age_days", 30)
    gemini_api_key = config.get("gemini_api_key", "").strip()
    count = 0

    for ticker in tickers:
        items = fetch_sec_filings(ticker, form_types)
        # 날짜 필터 적용 (오래된 공시 차단 — 신규 티커 추가 시 과거 폭탄 방지)
        items = filter_sec_by_age(items, max_age_days)
        for item in items:
            item_id = item["id"]
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            if not initial:
                # AI 한글 요약
                if gemini_api_key:
                    summary_title = (
                        f"[{ticker}] SEC {item.get('form_type','')} 공시 "
                        f"— {item.get('description','') or item.get('form_type','')}"
                    )
                    item["ai_summary"] = ai_summarize_news(
                        summary_title, "SEC EDGAR", gemini_api_key
                    )
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
    seen_sec_tickers: Set[str] = load_seen(SEEN_SEC_TICKERS_FILE)  # 초기화된 티커 집합
    seen_tweets: Set[str] = load_seen(SEEN_TWEETS_FILE)

    news_interval = config.get("check_interval_seconds", 300)
    sec_interval = config.get("sec_check_interval_seconds", 1800)
    twitter_interval = config.get("twitter_check_interval_seconds", 600)
    price_interval = config.get("price_check_interval_seconds", 300)
    monitor_sec = config.get("monitor_sec_filings", False)
    monitor_twitter = config.get("monitor_twitter", False)
    monitor_price = config.get("monitor_price", True)

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
    threshold = config.get("price_alert_threshold_pct", 5.0)
    logger.info("주가 변동 알람: %.1f%% 이상 시 알람  (체크 주기: %d초)", threshold, price_interval)
    logger.info("=" * 60)

    # ── 초기 로드 (기존 항목은 알람 없이 seen 처리) ──────────────────────────
    logger.info("기존 뉴스 초기 로드 중 (알람 없음)...")
    check_news(config, seen_news, initial=True)
    save_seen(SEEN_NEWS_FILE, seen_news)

    if monitor_sec:
        logger.info("기존 SEC 공시 초기 로드 중 (알람 없음)...")
        check_sec(config, seen_sec, initial=True)
        save_seen(SEEN_SEC_FILE, seen_sec)
        # 현재 티커들을 모두 초기화 완료로 기록
        for t in config.get("tickers", []):
            seen_sec_tickers.add(t.upper())
        save_seen(SEEN_SEC_TICKERS_FILE, seen_sec_tickers)

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
        # 새로 추가된 티커 감지 → 과거 공시 폭탄 방지를 위해 초기 로드 먼저 수행
        current_tickers = {t.upper() for t in cfg.get("tickers", [])}
        new_tickers = current_tickers - seen_sec_tickers
        if new_tickers:
            logger.info(
                "새 티커 감지 — SEC 초기 로드 중 (알람 없음): %s",
                ", ".join(sorted(new_tickers)),
            )
            temp_cfg = dict(cfg)
            temp_cfg["tickers"] = list(new_tickers)
            check_sec(temp_cfg, seen_sec, initial=True)
            seen_sec_tickers.update(new_tickers)
            save_seen(SEEN_SEC_TICKERS_FILE, seen_sec_tickers)
            save_seen(SEEN_SEC_FILE, seen_sec)
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

    def price_job() -> None:
        cfg = load_config()
        count = check_prices(cfg)
        if count > 0:
            logger.info("주가 변동 알람 전송: %d건", count)

    schedule.every(news_interval).seconds.do(news_job)
    if monitor_sec:
        schedule.every(sec_interval).seconds.do(sec_job)
    if monitor_twitter:
        schedule.every(twitter_interval).seconds.do(twitter_job)
    if monitor_price:
        schedule.every(price_interval).seconds.do(price_job)

    # ── 메인 루프 ─────────────────────────────────────────────────────────────
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
