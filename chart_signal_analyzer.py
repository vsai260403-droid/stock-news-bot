"""Technical chart signal scanner for registered tickers."""
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from ai_provider import ai_fallback_available, ai_generate_with_fallback


logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; StockAlarmBot/1.0)"}


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_chart(ticker: str, range_value: str, interval: str, include_prepost: bool = True) -> Optional[Dict[str, Any]]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?range={range_value}&interval={interval}&includePrePost={'true' if include_prepost else 'false'}"
    )
    try:
        resp = requests.get(url, timeout=10, headers=_HEADERS)
        if resp.status_code != 200:
            logger.info("[%s] chart signal Yahoo 응답 오류: HTTP %s", ticker, resp.status_code)
            return None
        results = resp.json().get("chart", {}).get("result", [])
        return results[0] if results else None
    except Exception as e:
        logger.info("[%s] chart signal 데이터 조회 실패: %s", ticker, e)
        return None


def _candles(result: Dict[str, Any]) -> List[Dict[str, float]]:
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    candles: List[Dict[str, float]] = []
    size = min(len(timestamps), len(closes))
    for idx in range(size):
        close = _to_float(closes[idx])
        if close is None:
            continue
        candles.append({
            "ts": float(timestamps[idx]),
            "open": _to_float(opens[idx] if idx < len(opens) else None) or close,
            "high": _to_float(highs[idx] if idx < len(highs) else None) or close,
            "low": _to_float(lows[idx] if idx < len(lows) else None) or close,
            "close": close,
            "volume": _to_float(volumes[idx] if idx < len(volumes) else None) or 0.0,
        })
    return candles


def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) <= period:
        return None
    gains = 0.0
    losses = 0.0
    recent = values[-(period + 1):]
    for prev, curr in zip(recent, recent[1:]):
        change = curr - prev
        if change >= 0:
            gains += change
        else:
            losses += abs(change)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _vwap(candles: List[Dict[str, float]]) -> Optional[float]:
    total_pv = 0.0
    total_volume = 0.0
    for candle in candles:
        volume = candle.get("volume", 0.0)
        typical = (candle["high"] + candle["low"] + candle["close"]) / 3
        total_pv += typical * volume
        total_volume += volume
    if total_volume <= 0:
        return None
    return total_pv / total_volume


def _latest_cross(values: List[float], reference: Optional[float]) -> str:
    if reference is None or len(values) < 2:
        return "none"
    prev = values[-2]
    curr = values[-1]
    if prev <= reference < curr:
        return "cross_up"
    if prev >= reference > curr:
        return "cross_down"
    return "above" if curr > reference else "below"


def _fmt_price(value: Optional[float]) -> str:
    return f"{value:.2f}" if isinstance(value, (int, float)) else "N/A"


def _support_resistance(daily: List[Dict[str, float]]) -> Tuple[Optional[float], Optional[float]]:
    if len(daily) < 2:
        return None, None
    prev = daily[-2]
    return prev.get("low"), prev.get("high")


def _score_signal(metrics: Dict[str, Any]) -> Dict[str, Any]:
    price = metrics["price"]
    rsi = metrics.get("rsi")
    ma5 = metrics.get("ma5")
    ma20 = metrics.get("ma20")
    ma60 = metrics.get("ma60")
    vwap = metrics.get("vwap")
    volume_ratio = metrics.get("volume_ratio")
    prev_high = metrics.get("prev_high")
    prev_low = metrics.get("prev_low")

    entry_score = 0
    exit_score = 0
    reasons: List[str] = []
    risks: List[str] = []

    if rsi is not None:
        if 35 <= rsi <= 62:
            entry_score += 15
            reasons.append(f"RSI {rsi:.0f}: 과열 전 중립권")
        elif rsi < 30:
            entry_score += 10
            risks.append(f"RSI {rsi:.0f}: 낙폭은 크지만 추세 확인 필요")
        elif rsi >= 75:
            exit_score += 25
            risks.append(f"RSI {rsi:.0f}: 단기 과열")

    if ma5 and ma20:
        if price > ma5 > ma20:
            entry_score += 25
            reasons.append("현재가 > 5MA > 20MA: 단기 상승 배열")
        elif price < ma5 < ma20:
            exit_score += 25
            risks.append("현재가 < 5MA < 20MA: 단기 하락 배열")
        elif price > ma20:
            entry_score += 12
            reasons.append("20MA 위 회복")
        elif price < ma20:
            exit_score += 12
            risks.append("20MA 아래 약세")

    if ma60:
        if price > ma60:
            entry_score += 10
        else:
            exit_score += 10
            risks.append("60MA 아래: 중기 추세 부담")

    vwap_state = _latest_cross(metrics.get("intraday_closes", []), vwap)
    if vwap_state in ("cross_up", "above"):
        entry_score += 20 if vwap_state == "cross_up" else 10
        reasons.append("VWAP 위 회복/유지")
    elif vwap_state in ("cross_down", "below"):
        exit_score += 20 if vwap_state == "cross_down" else 10
        risks.append("VWAP 아래 이탈/약세")

    if prev_high and price > prev_high:
        entry_score += 20
        reasons.append("전일 고가 돌파")
    if prev_low and price < prev_low:
        exit_score += 25
        risks.append("전일 저가 이탈")

    if isinstance(volume_ratio, (int, float)):
        if volume_ratio >= 2.0:
            entry_score += 10 if entry_score >= exit_score else 0
            exit_score += 10 if exit_score > entry_score else 0
            reasons.append(f"거래량 평균 대비 {volume_ratio:.1f}배")
        elif volume_ratio < 0.5:
            risks.append("거래량 부족: 신호 신뢰도 낮음")

    entry_score = max(0, min(100, entry_score))
    exit_score = max(0, min(100, exit_score))

    if entry_score >= 65 and entry_score >= exit_score + 15:
        signal_type = "entry_watch"
        title = "매수 관심 구간"
        action = "진입 관심"
    elif exit_score >= 60 and exit_score >= entry_score + 10:
        signal_type = "exit_watch"
        title = "매도/축소 주의 구간"
        action = "청산/축소 검토"
    else:
        signal_type = "neutral"
        title = "관망"
        action = "관망"

    return {
        "signal_type": signal_type,
        "title": title,
        "action": action,
        "entry_score": entry_score,
        "exit_score": exit_score,
        "reasons": reasons[:5],
        "risks": risks[:5],
    }


def _ai_commentary(signal: Dict[str, Any], metrics: Dict[str, Any], config: dict) -> Optional[str]:
    if signal.get("signal_type") == "neutral" or not ai_fallback_available(config):
        return None
    prompt = (
        "You are a trading assistant. Do not give direct buy/sell orders. "
        "Explain this chart setup in Korean in 2-3 short bullet points. "
        "Use cautious wording like 진입 관심, 추격 주의, 축소 검토.\n\n"
        f"Ticker: {metrics.get('ticker')}\n"
        f"Signal: {signal.get('title')}\n"
        f"Price: {metrics.get('price')}\n"
        f"RSI: {metrics.get('rsi')}\n"
        f"MA5/20/60: {metrics.get('ma5')}, {metrics.get('ma20')}, {metrics.get('ma60')}\n"
        f"VWAP: {metrics.get('vwap')}\n"
        f"Previous low/high: {metrics.get('prev_low')}, {metrics.get('prev_high')}\n"
        f"Volume ratio: {metrics.get('volume_ratio')}\n"
        f"Entry score: {signal.get('entry_score')} Exit score: {signal.get('exit_score')}\n"
        f"Reasons: {', '.join(signal.get('reasons') or [])}\n"
        f"Risks: {', '.join(signal.get('risks') or [])}"
    )
    return ai_generate_with_fallback(prompt, config, purpose=f"{metrics.get('ticker')} 차트 타점 해석")


def analyze_chart_signal(ticker: str, config: dict) -> Optional[Dict[str, Any]]:
    ticker = ticker.upper().strip()
    intraday_result = _fetch_chart(ticker, "5d", "5m", include_prepost=True)
    daily_result = _fetch_chart(ticker, "6mo", "1d", include_prepost=False)
    if not intraday_result or not daily_result:
        return None

    intraday = _candles(intraday_result)
    daily = _candles(daily_result)
    if len(intraday) < 20 or len(daily) < 25:
        logger.info("[%s] 차트 신호 분석 데이터 부족: intraday=%d daily=%d", ticker, len(intraday), len(daily))
        return None

    closes = [candle["close"] for candle in daily]
    intraday_closes = [candle["close"] for candle in intraday]
    price = intraday[-1]["close"]
    day_candles = intraday[-78:] if len(intraday) > 78 else intraday
    volume = sum(candle.get("volume", 0.0) for candle in day_candles)
    daily_volumes = [candle.get("volume", 0.0) for candle in daily[-20:] if candle.get("volume", 0.0) > 0]
    avg_volume = sum(daily_volumes) / len(daily_volumes) if daily_volumes else None
    volume_ratio = volume / avg_volume if volume and avg_volume else None
    prev_low, prev_high = _support_resistance(daily)

    metrics: Dict[str, Any] = {
        "ticker": ticker,
        "price": price,
        "rsi": _rsi(closes, 14),
        "ma5": _sma(closes, 5),
        "ma20": _sma(closes, 20),
        "ma60": _sma(closes, 60),
        "vwap": _vwap(day_candles),
        "prev_low": prev_low,
        "prev_high": prev_high,
        "volume": volume,
        "average_volume": avg_volume,
        "volume_ratio": volume_ratio,
        "intraday_closes": intraday_closes,
        "timestamp": int(intraday[-1]["ts"]),
    }
    signal = _score_signal(metrics)
    if signal["signal_type"] == "neutral":
        logger.info(
            "[%s] 차트 신호 없음: entry=%d exit=%d price=%s rsi=%s",
            ticker,
            signal["entry_score"],
            signal["exit_score"],
            _fmt_price(price),
            metrics.get("rsi"),
        )
        return None

    signal.update(metrics)
    signal["ai_commentary"] = _ai_commentary(signal, metrics, config)
    return signal


def signal_identity(signal: Dict[str, Any]) -> str:
    signal_type = str(signal.get("signal_type") or "neutral")
    return signal_type