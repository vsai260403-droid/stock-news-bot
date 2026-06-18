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

from ai_provider import ai_fallback_available
from app_state import (
    APP_DIR,
    SEEN_CHART_SIGNALS_FILE,
    SEEN_FEAR_GREED_FILE,
    SEEN_NEWS_FILE,
    SEEN_PRICE_LEVELS_FILE,
    SEEN_SEC_FILE,
    SEEN_SEC_TICKERS_FILE,
    SEEN_TWEETS_FILE,
    SEEN_LINKEDIN_FILE,
    SEEN_OFFICIAL_FILE,
    DEFAULT_GEMINI_MODEL,
    load_config,
    load_int_map,
    load_seen_map,
    load_seen,
    save_int_map,
    save_json,
    save_seen,
)
from chart_signal_analyzer import analyze_chart_signal, signal_identity
from discord_bot import start_bot_thread
from discord_notifier import send_chart_signal_alert, send_fear_greed_alert, send_linkedin_alert, send_news_alert, send_official_alert, send_sec_alert, send_tweet_alert, send_price_alert
from fear_greed_fetcher import fetch_fear_greed_index
from linkedin_fetcher import fetch_all_linkedin_posts
from market_signal_analyzer import analyze_price_move
from news_fetcher import fetch_all_news, ai_summarize_news, news_title_hash
from official_fetcher import fetch_all_official_posts
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


def load_chart_signal_seen(cooldown_hours: int) -> Dict[str, int]:
    retention_days = max(1, int(cooldown_hours / 24) + 2)
    seen = load_seen_map(SEEN_CHART_SIGNALS_FILE, retention_days=retention_days)
    cutoff = int(time.time()) - max(1, cooldown_hours) * 3600
    return {key: ts for key, ts in seen.items() if ts >= cutoff}


def save_chart_signal_seen(seen: Dict[str, int], cooldown_hours: int) -> None:
    cutoff = int(time.time()) - max(1, cooldown_hours) * 3600
    save_json(SEEN_CHART_SIGNALS_FILE, {key: ts for key, ts in seen.items() if ts >= cutoff})


def load_fear_greed_seen() -> Dict[str, int]:
    return load_seen_map(SEEN_FEAR_GREED_FILE, retention_days=14)


def save_fear_greed_seen(seen: Dict[str, int]) -> None:
    save_json(SEEN_FEAR_GREED_FILE, seen)


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


def check_chart_signals(config: dict, seen_signals: Dict[str, int], initial: bool = False) -> int:
    if not config.get("monitor_chart_signals", True):
        return 0

    webhook_url = config["discord_webhook_url"]
    tickers = config.get("tickers", [])
    cooldown_seconds = max(1, int(config.get("chart_signal_cooldown_hours", 6) or 6)) * 3600
    now = int(time.time())
    count = 0

    for ticker in tickers:
        signal = analyze_chart_signal(ticker, config)
        if not signal:
            continue

        identity = f"{ticker.upper()}_{signal_identity(signal)}"
        last_seen = int(seen_signals.get(identity, 0) or 0)
        if last_seen and now - last_seen < cooldown_seconds:
            logger.info("[%s] 차트 신호 중복 skip: %s", ticker, identity)
            continue

        seen_signals[identity] = now
        if initial:
            logger.info("[%s] 차트 신호 초기 로드 skip: %s", ticker, identity)
            continue

        if send_chart_signal_alert(webhook_url, signal):
            count += 1
            logger.info("[%s] 차트 신호 알림 전송: %s", ticker, signal.get("title"))
            time.sleep(0.5)
        else:
            logger.warning("[%s] 차트 신호 Discord 전송 실패: %s", ticker, signal.get("title"))

    return count


def check_fear_greed(config: dict, seen: Dict[str, int], force: bool = False) -> int:
    if not config.get("monitor_fear_greed", True):
        return 0

    webhook_url = config["discord_webhook_url"]
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    today_key = now_kst.strftime("%Y-%m-%d")
    alert_time = str(config.get("fear_greed_alert_time_kst", "21:00") or "21:00")
    try:
        hour_str, minute_str = alert_time.split(":", 1)
        target_hour = int(hour_str)
        target_minute = int(minute_str)
    except Exception:
        target_hour, target_minute = 21, 0

    due = now_kst.hour > target_hour or (now_kst.hour == target_hour and now_kst.minute >= target_minute)
    if not force and not due:
        return 0
    if today_key in seen:
        return 0

    item = fetch_fear_greed_index()
    if not item:
        logger.warning("CNN Fear & Greed 지수 조회 실패")
        return 0

    if send_fear_greed_alert(webhook_url, item):
        seen[today_key] = int(time.time())
        logger.info("CNN Fear & Greed 브리핑 전송: score=%.1f rating=%s", item.get("score", 0.0), item.get("rating"))
        return 1
    logger.warning("CNN Fear & Greed Discord 전송 실패")
    return 0


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

        signal_analysis = None
        if config.get("price_signal_analysis_enabled", True):
            signal_analysis = analyze_price_move(info, config)

        for lvl in range(max_alerted + 1, current_level + 1):
            target_pct = lvl * threshold * (1 if direction == "up" else -1)
            alert_info = dict(info)
            alert_info["alert_level"] = lvl
            alert_info["target_pct"] = target_pct
            alert_info["threshold"] = threshold
            if signal_analysis:
                alert_info["signal_analysis"] = signal_analysis
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
    ai_available = ai_fallback_available(config)
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
                if ai_available:
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
    ai_available = ai_fallback_available(config)
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
                if ai_available:
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
    ai_available = ai_fallback_available(config)
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
                    if ai_available and not item.get("ai_summary"):
                        item["ai_summary"] = ai_summarize_news(
                            item.get("text") or item.get("title", ""),
                            f"@{item.get('username', '')}",
                            gemini_api_key,
                            gemini_model,
                            config=config,
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
                    if ai_available and not tweet.get("ai_summary"):
                        tweet["ai_summary"] = ai_summarize_news(
                            tweet.get("text") or tweet.get("title", ""),
                            f"@{username}",
                            gemini_api_key,
                            gemini_model,
                            config=config,
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
    ai_available = ai_fallback_available(config)
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
            if ai_available and not item.get("ai_summary"):
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


# ─── 공식 IR/Newsroom 체크 ─────────────────────────────────────────────────
def check_official(config: dict, seen: Set[str], initial: bool = False) -> int:
    if not config.get("monitor_official", False):
        return 0

    webhook_url = config["discord_webhook_url"]
    gemini_api_key = config.get("gemini_api_key", "").strip()
    gemini_model = _gemini_summary_model(config)
    ai_available = ai_fallback_available(config)
    max_age_hours = config.get("official_max_age_hours", 72)
    cutoff_ts = int(time.time()) - (max_age_hours * 3600) if max_age_hours > 0 else 0
    count = 0

    for item in fetch_all_official_posts(config):
        publish_time = int(item.get("publish_time", 0) or 0)
        if cutoff_ts and publish_time and publish_time < cutoff_ts:
            logger.info("[Official] 시간 초과 skip: %s", item.get("title", "")[:120])
            continue

        item_id = item.get("id", "")
        if not item_id or item_id in seen:
            logger.info("[Official] 이미 seen skip (id=%s)", item_id)
            continue
        seen.add(item_id)

        if not initial:
            if ai_available and not item.get("ai_summary"):
                item["ai_summary"] = ai_summarize_news(
                    item.get("summary") or item.get("title", ""),
                    item.get("name", "Official"),
                    gemini_api_key,
                    gemini_model,
                    config=config,
                )
            if send_official_alert(webhook_url, item):
                count += 1
                logger.info("[Official] 알람 전송: %s", item.get("title", "")[:60])
                time.sleep(0.5)
            else:
                logger.warning("[Official] Discord 전송 실패: %s", item.get("title", "")[:120])
        else:
            logger.info("[Official] 초기 로드 skip (id=%s)", item_id)

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
    seen_official: Set[str] = load_seen(SEEN_OFFICIAL_FILE)
    chart_signal_cooldown_hours = int(config.get("chart_signal_cooldown_hours", 6) or 6)
    seen_chart_signals: Dict[str, int] = load_chart_signal_seen(chart_signal_cooldown_hours)
    seen_fear_greed: Dict[str, int] = load_fear_greed_seen()

    news_interval = config.get("check_interval_seconds", 300)
    sec_interval = config.get("sec_check_interval_seconds", 1800)
    twitter_interval = config.get("twitter_check_interval_seconds", 600)
    linkedin_interval = config.get("linkedin_check_interval_seconds", 900)
    official_interval = config.get("official_check_interval_seconds", 900)
    price_interval = config.get("price_check_interval_seconds", 300)
    chart_signal_interval = config.get("chart_signal_check_interval_seconds", 600)
    monitor_sec = config.get("monitor_sec_filings", False)
    monitor_twitter = config.get("monitor_twitter", False)
    monitor_linkedin = config.get("monitor_linkedin", False)
    monitor_official = config.get("monitor_official", False)
    monitor_price = config.get("monitor_price", True)
    monitor_chart_signals = config.get("monitor_chart_signals", True)
    monitor_fear_greed = config.get("monitor_fear_greed", True)

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
    if monitor_official:
        official_feeds = config.get("official_feeds", [])
        logger.info("공식 IR/Newsroom 모니터링: ON  (소스 %d개, 체크 주기: %d초)", len(official_feeds), official_interval)
    threshold = config.get("price_alert_threshold_pct", 5.0)
    logger.info("주가 변동 알람: %.1f%% 이상 시 알람  (체크 주기: %d초)", threshold, price_interval)
    if monitor_chart_signals:
        logger.info("차트 타점 알림: ON  (체크 주기: %d초, 쿨다운: %d시간)", chart_signal_interval, chart_signal_cooldown_hours)
    if monitor_fear_greed:
        logger.info("CNN Fear & Greed 브리핑: ON  (KST %s)", config.get("fear_greed_alert_time_kst", "21:00"))
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

    if monitor_official:
        logger.info("기존 공식 IR/Newsroom 초기 로드 중 (알람 없음)...")
        check_official(config, seen_official, initial=True)
        save_seen(SEEN_OFFICIAL_FILE, seen_official)

    if monitor_chart_signals:
        logger.info("기존 차트 타점 신호 초기 로드 중 (알람 없음)...")
        check_chart_signals(config, seen_chart_signals, initial=True)
        save_chart_signal_seen(seen_chart_signals, chart_signal_cooldown_hours)

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

    def official_job() -> None:
        cfg = load_config()
        count = check_official(cfg, seen_official)
        save_seen(SEEN_OFFICIAL_FILE, seen_official)
        if count > 0:
            logger.info("공식 IR/Newsroom 체크 완료 — 새 항목: %d건", count)

    def price_job() -> None:
        cfg = load_config()
        count = check_prices(cfg)
        if count > 0:
            logger.info("주가 변동 알람 전송: %d건", count)

    def chart_signal_job() -> None:
        cfg = load_config()
        cooldown_hours = int(cfg.get("chart_signal_cooldown_hours", 6) or 6)
        count = check_chart_signals(cfg, seen_chart_signals)
        save_chart_signal_seen(seen_chart_signals, cooldown_hours)
        if count > 0:
            logger.info("차트 타점 알림 전송: %d건", count)

    def fear_greed_job() -> None:
        cfg = load_config()
        count = check_fear_greed(cfg, seen_fear_greed)
        if count > 0:
            save_fear_greed_seen(seen_fear_greed)

    schedule.every(news_interval).seconds.do(news_job)
    if monitor_sec:
        schedule.every(sec_interval).seconds.do(sec_job)
    schedule.every(twitter_interval).seconds.do(twitter_job)
    if monitor_linkedin:
        schedule.every(linkedin_interval).seconds.do(linkedin_job)
    schedule.every(official_interval).seconds.do(official_job)
    if monitor_price:
        schedule.every(price_interval).seconds.do(price_job)
    if monitor_chart_signals:
        schedule.every(chart_signal_interval).seconds.do(chart_signal_job)
    if monitor_fear_greed:
        schedule.every(60).seconds.do(fear_greed_job)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
