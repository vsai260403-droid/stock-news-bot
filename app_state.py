"""Shared file paths and JSON persistence helpers for the stock alarm app."""
from copy import deepcopy
import json
import time
from pathlib import Path
from typing import Any, Dict, Set


APP_DIR = Path(__file__).resolve().parent

CONFIG_FILE = APP_DIR / "config.json"
SEEN_NEWS_FILE = APP_DIR / "seen_news.json"
SEEN_SEC_FILE = APP_DIR / "seen_sec.json"
SEEN_SEC_TICKERS_FILE = APP_DIR / "seen_sec_tickers.json"
SEEN_TWEETS_FILE = APP_DIR / "seen_tweets.json"
SEEN_PRICE_LEVELS_FILE = APP_DIR / "seen_price_levels.json"

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"

DEFAULT_CONFIG: Dict[str, Any] = {
    "discord_webhook_url": "YOUR_DISCORD_WEBHOOK_URL_HERE",
    "discord_bot_token": "YOUR_DISCORD_BOT_TOKEN_HERE",
    "discord_command_prefix": "!",
    "tickers": [],
    "news_sources": ["yahoo", "google_rss"],
    "finnhub_api_key": "",
    "openai_api_key": "",
    "gemini_api_key": "",
    "gemini_model": DEFAULT_GEMINI_MODEL,
    "gemini_relevance_model": DEFAULT_GEMINI_MODEL,
    "gemini_summary_model": DEFAULT_GEMINI_MODEL,
    "gemini_twitter_model": DEFAULT_GEMINI_MODEL,
    "news_max_age_hours": 24,
    "check_interval_seconds": 300,
    "monitor_sec_filings": True,
    "sec_form_types": ["8-K"],
    "sec_check_interval_seconds": 1800,
    "sec_max_age_days": 30,
    "monitor_twitter": False,
    "twitter_check_interval_seconds": 600,
    "twitter_accounts": {},
    "tweet_max_age_hours": 6,
    "global_tweet_max_age_hours": 24,
    "twitter_stale_max_age_hours": 24,
    "global_twitter_stale_max_age_hours": 24,
    "nitter_instances": [
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
        "https://nitter.catsarch.com",
        "https://nitter.unixfox.eu",
        "https://nitter.1d4.us",
    ],
    "monitor_price": True,
    "price_check_interval_seconds": 300,
    "price_alert_threshold_pct": 5.0,
}


def app_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else APP_DIR / candidate


def load_json(path: str | Path, default: Any = None) -> Any:
    target = app_path(path)
    if not target.exists():
        return default
    with target.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path: str | Path, data: Any) -> None:
    target = app_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def load_config() -> dict:
    data = load_json(CONFIG_FILE, {})
    config = deepcopy(DEFAULT_CONFIG)
    if isinstance(data, dict):
        config.update(data)
    return config


def save_config(config: dict) -> None:
    save_json(CONFIG_FILE, config)


def load_seen_map(path: str | Path, retention_days: int = 7) -> Dict[str, int]:
    data = load_json(path, {})
    now = int(time.time())
    cutoff = now - (retention_days * 24 * 3600)

    if isinstance(data, list):
        return {str(item): now for item in data}
    if not isinstance(data, dict):
        return {}

    result: Dict[str, int] = {}
    for key, value in data.items():
        if isinstance(value, (int, float)) and int(value) >= cutoff:
            result[str(key)] = int(value)
    return result


def load_seen(path: str | Path, retention_days: int = 7) -> Set[str]:
    return set(load_seen_map(path, retention_days).keys())


def save_seen(path: str | Path, seen: Set[str], retention_days: int = 7) -> None:
    now = int(time.time())
    cutoff = now - (retention_days * 24 * 3600)
    existing = load_seen_map(path, retention_days)
    result: Dict[str, int] = {}

    for item_id in seen:
        timestamp = existing.get(item_id, now)
        if timestamp >= cutoff:
            result[item_id] = timestamp

    save_json(path, result)


def load_int_map(path: str | Path) -> Dict[str, int]:
    data = load_json(path, {})
    if not isinstance(data, dict):
        return {}

    result: Dict[str, int] = {}
    for key, value in data.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def save_int_map(path: str | Path, values: Dict[str, int]) -> None:
    save_json(path, values)