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

from datetime import date, datetime
from typing import Dict, Set

from discord_bot import start_bot_thread
from discord_notifier import send_news_alert, send_sec_alert, send_tweet_alert, send_price_alert
from news_fetcher import fetch_all_news, ai_summarize_news
from price_fetcher import fetch_price
from sec_fetcher import fetch_sec_filings, filter_sec_by_age, fetch_filing_text
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
# "TICKER_{session_id}_up" 또는 "_down" 키 → 해당 거래 세션에서 트리거된 최대 레벨
_price_levels: Dict[str, int] = {}
# PRE 감지 시 갱신되는 거래 세션 ID (ticker → session_id)
# PRE = 새 거래일 시작 기준. REGULAR/POST는 동일 세션 이어받음.
_trading_session: Dict[str, str] = {}


def check_prices(config: dict) -> int:
    """등록 티커의 주가를 조회하고, 임계값 배수마다 새 레벨에서 Discord 알람을 보냅니다.

    동작 방식:
      threshold=5% 이면 5%/10%/15%... 돌파 시마다 알람.
      공배포(15% 돌파 후 10%로 다시 하락)는 알람 안 보냄.
      PRE 수신 시 새 거래 세션 시작 → 레벨 자동 초기화.
      REGULAR/POST는 PRE에서 시작된 동일 세션 이어받음.
      CLOSED/UNKNOWN 등 장 외 상태는 체크 스킵.
    """
    if not config.get("monitor_price", True):
        logger.info("주가 변동 알람 비활성화 상태 — 체크 생략")
        return 0

    webhook_url = config["discord_webhook_url"]
    tickers = config.get("tickers", [])
    threshold = float(config.get("price_alert_threshold_pct", 5.0) or 5.0)
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

        # REGULAR / PRE / POST 상태일 때만 체크 및 저장
        # CLOSED / UNKNOWN 등은 장이 열리지 않은 상태이므로 스킵
        if market_state not in ("REGULAR", "PRE", "POST"):
            logger.info(
                "[%s] 주가 체크 스킵: market=%s (REGULAR/PRE/POST 아님)",
                ticker, market_state,
            )
            continue

        # PRE 수신 시 새 거래 세션 시작 (타임존 하드코딩 없이 서버 상태 전환 기준)
        # REGULAR/POST는 기존 세션 이어받음, PRE가 없었던 경우(재시작 등)엔 새로 생성
        market_ts = info.get("timestamp", int(time.time()))
        if market_state == "PRE":
            new_session = str(market_ts)
            old_session = _trading_session.get(ticker)
            if old_session != new_session:
                # 같은 PRE 세션 내 중복 갱신 방지: 이전 세션과 5분 이상 차이날 때만 갱신
                if old_session is None or abs(market_ts - int(old_session)) > 300:
                    _trading_session[ticker] = new_session
                    logger.info("[%s] 새 거래 세션 시작 (PRE 감지): session_id=%s", ticker, new_session)
        elif ticker not in _trading_session:
            # 서버 재시작 등으로 PRE를 못 받은 경우 현재 timestamp로 세션 초기화
            _trading_session[ticker] = str(market_ts)
            logger.info("[%s] 거래 세션 초기화 (%s 감지): session_id=%s", ticker, market_state, str(market_ts))

        session_id = _trading_session[ticker]

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
                "[%s] 주가 체크: %.2f%% 레벨%d, 이미 레벨%d까지 알림 완료 (market=%s)",
                ticker, change_pct, current_level, max_alerted, market_state,
            )
            continue

        # REGULAR/PRE/POST 상태에서 새로 돌파한 레벨마다 알림
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
                # AI 한글 요약 — 실제 문서 본문 기반
                if gemini_api_key:
                    desc = item.get('description') or item.get('form_type', '')
                    body = fetch_filing_text(
                        item.get("link", ""),
                        cik_int=item.get("_cik_int", 0),
                        accession_clean=item.get("_accession_clean", ""),
                    )
                    if body:
                        summary_input = (
                            f"[{ticker}] SEC {item.get('form_type','')} 공시 ({desc})\n\n"
                            f"{body}"
                        )
                    else:
                        summary_input = f"[{ticker}] SEC {item.get('form_type','')} 공시 — {desc}"
                    item["ai_summary"] = ai_summarize_news(
                        summary_input, "SEC EDGAR", gemini_api_key
                    )
                if send_sec_alert(webhook_url, item):
                    count += 1
                    logger.info("[%s] SEC 공시 알람 전송: %s", ticker, item["form_type"])
                    time.sleep(0.5)

    return count


# ─── 트위터 체크 ──────────────────────────────────────────────────────────────
def check_tweets(config: dict, seen: Set[str], initial: bool = False) -> int:
    """등록된 트위터 계정에서 새 트윗을 확인하고 Discord로 전송합니다."""
    webhook_url = config["discord_webhook_url"]
    twitter_on = config.get("monitor_twitter", False)
    gemini_api_key = config.get("gemini_api_key", "").strip()
    count = 0

    # 티커 연동 계정 (트위터 알람 ON일 때만)
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
                        )
                    if send_tweet_alert(webhook_url, item):
                        count += 1
                        logger.info(
                            "[%s] 트윗 알람 전송: @%s — %s",
                            ticker,
                            item["username"],
                            item["title"][:60],
                        )
                        time.sleep(0.5)
                else:
                    logger.info("[%s] @%s 초기 로드 skip (initial=True, id=%s)", ticker, item.get("username"), item_id)

    # 티커 없는 전용 계정 (_GLOBAL_) — monitor_twitter ON/OFF와 무관하게 항상 체크
    global_accounts: list = config.get("twitter_accounts", {}).get("_GLOBAL_", [])
    if global_accounts:
        from twitter_fetcher import fetch_twitter_timeline
        # config의 nitter_instances 무시, twitter_fetcher.py의 최신 목록 사용
        max_age_hours = config.get("tweet_max_age_hours", 24)
        cutoff_ts = int(time.time()) - (max_age_hours * 3600) if max_age_hours > 0 else 0
        for username in global_accounts:
            tweets = fetch_twitter_timeline(username)  # nitter_instances=None → get_healthy_instances() 사용
            for tweet in tweets:
                if cutoff_ts and tweet.get("publish_time", 0) < cutoff_ts:
                    logger.info("[GLOBAL] @%s 트윗 시간 초과 skip (publish_time=%s, cutoff=%s)",
                                username, tweet.get("publish_time"), cutoff_ts)
                    continue
                tweet["ticker"] = ""  # 티커 없음
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
                        )
                    if send_tweet_alert(webhook_url, tweet):
                        count += 1
                        logger.info(
                            "[GLOBAL] 트윗 알람 전송: @%s — %s",
                            username,
                            tweet["title"][:60],
                        )
                        time.sleep(0.5)
                else:
                    logger.info("[GLOBAL] @%s 초기 로드 skip (initial=True, id=%s)", username, item_id)

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
            if t != "_GLOBAL_" and t in config.get("tickers", [])
        )
        global_accs = twitter_accounts.get("_GLOBAL_", [])
        if global_accs:
            global_summary = "전용: " + ", ".join("@" + u for u in global_accs)
            account_summary = ", ".join(filter(None, [account_summary, global_summary]))
        logger.info("트위터 모니터링: ON  (%s)", account_summary or "계정 없음")
        logger.info("트윗 체크 주기: %d초", twitter_interval)
    else:
        # monitor_twitter OFF여도 _GLOBAL_ 전용 계정이 있으면 로그 표시
        global_accs = config.get("twitter_accounts", {}).get("_GLOBAL_", [])
        if global_accs:
            logger.info(
                "트위터 모니터링: OFF  (전용 팔로우: %s)",
                ", ".join("@" + u for u in global_accs),
            )
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
    # twitter_job은 항상 스케줄 등록 (_GLOBAL_ 전용 계정은 monitor_twitter 무관)
    schedule.every(twitter_interval).seconds.do(twitter_job)
    if monitor_price:
        schedule.every(price_interval).seconds.do(price_job)

    # ── 메인 루프 ─────────────────────────────────────────────────────────────
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
