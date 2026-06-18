"""
main.py - 주식 뉴스 Discord 알람 메인 실행 파일

실행: python main.py
"""
import logging
import logging.handlers
import time
import schedule

from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Dict, Set

from app_state import (
    APP_DIR,
    SEEN_NEWS_FILE,
    SEEN_PRICE_LEVELS_FILE,
    SEEN_SEC_FILE,
    SEEN_SEC_TICKERS_FILE,
    SEEN_TWEETS_FILE,
    SEEN_LINKEDIN_FILE,
    DEFAULT_GEMINI_MODEL,
    load_config,
    load_int_map,
    load_seen,
    save_int_map,
    save_seen,
)
from discord_bot import start_bot_thread
from discord_notifier import send_linkedin_alert, send_news_alert, send_sec_alert, send_tweet_alert, send_price_alert
from linkedin_fetcher import fetch_all_linkedin_posts
from news_fetcher import fetch_all_news, ai_summarize_news, news_title_hash
from price_fetcher import fetch_price
from sec_fetcher import fetch_sec_filings, filter_sec_by_age, fetch_filing_text
from twitter_fetcher import fetch_all_tweets

# ─── 로깅 설정 ────────────────────────────────────────────────────────────────
_LOG_DIR = APP_DIR / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FILE = _LOG_DIR / "main.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger(__name__)


def _gemini_summary_model(config: dict) -> str:
    """뉴스/SEC/트윗 요약에 사용할 Gemini 모델을 config에서 읽습니다."""
    return (
        str(config.get("gemini_summary_model", "") or "").strip()
        or str(config.get("gemini_model", "") or "").strip()
        or DEFAULT_GEMINI_MODEL
    )


def _gemini_relevance_model(config: dict) -> str:
    """뉴스 관련성 필터에 사용할 Gemini 모델을 config에서 읽습니다."""
    return (
        str(config.get("gemini_relevance_model", "") or "").strip()
        or str(config.get("gemini_model", "") or "").strip()
        or DEFAULT_GEMINI_MODEL
    )


def load_price_levels() -> Dict[str, int]:
    """주가 알림 레벨 기록을 파일에서 로드합니다.

    예: {"ATOM_20260527_up": 4} 는 해당 거래 세션에서 +20%까지 알림 완료를 뜻합니다.
    봇을 재시작해도 같은 레벨 알림이 다시 오지 않도록 파일에 저장합니다.
    """
    try:
        return load_int_map(SEEN_PRICE_LEVELS_FILE)
    except Exception as e:
        logger.warning("주가 알림 기록 로드 실패: %s", e)
        return {}


def save_price_levels(levels: Dict[str, int]) -> None:
    """주가 알림 레벨 기록을 파일에 저장합니다."""
    try:
        save_int_map(SEEN_PRICE_LEVELS_FILE, levels)
    except Exception as e:
        logger.warning("주가 알림 기록 저장 실패: %s", e)


def _price_session_id(info: dict) -> str:
    """PRE/REGULAR/POST가 공유하는 거래 세션 ID를 반환합니다."""
    session_id = info.get("trading_session_id")
    if session_id:
        return str(session_id)
    try:
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    except Exception:
        return date.today().strftime("%Y%m%d")


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
# "TICKER_{session_id}_up" 또는 "_down" 키 → 해당 거래 세션에서 트리거된 최대 레벨
_price_levels: Dict[str, int] = load_price_levels()
_trading_session: Dict[str, str] = {}


def check_prices(config: dict) -> int:
    """등록 티커의 주가를 조회하고, 임계값 배수마다 새 레벨에서 Discord 알람을 보냅니다."""
    global _price_levels

    if not config.get("monitor_price", True):
        logger.info("주가 변동 알람 비활성화 상태 — 체크 생략")
        return 0

    disk_levels = load_price_levels()
    if disk_levels:
        _price_levels.update(disk_levels)

    webhook_url = config["discord_webhook_url"]
    tickers = config.get("tickers", [])
    threshold = float(config.get("price_alert_threshold_pct", 5.0) or 5.0)
    count = 0
    checked = 0
    levels_changed = False

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

        if market_state not in ("REGULAR", "PRE", "POST"):
            logger.info(
                "[%s] 주가 체크 스킵: market=%s (프리장/정규장/애프터장 아님)",
                ticker, market_state,
            )
            continue

        session_id = _price_session_id(info)
        if _trading_session.get(ticker) != session_id:
            _trading_session[ticker] = session_id
            logger.info("[%s] 거래 세션 설정: session_id=%s (market=%s)", ticker, session_id, market_state)

        if current_level == 0:
            logger.info(
                "[%s] 주가 체크: %.2f%% / 임계값 %.2f%% 미달 (market=%s)",
                ticker, change_pct, threshold, market_state,
            )
            continue

        level_key = f"{ticker}_{session_id}_{direction}"
        max_alerted = _price_levels.get(level_key, 0)

        if current_level <= max_alerted:
            logger.info(
                "[%s] 주가 체크: %.2f%% 레벨%d, 이미 레벨%d까지 알림 완료 (market=%s, session=%s)",
                ticker, change_pct, current_level, max_alerted, market_state, session_id,
            )
            continue

        for lvl in range(max_alerted + 1, current_level + 1):
            target_pct = lvl * threshold * (1 if direction == "up" else -1)
            alert_info = dict(info)
            alert_info["alert_level"] = lvl
            alert_info["target_pct"] = target_pct
            alert_info["threshold"] = threshold
            if send_price_alert(webhook_url, alert_info):
                _price_levels[level_key] = lvl
                levels_changed = True
                save_price_levels(_price_levels)
                count += 1
                logger.info(
                    "[%s] 주가 레벨%d 알람 전송: %.2f%% 돌파 (현재 %.2f%%, 가격 %.2f, market=%s, session=%s)",
                    ticker, lvl, target_pct, change_pct, info["price"], market_state, session_id,
                )
                time.sleep(0.5)
            else:
                logger.warning("[%s] 주가 레벨%d Discord 전송 실패", ticker, lvl)
        if _price_levels.get(level_key, 0) < current_level:
            _price_levels[level_key] = current_level
            levels_changed = True

    if levels_changed:
        save_price_levels(_price_levels)

    logger.info("주가 체크 완료 — 조회 %d개, 새 알람 %d건", checked, count)
    return count


# ─── 뉴스 체크 ────────────────────────────────────────────────────────────────
def check_news(config: dict, seen: Set[str], initial: bool = False) -> int:
    webhook_url = config["discord_webhook_url"]
    tickers = config.get("tickers", [])
    gemini_api_key = config.get("gemini_api_key", "").strip()
    gemini_model = _gemini_summary_model(config)
    count = 0

    for ticker in tickers:
        items = fetch_all_news(ticker, config)
        for item in items:
            item_id = item["id"]
            title_hash = news_title_hash(item.get("title", ""))
            if not item_id:
                logger.info("[%s] 뉴스 제외: item_id 없음 — %s", ticker, item.get("title", "")[:120])
                continue
            already_seen = item_id in seen or title_hash in seen
            seen.add(item_id)
            seen.add(title_hash)
            if already_seen:
                logger.info(
                    "[%s] 뉴스 제외: 이미 seen 또는 재배포 중복 — %s | key=%s",
                    ticker,
                    item.get("title", "")[:120],
                    title_hash,
                )
                continue
            if not initial:
                if gemini_api_key:
                    item["ai_summary"] = ai_summarize_news(
                        item.get("title", ""),
                        item.get("publisher", ""),
                        gemini_api_key,
                        gemini_model,
                        config=config,
                        content=item.get("summary", ""),
                    )
                if send_news_alert(webhook_url, item):
                    count += 1
                    logger.info("[%s] 뉴스 알람 전송: %s", ticker, item["title"][:60])
                    time.sleep(0.5)
                else:
                    logger.warning("[%s] 뉴스 Discord 전송 실패: %s", ticker, item.get("title", "")[:120])

    return count


# ─── SEC 공시 체크 ────────────────────────────────────────────────────────────
def check_sec(config: dict, seen: Set[str], initial: bool = False) -> int:
    if not config.get("monitor_sec_filings", False):
        return 0

    webhook_url = config["discord_webhook_url"]
    tickers = config.get("tickers", [])
    form_types = config.get("sec_form_types", ["8-K"])
    max_age_days = config.get("sec_max_age_days", 30)
    gemini_api_key = config.get("gemini_api_key", "").strip()
    gemini_model = _gemini_summary_model(config)
    count = 0

    for ticker in tickers:
        items = fetch_sec_filings(ticker, form_types)
        items = filter_sec_by_age(items, max_age_days)
        for item in items:
            item_id = item["id"]
            if not item_id or item_id in seen:
                logger.info("[%s] SEC 제외: item_id 없음 또는 이미 seen — %s", ticker, item.get("title", item_id))
                continue
            seen.add(item_id)
            if not initial:
                if gemini_api_key:
                    desc = item.get("description") or item.get("form_type", "")
                    body = fetch_filing_text(
                        item.get("link", ""),
                        cik_int=item.get("_cik_int", 0),
                        accession_clean=item.get("_accession_clean", ""),
                    )
                    if body:
                        summary_input = f"[{ticker}] SEC {item.get('form_type','')} 공시 ({desc})\n\n{body}"
                    else:
                        summary_input = f"[{ticker}] SEC {item.get('form_type','')} 공시 — {desc}"
                    item["ai_summary"] = ai_summarize_news(
                        summary_input,
                        "SEC EDGAR",
                        gemini_api_key,
                        gemini_model,
                        config=config,
                    )
                if send_sec_alert(webhook_url, item):
                    count += 1
                    logger.info("[%s] SEC 공시 알람 전송: %s", ticker, item["form_type"])
                    time.sleep(0.5)
                else:
                    logger.warning("[%s] SEC Discord 전송 실패: %s", ticker, item.get("id"))

    return count


# ─── 트위터 체크 ──────────────────────────────────────────────────────────────
def check_tweets(config: dict, seen: Set[str], initial: bool = False) -> int:
    webhook_url = config["discord_webhook_url"]
    twitter_on = config.get("monitor_twitter", False)
    gemini_api_key = config.get("gemini_api_key", "").strip()
    gemini_model = _gemini_summary_model(config)
    count = 0

    if twitter_on:
        tickers = config.get("tickers", [])
        for ticker in tickers:
            items = fetch_all_tweets(ticker, config)
            for item in items:
                item_id = item["id"]
                if not item_id or item_id in seen:
                    logger.info("[%s] @%s 이미 seen skip (id=%s)", ticker, item.get("username"), item_id)
                    continue
                seen.add(item_id)
                if not initial:
                    if gemini_api_key and not item.get("ai_summary"):
                        item["ai_summary"] = ai_summarize_news(
                            item.get("text") or item.get("title", ""),
                            f"@{item.get('username', '')}",
                            gemini_api_key,
                            gemini_model,
                        )
                    if send_tweet_alert(webhook_url, item):
                        count += 1
                        logger.info("[%s] 트윗 알람 전송: @%s — %s", ticker, item["username"], item["title"][:60])
                        time.sleep(0.5)
                    else:
                        logger.warning("[%s] 트윗 Discord 전송 실패: @%s — %s", ticker, item.get("username"), item.get("title", "")[:120])
                else:
                    logger.info("[%s] @%s 초기 로드 skip (initial=True, id=%s)", ticker, item.get("username"), item_id)

    global_accounts: list = config.get("twitter_accounts", {}).get("_GLOBAL_", [])
    if global_accounts:
        from twitter_fetcher import fetch_twitter_timeline
        max_age_hours = config.get("global_tweet_max_age_hours", config.get("tweet_max_age_hours", 24))
        stale_hours = config.get("global_twitter_stale_max_age_hours", config.get("twitter_stale_max_age_hours", 24))
        cutoff_ts = int(time.time()) - (max_age_hours * 3600) if max_age_hours > 0 else 0
        for username in global_accounts:
            tweets = fetch_twitter_timeline(username, stale_max_age_hours=stale_hours, config=config)
            for tweet in tweets:
                if cutoff_ts and tweet.get("publish_time", 0) < cutoff_ts:
                    logger.info("[GLOBAL] @%s 트윗 시간 초과 skip (publish_time=%s, cutoff=%s)", username, tweet.get("publish_time"), cutoff_ts)
                    continue
                tweet["ticker"] = ""
                item_id = tweet["id"]
                if not item_id or item_id in seen:
                    logger.info("[GLOBAL] @%s 이미 seen skip (id=%s)", username, item_id)
                    continue
                seen.add(item_id)
                if not initial:
                    if gemini_api_key and not tweet.get("ai_summary"):
                        tweet["ai_summary"] = ai_summarize_news(
                            tweet.get("text") or tweet.get("title", ""),
                            f"@{username}",
                            gemini_api_key,
                            gemini_model,
                        )
                    if send_tweet_alert(webhook_url, tweet):
                        count += 1
                        logger.info("[GLOBAL] 트윗 알람 전송: @%s — %s", username, tweet["title"][:60])
                        time.sleep(0.5)
                    else:
                        logger.warning("[GLOBAL] 트윗 Discord 전송 실패: @%s — %s", username, tweet.get("title", "")[:120])
                else:
                    logger.info("[GLOBAL] @%s 초기 로드 skip (initial=True, id=%s)", username, item_id)

    return count


# ─── LinkedIn 체크 ───────────────────────────────────────────────────────────
def check_linkedin(config: dict, seen: Set[str], initial: bool = False) -> int:
    if not config.get("monitor_linkedin", False):
        return 0

    webhook_url = config["discord_webhook_url"]
    gemini_api_key = config.get("gemini_api_key", "").strip()
    gemini_model = _gemini_summary_model(config)
    max_age_hours = config.get("linkedin_max_age_hours", 24)
    cutoff_ts = int(time.time()) - (max_age_hours * 3600) if max_age_hours > 0 else 0
    count = 0

    for item in fetch_all_linkedin_posts(config):
        if cutoff_ts and item.get("publish_time", 0) and item.get("publish_time", 0) < cutoff_ts:
            logger.info(
                "[LinkedIn] 시간 초과 skip: %s | publish_time=%s",
                item.get("title", "")[:120],
                item.get("publish_time"),
            )
            continue

        item_id = item.get("id", "")
        if not item_id or item_id in seen:
            logger.info("[LinkedIn] 이미 seen skip (id=%s)", item_id)
            continue
        seen.add(item_id)

        if not initial:
            if gemini_api_key and not item.get("ai_summary"):
                item["ai_summary"] = ai_summarize_news(
                    item.get("text") or item.get("title", ""),
                    item.get("account", "LinkedIn"),
                    gemini_api_key,
                    gemini_model,
                    config=config,
                )
            if send_linkedin_alert(webhook_url, item):
                count += 1
                logger.info("[LinkedIn] 알람 전송: %s", item.get("title", "")[:60])
                time.sleep(0.5)
            else:
                logger.warning("[LinkedIn] Discord 전송 실패: %s", item.get("title", "")[:120])
        else:
            logger.info("[LinkedIn] 초기 로드 skip (id=%s)", item_id)

    return count


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main() -> None:
    config = load_config()

    if not _validate_config(config):
        return

    seen_news: Set[str] = load_seen(SEEN_NEWS_FILE)
    seen_sec: Set[str] = load_seen(SEEN_SEC_FILE)
    seen_sec_tickers: Set[str] = load_seen(SEEN_SEC_TICKERS_FILE)
    seen_tweets: Set[str] = load_seen(SEEN_TWEETS_FILE)
    seen_linkedin: Set[str] = load_seen(SEEN_LINKEDIN_FILE)

    news_interval = config.get("check_interval_seconds", 300)
    sec_interval = config.get("sec_check_interval_seconds", 1800)
    twitter_interval = config.get("twitter_check_interval_seconds", 600)
    linkedin_interval = config.get("linkedin_check_interval_seconds", 900)
    price_interval = config.get("price_check_interval_seconds", 300)
    monitor_sec = config.get("monitor_sec_filings", False)
    monitor_twitter = config.get("monitor_twitter", False)
    monitor_linkedin = config.get("monitor_linkedin", False)
    monitor_price = config.get("monitor_price", True)

    start_bot_thread(config)

    logger.info("=" * 60)
    logger.info("주식 뉴스 Discord 알람 시스템 시작")
    logger.info("모니터링 티커: %s", ", ".join(config["tickers"]))
    logger.info("뉴스 소스: %s", ", ".join(config.get("news_sources", ["yahoo", "google_rss"])))
    logger.info("Gemini 요약 모델: %s", _gemini_summary_model(config))
    logger.info("Gemini 관련성 모델: %s", _gemini_relevance_model(config))
    logger.info("뉴스 체크 주기: %d초", news_interval)
    if monitor_sec:
        logger.info("SEC 공시 체크 주기: %d초  (양식: %s)", sec_interval, ", ".join(config.get("sec_form_types", ["8-K"])))
    if monitor_twitter:
        twitter_accounts = config.get("twitter_accounts", {})
        account_summary = ", ".join(
            f"{t}: {', '.join('@' + u for u in accs)}"
            for t, accs in twitter_accounts.items()
            if t != "_GLOBAL_" and t in config.get("tickers", [])
        )
        global_accs = twitter_accounts.get("_GLOBAL_", [])
        if global_accs:
            global_summary = "전용: " + ", ".join("@" + u for u in global_accs)
            account_summary = ", ".join(filter(None, [account_summary, global_summary]))
        logger.info("트위터 모니터링: ON  (%s)", account_summary or "계정 없음")
        logger.info("트윗 체크 주기: %d초", twitter_interval)
    else:
        global_accs = config.get("twitter_accounts", {}).get("_GLOBAL_", [])
        if global_accs:
            logger.info("트위터 모니터링: OFF  (전용 팔로우: %s)", ", ".join("@" + u for u in global_accs))
    if monitor_linkedin:
        linkedin_feeds = config.get("linkedin_feeds", [])
        logger.info("LinkedIn 모니터링: ON  (피드 %d개, 체크 주기: %d초)", len(linkedin_feeds), linkedin_interval)
    threshold = config.get("price_alert_threshold_pct", 5.0)
    logger.info("주가 변동 알람: %.1f%% 이상 시 알람  (체크 주기: %d초)", threshold, price_interval)
    logger.info("=" * 60)

    logger.info("기존 뉴스 초기 로드 중 (알람 없음)...")
    check_news(config, seen_news, initial=True)
    save_seen(SEEN_NEWS_FILE, seen_news)

    if monitor_sec:
        logger.info("기존 SEC 공시 초기 로드 중 (알람 없음)...")
        check_sec(config, seen_sec, initial=True)
        save_seen(SEEN_SEC_FILE, seen_sec)
        for t in config.get("tickers", []):
            seen_sec_tickers.add(t.upper())
        save_seen(SEEN_SEC_TICKERS_FILE, seen_sec_tickers)

    if monitor_twitter:
        logger.info("기존 트윗 초기 로드 중 (알람 없음)...")
        check_tweets(config, seen_tweets, initial=True)
        save_seen(SEEN_TWEETS_FILE, seen_tweets)

    if monitor_linkedin:
        logger.info("기존 LinkedIn 피드 초기 로드 중 (알람 없음)...")
        check_linkedin(config, seen_linkedin, initial=True)
        save_seen(SEEN_LINKEDIN_FILE, seen_linkedin)

    logger.info("초기 로드 완료. 새 항목이 생기면 Discord로 알람을 보냅니다.")

    def news_job() -> None:
        cfg = load_config()
        count = check_news(cfg, seen_news)
        save_seen(SEEN_NEWS_FILE, seen_news)
        logger.info("뉴스 체크 완료 — 새 항목: %d건", count)

    def sec_job() -> None:
        cfg = load_config()
        current_tickers = {t.upper() for t in cfg.get("tickers", [])}
        new_tickers = current_tickers - seen_sec_tickers
        if new_tickers:
            logger.info("새 티커 감지 — SEC 초기 로드 중 (알람 없음): %s", ", ".join(sorted(new_tickers)))
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

    def linkedin_job() -> None:
        cfg = load_config()
        count = check_linkedin(cfg, seen_linkedin)
        save_seen(SEEN_LINKEDIN_FILE, seen_linkedin)
        if count > 0:
            logger.info("LinkedIn 체크 완료 — 새 항목: %d건", count)

    def price_job() -> None:
        cfg = load_config()
        count = check_prices(cfg)
        if count > 0:
            logger.info("주가 변동 알람 전송: %d건", count)

    schedule.every(news_interval).seconds.do(news_job)
    if monitor_sec:
        schedule.every(sec_interval).seconds.do(sec_job)
    schedule.every(twitter_interval).seconds.do(twitter_job)
    if monitor_linkedin:
        schedule.every(linkedin_interval).seconds.do(linkedin_job)
    if monitor_price:
        schedule.every(price_interval).seconds.do(price_job)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
