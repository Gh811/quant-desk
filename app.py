import hmac
import hashlib
import json
import math
import os
import time
from datetime import datetime
from collections import Counter
import threading
import sys
import ssl
import subprocess
import sqlite3
from contextlib import closing, contextmanager
import uuid
import zlib
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
import statistics
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from itertools import islice
from array import array
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen



import math
import statistics
from decimal import Decimal


US_EQUITY_ASSETS = set('SNDK SOXL KORU MU NVDA QQQ GOOGL SPCX META TSLA AMZN INTC CRCL'.split())
HOUR = 3600000


def equity_kind(symbol, item):
    if symbol[:-4] in US_EQUITY_ASSETS:
        return 'us'
    if not item:
        return None
    tags = {str(tag).upper() for tag in [item.get('underlyingType'), *(item.get('underlyingSubType') or [])]}
    if tags & {'COMMODITY', 'METAL', 'FOREX', 'FX', 'PRECIOUS_METAL', 'ENERGY'} or symbol[:-4] in {
            'XAU', 'XAG', 'XPT', 'XPD', 'CL', 'BZ', 'WTI', 'BRENT', 'NG'}:
        return None
    if tags & {'HK', 'KOREA', 'JAPAN', 'KR', 'JP', 'NON_US_EQUITY'} or symbol[:-4] in {'SKHYNIX', 'SAMSUNG'}:
        return 'other'
    if tags & {'STOCK', 'STOCKS', 'EQUITY', 'EQUITIES', 'ETF', 'US_STOCK', 'US_EQUITY', 'US_STOCKS', 'US_EQUITIES'}:
        return 'us'
    return 'unclassified' if item['contractType'] == 'TRADIFI_PERPETUAL' else None


def trend_metrics(bars, direction=1, *, fast_period=12, slow_period=48):
    closes = [bar[4] for bar in bars]
    fast = statistics.mean(closes[-fast_period:])
    slow = statistics.mean(closes[-slow_period:])
    true_ranges = [
        max(high-low, abs(high-prev[4]), abs(low-prev[4]))
        for prev, (_, _, high, low, _) in zip(bars[-15:-1], bars[-14:])
    ]
    atr_fraction = statistics.mean(true_ranges) / closes[-1]
    return direction*closes[-1] > direction*fast > direction*slow, Decimal(str(atr_fraction))


STRATEGY_NAMES = {
    'mu_relative_strength': 'MU · 15 分钟相对强弱回撤',
    'swing': '中长线 · 1 小时双向突破与回踩',
    'intraday': '短线 · 15 分钟双向突破与回踩',
    'scalp': '1 分钟双向 · 高频测试',
    'fast': '短线 · 5 分钟双向突破与回踩',
    'us_open': '美股双向测试',
}
SIGNAL_TIMEFRAMES = {
    'mu_relative_strength': ('15m', '1h'),
    'scalp': ('1m', '5m', '15m'),
    'fast': ('5m', '15m', '1h'),
    'intraday': ('15m', '1h', '4h'),
    'swing': ('1h', '4h', '1d'),
    'us_open': ('15m', '1h', '4h'),
}
TIMEFRAME_MINUTES = {'1m': 1, '5m': 5, '15m': 15, '1h': 60, '4h': 240, '1d': 1440}
STRATEGY_INTERVALS = {mode: TIMEFRAME_MINUTES[frames[0]] * 60000 for mode, frames in SIGNAL_TIMEFRAMES.items()}
STRATEGY_LOOKBACK = {'mu_relative_strength': 20, 'scalp': 6, 'fast': 12, 'intraday': 20, 'swing': 24, 'us_open': 20}
STRATEGY_MAX_ATR = {'mu_relative_strength': Decimal('.012'), 'scalp': Decimal('.008'), 'fast': Decimal('.008'), 'intraday': Decimal('.012'), 'swing': Decimal('.02'),
                    'us_open': Decimal('.015')}
ENTRY_EQUITY_RISK = {'mu_relative_strength': Decimal('.00325'), 'fast': Decimal('.005'), 'intraday': Decimal('.00325'), 'swing': Decimal('.0075'),
                     'us_open': Decimal('.002')}
SHORT_COST_CAP = Decimal('.30')
SHORT_REENTRY_COOLDOWN_BARS = 3
FAST_TREND_TARGET = Decimal('3')
FAST_TREND_TRAIL_START = Decimal('1.5')
FAST_REVIEW_RETRY_MS = 30000
FAST_LEARNING_WINDOW = 60 * 24 * HOUR
FAST_LEARNING_HALF_LIFE = 14 * 24 * HOUR


def entry_stop_fraction(asset, mode, volatility, *, us_equity=False):
    if mode == 'us_open':
        return min(Decimal('.025'), max(Decimal('.008'), Decimal('2.5') * volatility))
    if mode == 'fast' and (us_equity or asset in US_EQUITY_ASSETS):
        return min(Decimal('.02'), max(Decimal('.0065'), Decimal('2.5') * volatility))
    low, high = (Decimal('.005'), Decimal('.02')) if mode == 'swing' else (Decimal('.0035'), Decimal('.01'))
    return min(high, max(low, 2 * volatility))


def entry_notional_budget(mode, equity, free_margin, leverage, slots, stop, cost, risk_scale=1):
    capital_cap = equity * (3 if mode == 'swing' else 1)
    margin_cap = leverage * free_margin * Decimal('.95')
    if mode == 'scalp':
        return max(Decimal(0), min(capital_cap, margin_cap))
    if mode != 'fast':
        margin_cap /= slots
    risk_cap = equity * ENTRY_EQUITY_RISK[mode] * risk_scale / (stop + cost)
    return max(Decimal(0), min(capital_cap, margin_cap, risk_cap))


def entry_signal(symbol, strategy, bars):
    lookback = STRATEGY_LOOKBACK[strategy]
    close = bars[-1][4]
    fast_period, slow_period = (6, 20) if strategy == 'scalp' else (12, 48)
    direction = 1
    trend, volatility = trend_metrics(bars, fast_period=fast_period, slow_period=slow_period)
    if not trend:
        trend, _ = trend_metrics(bars, -1, fast_period=fast_period, slow_period=slow_period)
        if trend:
            direction = -1
    recent = bars[-lookback-1:-1]
    boundary = max(bar[2] for bar in recent) if direction > 0 else min(bar[3] for bar in recent)
    breakout = direction*(close/boundary - 1)
    score = None
    if not trend:
        reason = '均线趋势未确认'
    elif volatility > STRATEGY_MAX_ATR[strategy]:
        reason = f'ATR 超过 {STRATEGY_MAX_ATR[strategy]:.1%}'
    elif breakout > 0:
        reason = '突破做多' if direction > 0 else '跌破做空'
        score = direction*breakout
    else:
        fast_average = statistics.mean(bar[4] for bar in bars[-fast_period:])
        previous_average = statistics.mean(bar[4] for bar in bars[-fast_period-1:-1])
        previous = bars[-2]
        if direction > 0 and previous[3] <= previous_average and close > max(fast_average, previous[2]):
            reason, score = '回踩做多', close / max(fast_average, previous[2]) - 1
        elif direction < 0 and previous[2] >= previous_average and close < min(fast_average, previous[3]):
            reason, score = '反抽做空', close / min(fast_average, previous[3]) - 1
        else:
            reason = '等待向上突破或回踩' if direction > 0 else '等待向下跌破或反抽'
            reason += f' · 距前 {lookback} 根极值 {-breakout:.2%}'
    return bars[-1][0], score, volatility, reason


def fast_volatility_setup(bars, direction, volatility):
    ranges = [max(bar[2]-bar[3], abs(bar[2]-prev[4]), abs(bar[3]-prev[4])) / prev[4]
              for prev, bar in zip(bars[-24:-1], bars[-23:])]
    baseline = statistics.median(ranges[:-3])
    _, opened, high, low, close = bars[-1]
    wick = min(high-max(opened, close), min(opened, close)-low)
    stressed = (baseline > 0 and max(ranges[-3:]) >= 2 * baseline
                or high > low and wick >= .25 * (high-low))
    if not stressed:
        return False, None
    tolerance = float(volatility) / 4
    for offset in range(len(bars)-2, len(bars)-5, -1):
        prior = bars[offset-STRATEGY_LOOKBACK['fast']:offset]
        level = max(bar[2] for bar in prior) if direction > 0 else min(bar[3] for bar in prior)
        touch = low if direction > 0 else high
        if (direction * (bars[offset][4]/level - 1) > 0
                and all(direction * (bar[4]/level - 1) >= 0 for bar in bars[offset+1:])
                and abs(touch/level - 1) <= tolerance
                and direction * (close/opened - 1) > 0):
            return True, level
    return True, None


def fast_structure_stop(entry, direction, extreme, volatility, base_stop, contract_type):
    invalidation = extreme * (1 - direction * volatility / 4)
    distance = max(base_stop, direction * (1 - invalidation/entry))
    maximum = Decimal('.025') if contract_type == 'TRADIFI_PERPETUAL' else Decimal('.015')
    if distance > maximum:
        raise ValueError(f'结构止损需 {distance:.2%}，超过 {maximum:.2%} 上限')
    return distance


def evaluate_entry_signal(symbol, mode, now, read, *, us_equity=False, continuation=False):
    base, confirm, backdrop = SIGNAL_TIMEFRAMES[mode]
    bars, raw = read(base, now, 49)
    candle, score, volatility, reason = entry_signal(symbol, mode, bars)
    stressed = False
    retest = None
    if mode == 'fast' and 0 < volatility <= STRATEGY_MAX_ATR[mode]:
        direction = next((side for side in (1, -1) if trend_metrics(bars, side)[0]), None)
        if direction is not None:
            stressed, retest = fast_volatility_setup(bars, direction, volatility)
            if stressed and retest is None:
                column = 2 if direction > 0 else 3
                lookback = STRATEGY_LOOKBACK['fast']
                level = (max if direction > 0 else min)(bar[column] for bar in bars[-lookback-2:-2])
                previous, current = bars[-2:]
                if (not continuation or direction * (previous[4] - level) <= 0
                        or direction * (current[3 if direction > 0 else 2] - level) <= 0
                        or direction * (current[4] - previous[4]) <= 0):
                    return candle, None, volatility, '波动突增 · 等待突破后回踩确认', {}
                score = current[4] / level - 1
                reason = '高波动突破延续做多' if direction > 0 else '高波动跌破延续做空'
            if retest is not None:
                score = bars[-1][4]/retest - 1
                reason = '高波动回踩做多' if direction > 0 else '高波动反抽做空'
    if score is None or score == 0:
        return candle, None, volatility, reason, {}

    signal_at = candle + STRATEGY_INTERVALS[mode]
    direction = 1 if score > 0 else -1
    confirmation, _ = read(confirm, signal_at, 48)
    confirmed = trend_metrics(confirmation, direction)[0]
    if mode == 'scalp':
        if not confirmed and trend_metrics(confirmation, -direction)[0]:
            return candle, None, volatility, f'{reason} · {confirm} 趋势反向', {}
    elif not confirmed:
        return candle, None, volatility, f'{reason} · {confirm} 趋势未确认', {}
    background, _ = read(backdrop, signal_at, 48)
    if trend_metrics(background, -direction)[0]:
        return candle, None, volatility, f'{reason} · {backdrop} 趋势反向', {}
    aligned = trend_metrics(background, direction)[0]
    if mode == 'fast' and (us_equity or symbol[:-4] in US_EQUITY_ASSETS) and not aligned:
        return candle, None, volatility, f'{reason} · 美股要求 1h 趋势同向', {}

    quote_volume = float(raw[-1][7])
    taker_buy = float(raw[-1][10])
    previous_volumes = [float(row[7]) for row in raw[-21:-1]]
    if (not all(math.isfinite(volume) and volume >= 0 for volume in previous_volumes)
            or not math.isfinite(quote_volume) or not 0 <= taker_buy <= quote_volume < math.inf):
        raise ValueError(f'{base} 成交量或主动成交数据无效')
    baseline = statistics.median(previous_volumes)
    if baseline <= 0 or quote_volume <= 0 or volatility <= 0:
        return candle, None, volatility, f'{reason} · 成交或波动样本不足', {}
    relative_volume = quote_volume / baseline
    side_flow = (taker_buy if direction > 0 else quote_volume - taker_buy) / quote_volume
    volume_floor = 1.2 if reason in {'突破做多', '跌破做空'} else 0.8
    if mode == 'fast':
        volume_floor = 1.0 if reason in {'突破做多', '跌破做空'} else 0.7
    if mode == 'scalp':
        volume_floor = 0.65
    if relative_volume < volume_floor:
        return candle, None, volatility, f'{reason} · 相对成交量偏低（{relative_volume:.2f}×）', {}
    if side_flow < 0.45:
        return candle, None, volatility, f'{reason} · 同向主动成交偏弱（{side_flow:.0%}）', {}
    if abs(score) > 1.5 * float(volatility):
        return candle, None, volatility, f'{reason} · 触发幅度过大，避免追价', {}

    close = bars[-1][4]
    structure = bars[-2:] if reason in {'回踩做多', '反抽做空'} else bars[-1:]
    extreme = min(bar[3] for bar in structure) if direction > 0 else max(bar[2] for bar in structure)
    obstacles = []
    if mode == 'fast':
        column = 2 if direction > 0 else 3
        for series in (confirmation, background):
            prices = [direction * bar[column] for bar in series]
            obstacles.extend(Decimal(str(direction * middle)) for left, middle, right
                             in zip(prices, prices[1:], prices[2:]) if middle > max(left, right))
    return candle, score, volatility, reason, dict(
        stressed=stressed, extreme=Decimal(str(extreme)), obstacles=obstacles,
        priority=int(aligned) + min(relative_volume, 2.0) / 2 + side_flow
                 + min(abs(score) / float(volatility), 0.75),
        close=Decimal(str(close)), boundary=Decimal(str(retest if retest is not None else close / (1 + score))),
        detail=f'{confirm}{" 同向" if confirmed else " 中性"} · {backdrop}{" 同向" if aligned else " 中性"} · '
               f'相对成交量 {relative_volume:.2f}× · 同向主动成交 {side_flow:.0%}')


def fast_position_market(now, read):
    bars, _ = read('5m', now, 50)
    closed_at = bars[-1][0] + STRATEGY_INTERVALS['fast']
    if closed_at != now // STRATEGY_INTERVALS['fast'] * STRATEGY_INTERVALS['fast']:
        raise ValueError('最新 5 分钟 K 线尚未就绪')
    higher_frames = []
    for frame in ('15m', '1h'):
        try:
            higher, _ = read(frame, closed_at, 48)
        except (OSError, ValueError, KeyError, IndexError, TypeError, ArithmeticError):
            higher = None
        higher_frames.append(higher)
    closes = [bar[4] for bar in bars]
    averages = (statistics.mean(closes[-13:-1]), statistics.mean(closes[-12:]))
    return closed_at, closes[-2:], averages, *higher_frames


def fast_position_assessment(market, opened, direction, setup, *, us_equity=False):
    if market is None or market[0] <= opened:
        return None, ''
    closed_at, closes, averages, confirmation, background = market
    close = Decimal(str(closes[-1]))
    post_entry_pair = closed_at - STRATEGY_INTERVALS['fast'] > opened
    opposite = next((frame for frame, bars in (('1 小时', background), ('15 分钟', confirmation))
                     if bars is not None and trend_metrics(bars, -direction)[0]), None)
    below_average = direction * (closes[-1] - averages[-1]) < 0
    weak_pair = post_entry_pair and all(direction * (price - mean) < 0
                                        for price, mean in zip(closes, averages))
    if setup:
        boundary, extreme, atr = (Decimal(setup[key]) for key in ('boundary', 'extreme', 'atr'))
        buffer = atr * Decimal('.15')
        lost_boundary = direction * (close / boundary - 1) < -buffer
        lost_structure = direction * (close / extreme - 1) < -buffer
        failed_pair = lost_boundary and post_entry_pair and direction * (
            Decimal(str(closes[0])) / boundary - 1) < -buffer
        if lost_structure:
            structure_confirmed = post_entry_pair and direction * (Decimal(str(closes[0])) / extreme - 1) < -buffer
            if not us_equity or opposite or structure_confirmed:
                return 'exit', f'5 分钟收盘 {close:f} 穿过入场结构极值 {extreme:f}'
        if lost_boundary and opposite:
            return 'exit', f'5 分钟收盘 {close:f} 穿过入场触发位 {boundary:f}，{opposite}趋势反向'
        if failed_pair and confirmation is not None and not trend_metrics(confirmation, direction)[0]:
            return 'exit', f'连续两根 5 分钟 K 线收盘失守触发位 {boundary:f}，15 分钟同向趋势消失'
        if lost_structure:
            return 'weak', f'首次 5 分钟收盘穿过入场结构极值 {extreme:f}，等待再次确认'
        if lost_boundary:
            return 'weak', f'5 分钟收盘 {close:f} 失守入场触发位 {boundary:f}'
    if opposite and weak_pair:
        return 'exit', f'{opposite}趋势反向，连续两根 5 分钟 K 线收在均线不利侧'
    if weak_pair or (opposite and below_average):
        return 'weak', f'{opposite}趋势反向，5 分钟收盘走弱' if opposite else '连续两根 5 分钟 K 线收盘走弱'
    return None, ''


def fast_position_levels(risk, price_return, peak, weakened):
    stop = peak - risk if peak >= FAST_TREND_TRAIL_START * risk else None
    take = None
    if weakened:
        stop = max(-Decimal('.55') * risk, stop if stop is not None else -risk)
        if peak >= Decimal('.75') * risk:
            stop = max(stop, peak - Decimal('.45') * risk)
        if price_return >= Decimal('.5') * risk:
            take = min(FAST_TREND_TARGET * risk, price_return + Decimal('.35') * risk)
    return stop, take


def fast_entry_setup(context, volatility):
    return dict(boundary=str(context['boundary']), extreme=str(context['extreme']), atr=str(volatility))


def fast_take_target(entry, direction, risk, volatility, cost, obstacles):
    target = min(FAST_TREND_TARGET * risk, max(risk, Decimal('2.5') * volatility))
    buffer = volatility * Decimal('.15')
    ahead = [direction * (level / entry - 1) for level in obstacles]
    nearest = min((distance for distance in ahead if distance > 0), default=None)
    if nearest is not None and nearest - buffer < target:
        target = nearest - buffer
        source = '附近阻力前' if direction > 0 else '附近支撑前'
    else:
        source = '近期波动范围'
    if target < max(risk * Decimal('.75'), 2 * cost):
        raise ValueError(f'{source}止盈空间仅 {target:.3%}，低于风险与交易成本要求')
    return target, source


def fast_signal_family(reason):
    if reason in {'突破做多', '跌破做空'}:
        return 'breakout'
    if reason in {'高波动回踩做多', '高波动反抽做空'}:
        return 'volatile_retest'
    if reason in {'回踩做多', '反抽做空'}:
        return 'retest'
    return None


def fast_entry_evidence(samples, symbol, direction, reason, now):
    family = fast_signal_family(reason)
    weight_sum = wins = net_r = 0.0
    count = 0
    for closed, other_symbol, side, other_family, result_r in samples:
        age = now - closed
        if side != direction or not 0 <= age <= FAST_LEARNING_WINDOW:
            continue
        similarity = 1.0 if other_family == family else .35
        if other_symbol != symbol:
            similarity *= .65
        weight = similarity * 2 ** (-age / FAST_LEARNING_HALF_LIFE)
        weight_sum += weight
        wins += weight * (result_r > 0)
        net_r += weight * result_r
        count += 1
    # Eight neutral prior samples keep sparse history from driving the decision.
    probability = (wins + 4) / (weight_sum + 8)
    expectancy = net_r / (weight_sum + 8)
    adjustment = max(-.30, min(.30, .5 * expectancy + .25 * (probability - .5)))
    risk_scale = 1.0
    if weight_sum >= 12 and expectancy < -.1:
        risk_scale = max(.8, 1 + expectancy * .25)
    return dict(samples=count, weight=weight_sum, probability=probability if count else None,
                expectancy=expectancy if count else None, adjustment=adjustment, risk_scale=risk_scale)


candle_entry_signal = entry_signal

import statistics
from collections import Counter, deque
from decimal import Decimal



BACKTEST_MAX_POSITIONS = 5
FAST_CORRELATED_ASSETS = {'BTC', 'ETH', 'SOL'}
FAST_CRYPTO_ASSETS = FAST_CORRELATED_ASSETS | {'BNB', 'ZEC'}


BACKTEST_RULES = {'current': '当前策略', 'continuation': '突破延续研究', 'confirmation': '严格确认', 'retest': '严格确认＋回踩'}


def backtest_entry_signal(symbol, mode, now, read, rules, *, us_equity=False):
    candle, score, volatility, reason, context = evaluate_entry_signal(symbol, mode, now, read, us_equity=us_equity,
                                                                      continuation=rules == 'continuation')
    if rules in {'current', 'continuation'} or score is None:
        return candle, score, volatility, reason, context
    if rules == 'retest' and mode != 'swing' and reason in {'突破做多', '跌破做空'}:
        return candle, None, volatility, '等待回踩或反抽', {}

    direction = 1 if score > 0 else -1
    base, confirm, backdrop = SIGNAL_TIMEFRAMES[mode]
    cutoff = candle + STRATEGY_INTERVALS[mode]
    background, _ = read(backdrop, cutoff, 48)
    confirmation, _ = read(confirm, cutoff, 48)
    _, raw = read(base, cutoff, 49)
    closes = [bar[4] for bar in confirmation[-12:]]
    path = sum(abs(b-a) for a, b in zip(closes, closes[1:]))
    efficiency = direction*(closes[-1]-closes[0])/path if path else 0
    buy_flow = float(raw[-1][10])/float(raw[-1][7])
    side_flow = buy_flow if direction > 0 else 1-buy_flow
    if not trend_metrics(background, direction)[0] or efficiency < .30 or side_flow < .52:
        return candle, None, volatility, '大周期方向、趋势效率或主动成交未达标', {}
    return candle, score, volatility, reason, context


def replay_portfolio(mode, markets, start, end, fee, slippage, event_at=None, *, rules='current', contracts=None, maker_fee=None):
    entry_fee = fee if maker_fee is None else maker_fee
    entry_slip = slippage if maker_fee is None else Decimal(0)
    starts = {asset: {frame: series[0][0][0] for frame, series in data.items()}
              for asset, (data, _, _) in markets.items()}
    funding = {asset: {stamp: (rate, price) for stamp, rate, price in rows}
               for asset, (_, _, rows) in markets.items()}

    def read(asset, frame, cutoff, required):
        duration = TIMEFRAME_MINUTES[frame] * 60000
        end_index = (cutoff - starts[asset][frame]) // duration
        if end_index < required:
            raise ValueError('回测预热数据不足')
        return tuple(zip(*markets[asset][0][frame][end_index-required:end_index]))

    def value(position, price):
        direction, amount, entry = (position[key] for key in ('direction', 'amount', 'entry'))
        return amount*(direction*(price-entry)-(entry_fee*entry + fee*price)) + position['funding']

    initial = Decimal(10000)
    balance = high_water = equity = initial
    drawdown = fees_paid = estimated_slippage = funding_net = Decimal(0)
    positions = {}
    trades = []
    learning_samples = {side: deque(maxlen=500) for side in (1, -1)}
    event_skips = capacity_skips = peak_positions = 0
    entry_checks = Counter()
    curve = [dict(time=start, equity=float(initial))]
    interval = STRATEGY_INTERVALS[mode]
    fast_trend = mode == 'fast'
    cost_control = mode != 'swing'
    take_multiple = FAST_TREND_TARGET if fast_trend else 2
    cost_cap = SHORT_COST_CAP if cost_control else Decimal('.75')
    reentry_at = {}
    max_hours = {'intraday': 4, 'swing': 48, 'us_open': 1.5}.get(mode)
    assets = sorted(asset for asset in markets if mode != 'us_open' or equity_kind(asset+'USDT', (contracts or {}).get(asset)) == 'us')
    us_assets = {asset for asset in assets if equity_kind(asset+'USDT', (contracts or {}).get(asset)) == 'us'}

    for index, stamp in enumerate(range(start, end, 60000)):
        if stamp % interval == 0 and equity > 0:
            candidates = []
            for asset in assets:
                if asset in positions:
                    entry_checks['持仓中'] += 1
                    continue
                if stamp < reentry_at.get(asset, 0):
                    entry_checks['平仓冷却'] += 1
                    continue
                _, score, volatility, reason, context = backtest_entry_signal(
                    asset+'USDT', mode, stamp, lambda frame, cutoff, required: read(asset, frame, cutoff, required),
                    rules, us_equity=asset in us_assets)
                if score is None:
                    label = reason.split(' · ')[0] if ' · 距前 ' in reason else reason.split(' · ')[-1]
                    label = label.split('（')[0]
                    entry_checks[label or '无入场信号'] += 1
                else:
                    entry_checks['通过信号过滤'] += 1
                    if event_at is not None and abs(stamp-event_at) <= 15*60000:
                        event_skips += 1
                    else:
                        evidence = None
                        if fast_trend:
                            direction = 1 if score > 0 else -1
                            evidence = fast_entry_evidence(learning_samples[direction], asset+'USDT', direction,
                                                           reason, stamp)
                        priority = context['priority'] + (evidence['adjustment'] if evidence else 0)
                        candidates.append((priority, asset, score, volatility, reason, context, evidence))

            free_margin = max(Decimal(0), equity - sum(p['margin'] for p in positions.values()))
            for _, asset, score, volatility, reason, context, evidence in sorted(candidates, key=lambda row: (-row[0], row[1])):
                if len(positions) == BACKTEST_MAX_POSITIONS:
                    capacity_skips += 1
                    continue
                direction = 1 if score > 0 else -1
                if (mode == 'fast' and asset in FAST_CORRELATED_ASSETS and
                        any(other in FAST_CORRELATED_ASSETS and p['direction'] == direction for other, p in positions.items())):
                    continue
                opening = Decimal(str(markets[asset][0]['1m'][index][0][1]))
                entry = opening * (1 + direction*entry_slip)
                if maker_fee is not None:
                    entry = opening * (1 - direction*Decimal('.0001'))
                if (direction*(opening/context['close']-1) > volatility/2 or
                        direction*(opening/context['boundary']-1) < (0 if context['stressed'] else -volatility/4)):
                    entry_checks['开盘价偏离信号'] += 1
                    continue
                contract = (contracts or {}).get(asset)
                stop = entry_stop_fraction(asset, mode, volatility, us_equity=equity_kind(asset+'USDT', contract) == 'us')
                if mode == 'fast':
                    try:
                        stop = fast_structure_stop(entry, direction, context['extreme'], volatility, stop,
                                                   contract['contractType'] if contract else
                                                   ('PERPETUAL' if asset in FAST_CRYPTO_ASSETS else 'TRADIFI_PERPETUAL'))
                    except ValueError:
                        entry_checks['结构止损超限'] += 1
                        continue
                cost = entry_fee + fee + entry_slip + slippage
                if cost > stop*cost_cap:
                    entry_checks['预估成本过高'] += 1
                    continue
                target_return = take_multiple*stop
                if fast_trend:
                    try:
                        target_return, _ = fast_take_target(entry, direction, stop, volatility, cost,
                                                            context.get('obstacles', ()))
                    except ValueError:
                        entry_checks['止盈空间不足'] += 1
                        continue
                leverage = 5 if mode == 'fast' else min(5 if mode == 'swing' else 3, max(1, int(Decimal('.05')/stop)))
                risk_scale = Decimal(str(evidence['risk_scale'])) if evidence else 1
                notional = entry_notional_budget(mode, equity, free_margin, leverage,
                                                 BACKTEST_MAX_POSITIONS-len(positions), stop, cost, risk_scale)
                if notional <= 0:
                    entry_checks['风险预算不足'] += 1
                    continue
                if maker_fee is not None:
                    bar = markets[asset][0]['1m'][index][0]
                    touch = Decimal(str(bar[3 if direction > 0 else 2]))
                    if direction*(touch-entry) >= 0:
                        entry_checks['挂单未成交'] += 1
                        continue
                entry_checks['模拟开仓'] += 1
                margin = notional/leverage
                free_margin -= margin
                positions[asset] = dict(opened=stamp, entry=entry, direction=direction, amount=notional/entry,
                                        risk=stop, trail=entry*(1-direction*stop), margin=margin,
                                        take=entry*(1+direction*target_return),
                                        initial_take=entry*(1+direction*target_return),
                                        setup=fast_entry_setup(context, volatility) if fast_trend else None,
                                        entry_slippage=abs(entry-opening)*notional/entry if maker_fee is None else Decimal(0),
                                        peak=Decimal(0), funding=Decimal(0), signal=reason, evidence=evidence)
                peak_positions = max(peak_positions, len(positions))

        for asset, position in list(positions.items()):
            data, marks, _ = markets[asset]
            _, mark_open, mark_high, mark_low, mark_close = marks[index][0]
            _, trade_open, _, _, trade_close = data['1m'][index][0]
            entry, direction, amount, stop = (position[k] for k in ('entry','direction','amount','risk'))
            if stamp in funding[asset] and stamp > position['opened']:
                rate, price = funding[asset][stamp]
                position['funding'] -= direction*amount*rate*price
            adverse = Decimal(str(mark_low if direction > 0 else mark_high))
            favorable = Decimal(str(mark_high if direction > 0 else mark_low))
            take = position['take']
            trail = position['trail']
            reason = None
            price = None
            if fast_trend and stamp % interval == 0 and stamp > position['opened']:
                market = fast_position_market(stamp, lambda frame, cutoff, required: read(asset, frame, cutoff, required))
                position['review'] = fast_position_assessment(
                    market, position['opened'], direction, position['setup'], us_equity=asset in us_assets)
            maker_fill_bar = maker_fee is not None and stamp == position['opened']
            if fast_trend and not maker_fill_bar:
                review, detail = position.get('review', (None, ''))
                mark = Decimal(str(mark_open))
                current = direction*(mark/entry-1)
                position['peak'] = max(position['peak'], current)
                stop_return, take_return = fast_position_levels(stop, current, position['peak'], review == 'weak')
                if review == 'exit':
                    reason, price = f'入场条件失效 · {detail}', Decimal(str(trade_open))
                elif review == 'weak' and current <= stop_return:
                    reason, price = f'行情走弱触及收紧保护线 · {detail}', Decimal(str(trade_open))
                early_breakeven = position['peak'] >= stop and current > 0
                if (stop_return is not None or early_breakeven) and reason is None:
                    candidate = entry*(1+direction*stop_return) if stop_return is not None else None
                    breakeven = (entry*(1+direction*entry_fee)-direction*position['funding']/amount)/(1-direction*fee)
                    breakeven /= 1-direction*slippage
                    if direction*(mark-breakeven) > 0:
                        candidate = (breakeven if candidate is None else
                                     (max if direction > 0 else min)(candidate, breakeven))
                    if candidate is not None and direction*(mark-candidate) > 0:
                        trail = position['trail'] = (max if direction > 0 else min)(trail, candidate)
                review_window = stamp // interval
                if take_return is not None and reason is None and position.get('take_reviewed') != review_window:
                    take = position['take'] = (min if direction > 0 else max)(take, entry*(1+direction*take_return))
                    position['take_reviewed'] = review_window
            if reason is None:
                if direction*(adverse-trail) <= 0:
                    reason = '移动止损' if trail != entry*(1-direction*stop) else '止损'
                    price = trail if maker_fill_bar else (min if direction > 0 else max)(trail, Decimal(str(trade_open)))
                elif not maker_fill_bar and direction*(favorable-take) >= 0:
                    reason, price = '动态止盈' if take != position['initial_take'] else '止盈', take
                elif max_hours is not None and stamp-position['opened'] >= max_hours*HOUR:
                    reason, price = '持仓到期', Decimal(str(trade_open))
                elif stamp+60000 >= end:
                    reason, price = '回测结束结算', Decimal(str(trade_close))
            if reason:
                price_before_slippage = price
                price *= 1-direction*slippage
                fees = amount*(entry_fee*entry+fee*price)
                fees_paid += fees
                estimated_slippage += position['entry_slippage'] + amount*abs(price-price_before_slippage)
                funding_net += position['funding']
                net = direction*amount*(price-entry)-fees+position['funding']
                balance += net
                family = fast_signal_family(position['signal'])
                if fast_trend and family and reason != '回测结束结算':
                    result_r = net / (entry * amount * stop)
                    learning_samples[direction].append((stamp + 60000, asset+'USDT', direction, family,
                                                        float(max(Decimal(-3), min(Decimal(3), result_r)))))
                trades.append(dict(symbol=asset+'USDT', opened=position['opened'], closed=stamp, entry=float(entry), exit=float(price),
                                   direction=direction, net=float(net), reason=reason, signal=position['signal'],
                                   evidence=position['evidence']))
                if cost_control:
                    reentry_at[asset] = stamp + SHORT_REENTRY_COOLDOWN_BARS * interval
                del positions[asset]
            else:
                close = Decimal(str(mark_close))
                peak = max(position['peak'], direction*(close/entry-1))
                position['peak'] = peak
                if not fast_trend and peak >= stop:
                    breakeven = (entry*(1+direction*entry_fee)-direction*position['funding']/amount)/(1-direction*fee)
                    candidate = (max if direction > 0 else min)(entry*(1+direction*(peak-stop)), breakeven)
                    if direction*(close-candidate) > 0 and direction*(candidate-trail) > 0:
                        position['trail'] = candidate
        equity = balance + sum(value(p, Decimal(str(markets[asset][0]['1m'][index][0][4])))
                               for asset, p in positions.items())
        high_water = max(high_water, equity)
        drawdown = max(drawdown, (high_water-equity)/high_water)
        if stamp % HOUR == 0 or stamp+60000 >= end:
            curve.append(dict(time=stamp+60000, equity=float(equity)))
    name = STRATEGY_NAMES[mode] if rules == 'current' else f'{STRATEGY_NAMES[mode]} · {BACKTEST_RULES[rules]}'
    net = balance-initial
    wins = [trade['net'] for trade in trades if trade['net'] > 0]
    losses = [-trade['net'] for trade in trades if trade['net'] < 0]
    return dict(mode=mode, name=name, initial=float(initial), net=float(net),
                gross=float(net+fees_paid+estimated_slippage-funding_net), fees=float(fees_paid),
                slippage=float(estimated_slippage), funding=float(funding_net),
                roi=float(balance/initial-1), drawdown=float(drawdown), count=len(trades),
                win_rate=len(wins)/len(trades) if trades else None,
                payoff_ratio=statistics.mean(wins)/statistics.mean(losses) if wins and losses else None,
                profit_factor=sum(wins)/sum(losses) if losses else None,
                expectancy=float(net/len(trades)) if trades else None,
                curve=curve, trades=list(reversed(trades)), event_skips=event_skips,
                capacity_skips=capacity_skips, peak_positions=peak_positions, assets=assets,
                entry_checks=dict(entry_checks.most_common()))


ASSETS = 'ETH BTC XAU SNDK SOXL KORU MU NVDA SOL SKHYNIX SAMSUNG QQQ GOOGL SPCX META TSLA AMZN BNB ZEC INTC'.split()
HOSTS = {'OKX': 'https://openapi.okx.com', 'Binance': 'https://fapi.binance.com'}
DEMO_VENUE = 'Binance 模拟盘'
HOSTS[DEMO_VENUE] = 'https://demo-fapi.binance.com'
DEMO_ORDER_ACTIVE = {'NEW', 'PARTIALLY_FILLED'}
DEMO_ORDER_PENDING = {'待确认', '待核对'} | DEMO_ORDER_ACTIVE
DEMO_ORDER_TERMINAL = {'FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}
DEMO_CLOCK_TTL_NS = 60 * 1_000_000_000
TRADIFI_QUOTE_MAX_AGE_MS = 30_000
TRADIFI_QUOTE_MAX_DEVIATION = Decimal('.03')
ENTRY_WAIT_SECONDS = {'mu_relative_strength': 90, 'scalp': 8, 'fast': 60, 'intraday': 90, 'swing': 120, 'us_open': 60}
ENTRY_QUOTE_MAX_AGE_MS = 5_000
ENTRY_RETRY_COOLDOWN_SECONDS = {'mu_relative_strength': 30, 'scalp': 8, 'fast': 20, 'intraday': 30, 'swing': 60, 'us_open': 30}
demo_clock_lock = threading.Lock()
demo_clock_sample = None
catalog_locks = {venue: threading.Lock() for venue in ('Binance', DEMO_VENUE)}
catalog_cache = {}
opportunity_lock = threading.Lock()
opportunity_cache = None
opportunity_report_lock = threading.Lock()
opportunity_report = {'candle_at': None, 'rows': [], 'status': 'waiting'}


@lru_cache(maxsize=1)
def deployed_commit_lookup(directory):
    files = {}
    for name in ('app.py', 'index.html'):
        content = (directory / name).read_bytes()
        files[name] = hashlib.sha1(f'blob {len(content)}\0'.encode() + content).hexdigest()

    api = 'https://api.github.com/repos/Gh811/quant-desk'
    with urlopen(api + '/commits?sha=main&per_page=8', timeout=3) as response:
        commits = json.load(response)
    for commit in commits:
        tree_sha = commit['commit']['tree']['sha']
        with urlopen(api + '/git/trees/' + tree_sha, timeout=3) as response:
            tree = json.load(response)['tree']
        if {item['path']: item['sha'] for item in tree if item['path'] in files} == files:
            return commit['sha']
    raise ValueError('未找到与部署文件匹配的提交')


def deployed_commit():
    directory = Path(__file__).resolve().parent
    try:
        revision = (directory / '.deployed-commit').read_text().strip()
    except PermissionError:
        # Existing updaters create the deployment record with a root-only umask.
        try:
            revision = deployed_commit_lookup(directory)
        except (OSError, KeyError, ValueError, TypeError):
            return None
    except (OSError, ValueError):
        return None
    if revision and len(revision) == 40 and all(char in '0123456789abcdef' for char in revision):
        return revision
    return None

def request(venue, path, **params):
    endpoint = HOSTS[venue] + path
    try:
        with urlopen(endpoint + '?' + urlencode(params), timeout=8) as response:
            payload = json.load(response)
    except HTTPError as error:
        error.msg = f'{venue} 接口拒绝请求（{endpoint}）'
        raise
    except URLError as error:
        raise ValueError(f'连接 {venue} 失败（{endpoint}）：{error.reason}') from error
    except TimeoutError as error:
        raise ValueError(f'{venue} 请求超时（{endpoint}）') from error
    if venue == 'OKX':
        if payload['code'] != '0':
            raise ValueError(payload['msg'])
        return payload['data']
    return payload


def futures_catalog(venue):
    with catalog_locks[venue]:
        cached = catalog_cache.get(venue)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        rows = request(venue, '/fapi/v1/exchangeInfo')['symbols']
        catalog = {item['symbol']: item for item in rows
                   if item['symbol'].endswith('USDT') and item.get('quoteAsset', 'USDT') == 'USDT'}
        catalog_cache[venue] = (time.monotonic(), catalog)
        return catalog


def tradable_future(item):
    return bool(item and item['status'] == 'TRADING'
                and item['contractType'] in {'PERPETUAL', 'TRADIFI_PERPETUAL'})


def market_universe():
    catalogs, errors = {}, {}
    for venue in ('Binance', DEMO_VENUE):
        try:
            catalogs[venue] = futures_catalog(venue)
        except (OSError, ValueError, KeyError) as error:
            catalogs[venue] = {}
            errors[venue] = str(error)
    live, demo = catalogs['Binance'], catalogs[DEMO_VENUE]
    default_symbols = [asset + 'USDT' for asset in ASSETS]
    pool = {symbol for symbol, item in live.items() if tradable_future(item)
            and equity_kind(symbol, item) in {'us', 'unclassified'}}
    rows = []
    for symbol in dict.fromkeys([*default_symbols, *sorted(pool)]):
        formal, simulated = live.get(symbol), demo.get(symbol)
        rows.append(dict(asset=symbol[:-4], symbol=symbol, default=symbol in default_symbols,
                         pool=symbol in pool, equity=equity_kind(symbol, formal or simulated),
                         live_tradeable=tradable_future(formal), demo_tradeable=tradable_future(simulated),
                         live_contract_type=formal['contractType'] if formal else None,
                         status=simulated['status'] if simulated else None,
                         contract_type=(simulated or formal or {}).get('contractType')))
    return dict(markets=rows, live_error=errors.get('Binance'), demo_error=errors.get(DEMO_VENUE))

def sync_demo_clock(rejected_sample=None):
    global demo_clock_sample
    with demo_clock_lock:
        sample = demo_clock_sample
        started = time.monotonic_ns()
        if sample is not None and sample is not rejected_sample and started - sample[0] < DEMO_CLOCK_TTL_NS:
            return sample
        server_time = int(request(DEMO_VENUE, '/fapi/v1/time')['serverTime'])
        received = time.monotonic_ns()
        # Bound the midpoint correction so an asymmetric connection cannot push the clock too far ahead.
        correction = min((received - started) // 2, 500_000_000)
        demo_clock_sample = (received - correction, server_time)
        return demo_clock_sample


def demo_now_ms(sample=None):
    sample = sample or demo_clock_sample
    if sample is None:
        return time.time_ns() // 1_000_000
    return sample[1] + (time.monotonic_ns() - sample[0]) // 1_000_000


def demo_signed(path, method='GET', **params):
    key = os.getenv('BINANCE_DEMO_API_KEY')
    secret = os.getenv('BINANCE_DEMO_API_SECRET')
    if not key or not secret:
        raise ValueError('请在本机设置币安模拟盘专用 API Key 和 Secret')
    def send(sample):
        query = urlencode(dict(params, timestamp=demo_now_ms(sample), recvWindow=5000))
        signature = hmac.new(secret.encode(), query.encode(), 'sha256').hexdigest()
        req = Request(HOSTS[DEMO_VENUE] + path + '?' + query + '&signature=' + signature,
                      headers={'X-MBX-APIKEY': key}, method=method)
        with urlopen(req, timeout=8) as response:
            return json.load(response)

    sample = sync_demo_clock()
    try:
        return send(sample)
    except HTTPError as error:
        with error:
            body = error.read()
        try:
            code = int(json.loads(body)['code'])
        except (ValueError, TypeError, KeyError):
            code = None
        if error.code != 400 or code != -1021:
            raise HTTPError(error.url, error.code, error.msg, error.headers, BytesIO(body)) from None
    sample = sync_demo_clock(rejected_sample=sample)
    record_runtime('签名时间已重新校准', detail=f'{method} {path} · 币安返回 -1021，重试一次')
    return send(sample)


def demo_entry_book(symbol, contract_type):
    tradifi = contract_type == 'TRADIFI_PERPETUAL'
    reference = request('Binance', '/fapi/v1/premiumIndex', symbol=symbol) if tradifi else None
    book = request(DEMO_VENUE, '/fapi/v1/ticker/bookTicker', symbol=symbol)
    bid, ask = Decimal(book['bidPrice']), Decimal(book['askPrice'])
    if not bid.is_finite() or not ask.is_finite() or not 0 < bid < ask or book.get('symbol') != symbol:
        raise ValueError('模拟盘盘口报价无效')
    if tradifi:
        mark = Decimal(reference['markPrice'])
        if reference.get('symbol') != symbol or not mark.is_finite() or mark <= 0:
            raise ValueError('币安正式合约标记价无效')
        now = demo_now_ms()
        for quote, label in ((reference, '币安正式合约标记价'), (book, '模拟盘盘口')):
            if not -1000 <= now - int(quote['time']) <= TRADIFI_QUOTE_MAX_AGE_MS:
                raise ValueError(f'{label}已过期或时间异常')
        if max(abs(bid/mark - 1), abs(ask/mark - 1)) > TRADIFI_QUOTE_MAX_DEVIATION:
            raise ValueError(f'模拟盘盘口 {bid:f}/{ask:f} 与币安正式标记价 {mark:f} 偏离超过 {TRADIFI_QUOTE_MAX_DEVIATION:.0%}')
    return book, bid, ask


def select_demo_entry(direction, book, bid, ask, budget, step, stop_loss, cost, *, minimum_quantity=None):
    price = ask if direction > 0 else bid
    amount = (budget / price // step) * step
    try:
        depth = Decimal(book.get('askQty' if direction > 0 else 'bidQty') or '0')
    except (ValueError, TypeError, InvalidOperation):
        depth = Decimal(0)
    if depth.is_finite() and depth > 0 and cost <= stop_loss / 2:
        if 0 < amount <= depth:
            return price, amount, 'IOC'
        if minimum_quantity is not None:
            available = (depth // step) * step
            if available >= max(minimum_quantity, amount * Decimal('.6')):
                return price, available, 'IOC'
    price = bid if direction > 0 else ask
    return price, (budget / price // step) * step, 'GTX'


def entry_retry_block(previous, current, cooldown_seconds, *, max_ioc_upgrade=Decimal(0)):
    if previous is None or previous[:2] != current[:2]:
        return None
    _, direction, now, price, time_in_force, cost = current
    _, _, last_at, last_price, last_time_in_force, last_cost = previous
    if now - last_at < cooldown_seconds * 1000:
        return '短暂冷却，之后继续复查盘口'
    upgrade = last_time_in_force == 'GTX' and time_in_force == 'IOC'
    if direction * (price - last_price) > (max_ioc_upgrade if upgrade else 0):
        return '价格较上次委托不利，等待更合适的盘口'
    if price == last_price and not upgrade and cost >= last_cost * Decimal('.9'):
        return '同价委托已尝试，等待成交条件改善'
    return None


def demo_entry_cost(symbol, direction, funding_window):
    fee = demo_taker_fee_rate(symbol)
    if fee is None:
        raise ValueError('手续费率未确认')
    reference = request(DEMO_VENUE, '/fapi/v1/premiumIndex', symbol=symbol)
    now = demo_now_ms()
    if reference.get('symbol') != symbol or not -1000 <= now - int(reference['time']) <= 30_000:
        raise ValueError('资金费报价已过期或时间异常')
    funding_at = int(reference['nextFundingTime'])
    if funding_at == 0:
        return fee, Decimal(0), reference
    funding_rate = Decimal(reference['lastFundingRate'])
    if not funding_rate.is_finite() or funding_at < now - 1000:
        raise ValueError('资金费率或结算时间异常')
    funding_cost = max(Decimal(0), direction * funding_rate) if funding_at <= now + funding_window else Decimal(0)
    return fee, funding_cost, reference


def set_demo_leverage(symbol, target):
    result = demo_signed('/fapi/v1/leverage', method='POST', symbol=symbol, leverage=target)
    if int(result['leverage']) != target:
        raise ValueError('交易所回报的杠杆与请求不一致')


PROTECTION_TYPES = {'stop': 'STOP_MARKET', 'take': 'TAKE_PROFIT_MARKET'}
protection_lock = threading.RLock()


def open_protection(symbol=None):
    params = {'symbol': symbol} if symbol else {}
    return demo_signed('/fapi/v1/openAlgoOrders', **params)


def owned_protection(order):
    return str(order.get('clientAlgoId', '')).startswith('quant-guard-')


def entry_side(direction):
    return 'BUY' if direction > 0 else 'SELL'


def exit_side(direction):
    return 'SELL' if direction > 0 else 'BUY'


def strategy_limit_entry(order):
    return order.get('maker_only') or order.get('time_in_force') == 'IOC'


def has_order_fill(order):
    return order['status'] == 'FILLED' or (
        strategy_limit_entry(order) and Decimal(order.get('executed_qty') or '0') > 0
    )


def protection_http_error(error, operation, symbol, client_id):
    with error:
        body = error.read()
    code = None
    message = error.msg
    try:
        payload = json.loads(body)
        code, message = payload['code'], payload['msg']
    except (ValueError, TypeError, KeyError):
        pass
    detail = f'{operation} · {symbol} · 条件单 {client_id} · 币安 {code if code is not None else error.code}：{message}'
    return code, detail, HTTPError(error.url, error.code, detail, error.headers, BytesIO(body))


def place_protection(symbol, kind, price, context=None, *, direction=1):
    client_id = 'quant-guard-' + uuid.uuid4().hex[:20]
    intent = dict(symbol=symbol, kind=kind, price=str(price), clientAlgoId=client_id, context=context, side=exit_side(direction))
    journal('demo_protection_intent', intent)
    try:
        response = demo_signed('/fapi/v1/algoOrder', method='POST', algoType='CONDITIONAL',
                               symbol=symbol, side=intent['side'], positionSide='BOTH',
                               type=PROTECTION_TYPES[kind], closePosition='true',
                               workingType='MARK_PRICE', triggerPrice=format(price, 'f'),
                               clientAlgoId=client_id)
    except HTTPError as error:
        code, detail, failure = protection_http_error(error, '提交新保护单', symbol, client_id)
        if 400 <= error.code < 500 and error.code not in {408, 429}:
            journal('demo_protection_resolved', dict(clientAlgoId=client_id, rejected=error.code,
                                                     exchange_code=code, detail=detail))
        failure.exchange_code = code
        raise failure from None
    journal('demo_protection_response', dict(intent=intent, response=response))
    try:
        orders = open_protection(symbol)
    except HTTPError as error:
        _, _, failure = protection_http_error(error, '查询新保护单', symbol, client_id)
        raise failure from None
    if not any(order['clientAlgoId'] == client_id and order['algoStatus'] == 'NEW' for order in orders):
        raise ValueError(f'提交已返回 · {symbol} · 条件单 {client_id} · 挂单列表未确认 NEW')
    try:
        journal('demo_protection_confirmed', dict(intent=intent), timeout=0.15)
    except (OSError, sqlite3.Error) as error:
        print(f'保护单跟随记录未写入：{error}', flush=True)
    return client_id


def cancel_protection(order):
    client_id = order['clientAlgoId']
    journal('demo_protection_cancel_intent', dict(symbol=order['symbol'], clientAlgoId=client_id))
    symbol = order['symbol']
    try:
        result = demo_signed('/fapi/v1/algoOrder', method='DELETE', clientAlgoId=client_id)
    except HTTPError as error:
        _, _, failure = protection_http_error(error, '撤销保护单', symbol, client_id)
        raise failure from None
    journal('demo_protection_cancel_response', dict(clientAlgoId=client_id, response=result))
    try:
        orders = open_protection(symbol)
    except HTTPError as error:
        _, _, failure = protection_http_error(error, '核对保护单撤销结果', symbol, client_id)
        raise failure from None
    if any(item['clientAlgoId'] == client_id for item in orders):
        raise ValueError(f'撤销已返回 · {symbol} · 条件单 {client_id} · 仍在挂单列表')


def confirmed_strategy_stop(symbol, target, *, direction=1):
    return any(owned_protection(order) and order['orderType'] == PROTECTION_TYPES['stop']
               and order.get('side') == exit_side(direction) and order['algoStatus'] == 'NEW'
               and direction * (Decimal(order['triggerPrice']) - target) >= 0
               for order in open_protection(symbol))


def close_strategy_position(symbol, amount, opened, leverage, opening_id, mode, decision=None):
    with protection_lock:
        fresh = demo_signed('/fapi/v3/account')
        remaining = [item for item in fresh['positions']
                     if item['symbol'] == symbol and Decimal(item['positionAmt'])]
        if not remaining:
            return
        if (len(remaining) != 1 or remaining[0].get('positionSide', 'BOTH') != 'BOTH'
                or Decimal(remaining[0]['positionAmt']) != amount):
            raise ValueError(f'{symbol} 平仓前仓位已变化，请核对模拟账户')
        order = submit_demo_order(symbol, exit_side(amount), str(abs(amount)),
                                  notional=str(abs(Decimal(remaining[0]['notional']))),
                                  leverage=leverage, reduce_only=True, opened=int(opened*1000),
                                  opening_id=opening_id, strategy=mode, decision=decision,
                                  defer_confirmation=True)
        if order.get('status') not in {'FILLED'} | DEMO_ORDER_PENDING:
            raise ValueError(f'{symbol} 平仓结果待核对')
        return order


def protection_price(entry, change, tick, *, direction, kind):
    rounding = ROUND_DOWN if (kind == 'stop') == (direction > 0) else ROUND_UP
    return (entry*(1+direction*change)/tick).to_integral_value(rounding=rounding)*tick


def entry_stop_preflight(symbol, reference, now, entry, stop_loss, tick, direction):
    mark = Decimal(reference['markPrice'])
    stop = protection_price(entry, -stop_loss, tick, direction=direction, kind='stop')
    if (reference.get('symbol') != symbol or not mark.is_finite() or min(mark, stop) <= 0
            or not -1000 <= now - int(reference['time']) <= ENTRY_QUOTE_MAX_AGE_MS):
        raise ValueError('模拟盘标记价已过期或无效')
    if direction*(mark-stop) < 2*tick:
        raise ValueError(f'模拟盘标记价 {mark:f} 已触及或过近于计划止损 {stop:f}')
    return mark, stop


def set_initial_protection(symbol, entry, stop_loss, tick, context, *, direction=1, take_multiple=2,
                           target_return=None):
    if entry <= 0 or tick <= 0:
        raise ValueError('成交均价或价格步长无效')
    with protection_lock:
        take_return = take_multiple*stop_loss if target_return is None else target_return
        for kind, change in (('stop', -stop_loss), ('take', take_return)):
            price = protection_price(entry, change, tick, direction=direction, kind=kind)
            place_protection(symbol, kind, price, context, direction=direction)


class ReplacementStopReached(ValueError):
    pass


def replace_conflicting_strategy_stop(symbol, old, target, context, *, amount):
    direction = 1 if amount > 0 else -1
    previous = Decimal(old['triggerPrice'])

    def check_position_and_mark(trigger):
        account = demo_signed('/fapi/v3/account')
        positions = [p for p in account['positions']
                     if p['symbol'] == symbol and Decimal(p['positionAmt'])]
        if (len(positions) != 1 or positions[0].get('positionSide', 'BOTH') != 'BOTH'
                or Decimal(positions[0]['positionAmt']) != amount):
            raise ValueError(f'{symbol} 替换止损期间仓位已变化，等待重新核对')
        mark = Decimal(request(DEMO_VENUE, '/fapi/v1/premiumIndex', symbol=symbol)['markPrice'])
        if not mark.is_finite() or mark <= 0:
            raise ValueError(f'{symbol} 标记价不可用')
        if direction * (mark - trigger) <= 0:
            raise ReplacementStopReached(f'标记价 {mark:f} 已触及移动止损 {trigger:f}')

    check_position_and_mark(target)
    try:
        cancel_protection(old)
        check_position_and_mark(target)
        place_protection(symbol, 'stop', target, context, direction=direction)
    except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as error:
        if isinstance(error, ReplacementStopReached):
            raise
        try:
            if not confirmed_strategy_stop(symbol, previous, direction=direction):
                check_position_and_mark(target)
                previous_intent = pending_protection().get(old['clientAlgoId'], {})
                restored_context = previous_intent.get('context') or dict(context, moved=False)
                place_protection(symbol, 'stop', previous, restored_context, direction=direction)
        except ReplacementStopReached:
            raise
        except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as restore_error:
            raise ValueError(f'{error} · 原止损恢复未确认：{restore_error}') from restore_error
        if confirmed_strategy_stop(symbol, target, direction=direction):
            return
        raise ValueError(f'{error} · 原止损仍有效或已恢复 {previous:f}') from error


def replace_conflicting_strategy_take(symbol, old, target, context, *, amount):
    direction = 1 if amount > 0 else -1
    previous = Decimal(old['triggerPrice'])

    def can_replace(trigger):
        account = demo_signed('/fapi/v3/account')
        positions = [p for p in account['positions'] if p['symbol'] == symbol
                     and Decimal(p['positionAmt'])]
        if (len(positions) != 1 or positions[0].get('positionSide', 'BOTH') != 'BOTH'
                or Decimal(positions[0]['positionAmt']) != amount):
            raise ValueError(f'{symbol} 替换止盈期间仓位已变化')
        mark = Decimal(request(DEMO_VENUE, '/fapi/v1/premiumIndex', symbol=symbol)['markPrice'])
        if not mark.is_finite() or mark <= 0 or direction*(trigger-mark) <= 0:
            raise ValueError(f'{symbol} 标记价不可用或已触及新止盈')

    def is_open(trigger):
        return any(owned_protection(order) and order['orderType'] == PROTECTION_TYPES['take']
                   and order['side'] == exit_side(direction) and order['algoStatus'] == 'NEW'
                   and Decimal(order['triggerPrice']) == trigger for order in open_protection(symbol))

    can_replace(target)
    try:
        cancel_protection(old)
        can_replace(target)
        place_protection(symbol, 'take', target, context, direction=direction)
    except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as error:
        try:
            if is_open(target):
                return
            if not is_open(previous):
                can_replace(previous)
                previous_intent = pending_protection().get(old['clientAlgoId'], {})
                restored = previous_intent.get('context') or dict(context, moved=False)
                place_protection(symbol, 'take', previous, restored, direction=direction)
        except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as restore_error:
            raise ValueError(f'{error} · 原止盈恢复未确认：{restore_error}') from restore_error
        raise ValueError(f'{error} · 原止盈仍有效或已恢复 {previous:f}') from error


def align_strategy_protection(symbol, entry, mark, stop_loss, stop, tick, context, *, direction=1, take=None,
                              take_multiple=2, review='', replace_amount=None):
    if min(entry, mark, tick) <= 0:
        raise ValueError('保护单价格数据无效')
    initial_take = protection_price(entry, take_multiple*stop_loss, tick, direction=direction, kind='take')
    if take is None:
        take = initial_take
    if min(stop, take) <= 0 or not direction*stop < direction*mark < direction*take:
        raise ValueError('标记价格已到达保护区间边界')
    with protection_lock:
        orders = open_protection(symbol)
        triggers = []
        for kind, target in (('stop', stop), ('take', take)):
            matching = [order for order in orders if owned_protection(order)
                        and order['orderType'] == PROTECTION_TYPES[kind]
                        and order.get('side') == exit_side(direction) and order['algoStatus'] == 'NEW']
            def acceptable(order):
                trigger = direction*Decimal(order['triggerPrice'])
                return (direction*target <= trigger < direction*mark if kind == 'stop'
                        else direction*mark < trigger <= direction*target)

            valid = [order for order in matching if acceptable(order)]
            if valid:
                keep = (max if kind == 'stop' else min)(
                    valid, key=lambda order: direction*Decimal(order['triggerPrice']))
            else:
                initial = (protection_price(entry, -stop_loss, tick, direction=direction, kind='stop')
                           if kind == 'stop' else initial_take)
                movement = target != initial
                if movement:
                    action = '移动止损' if kind == 'stop' else '移动止盈'
                    previous = '、'.join(str(order['triggerPrice']) for order in matching) or '无已确认挂单'
                    detail = f'{previous} → {target:f} · 标记价 {mark:f}'
                    if review:
                        detail += f' · {review}'
                    event = dict(mode=context['strategy'], scope='protection', symbol=symbol)
                    record_runtime(f'{action}已发起', detail=detail, **event)
                try:
                    protection_context = dict(context, moved=movement, review=review) if movement else context
                    try:
                        place_protection(symbol, kind, target, protection_context, direction=direction)
                    except HTTPError as conflict:
                        if (str(getattr(conflict, 'exchange_code', None)) != '-4130'
                                or len(matching) != 1 or not replace_amount):
                            raise
                        old = matching[0]
                        if (old.get('positionSide', 'BOTH') != 'BOTH'
                                or (1 if kind == 'stop' else -1) * direction
                                   * (target - Decimal(old['triggerPrice'])) <= 0):
                            raise
                        replace = (replace_conflicting_strategy_stop if kind == 'stop'
                                   else replace_conflicting_strategy_take)
                        replace(symbol, old, target, protection_context, amount=replace_amount)
                        matching = []
                except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as error:
                    if movement:
                        record_runtime(f'{action}未确认', detail=f'{detail} · {error}', **event)
                    raise
                if movement:
                    record_runtime(f'{action}已确认', detail=detail, **event)
                keep = None
            for order in matching:
                if order is not keep:
                    cancel_protection(order)
            triggers.append(Decimal(keep['triggerPrice']) if keep else target)
        return tuple(triggers)


def replace_protection(symbol, kind, raw_price):
    if not symbol.isascii() or not symbol.isalnum() or not symbol.endswith('USDT') or kind not in PROTECTION_TYPES:
        raise ValueError('合约或保护单类型无效')
    price = Decimal(raw_price)
    if not price.is_finite() or price <= 0:
        raise ValueError('触发价必须大于零')
    with protection_lock:
        account = demo_signed('/fapi/v3/account')
        positions = [p for p in account['positions'] if p['symbol'] == symbol and Decimal(p['positionAmt'])]
        if len(positions) != 1 or positions[0].get('positionSide', 'BOTH') != 'BOTH':
            raise ValueError('需要有一笔单向持仓')
        amount = Decimal(positions[0]['positionAmt'])
        direction = 1 if amount > 0 else -1
        orders = open_protection(symbol)
        relevant = [order for order in orders if order['orderType'] == PROTECTION_TYPES[kind]
                    and order.get('side') == exit_side(direction)]
        if any(not owned_protection(order) for order in relevant):
            raise ValueError('币安已有手动设置的同类保护单，请在币安修改')
        old = relevant
        if len(old) > 1:
            raise ValueError('存在多张同类保护单，请先在币安核对')
        catalog = request(DEMO_VENUE, '/fapi/v1/exchangeInfo')['symbols']
        instrument = next((item for item in catalog if item['symbol'] == symbol), None)
        if not instrument:
            raise ValueError('找不到合约价格规则')
        tick = next(Decimal(f['tickSize']) for f in instrument['filters'] if f['filterType'] == 'PRICE_FILTER')
        if tick <= 0 or price % tick:
            raise ValueError(f'触发价须为 {tick} 的整数倍')
        if old and Decimal(old[0]['triggerPrice']) == price:
            return
        mark = Decimal(request(DEMO_VENUE, '/fapi/v1/premiumIndex', symbol=symbol)['markPrice'])
        if not mark.is_finite() or mark <= 0:
            raise ValueError('币安标记价不可用')
        valid_side = direction*(mark-price) if kind == 'stop' else direction*(price-mark)
        if valid_side <= 0:
            raise ValueError(f'触发价 {price:f} 已越过当前标记价 {mark:f}，会立即触发')
        contexts = pending_protection()
        linked = next((contexts[order['clientAlgoId']].get('context')
                       for order in orders
                       if order['clientAlgoId'] in contexts and contexts[order['clientAlgoId']].get('context')), None)
        if not linked:
            latest = next((order for order in recent_orders(limit=None)
                           if order['symbol'] == symbol and has_order_fill(order)), None)
            if (latest and not latest.get('reduce_only') and latest['side'] == entry_side(amount) and latest.get('executed_qty') and
                    Decimal(latest['executed_qty']) == abs(amount) and
                    int(positions[0].get('updateTime') or 0) <= int(latest.get('opened') or 0)):
                linked = dict(opening_id=latest['exchange_order_id'], opened=latest['opened'],
                              leverage=latest.get('leverage'), strategy=latest.get('strategy'))
        place_protection(symbol, kind, price, linked, direction=direction)
        if old:
            cancel_protection(old[0])


def cleanup_protection(positions, orders):
    for order in orders:
        if owned_protection(order) and order['symbol'] not in positions:
            cancel_protection(order)


def pending_protection():
    path = journal_path()
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path)) as database:
        events = database.execute(
            "SELECT kind, payload FROM events WHERE kind IN ('demo_protection_intent','demo_protection_resolved') ORDER BY id"
        )
        pending = {}
        for kind, raw in events:
            event = json.loads(raw)
            if kind == 'demo_protection_intent':
                pending[event['clientAlgoId']] = event
            else:
                pending.pop(event['clientAlgoId'], None)
        return pending


def reconcile_protection_fill(event):
    client_id = event['clientAlgoId']
    symbol = event['symbol']
    conditional = demo_signed('/fapi/v1/algoOrder', clientAlgoId=client_id)
    child_id = conditional.get('actualOrderId')
    if child_id:
        order = demo_signed('/fapi/v1/order', symbol=symbol, orderId=child_id)
        if order.get('status') != 'FILLED':
            return
        context = event.get('context') or {}
        action = '止损' if event['kind'] == 'stop' else '止盈'
        if context.get('moved'):
            action = '移动' + action
        detail = '触发价 ' + event['price']
        if context.get('review'):
            detail += ' · ' + context['review']
        intent = dict(context, clientOrderId=client_id, symbol=symbol, side=order.get('side') or event.get('side', 'SELL'),
                      quantity=order['executedQty'], reduce_only=True,
                      decision=dict(reason=f'交易所{action}触发', detail=detail))
        order.setdefault('side', intent['side'])
        journal('demo_order_response', dict(intent=intent, response=order))
        if context.get('opening_id') is not None:
            try:
                settlement = settle_demo_trade(symbol, context['opening_id'], order, context.get('opened'))
            except (OSError, ValueError, KeyError, ArithmeticError):
                return
            journal('demo_order_settled', dict(intent=intent, response=order, **settlement))
    elif conditional.get('algoStatus') not in {'CANCELED', 'EXPIRED', 'REJECTED'}:
        return
    journal('demo_protection_resolved', dict(clientAlgoId=client_id))


def protection_watch():
    while True:
        try:
            with protection_lock:
                pending = pending_protection()
                if pending:
                    orders = open_protection()
                    account = demo_signed('/fapi/v3/account')
                    positions = {p['symbol'] for p in account['positions'] if Decimal(p['positionAmt'])}
                    cleanup_protection(positions, orders)
                    still_open = {order['clientAlgoId'] for order in orders}
                    for client_id, event in pending.items():
                        if client_id not in still_open:
                            try:
                                reconcile_protection_fill(event)
                            except (OSError, ValueError, KeyError, ArithmeticError) as error:
                                print(f'保护单 {client_id} 待核对：{error}', flush=True)
        except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as error:
            print(f'保护单对账失败：{error}', flush=True)
        try:
            retry_pending_settlements()
        except (OSError, ValueError, KeyError, sqlite3.Error) as error:
            print(f'平仓对账失败：{error}', flush=True)
        time.sleep(15)


demo_fee_rates = {}


def demo_taker_fee_rate(symbol):
    cached = demo_fee_rates.get(symbol)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        rate = Decimal(demo_signed('/fapi/v1/commissionRate', symbol=symbol)['takerCommissionRate'])
        if not rate.is_finite() or rate < 0:
            rate = None
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        rate = None
    demo_fee_rates[symbol] = (time.monotonic() + (60 if rate is not None else 15), rate)
    return rate


position_open_times = {}


def position_opened_at(position):
    symbol = position['symbol']
    side = position.get('positionSide') or 'BOTH'
    quantity = Decimal(position['positionAmt'])
    key = (symbol, side)
    fingerprint = (quantity, position.get('updateTime'))
    cached = position_open_times.get(key)
    if cached and cached[0] == fingerprint and cached[1] > time.monotonic():
        return cached[2]

    opening = (None, None)
    try:
        fills = demo_signed('/fapi/v1/userTrades', symbol=symbol, limit=1000)
        remaining = quantity
        for fill in sorted(fills, key=lambda item: (int(item['time']), int(item['id'])), reverse=True):
            if (fill.get('positionSide') or 'BOTH') != side:
                continue
            if fill['side'] not in {'BUY', 'SELL'}:
                break
            change = Decimal(fill['qty']) * (1 if fill['side'] == 'BUY' else -1)
            remaining -= change
            if remaining * quantity <= 0:
                opening = (int(fill['time']), fill.get('orderId'))
                break
    except (OSError, ValueError, KeyError, ArithmeticError):
        pass
    position_open_times[key] = (fingerprint, time.monotonic() + (60 if opening[0] else 15), opening)
    return opening


def demo_account():
    started = time.monotonic()
    try:
        account = demo_signed('/fapi/v3/account')
    except HTTPError as error:
        try:
            code = int(json.load(error)['code'])
        except (ValueError, KeyError, TypeError):
            code = '未知'
        raise ValueError(f'币安模拟盘账户接口 HTTP {error.code}，交易所错误码 {code}') from None
    latency = round((time.monotonic()-started)*1000)
    demo_return_baseline(account['totalWalletBalance'])
    positions = [p for p in account['positions'] if float(p['positionAmt']) != 0]
    symbols = sorted({p['symbol'] for p in positions})
    with ThreadPoolExecutor(max_workers=4) as pool:
        rates = dict(zip(symbols, pool.map(demo_taker_fee_rate, symbols)))
    holdings = []
    orders = recent_orders() if positions else []
    protection_error = None
    guards = []
    if positions:
        try:
            guards = open_protection()
        except (ValueError, OSError, KeyError) as error:
            protection_error = str(error)
    with ThreadPoolExecutor(max_workers=4) as pool:
        opening_times = list(pool.map(position_opened_at, positions))
    for position, (opened, opening_order_id) in zip(positions, opening_times):
        symbol = position['symbol']
        rate = rates[symbol]
        notional = position.get('notional')
        fee = float(abs(Decimal(notional))*rate) if rate is not None and notional is not None else None
        pnl = Decimal(position['unrealizedProfit'])
        quantity = Decimal(position['positionAmt'])
        mark_value = abs(Decimal(notional)) if notional is not None else None
        entry_value = mark_value - (pnl if quantity > 0 else -pnl) if mark_value is not None else None
        entry_price = position.get('entryPrice') or (str(entry_value/abs(quantity)) if entry_value is not None else None)
        mark_price = position.get('markPrice') or (str(mark_value/abs(quantity)) if mark_value is not None else None)
        latest = next((order for order in orders if order['symbol'] == symbol and has_order_fill(order)), None)
        if opened is None and latest and not latest.get('reduce_only') and latest['side'] == entry_side(quantity) and latest.get('executed_qty') and (
            Decimal(latest['executed_qty']) == abs(quantity)
            and int(position.get('updateTime') or 0) <= int(latest.get('opened') or 0)
        ):
            opened = latest.get('opened')
            opening_order_id = latest.get('exchange_order_id')
        holdings.append(dict(symbol=symbol, quantity=position['positionAmt'], opened=opened, opening_order_id=opening_order_id,
                             notional=str(abs(Decimal(notional))) if notional is not None else None,
                             pnl=str(pnl), fee=fee, entry_price=entry_price, mark_price=mark_price,
                             protection=[dict(type=g['orderType'], trigger=g['triggerPrice'],
                                              status=g['algoStatus'], owned=owned_protection(g))
                                         for g in guards if g['symbol'] == symbol
                                         and g.get('side') == exit_side(quantity)
                                         and g.get('positionSide', 'BOTH') == position.get('positionSide', 'BOTH')
                                         and g['orderType'] in PROTECTION_TYPES.values()],
                             roi=float(pnl/entry_value) if entry_value and entry_value > 0 else None))
    return dict(equity=account['totalMarginBalance'], balance=account['totalWalletBalance'],
                available=account['availableBalance'], pnl=account['totalUnrealizedProfit'],
                margin=account['totalInitialMargin'], count=len(positions),
                latency=latency, holdings=holdings, protection_error=protection_error)


def markets(venue):
    if venue == 'OKX':
        rows = request(venue, '/api/v5/public/instruments', instType='SWAP')
        return {a: a+'-USDT-SWAP' for a in ASSETS if any(
            x['instId'] == a+'-USDT-SWAP' and x['state'] == 'live' for x in rows)}
    rows = request(venue, '/fapi/v1/exchangeInfo')['symbols']
    return {a: a+'USDT' for a in ASSETS if any(
        x['symbol'] == a+'USDT' and x['status'] == 'TRADING' and
        x['contractType'] in {'PERPETUAL', 'TRADIFI_PERPETUAL'} for x in rows)}

def history(venue, symbol):
    if venue == 'OKX':
        raw = request(venue, '/api/v5/market/candles', instId=symbol, bar='1H', limit=240)
        bars = sorted((int(x[0]), *map(float, x[1:5])) for x in raw if x[8] == '1')
        raw = request(venue, '/api/v5/public/funding-rate-history', instId=symbol, limit=100)
        funding = [(int(x['fundingTime']), float(x['realizedRate'] or x['fundingRate'])) for x in raw]
    else:
        raw = request(venue, '/fapi/v1/klines', symbol=symbol, interval='1h', limit=241)
        bars = [(int(x[0]), *map(float, x[1:5])) for x in raw[:-1]]
        raw = request(venue, '/fapi/v1/fundingRate', symbol=symbol, startTime=bars[0][0], limit=1000)
        funding = [(int(x['fundingTime']), float(x['fundingRate'])) for x in raw]
    if len(bars) < 100 or any(b[0]-a[0] != HOUR for a,b in zip(bars,bars[1:])):
        raise ValueError('K 线不足或存在缺口')
    if not funding or (venue == 'OKX' and min(t for t,_ in funding) > bars[0][0]+HOUR):
        raise ValueError('资金费历史覆盖不足')
    return bars, sorted(funding)

def replay(bars, funding):
    position = None
    pending = False
    trades = []
    closes = [b[4] for b in bars]
    status = '等待突破'
    score = 0
    equity = []
    realized = 0
    for i in range(96, len(bars)):
        t, op, high, low, close = bars[i]
        if pending and position is None:
            position = (i, op, 0, op)
        if position:
            start, entry, carry, peak = position
            carry += sum(rate*op/entry for ts,rate in funding if t <= ts < t+HOUR and ts > bars[start][0])
            net = close/entry-1-carry
            peak = max(peak, close)
            reason = ''
            if net <= -.02:
                reason = '止损'
            elif net >= .04:
                reason = '止盈'
            elif net > 0 and close < peak*.985:
                reason = '移动止盈'
            elif close < statistics.mean(closes[i-11:i+1]):
                reason = '趋势退出'
            elif i-start+1 >= 48:
                reason = '持仓到期'
            if reason and i+1 < len(bars):
                exit_price = bars[i+1][1]
                net = exit_price/entry-1-carry
                realized += net
                trades.append(dict(time=bars[i+1][0], opened=bars[start][0],
                                   entry=entry, exit=exit_price, net=net, funding=carry, reason=reason))
                position = None
            else:
                position = (start, entry, carry, peak)
        trend = statistics.mean(closes[i-20:i+1:4]) > statistics.mean(closes[i-80:i+1:4])
        score = close/max(closes[i-24:i])-1
        pending = position is None and trend and score > 0
        status = '回放持仓中' if position else '入场候选' if pending else '等待突破' if trend else '趋势未确认'
        equity.append(realized + (net if position else 0))
    holding = None
    if position:
        start, entry, carry, _ = position
        holding = dict(opened=bars[start][0], entry=entry, mark=closes[-1],
                       funding=carry, net=net)
    return dict(status=status, score=score, trades=trades, realized=realized,
                equity=equity, holding=holding)

def scan(venue, selected):
    try:
        instruments = markets(venue)
    except HTTPError as error:
        if venue != 'OKX' or error.code != 403:
            raise
        instruments = {asset: asset + '-USDT-SWAP' for asset in selected}
    def check(asset):
        if asset not in instruments:
            return dict(asset=asset, status='当前接口未匹配 USDT 永续')
        try:
            return dict(asset=asset, **replay(*history(venue,instruments[asset])))
        except (ValueError, OSError, KeyError, IndexError) as error:
            return dict(asset=asset, status='数据不可用', detail=str(error))
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(check, selected))
    return sorted(rows, key=lambda row: (row['status']=='入场候选', row.get('score',0)), reverse=True)

def closed_demo_bars(symbol, interval, cutoff, required, *, venue=DEMO_VENUE, fetch=None):
    minutes = TIMEFRAME_MINUTES[interval]
    duration = minutes * 60000
    raw = (fetch or request)(venue, '/fapi/v1/klines', symbol=symbol, interval=interval, limit=65)
    closed = [row for row in raw if int(row[6]) < cutoff][-required:]
    bars = [(int(row[0]), *map(float, row[1:5])) for row in closed]
    if (len(bars) < required or cutoff - int(closed[-1][6]) > duration + 15000
            or (interval != '5m' and bars[-1][0] != (cutoff // duration - 1) * duration)
            or any(bar[0] + duration - 1 != int(row[6]) for bar, row in zip(bars, closed))
            or any(b[0]-a[0] != duration for a, b in zip(bars, bars[1:]))
            or any(not all(math.isfinite(price) and price > 0 for price in bar[1:]) for bar in bars)):
        raise ValueError(f'{minutes} 分钟 K 线不足、过期或存在缺口')
    return bars, closed


def entry_signal(symbol, strategy, bars=None):
    if bars is None:
        bars, _ = closed_demo_bars(symbol, SIGNAL_TIMEFRAMES[strategy][0], demo_now_ms(), 49)
    return candle_entry_signal(symbol, strategy, bars)


def scan_entry_signal(symbol, mode, *, venue=DEMO_VENUE, us_equity=False):
    now = demo_now_ms() if venue == DEMO_VENUE else time.time_ns() // 1_000_000
    def read(interval, cutoff, required):
        return closed_demo_bars(symbol, interval, cutoff, required, venue=venue)
    if mode == 'mu_relative_strength':
        bars, _ = read('15m', now, 49)
        candle, opened, _, low, close = bars[-1]
        a, b, c = (bar[4] for bar in bars[-4:-1])
        changes = (b-a, c-b)
        path = sum(map(abs, changes))
        rsi = 100 * sum(max(0, v) for v in changes) / path if path else 50
        if rsi > 20 or close <= max(c, opened):
            return candle, None, Decimal(0), '等待 15 分钟回撤修复', {}
        cutoff = candle + STRATEGY_INTERVALS[mode]
        hourly, _ = read('1h', cutoff, 50)
        last = hourly[-1][4]
        mean = statistics.mean(bar[4] for bar in hourly[-49:-1])
        _, volatility = trend_metrics(hourly)
        if not (last > mean > statistics.mean(bar[4] for bar in hourly[-50:-2])
                and 0 < volatility <= STRATEGY_MAX_ATR[mode]):
            return candle, None, volatility, '小时趋势或波动未确认', {}
        benchmark, _ = closed_demo_bars('QQQUSDT', '1h', cutoff, 25, venue=venue)
        strength = last/hourly[-25][4] - benchmark[-1][4]/benchmark[0][4]
        if strength <= 0:
            return candle, None, volatility, 'MU 过去 24 小时未跑赢 QQQ', {}
        return candle, 1, volatility, '15 分钟 RSI 回撤修复', dict(
            close=Decimal(str(close)), boundary=Decimal(str(low)), stressed=False,
            priority=strength, detail=f'RSI {rsi:.1f} · 24 小时领先 QQQ {strength:.2%}')
    return evaluate_entry_signal(symbol, mode, now, read, us_equity=us_equity)


def scan_opportunities():
    global opportunity_cache
    with opportunity_lock:
        now = time.time_ns() // 1_000_000
        cutoff = now // STRATEGY_INTERVALS['fast'] * STRATEGY_INTERVALS['fast']
        if opportunity_cache and opportunity_cache['candle_at'] == cutoff:
            return opportunity_cache
        publish_opportunities(cutoff, [], 'scanning')
        universe = market_universe()
        if universe['live_error']:
            raise ValueError('币安实盘合约目录不可用：' + universe['live_error'])
        markets = [item for item in universe['markets'] if item['live_tradeable']]

        def evaluate(item):
            symbol = item['symbol']
            try:
                def read(interval, before, required):
                    return closed_demo_bars(symbol, interval, before, required, venue='Binance', fetch=backtest_request)

                candle, score, volatility, reason, context = evaluate_entry_signal(
                    symbol, 'fast', cutoff, read, us_equity=item['equity'] == 'us')
                if candle + STRATEGY_INTERVALS['fast'] != cutoff:
                    raise ValueError('最新 5 分钟 K 线尚未齐备')
                if score is None:
                    return None, False
                side = 1 if score > 0 else -1
                entry = context['close']
                stop = entry_stop_fraction(item['asset'], 'fast', volatility, us_equity=item['equity'] == 'us')
                try:
                    stop = fast_structure_stop(entry, side, context['extreme'], volatility, stop,
                                               item['live_contract_type'])
                    target, source = fast_take_target(entry, side, stop, volatility, Decimal(0),
                                                     context['obstacles'])
                except ValueError:
                    return None, False
                return dict(**item, direction='多' if side > 0 else '空', level='信号成立',
                            reason=f'{reason} · {context["detail"]} · {source}', rank=context['priority'],
                            reference=str(entry), stop=f'{stop:.2%}', target=f'{target:.2%}'), False
            except (OSError, ValueError, KeyError, TypeError, IndexError, ArithmeticError):
                return None, True

        rows = []
        unavailable = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = [pool.submit(evaluate, item) for item in markets]
            for job in as_completed(jobs):
                row, failed = job.result()
                unavailable += failed
                if row:
                    rows.append(row)
                    rows.sort(key=lambda item: (-item['rank'], item['symbol']))
                    remember_opportunity(cutoff, row)
                    publish_opportunities(cutoff, rows, 'scanning')
        opportunity_cache = dict(scanned_at=now, candle_at=cutoff, scanned=len(markets),
                                 unavailable=unavailable, rows=rows)
        status = 'unavailable' if not markets or unavailable == len(markets) else 'partial' if unavailable else 'ready'
        publish_opportunities(cutoff, rows, status)
        return opportunity_cache


def publish_opportunities(candle_at, rows, status):
    with opportunity_report_lock:
        opportunity_report.update(candle_at=candle_at, rows=rows.copy(), status=status)


OPPORTUNITY_HISTORY_MS = 24 * 60 * 60 * 1000


@contextmanager
def opportunity_history_database():
    path = journal_path().with_name('opportunity-history.sqlite3')
    with closing(sqlite3.connect(path)) as database, database:
        database.execute("""
            CREATE TABLE IF NOT EXISTS opportunity_signals (
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                candle_at INTEGER NOT NULL,
                reference TEXT NOT NULL,
                PRIMARY KEY (candle_at, symbol, direction)
            )
        """)
        yield database


def remember_opportunity(candle_at, row):
    try:
        with opportunity_history_database() as database:
            database.execute("""
                INSERT INTO opportunity_signals (symbol, direction, candle_at, reference)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (candle_at, symbol, direction) DO UPDATE SET reference = excluded.reference
            """, (row['symbol'], row['direction'], candle_at, row['reference']))
            database.execute('DELETE FROM opportunity_signals WHERE candle_at <= ?',
                             (time.time_ns() // 1_000_000 - OPPORTUNITY_HISTORY_MS,))
    except (OSError, sqlite3.Error) as error:
        print(f'机会历史写入失败：{error}', flush=True)


def recent_opportunities(now):
    with opportunity_history_database() as database:
        return [dict(symbol=symbol, direction=direction, candle_at=candle_at, reference=reference)
                for symbol, direction, candle_at, reference in database.execute("""
                    SELECT symbol, direction, candle_at, reference FROM opportunity_signals
                    WHERE candle_at > ? ORDER BY candle_at DESC, symbol, direction
                """, (now - OPPORTUNITY_HISTORY_MS,))]


def current_opportunities():
    interval = STRATEGY_INTERVALS['fast']
    now = time.time_ns() // 1_000_000
    cutoff = now // interval * interval
    with opportunity_report_lock:
        if opportunity_report['candle_at'] == cutoff:
            report = opportunity_report.copy()
        else:
            report = dict(candle_at=cutoff, rows=[], status='waiting')
    try:
        report['history'] = recent_opportunities(now)
    except (OSError, sqlite3.Error):
        report['history'] = None
    return report


def watch_opportunities():
    global opportunity_cache
    interval = STRATEGY_INTERVALS['fast']
    while True:
        now = time.time_ns() // 1_000_000
        cutoff = now // interval * interval
        if now - cutoff < 3000:
            time.sleep((cutoff + 3000 - now) / 1000)
        try:
            report = scan_opportunities()
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            publish_opportunities(cutoff, [], 'unavailable')
            print(f'机会扫描暂不可用：{error}', flush=True)
            time.sleep(20)
        else:
            if report['unavailable'] == report['scanned']:
                with opportunity_lock:
                    opportunity_cache = None
                time.sleep(20)
                continue
            next_scan = report['candle_at'] + interval + 3000
            time.sleep(max(0, (next_scan - time.time_ns() // 1_000_000) / 1000))


def fast_evidence_label(evidence):
    if not evidence['samples']:
        return '同方向暂无已结算样本 · 按当前信号判断'
    return (f'历史净盈利估计 {evidence["probability"]:.0%} · '
            f'净期望 {evidence["expectancy"]:+.2f}R · 同向 {evidence["samples"]} 笔'
            f'（加权 {evidence["weight"]:.1f}） · 排序 {evidence["adjustment"]:+.2f}'
            f' · 风险预算 {evidence["risk_scale"]:.0%}')


def settled_fast_samples(orders, now):
    openings = {}
    exits = {}
    for order in orders:
        order_id = order.get('exchange_order_id')
        if order_id is None:
            continue
        symbol = order['symbol']
        if order.get('reduce_only'):
            opening_id = order.get('opening_id')
            if opening_id is not None and order.get('pnl') is not None and order.get('closed'):
                exits.setdefault((symbol, str(opening_id)), {})[str(order_id)] = order
        elif order.get('strategy') == 'fast' and order['status'] in DEMO_ORDER_TERMINAL and has_order_fill(order):
            openings[(symbol, str(order_id))] = order

    samples = []
    for key, opening in openings.items():
        family = fast_signal_family((opening.get('decision') or {}).get('reason'))
        closed_orders = list(exits.get(key, {}).values())
        if not family or not closed_orders or opening.get('side') not in {'BUY', 'SELL'}:
            continue
        direction = 1 if opening['side'] == 'BUY' else -1
        if any(order.get('side') != exit_side(direction) or
               (order.get('decision') or {}).get('reason') in {'币安模拟盘外部平仓', '停止策略并清仓'}
               for order in closed_orders):
            continue
        try:
            closed = max(int(order['closed']) for order in closed_orders)
            if not 0 <= now - closed <= FAST_LEARNING_WINDOW:
                continue
            quantities = [Decimal(order['executed_qty']) for order in closed_orders]
            if any(not qty.is_finite() or qty <= 0 for qty in quantities):
                continue
            if sum(quantities) != Decimal(opening['executed_qty']):
                continue
            values = [Decimal(order['entry_value']) for order in closed_orders]
            stop = Decimal(opening['stop_loss'])
            if any(not value.is_finite() or value <= 0 for value in values) or not 0 < stop < 1:
                continue
            risk = sum(values) * stop
            net = sum(Decimal(order['pnl']) for order in closed_orders)
            if not risk.is_finite() or risk <= 0 or not net.is_finite():
                continue
        except (TypeError, ValueError, KeyError, ArithmeticError):
            continue
        # Cap outliers in the expectancy estimate, while preserving win/loss signs.
        result = float(max(Decimal(-3), min(Decimal(3), net / risk)))
        samples.append((closed, key[0], direction, family, result))
    return samples



BACKTEST_WORKERS = ThreadPoolExecutor(max_workers=1)
BACKTEST_SUBMIT_LOCK = threading.Lock()
backtest_processes = {}
BACKTEST_CANCEL_BEFORE = 1794572184000
BACKTEST_JOB_LIMIT = 500
BACKTEST_FEEDS = {'live': ('Binance', '币安实盘行情'), 'demo': (DEMO_VENUE, '币安模拟盘行情')}
backtest_request_lock = threading.Lock()
backtest_next_request = {}
BACKTEST_REQUEST_SPACING = {'Binance': .15, DEMO_VENUE: .35}


def backtest_feed(params):
    source = params.get('source', ['live'])[0]
    if source not in BACKTEST_FEEDS:
        raise ValueError('回测行情来源无效')
    return BACKTEST_FEEDS[source]


class BacktestCertificateError(ValueError):
    pass


def backtest_request(venue, endpoint, **params):
    for attempt in range(2):
        while True:
            with backtest_request_lock:
                now = time.monotonic()
                delay = backtest_next_request.get(venue, 0) - now
                if delay <= 0:
                    backtest_next_request[venue] = now + BACKTEST_REQUEST_SPACING[venue]
                    break
            if delay > 30:
                raise ValueError(f'{venue} 行情接口限流（429），请稍后重试或缩短回测跨度')
            time.sleep(delay)
        try:
            return request(venue, endpoint, **params)
        except HTTPError as error:
            if error.code != 429:
                raise
            try:
                retry_after = max(2, int(error.headers.get('Retry-After', '5')))
            except (AttributeError, ValueError):
                retry_after = 5
            error.close()
            with backtest_request_lock:
                backtest_next_request[venue] = max(backtest_next_request[venue], time.monotonic()+retry_after)
            if attempt or retry_after > 30:
                raise ValueError(f'{venue} 行情接口限流（429），请稍后重试或缩短回测跨度') from None

        except ValueError as error:
            cause = error.__cause__
            if not (isinstance(cause, URLError) and isinstance(cause.reason, ssl.SSLCertVerificationError)):
                raise
            batch = ', '.join(f'{key}={params[key]}' for key in
                              ('symbol', 'interval', 'startTime', 'endTime', 'limit') if key in params)
            detail = f'{error} · {batch} · TLS 校验第 {attempt+1}/2 次失败'
            print(detail, file=sys.stderr, flush=True)
            if attempt:
                raise BacktestCertificateError(detail) from error
            time.sleep(1)


class MinuteBars:
    """Store minute OHLC as four doubles per bar, with implicit timestamps."""

    def __init__(self, start):
        self.start = start
        self.prices = array('d')

    def __len__(self):
        return len(self.prices) // 4

    def append(self, bar):
        self.prices.extend(bar[1:])

    def __getitem__(self, index):
        if not 0 <= index < len(self):
            raise IndexError(index)
        offset = index * 4
        return (self.start + index * 60000, *self.prices[offset:offset+4]), None


def historical_bars(venue, symbol, interval, start, end, *, mark=False, compact=False):
    step = (1 if interval == '1m' else TIMEFRAME_MINUTES[interval]) * 60000
    first = start // step * step
    path = '/fapi/v1/markPriceKlines' if mark else '/fapi/v1/klines'
    batch_size = 499
    width = batch_size * step
    def fetch_chunk(cursor):
        batch_end = min(end // step * step, cursor + width)
        key = (venue, symbol, path, interval, cursor)
        if batch_end == cursor + width:
            with backtest_database() as database:
                cached = database.execute("""
                    SELECT bars FROM candle_cache WHERE venue=? AND symbol=? AND path=? AND frame=? AND start=?
                """, key).fetchone()
            if cached:
                return json.loads(zlib.decompress(cached[0]))
        for attempt in range(2):
            try:
                batch = backtest_request(venue, path, symbol=symbol, interval=interval,
                                         startTime=cursor, endTime=batch_end-1, limit=batch_size)
                break
            except (HTTPError, BacktestCertificateError):
                raise
            except (OSError, ValueError):
                if attempt:
                    raise
        if batch_end == cursor + width and len(batch) == batch_size and all(
                int(row[0]) == cursor + index*step and int(row[6]) == cursor + (index+1)*step - 1
                for index, row in enumerate(batch)):
            with backtest_database() as database:
                database.execute("INSERT OR REPLACE INTO candle_cache VALUES (?, ?, ?, ?, ?, ?)",
                                 (*key, zlib.compress(json.dumps(batch).encode(), 1)))
        return batch

    rows = MinuteBars(first) if compact else []
    with ThreadPoolExecutor(max_workers=4) as pool:
        cursors = iter(range(first // width * width, end-step+1, width))
        while (window := list(islice(cursors, 8))):
            for batch in pool.map(fetch_chunk, window):
                for raw in batch:
                    stamp = int(raw[0])
                    if stamp < first:
                        continue

                    if stamp + step > end:
                        break
                    if stamp != first + len(rows)*step or int(raw[6]) != stamp + step - 1:
                        raise ValueError(f'{symbol} {interval} K 线存在缺口或预热数据不足')
                    bar = (stamp, *map(float, raw[1:5]))
                    if not all(math.isfinite(p) and p > 0 for p in bar[1:]):
                        raise ValueError(f'{symbol} {interval} K 线价格无效')
                    if compact:
                        rows.append(bar)
                    else:
                        rows.append((bar, raw))
    if len(rows) != (end-first)//step:
        raise ValueError(f'{symbol} {interval} K 线覆盖不足，请缩短区间或选较晚日期')
    return rows


def backtest_range(params):
    assets = sorted(set(params.get('asset', ['BTC'])))
    if not assets or len(assets) > 250 or any(not asset or len(asset) > 24 or not asset.isascii()
                                             or not asset.isalnum() or asset != asset.upper() for asset in assets):
        raise ValueError('标的代码无效')
    modes = ['fast']
    intervals = SIGNAL_TIMEFRAMES['fast']
    now = time.time_ns() // 1_000_000 // 60000 * 60000
    if params.get('days') == ['all']:
        start, end = 0, now
    elif 'start_at' in params:
        start, end = (int(params[key][0]) for key in ('start_at', 'end_at'))
    elif 'start' in params and 'end' in params:
        start, end = (int(datetime.fromisoformat(params[key][0]+'T00:00:00+08:00').timestamp()*1000)
                      for key in ('start', 'end'))
        end = min(end+24*HOUR, now)
    else:
        days = int(params.get('days', ['7'])[0])
        end = now
        start = end-days*24*HOUR
    if start < 0 or end > now or end - start < 60000 or start % 60000 or end % 60000:
        raise ValueError('回测区间至少为 1 分钟，终点不能晚于最新收盘')
    return assets, modes, intervals, start, end


def backtest_coverage(params):
    assets, modes, intervals, requested_start, requested_end = backtest_range(params)
    venue, source_label = backtest_feed(params)
    requested_modes = params.get('mode', ['fast'])
    modes = list(dict.fromkeys(requested_modes))
    if not modes or any(mode not in {'fast', 'intraday', 'swing', 'us_open'} for mode in modes):
        raise ValueError('回测策略无效')
    intervals = sorted({frame for mode in modes for frame in SIGNAL_TIMEFRAMES[mode]}, key=TIMEFRAME_MINUTES.get)
    feeds = [(interval, '/fapi/v1/klines', 50) for interval in intervals]
    feeds += [('1m', '/fapi/v1/klines', 0), ('1m', '/fapi/v1/markPriceKlines', 0)]

    def first_usable(feed):
        asset, interval, endpoint, warmup = feed
        step = TIMEFRAME_MINUTES.get(interval, 1) * 60000
        desired = max(0, requested_start // step * step - warmup*step)
        rows = backtest_request(venue, endpoint, symbol=asset+'USDT', interval=interval,
                                startTime=desired, endTime=requested_end-1, limit=1)
        if not rows:
            raise ValueError(f'{source_label}中 {asset} 的 {interval} 历史行情不足或不可用')
        first = int(rows[0][0])
        return requested_start if first <= requested_start // step * step - warmup*step else first + warmup*step

    with ThreadPoolExecutor(max_workers=4) as pool:
        starts = list(pool.map(first_usable, [(asset, *feed) for asset in assets for feed in feeds]))
    start = max(requested_start, *starts)
    if start >= requested_end:
        raise ValueError('该标的历史不足以完成所选策略的预热，无法回测')
    return dict(assets=assets, venue=venue, source_label=source_label, start=start, end=requested_end,
                requested_start=requested_start,
                modes=modes, intervals=intervals, strategies=[STRATEGY_NAMES[key] for key in modes])


@contextmanager
def backtest_database():
    with closing(sqlite3.connect(journal_path().with_name('backtests.sqlite3'), timeout=15)) as database, database:
        database.execute("""
            CREATE TABLE IF NOT EXISTS backtest_jobs (
                id TEXT PRIMARY KEY, asset TEXT NOT NULL, source TEXT NOT NULL,
                start INTEGER NOT NULL, end INTEGER NOT NULL, created INTEGER NOT NULL,
                state TEXT NOT NULL, detail TEXT, result BLOB
            )
        """)
        database.execute("CREATE INDEX IF NOT EXISTS backtest_recent ON backtest_jobs(created DESC)")
        database.execute("""
            CREATE TABLE IF NOT EXISTS candle_cache (
                venue TEXT, symbol TEXT, path TEXT, frame TEXT, start INTEGER, bars BLOB,
                PRIMARY KEY (venue, symbol, path, frame, start)
            )
        """)
        database.execute("""
            CREATE TABLE IF NOT EXISTS backtest_costs (job TEXT PRIMARY KEY, settings TEXT NOT NULL)
        """)
        yield database


def submit_backtests(params):
    assets, _, _, start, end = backtest_range(params)
    backtest_feed(params)
    source = params.get('source', ['live'])[0]
    settings = json.dumps(backtest_cost_settings(params))
    jobs = []
    with BACKTEST_SUBMIT_LOCK:
        with backtest_database() as database:
            active = database.execute("""
                SELECT id, asset, source, start, end, settings FROM backtest_jobs
                LEFT JOIN backtest_costs ON job=id WHERE state IN ('queued', 'running')
            """).fetchall()
            existing = {(asset, feed, begin, finish): job for job, asset, feed, begin, finish, cost in active if cost == settings}
            new = [asset for asset in assets if (asset, source, start, end) not in existing]
            if len(active) + len(new) > BACKTEST_JOB_LIMIT:
                raise ValueError('待运行任务太多，请等部分标的完成后再提交')
            created = time.time_ns() // 1_000_000
            for asset in new:
                job = uuid.uuid4().hex
                database.execute("INSERT INTO backtest_jobs VALUES (?, ?, ?, ?, ?, ?, 'queued', NULL, NULL)",
                                 (job, asset, source, start, end, created))
                database.execute('INSERT INTO backtest_costs VALUES (?, ?)', (job, settings))
                existing[asset, source, start, end] = job
                jobs.append((job, asset))
        for job, asset in jobs:
            BACKTEST_WORKERS.submit(run_backtest_job, job, asset, source, start, end)
    return {'jobs': [existing[asset, source, start, end] for asset in assets]}


def resume_backtests():
    with backtest_database() as database:
        database.execute("""
            UPDATE backtest_jobs SET state='queued', detail='服务重启，等待恢复'
            WHERE state='running'
        """)
        jobs = database.execute("""
            SELECT id, asset, source, start, end FROM backtest_jobs
            WHERE state='queued' ORDER BY created, rowid
        """).fetchall()
    for job in jobs:
        BACKTEST_WORKERS.submit(run_backtest_job, *job)


def cancel_backtests(job=None, *, before=None):
    condition = "state IN ('queued', 'running')"
    params = []
    if job is not None:
        condition += ' AND id=?'
        params.append(job)
    if before is not None:
        condition += ' AND created<=?'
        params.append(before)
    with BACKTEST_SUBMIT_LOCK:
        with backtest_database() as database:
            database.execute('BEGIN IMMEDIATE')
            ids = [row[0] for row in database.execute(
                'SELECT id FROM backtest_jobs WHERE ' + condition, params)]
            database.execute("UPDATE backtest_jobs SET state='cancelled', detail='已取消' WHERE " + condition, params)
        for job_id in ids:
            process = backtest_processes.get(job_id)
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
    return {'cancelled': len(ids)}


def run_backtest_job(job, asset, source, start, end):
    try:
        with BACKTEST_SUBMIT_LOCK:
            with backtest_database() as database:
                row = database.execute('SELECT state FROM backtest_jobs WHERE id=?', (job,)).fetchone()
            if row != ('queued',):
                return
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--backtest-worker',
                                        job, asset, source, str(start), str(end)])
            backtest_processes[job] = process
        try:
            code = process.wait()
            if code:
                raise subprocess.CalledProcessError(code, process.args)
        finally:
            with BACKTEST_SUBMIT_LOCK:
                backtest_processes.pop(job, None)
    except (OSError, subprocess.CalledProcessError) as error:
        with backtest_database() as database:
            database.execute("""
                UPDATE backtest_jobs SET state='failed', detail=?
                WHERE id=? AND state IN ('queued', 'running')
            """, (f'回测进程异常退出：{error.returncode if isinstance(error, subprocess.CalledProcessError) else error}', job))


def execute_backtest_job(job, asset, source, start, end):
    def update(state, detail=None, result=None):
        with backtest_database() as database:
            database.execute("UPDATE backtest_jobs SET state=?, detail=?, result=? WHERE id=? AND state IN ('queued', 'running')",
                             (state, detail, result, job))

    try:
        update('running', '检查行情覆盖')
        params = {'asset': [asset], 'source': [source], 'start_at': [str(start)], 'end_at': [str(end)]}
        with backtest_database() as database:
            row = database.execute('SELECT settings FROM backtest_costs WHERE job=?', (job,)).fetchone()
        settings = json.loads(row[0]) if row else None
        if settings and settings.get('modes'):
            params['mode'] = settings['modes']
        result = run_backtest(params, lambda detail: update('running', detail), settings=settings)
        update('done', result=zlib.compress(json.dumps(result, allow_nan=False).encode()))
    except Exception as error:
        update('failed', str(error) or type(error).__name__)


def backtest_jobs(params):
    job = params.get('id', [None])[0]
    with backtest_database() as database:
        if job is not None:
            row = database.execute("SELECT result FROM backtest_jobs WHERE id=? AND state='done'", (job,)).fetchone()
            if not row:
                raise ValueError('该回测结果尚不可用')
            return json.loads(zlib.decompress(row[0]))
        filters, values = [], []
        for column in ('source', 'asset'):
            if params.get(column):
                filters.append(column + '=?')
                values.append(params[column][0])
        where = ' WHERE ' + ' AND '.join(filters) if filters else ''
        page = max(1, int(params.get('page', ['1'])[0]))
        fields = ('id', 'asset', 'source', 'start', 'end', 'created', 'state', 'detail')
        total = database.execute('SELECT COUNT(*) FROM backtest_jobs' + where, values).fetchone()[0]
        page = min(page, max(1, (total + 24) // 25))
        rows = database.execute('SELECT ' + ','.join(fields) + ' FROM backtest_jobs' + where +
                                ' ORDER BY created DESC, rowid DESC LIMIT 25 OFFSET ?', (*values, (page-1)*25))
        return dict(jobs=[dict(zip(fields, row)) for row in rows], total=total, page=page)


def backtest_cost_settings(params):
    mode = params.get('execution', ['taker'])[0]
    if mode not in {'maker', 'taker'}:
        raise ValueError('成交模型无效')
    modes = list(dict.fromkeys(params.get('mode', ['fast'])))
    if not modes or any(key not in {'fast', 'intraday', 'swing', 'us_open'} for key in modes):
        raise ValueError('回测策略无效')
    settings = {'execution': mode, 'modes': modes}
    if params.get('compare') == ['1'] and 'fast' in modes:
        settings['compare'] = True
    for name, default in (('maker_bps', '2'), ('taker_bps', '5'), ('slippage_bps', '0')):
        try:
            value = Decimal(params.get(name, [default])[0])
        except InvalidOperation:
            raise ValueError('请输入有效的费率和滑点') from None
        if not value.is_finite() or not 0 <= value <= 100:
            raise ValueError('费率和滑点须为 0 至 100 bp')
        settings[name] = str(value.normalize())
    return settings


def run_backtest(params, progress, *, settings=None):
    coverage = backtest_coverage(params)
    asset, = coverage['assets']
    venue, start, end = (coverage[key] for key in ('venue', 'start', 'end'))
    symbol = asset + 'USDT'
    # Replay only needs optional metadata. Never wait on the trading catalogue lock.
    cached = catalog_cache.get(venue)
    contract = cached[1].get(symbol) if cached else None
    if contract is None and 'us_open' in coverage['modes'] and equity_kind(symbol, None) != 'us':
        catalog = backtest_request(venue, '/fapi/v1/exchangeInfo')
        contract = next((item for item in catalog['symbols'] if item['symbol'] == symbol), None)
    progress('读取历史 K 线与资金费')

    def load_funding():
        rows, cursor = [], start
        while cursor < end:
            batch = backtest_request(venue, '/fapi/v1/fundingRate', symbol=symbol,
                                     startTime=cursor, endTime=end-1, limit=1000)
            rows.extend(batch)
            if len(batch) < 1000:
                break
            next_cursor = int(batch[-1]['fundingTime']) + 1
            if next_cursor <= cursor:
                raise ValueError('资金费分页时间无效')
            cursor = next_cursor
        return [(int(row['fundingTime'])//60000*60000, Decimal(row['fundingRate']), Decimal(row['markPrice']))
                for row in rows]

    with ThreadPoolExecutor(max_workers=3) as pool:
        trades = pool.submit(historical_bars, venue, symbol, '1m', start, end, compact=True)
        marks = pool.submit(historical_bars, venue, symbol, '1m', start, end, mark=True, compact=True)
        funding = pool.submit(load_funding)
        data = {frame:historical_bars(venue, symbol, frame, start-50*TIMEFRAME_MINUTES[frame]*60000, end)
                for frame in coverage['intervals']}
        data['1m'] = trades.result()
        mark_bars = marks.result()
        funding_rows = funding.result()
    progress('运行所选策略')
    if settings is None:
        settings = {'execution': 'taker', 'maker_bps': '2', 'taker_bps': '5', 'slippage_bps': '0'}
    fee = Decimal(settings['taker_bps']) / 10000
    slippage = Decimal(settings['slippage_bps']) / 10000
    maker_fee = Decimal(settings['maker_bps']) / 10000 if settings['execution'] == 'maker' else None
    variants = [(mode, 'current') for mode in coverage['modes'] if mode != 'us_open' or equity_kind(symbol, contract) == 'us']
    if settings.get('compare') and ('fast', 'current') in variants:
        variants.append(('fast', 'continuation'))
    if not variants:
        raise ValueError('该标的不适用所选策略')
    results = []
    for mode, rules in variants:
        progress(f'运行 {STRATEGY_NAMES[mode]} · {BACKTEST_RULES[rules]}')
        results.append(replay_portfolio(mode, {asset: (data, mark_bars, funding_rows)}, start, end,
                                       fee, slippage, rules=rules, contracts={asset: contract}, maker_fee=maker_fee))
    return {**coverage, 'cost_settings': settings, 'funding_samples': len(funding_rows), 'results': results}


def restore_strategy_positions(positions, assets, mode):
    if not positions:
        return {}, {}, {}
    orders = recent_orders(limit=None)
    holdings, ticks, setups = {}, {}, {}
    catalog = None
    for symbol, position in positions.items():
        amount = Decimal(position['positionAmt'])
        if (symbol[:-4] not in assets or not amount
                or position.get('positionSide', 'BOTH') != 'BOTH'):
            continue
        opened_at, order_id = position_opened_at(position)
        previous = next((order for order in orders
                         if order['symbol'] == symbol and has_order_fill(order)), None)
        if not (previous and not previous.get('reduce_only') and previous['side'] == entry_side(amount) and previous.get('strategy') == mode
                and opened_at and order_id is not None and str(previous['exchange_order_id']) == str(order_id)
                and Decimal(previous['executed_qty']) == abs(amount)):
            continue

        stop_loss = previous.get('stop_loss')
        tick = previous.get('price_tick')
        if not stop_loss or not tick:
            if catalog is None:
                rows = request(DEMO_VENUE, '/fapi/v1/exchangeInfo')['symbols']
                catalog = {item['symbol']: item for item in rows}
            if symbol not in catalog:
                continue
            filters = {item['filterType']: item for item in catalog[symbol]['filters']}
            tick = filters['PRICE_FILTER']['tickSize']
            stop_loss = '.02' if mode == 'swing' else '.01'
            journal('demo_legacy_position_recovered', dict(symbol=symbol, opening_id=order_id,
                                                           strategy=mode, stop_loss=stop_loss,
                                                           price_tick=tick))
        tick = Decimal(tick)
        if tick <= 0:
            continue
        holdings[symbol] = (amount, opened_at/1000, int(previous['leverage']),
                            Decimal(stop_loss), order_id)
        ticks[symbol] = tick
        setup = (previous.get('decision') or {}).get('setup')
        if mode == 'fast' and setup:
            setups[symbol] = setup
    return holdings, ticks, setups


def flatten_demo_account():
    with protection_lock:
        orders = demo_signed('/fapi/v1/openOrders')
        known = {order['clientOrderId']: order for order in recent_orders(limit=None)}
        for order in orders:
            if order.get('clientOrderId', '').startswith('quant-demo-'):
                intent = known.get(order['clientOrderId'])
                if intent and strategy_limit_entry(intent) and valid_demo_order_response(intent, order):
                    reason = '用户停止策略并清仓，程序撤销剩余委托'
                    response, outcome = cancel_entry_order(intent, order, reason)
                    journal('demo_order_response', dict(intent=intent, response=response,
                                                        outcome=outcome, cancel_requested=reason))
                else:
                    demo_signed('/fapi/v1/order', method='DELETE', symbol=order['symbol'], orderId=order['orderId'])
        if demo_signed('/fapi/v1/openOrders'):
            raise ValueError('币安仍有普通挂单，请核对后重试')

        account = demo_signed('/fapi/v3/account')
        active = [position for position in account['positions'] if Decimal(position['positionAmt'])]
        if any(position.get('positionSide', 'BOTH') != 'BOTH' for position in active):
            raise ValueError('检测到双向持仓，请先在币安模拟盘处理')
        if any(not owned_protection(order) for order in open_protection()):
            raise ValueError('币安仍有手动条件单，请先核对后重试')

        previous = recent_orders(limit=None) if active else []
        for position in active:
            symbol = position['symbol']
            amount = Decimal(position['positionAmt'])
            opened, opening_id = position_opened_at(position)
            source = next((order for order in previous if opening_id is not None
                           and order['symbol'] == symbol and not order.get('reduce_only') and order['side'] == entry_side(amount)
                           and str(order['exchange_order_id']) == str(opening_id)), None)
            fresh = demo_signed('/fapi/v3/account')
            remaining = [item for item in fresh['positions']
                         if item['symbol'] == symbol and Decimal(item['positionAmt'])]
            if not remaining:
                continue
            if len(remaining) != 1 or remaining[0].get('positionSide', 'BOTH') != 'BOTH':
                raise ValueError(f'{symbol} 仓位模式变化，请核对')
            amount = Decimal(remaining[0]['positionAmt'])
            close = submit_demo_order(symbol, 'SELL' if amount > 0 else 'BUY', str(abs(amount)),
                                      notional=str(abs(Decimal(remaining[0]['notional']))),
                                      leverage=(source or {}).get('leverage'), reduce_only=True,
                                      opened=opened, opening_id=opening_id,
                                      strategy=(source or {}).get('strategy'),
                                      decision=dict(reason='停止策略并清仓'))
            if close.get('status') != 'FILLED':
                raise ValueError(f'{symbol} 平仓结果待核对')
        pending = reconcile_demo_orders()
        if pending:
            raise ValueError('普通委托仍待核对：' + '、'.join(sorted({order['symbol'] for order in pending})))
        final = demo_signed('/fapi/v3/account')
        if any(Decimal(position['positionAmt']) for position in final['positions']):
            raise ValueError('币安仍有持仓，请核对后重试')
        cleanup_protection(set(), open_protection())
        if open_protection() or demo_signed('/fapi/v1/openOrders'):
            raise ValueError('币安仍有挂单，请核对后重试')


class DemoStrategy:
    MAX_POSITIONS = 3
    FAST_MAX_POSITIONS = 5
    FAST_LEVERAGE = 5

    def __init__(self):
        self.lock = threading.Lock()
        self.control = threading.Lock()
        self.draining = False
        self.switch_blocked = False
        self.stop = threading.Event()
        self.entries_paused = threading.Event()
        self.wake = threading.Event()
        self.status = '未启动'
        self.mode = 'swing'
        self.fixed_notional = None
        self.assets = []
        self.busy_assets = []
        self.checks = {}
        self.checked_at = None
        self.scan_ms = None

    def change_asset(self, asset, enabled):
        if not asset or len(asset) > 24 or not asset.isascii() or not asset.isalnum() or asset != asset.upper():
            raise ValueError('标的代码无效')
        with self.control:
            if not self.lock.locked() or self.stop.is_set() or self.draining:
                raise ValueError('当前没有可调整的运行中策略')
            if enabled == (asset in self.assets):
                return
            symbol = asset + 'USDT'
            if enabled:
                if self.mode == 'mu_relative_strength' and asset != 'MU':
                    raise ValueError('相对强弱回撤策略只交易 MU')
                demo = futures_catalog(DEMO_VENUE).get(symbol)
                if not tradable_future(demo) or (self.mode == 'us_open' and equity_kind(symbol, demo) != 'us'):
                    raise ValueError('模拟盘或当前策略不支持：' + asset)
                if asset not in ASSETS and not any(row['asset'] == asset and row['pool']
                                                   for row in market_universe()['markets']):
                    raise ValueError('标的不在实盘候选池中，请刷新合约目录')
                self.assets = sorted([*self.assets, asset])
            else:
                pending = any(order['symbol'] == symbol and order['status'] in DEMO_ORDER_PENDING
                              for order in recent_orders(limit=None))
                if pending:
                    raise ValueError(asset + ' 仍有待核对委托')
                with ThreadPoolExecutor(max_workers=3) as pool:
                    orders = pool.submit(demo_signed, '/fapi/v1/openOrders', symbol=symbol)
                    protection = pool.submit(open_protection, symbol)
                    account = pool.submit(demo_signed, '/fapi/v3/account')
                    if orders.result() or protection.result():
                        raise ValueError(asset + ' 仍有挂单')
                    if any(position['symbol'] == symbol and Decimal(position['positionAmt'])
                           for position in account.result()['positions']):
                        raise ValueError(asset + ' 仍有持仓')
                self.assets = [item for item in self.assets if item != asset]
                self.checks = {key: value for key, value in self.checks.items() if key != asset}
            self.wake.set()

    def stop_and_flatten(self):
        with self.control:
            if self.draining:
                return
            self.draining = True
            self.switch_blocked = True
            self.stop.set()
            self.wake.set()
            record_runtime('收到停止并平仓请求', mode=self.mode)
            threading.Thread(target=self.finish_stop, daemon=True).start()

    def finish_stop(self):
        with self.lock:
            self.status = '停止中 · 正在撤单和平仓'
            try:
                flatten_demo_account()
            except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as error:
                self.status = f'停机未完成 · {error}'
                record_runtime('停机未完成', mode=self.mode, detail=str(error))
            else:
                self.switch_blocked = False
                self.checks = {asset: '策略已停止 · 已确认空仓' for asset in self.assets}
                self.status = '已停止 · 币安已确认空仓、无挂单'
                record_runtime('策略已停止', mode=self.mode, detail='币安已确认空仓、无挂单')
            finally:
                self.draining = False

    def run(self, assets, mode, fixed_notional=None):
        short_term = mode != 'swing'
        fast_trend = mode == 'fast'
        cost_control = short_term
        take_multiple = Decimal('1.25') if mode == 'mu_relative_strength' else (FAST_TREND_TARGET if fast_trend else 2)
        cost_cap = SHORT_COST_CAP if cost_control else Decimal('.75')
        scan_interval = {'scalp': 5, 'fast': 3}.get(mode, 30)
        cooldown_bars = 1 if mode == 'scalp' else SHORT_REENTRY_COOLDOWN_BARS
        cooldown_ms = cooldown_bars * STRATEGY_INTERVALS[mode]
        reentry_at = {}
        fixed_leverage = mode in {'fast', 'scalp', 'mu_relative_strength'}
        max_positions = self.FAST_MAX_POSITIONS if fixed_leverage else self.MAX_POSITIONS
        max_hold_hours = {'mu_relative_strength': 24, 'scalp': .5, 'intraday': 4, 'swing': 48, 'us_open': 1.5}.get(mode)
        holdings = {}
        entry_attempts = {}
        peaks = {}
        protection_errors = {}
        ticks = {}
        breakeven_costs = {}
        trailing_stops = {}
        take_reviewed = {}
        fast_setups = {}
        fast_reviews = {}
        review_jobs = {}
        review_retry = {}
        fast_takes = {}
        fast_exit_decisions = {}
        review_pool = ThreadPoolExecutor(max_workers=self.FAST_MAX_POSITIONS) if fast_trend else None
        quote_pool = ThreadPoolExecutor(max_workers=6) if fast_trend else None
        prepared_leverage = set()
        prepare_at_start = mode == 'fast'
        recovering = True
        last_scan_log = None
        next_entry_scan = 0
        learning_samples = {1: [], -1: []}
        next_learning_refresh = 0
        entry_outcomes = Counter()
        entry_blockers = Counter()
        entry_signal_count = 0

        def forget_protection(symbol):
            for state in (peaks, ticks, protection_errors, breakeven_costs, trailing_stops, take_reviewed,
                          fast_setups, fast_reviews, review_retry, fast_takes, fast_exit_decisions):
                state.pop(symbol, None)
            job = review_jobs.pop(symbol, None)
            if job is not None:
                job.cancel()

        def review_market(symbol, now):
            return fast_position_market(now, lambda frame, cutoff, required:
                                        closed_demo_bars(symbol, frame, cutoff, required))

        active_selection = self.assets
        try:
            record_runtime('收到启动请求', mode=mode,
                           detail=f'{STRATEGY_NAMES[mode]} · {"、".join(sorted(assets))}')
            while not self.stop.is_set():
                with self.control:
                    self.wake.clear()
                    if active_selection is not self.assets:
                        active_selection = self.assets
                        next_entry_scan = 0
                    assets = set(active_selection)
                if recovering:
                    self.status = '正在读取模拟账户持仓'
                pending = reconcile_demo_orders(cancel_entries=self.entries_paused.is_set())
                resting_entries = {order['symbol'] for order in pending if order.get('entry_expires_at')
                                   and order['status'] == 'NEW' and not order.get('cancel_requested')}
                pending_symbols = {order['symbol']: order['status'] for order in pending}
                pending_entries = {order['symbol'] for order in pending if not order.get('reduce_only')}
                pending_closes = {order['symbol'] for order in pending if order.get('reduce_only')}
                owned_pending = {order['symbol'] for order in pending if order.get('strategy') == mode
                                 and order['symbol'][:-4] in assets}
                account = demo_signed('/fapi/v3/account')
                active = [p for p in account['positions'] if Decimal(p['positionAmt'])]
                positions = {p['symbol']: p for p in active}
                self.busy_assets = sorted(symbol[:-4] for symbol in positions.keys() | pending_symbols.keys())
                if recovering:
                    demo_return_baseline(account.get('totalWalletBalance'))
                to_restore = {symbol: position for symbol, position in positions.items()
                              if symbol not in holdings or Decimal(position['positionAmt']) != holdings[symbol][0]}
                if to_restore:
                    restored, restored_ticks, restored_setups = restore_strategy_positions(to_restore, assets, mode)
                    for symbol, restored_holding in restored.items():
                        if symbol in holdings and holdings[symbol][4] != restored_holding[4]:
                            forget_protection(symbol)
                        breakeven_costs.pop(symbol, None)
                    holdings.update(restored)
                    ticks.update(restored_ticks)
                    fast_setups.update(restored_setups)
                if recovering:
                    recovering = False
                    record_runtime('账户核对完成', mode=mode, detail=f'已接管 {len(holdings)} 个持仓')
                closed_symbols = holdings.keys() - positions.keys()
                if closed_symbols:
                    external_demo_close_wake.set()
                for symbol in closed_symbols:
                    amount, opened, leverage, _, opening_id = holdings.pop(symbol)
                    if cost_control:
                        reentry_at[symbol] = demo_now_ms() + cooldown_ms
                    forget_protection(symbol)
                    self.checks = {**self.checks, symbol[:-4]: '已平仓 · 待下轮扫描'}
                    journal('demo_protection_position_closed', dict(symbol=symbol, opening_id=opening_id))
                unmatched = {symbol for symbol, position in positions.items()
                             if symbol not in holdings or Decimal(position['positionAmt']) != holdings[symbol][0]}
                if len(positions) != len(active) or unmatched - owned_pending:
                    details = '：' + '、'.join(sorted(unmatched)) if unmatched else ''
                    raise ValueError('账户持仓无法与所选策略的开仓成交核对' + details)
                self.checks = {**self.checks, **{symbol[:-4]: ('限价挂单等待成交' if symbol in resting_entries
                                                            else f'平仓委托待核对 · {status}' if symbol in pending_closes
                                                            else f'委托待核对 · {status}')
                               for symbol, status in pending_symbols.items()
                               if symbol[:-4] in assets and (symbol not in protection_errors or symbol in pending_closes)}}
                closed = False
                pending_partial_entry = False
                for symbol, (amount, opened, leverage, stop_loss, opening_id) in list(holdings.items()):
                    if self.stop.is_set():
                        break
                    if symbol in pending_closes or symbol in unmatched:
                        continue
                    position = positions[symbol]
                    notional = abs(Decimal(position['notional']))
                    pnl = Decimal(position['unrealizedProfit'])
                    direction = 1 if amount > 0 else -1
                    entry_value = Decimal(position.get('entryPrice') or '0') * abs(amount)
                    if entry_value <= 0:
                        entry_value = notional - direction*pnl
                    now = demo_now_ms()
                    elapsed = max(0, int(now/1000-opened)) if opened > 0 else None
                    timed_out = (max_hold_hours is not None and opened > 0
                                 and elapsed >= max_hold_hours * 3600)
                    price_return = pnl / entry_value if entry_value > 0 else Decimal(0)
                    peak = max(peaks.get(symbol, price_return), price_return)
                    peaks[symbol] = peak
                    setup = fast_setups.get(symbol, {})
                    target_return = Decimal(setup.get('target_return', take_multiple * stop_loss))
                    fixed_exit = price_return <= -stop_loss or price_return >= target_return
                    entry_price = entry_value/abs(amount)
                    mark = notional/abs(amount)
                    tick = ticks[symbol]
                    minimum_stop = protection_price(entry_price, -stop_loss, tick, direction=direction, kind='stop')
                    stop = trailing_stops.get(symbol, minimum_stop)
                    take = fast_takes.get(symbol)
                    dynamic_exit = fast_exit_decisions.get(symbol)
                    review_note = ''
                    proposed_stop = peak-stop_loss if not fast_trend and peak >= stop_loss else None
                    if fast_trend:
                        window = now // STRATEGY_INTERVALS['fast'] * STRATEGY_INTERVALS['fast']
                        job = review_jobs.get(symbol)
                        if job is not None and job.done():
                            del review_jobs[symbol]
                            try:
                                market = job.result()
                            except (OSError, ValueError, KeyError, IndexError, TypeError, ArithmeticError):
                                review_retry[symbol] = now + FAST_REVIEW_RETRY_MS
                            else:
                                cached = catalog_cache.get(DEMO_VENUE)
                                contract = cached[1].get(symbol) if cached else None
                                state, detail = fast_position_assessment(
                                    market, opened*1000, direction, fast_setups.get(symbol),
                                    us_equity=equity_kind(symbol, contract) == 'us')
                                if market[0] == window:
                                    fast_reviews[symbol] = (market[0], state, detail)
                                if state == 'exit' and market[0] == window:
                                    dynamic_exit = fast_exit_decisions[symbol] = ('入场条件失效', detail)
                                if market[0] == window and (market[3] is None or market[4] is None):
                                    review_retry[symbol] = now + FAST_REVIEW_RETRY_MS
                                else:
                                    review_retry.pop(symbol, None)
                        reviewed_at, state, detail = fast_reviews.get(symbol, (0, None, ''))
                        retry_at = review_retry.get(symbol, 0)
                        review_due = window > reviewed_at or bool(retry_at)
                        if (dynamic_exit is None and window > opened*1000 and review_due and
                                symbol not in review_jobs and now >= retry_at):
                            review_jobs[symbol] = review_pool.submit(review_market, symbol, now)
                        weakened = state == 'weak' and reviewed_at == window
                        proposed_stop, proposed_take = fast_position_levels(stop_loss, price_return, peak, weakened)
                        if weakened:
                            review_note = detail
                            if price_return <= proposed_stop:
                                dynamic_exit = fast_exit_decisions[symbol] = (
                                    '行情走弱触及收紧保护线', detail)
                            if proposed_take is not None and proposed_take < target_return and take_reviewed.get(symbol) != window:
                                closer = protection_price(entry_price, proposed_take, tick, direction=direction, kind='take')
                                if direction*(closer-mark) >= 2*tick:
                                    take = (min if direction > 0 else max)(take, closer) if take is not None else closer
                    early_breakeven = fast_trend and peak >= stop_loss and price_return > 0
                    if (proposed_stop is not None or early_breakeven) and not (timed_out or fixed_exit or dynamic_exit):
                        candidate = (protection_price(entry_price, proposed_stop, tick, direction=direction, kind='stop')
                                     if proposed_stop is not None else None)
                        try:
                            breakeven = None
                            if not fast_trend or price_return > 0:
                                costs = position_breakeven_costs(symbol, opening_id, amount, breakeven_costs)
                                breakeven = net_breakeven_price(entry_price, amount, tick, *costs)
                        except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
                            breakeven = None
                        if breakeven is not None and (not fast_trend or direction*(mark-breakeven) >= 2*tick):
                            candidate = (breakeven if candidate is None else
                                         (max if direction > 0 else min)(candidate, breakeven))
                        if (candidate is not None and (fast_trend or breakeven is not None)
                                and direction*(mark-candidate) >= 2*tick and direction*(candidate-stop) > 0):
                            stop = candidate
                            if early_breakeven and candidate == breakeven and not review_note:
                                review_note = f'最大浮盈 {peak/stop_loss:.2f}R · 按已核对费用移至净保本价'
                    trailing = stop != minimum_stop and direction*(mark-stop) <= 0
                    dynamic_take_hit = (fast_trend or mode == 'mu_relative_strength') and take is not None and direction*(take-mark) <= 0
                    missing_stop = False
                    if not (timed_out or fixed_exit or trailing or dynamic_exit or dynamic_take_hit):
                        reviewed_window = None
                        if not fast_trend and stop != minimum_stop and price_return >= (
                                stop_loss * Decimal('.75') if mode == 'mu_relative_strength' else stop_loss):
                            window = demo_now_ms() // STRATEGY_INTERVALS[mode]
                            if take_reviewed.get(symbol) != window:
                                try:
                                    candle, signal, _, assessment = entry_signal(symbol, mode)
                                    reviewed_window = min(window, (candle + STRATEGY_INTERVALS[mode]) // STRATEGY_INTERVALS[mode])
                                except (OSError, ValueError, KeyError, IndexError, ArithmeticError):
                                    pass
                                else:
                                    weakened = assessment == '均线趋势未确认' or (signal is not None and signal*direction < 0)
                                    target = price_return + stop_loss/4
                                    if weakened and candle + STRATEGY_INTERVALS[mode] > opened*1000 and target < target_return:
                                        closer = protection_price(entry_price, target, tick, direction=direction, kind='take')
                                        if direction*(closer-mark) > 0:
                                            take = closer
                        try:
                            actual_stop, actual_take = align_strategy_protection(symbol, entry_price, mark, stop_loss, stop, tick,
                                                      dict(opening_id=opening_id, opened=int(opened*1000),
                                                           leverage=leverage, strategy=mode), direction=direction,
                                                      take=take, take_multiple=target_return / stop_loss, review=review_note,
                                                      replace_amount=amount if symbol not in pending_entries else None)
                            if fast_trend or mode == 'mu_relative_strength':
                                fast_takes[symbol] = actual_take
                                stop = actual_stop
                                if proposed_take is not None and review_note:
                                    take_reviewed[symbol] = window
                            if reviewed_window is not None:
                                take_reviewed[symbol] = reviewed_window
                            if stop != minimum_stop:
                                trailing_stops[symbol] = stop
                            protection_errors.pop(symbol, None)
                            label = f'持仓走弱 · {review_note}' if review_note else '持仓监控中'
                            if fast_trend and not review_note and symbol in review_retry:
                                label = '持仓监控中 · 趋势行情待重试'
                            self.checks = {**self.checks, symbol[:-4]: label}
                        except ReplacementStopReached as error:
                            dynamic_exit = ('达到移动止损线', str(error))
                        except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as error:
                            detail = str(error)
                            if protection_errors.get(symbol) != detail:
                                record_runtime('保护单核对异常', mode=mode, detail=f'{symbol} · {detail}')
                            protection_errors[symbol] = detail
                            try:
                                missing_stop = not confirmed_strategy_stop(symbol, minimum_stop, direction=direction)
                            except (OSError, ValueError, KeyError, ArithmeticError):
                                missing_stop = True
                            label = ('止损未确认 · 等待开仓余单核对' if missing_stop
                                     else '止损有效 · 保护单待核对')
                            self.checks = {**self.checks, symbol[:-4]: label}
                    if timed_out or fixed_exit or trailing or dynamic_exit or dynamic_take_hit or missing_stop:
                        if symbol in pending_entries:
                            continue
                        self.status = f'正在平仓 {symbol}'
                        if timed_out:
                            reason, threshold = '达到最长持仓时间', f'{max_hold_hours:g} 小时'
                        elif price_return <= -stop_loss:
                            reason, threshold = '达到止损线', f'{-stop_loss:.3%}'
                        elif price_return >= target_return:
                            reason, threshold = '达到止盈线', f'{target_return:.3%}'
                        elif trailing:
                            reason, threshold = '触发移动止损', f'移动止损价 {stop:f}'
                        elif dynamic_take_hit:
                            reason, threshold = '达到动态止盈线', f'动态止盈价 {take:f}'
                        elif dynamic_exit:
                            reason, threshold = dynamic_exit
                        else:
                            reason, threshold = '无法确认有效止损，保护性退出', protection_errors.get(symbol, '')
                        age = (f' · 持仓 {elapsed} 秒' if elapsed < 60 else f' · 持仓 {elapsed // 60} 分钟') if elapsed is not None else ''
                        decision = dict(reason=reason, detail=f'判断时浮动收益率 {price_return:.3%}{age} · {threshold}'
                                        f' · 最大浮盈 {max(Decimal(0), peak):.3%}'
                                        f' · 开仓均价 {entry_price:f} · 判断时标记价 {mark:f}'
                                        f' · 初始止损 {minimum_stop:f} · 最终止损 {stop:f}')
                        self.checks = {**self.checks, symbol[:-4]: f'正在提交平仓 · {reason}'}
                        try:
                            order = close_strategy_position(symbol, amount, opened, leverage, opening_id, mode, decision)
                        except (OSError, ValueError) as error:
                            if not fast_trend:
                                raise
                            self.checks = {**self.checks, symbol[:-4]: f'平仓待重试 · {reason} · {error}'}
                            continue
                        if order and order['status'] in DEMO_ORDER_PENDING:
                            pending_symbols[symbol] = order['status']
                            self.checks = {**self.checks, symbol[:-4]: f'平仓委托待核对 · {order["status"]}'}
                            continue
                        del holdings[symbol]
                        if cost_control:
                            reentry_at[symbol] = demo_now_ms() + cooldown_ms
                        forget_protection(symbol)
                        self.checks = {**self.checks, symbol[:-4]: f'已平仓 · {reason}'}
                        closed = True
                if (not closed and not closed_symbols and not self.stop.is_set()
                        and not self.entries_paused.is_set()
                        and (prepare_at_start or len(holdings) < max_positions)
                        and (not holdings or time.monotonic() >= next_entry_scan)):
                    next_entry_scan = time.monotonic() + (3 if pending_symbols else scan_interval)
                    self.status = '正在核对模拟盘普通挂单'
                    open_orders = demo_signed('/fapi/v1/openOrders')
                    occupied = positions.keys() | pending_entries | {order['symbol'] for order in open_orders
                                                                     if not order.get('reduceOnly')}
                    for order in open_orders:
                        pending_symbols.setdefault(order['symbol'], order.get('status') or '交易所有挂单')
                    listed = futures_catalog(DEMO_VENUE)
                    started = time.monotonic()
                    if fast_trend and started >= next_learning_refresh:
                        next_learning_refresh = started + 30
                        try:
                            samples = settled_fast_samples(recent_orders(limit=None), demo_now_ms())
                        except (OSError, ValueError, KeyError, TypeError, ArithmeticError, sqlite3.Error) as error:
                            record_runtime('入场历史刷新失败', mode=mode, detail=str(error))
                        else:
                            samples.sort(key=lambda sample: sample[0], reverse=True)
                            learning_samples = {side: [sample for sample in samples if sample[2] == side][:500]
                                                for side in (1, -1)}
                    self.status = '正在检查行情与入场信号'
                    candidates = []
                    checks = {}
                    with ThreadPoolExecutor(max_workers=max(1, min(8, len(assets)))) as pool:
                        if prepare_at_start:
                            self.status = f'正在初始化所选标的的 {self.FAST_LEVERAGE}× 杠杆'
                            eligible = [asset + 'USDT' for asset in sorted(assets)
                                        if asset + 'USDT' in listed and asset + 'USDT' not in holdings
                                        and asset + 'USDT' not in pending_symbols
                                        and listed[asset + 'USDT']['status'] == 'TRADING'
                                        and listed[asset + 'USDT']['contractType'] in {'PERPETUAL', 'TRADIFI_PERPETUAL'}]
                            for batch_start in range(0, len(eligible), 4):
                                if self.stop.is_set():
                                    break
                                tasks = {pool.submit(set_demo_leverage, symbol, self.FAST_LEVERAGE): symbol
                                         for symbol in eligible[batch_start:batch_start+4]}
                                for task in as_completed(tasks):
                                    symbol = tasks[task]
                                    try:
                                        task.result()
                                    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
                                        checks[symbol[:-4]] = f'跳过 · 杠杆初始化未确认：{error}'
                                    else:
                                        prepared_leverage.add(symbol)
                            record_runtime('标的杠杆初始化完成', mode=mode,
                                           detail=f'{self.FAST_LEVERAGE}× · 已确认 {len(prepared_leverage)}/{len(eligible)} 个标的')
                            prepare_at_start = False
                            self.status = '正在检查行情与入场信号'
                        jobs = {}
                        scan_at = demo_now_ms()
                        for asset in sorted(assets):
                            symbol = asset + 'USDT'
                            item = listed.get(symbol)
                            if symbol in pending_symbols:
                                if symbol in pending_closes:
                                    checks[asset] = f'平仓委托待核对 · {pending_symbols[symbol]}'
                                elif symbol in protection_errors and asset in self.checks:
                                    checks[asset] = self.checks[asset]
                                else:
                                    checks[asset] = ('限价挂单等待成交' if symbol in resting_entries
                                                     else f'委托待核对 · {pending_symbols[symbol]}')
                            elif symbol in holdings:
                                checks[asset] = self.checks.get(asset, '持仓监控中')
                            elif scan_at < reentry_at.get(symbol, 0):
                                remaining = (reentry_at[symbol] - scan_at + 999) // 1000
                                checks[asset] = f'平仓后冷却 · 剩余 {remaining} 秒'
                            elif not item:
                                checks[asset] = '模拟盘目录未匹配'
                            elif (item['status'] != 'TRADING' or item['contractType'] not in
                                  {'PERPETUAL', 'TRADIFI_PERPETUAL'}):
                                checks[asset] = '模拟盘合约当前不可交易'
                            else:
                                checks[asset] = '正在读取行情'
                                jobs[pool.submit(scan_entry_signal, symbol, mode,
                                                 us_equity=equity_kind(symbol, item) == 'us')] = (asset, item)
                        self.checks = checks.copy()
                        for job in as_completed(jobs):
                            asset, item = jobs[job]
                            symbol = item['symbol']
                            try:
                                candle, score, volatility, reason, signal_context = job.result()
                                if score is not None:
                                    evidence = None
                                    if fast_trend:
                                        direction = 1 if score > 0 else -1
                                        evidence = fast_entry_evidence(learning_samples[direction], symbol, direction,
                                                                       reason, scan_at)
                                    priority = signal_context['priority'] + (evidence['adjustment'] if evidence else 0)
                                    candidates.append((priority, symbol, candle, volatility,
                                                       reason, item, score, signal_context, evidence))
                                    reason = f'信号成立 · {reason} · 等待下单检查'
                            except (OSError, ValueError, KeyError, IndexError, TypeError, ArithmeticError) as error:
                                reason = f'行情不可用 · {error}'
                            checks[asset] = reason
                            self.checks = checks.copy()
                    self.checked_at = demo_now_ms()
                    self.scan_ms = round((time.monotonic()-started)*1000)
                    log_scan = last_scan_log is None or self.checked_at - last_scan_log >= 60000
                    scan_signals = len(candidates)
                    if log_scan:
                        unavailable = sum(label.startswith('行情不可用') for label in checks.values())
                        record_runtime('行情扫描完成', mode=mode, at=self.checked_at,
                                       detail=f'读取 {len(jobs)} 个标的 · 行情不可用 {unavailable} 个 · 耗时 {self.scan_ms} ms')
                        last_scan_log = self.checked_at
                    free_margin = Decimal(account['availableBalance'])
                    ranked = sorted(candidates, key=lambda candidate: (-candidate[0], candidate[1]))
                    quotes = {}
                    funding_window = (max_hold_hours or 1) * HOUR
                    if quote_pool and not self.wake.is_set() and len(occupied) < max_positions:
                        for _, symbol, _, _, _, item, score, *_ in ranked[:3]:
                            direction = 1 if score > 0 else -1
                            quotes[symbol] = (
                                quote_pool.submit(demo_entry_cost, symbol, direction, funding_window),
                                quote_pool.submit(demo_entry_book, symbol, item['contractType']),
                            )
                    for _, symbol, candle, volatility, reason, item, score, signal_context, evidence in ranked:
                        if self.wake.is_set() or self.stop.is_set() or self.entries_paused.is_set() or len(occupied) >= max_positions:
                            break
                        filters = {f['filterType']: f for f in item['filters']}
                        lot = filters['LOT_SIZE']
                        direction = 1 if score > 0 else -1
                        if mode == 'fast' and symbol in {'BTCUSDT', 'ETHUSDT', 'SOLUSDT'}:
                            peers = {'BTCUSDT', 'ETHUSDT', 'SOLUSDT'} - {symbol}
                            same_side = entry_side(direction)
                            if (any(peer in positions and direction*Decimal(positions[peer]['positionAmt']) > 0
                                    for peer in peers)
                                    or any(order['symbol'] in peers and not order.get('reduce_only')
                                           and order.get('side') in {None, same_side} for order in pending)
                                    or any(order['symbol'] in peers and not order.get('reduceOnly')
                                           and order.get('side') == same_side for order in open_orders)):
                                checks[symbol[:-4]] = '跳过 · BTC / ETH / SOL 已有同向持仓或开仓单'
                                self.checks = checks.copy()
                                continue
                        try:
                            quote_started = time.monotonic()
                            if symbol in quotes:
                                cost_job, book_job = quotes.pop(symbol)
                                fee, funding_cost, reference = cost_job.result()
                                book, bid, ask = book_job.result()
                                quote_now = demo_now_ms()
                                if quote_now - int(reference['time']) > ENTRY_QUOTE_MAX_AGE_MS:
                                    fee, funding_cost, reference = demo_entry_cost(symbol, direction, funding_window)
                                if demo_now_ms() - int(book['time']) > ENTRY_QUOTE_MAX_AGE_MS:
                                    book, bid, ask = demo_entry_book(symbol, item['contractType'])
                            else:
                                fee, funding_cost, reference = demo_entry_cost(symbol, direction, funding_window)
                                book, bid, ask = demo_entry_book(symbol, item['contractType'])
                            book_time = int(book['time'])
                            midpoint = (bid + ask) / 2
                            spread = (ask - bid) / midpoint
                            cost = spread + 2 * fee + funding_cost
                            if direction * (midpoint / signal_context['close'] - 1) > volatility / 2:
                                raise ValueError('盘口已远离信号收盘价，避免追价')
                            tolerance = 0 if signal_context['stressed'] else -volatility / 4
                            if direction * (midpoint / signal_context['boundary'] - 1) < tolerance:
                                raise ValueError('价格已反向穿过触发位')
                            quote_ms = round((time.monotonic() - quote_started) * 1000)
                        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
                            checks[symbol[:-4]] = f'跳过 · 价格核验未通过：{error}'
                            self.checks = checks.copy()
                            continue
                        step = Decimal(lot['stepSize'])
                        equity = Decimal(account['totalMarginBalance'])
                        stop_loss = entry_stop_fraction(symbol[:-4], mode, volatility,
                                                        us_equity=equity_kind(symbol, item) == 'us')
                        if mode == 'fast':
                            try:
                                stop_loss = fast_structure_stop(
                                    ask if direction > 0 else bid, direction, signal_context['extreme'],
                                    volatility, stop_loss, item['contractType'])
                            except ValueError as error:
                                checks[symbol[:-4]] = f'跳过 · {error}'
                                self.checks = checks.copy()
                                continue
                        if cost > stop_loss * cost_cap:
                            checks[symbol[:-4]] = (f'跳过 · 点差与交易成本过高：预估 {cost:.3%}'
                                                   f' · 上限 {stop_loss * cost_cap:.3%}')
                            self.checks = checks.copy()
                            continue
                        leverage_cap = 3 if short_term else 5
                        leverage_target = (self.FAST_LEVERAGE if fixed_leverage
                                           else min(leverage_cap, max(1, int(Decimal('.05') / stop_loss))))
                        risk_scale = Decimal(str(evidence['risk_scale'])) if evidence else 1
                        budget = entry_notional_budget(mode, equity, free_margin, leverage_target,
                                                       max_positions-len(occupied), stop_loss, cost, risk_scale)
                        if fixed_notional is not None:
                            budget = min(budget, fixed_notional)
                        minimum = Decimal(filters.get('MIN_NOTIONAL', {}).get('notional', '0'))
                        aggressive_price = ask if direction > 0 else bid
                        minimum_quantity = max(Decimal(lot['minQty']), minimum / aggressive_price)
                        price, amount, time_in_force = select_demo_entry(
                            direction, book, bid, ask, budget, step, stop_loss, cost,
                            minimum_quantity=minimum_quantity if fast_trend else None)
                        attempt = (candle, direction, demo_now_ms(), price, time_in_force, cost)
                        retry_block = entry_retry_block(
                            entry_attempts.get(symbol), attempt, ENTRY_RETRY_COOLDOWN_SECONDS[mode],
                            max_ioc_upgrade=aggressive_price * stop_loss / 4 if fast_trend else Decimal(0))
                        if retry_block:
                            checks[symbol[:-4]] = f'等待复查 · {retry_block}'
                            self.checks = checks.copy()
                            continue
                        notional = amount * price
                        if not Decimal(lot['minQty']) <= amount <= Decimal(lot['maxQty']) or notional < minimum or not amount:
                            checks[symbol[:-4]] = '跳过 · 数量或名义金额不符合合约要求'
                            self.checks = checks.copy()
                            continue
                        target_return = None
                        if fast_trend:
                            try:
                                target_return, target_source = fast_take_target(
                                    price, direction, stop_loss, volatility, cost, signal_context.get('obstacles', ()))
                            except ValueError as error:
                                checks[symbol[:-4]] = f'跳过 · 止盈空间不足：{error}'
                                self.checks = checks.copy()
                                continue
                        try:
                            if open_protection(symbol):
                                checks[symbol[:-4]] = '跳过 · 该合约仍有条件单'
                                self.checks = checks.copy()
                                continue
                        except (OSError, ValueError) as error:
                            checks[symbol[:-4]] = f'跳过 · 该标的保护单未能核对：{error}'
                            self.checks = checks.copy()
                            continue
                        if symbol not in prepared_leverage:
                            checks[symbol[:-4]] = '信号成立 · 正在设置杠杆'
                            self.checks = checks.copy()
                            try:
                                set_demo_leverage(symbol, leverage_target)
                            except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
                                checks[symbol[:-4]] = f'跳过 · 杠杆设置未确认：{error}'
                                self.checks = checks.copy()
                                continue
                            if mode == 'fast':
                                prepared_leverage.add(symbol)
                        if self.wake.is_set() or self.stop.is_set() or self.entries_paused.is_set():
                            break
                        signal_at = candle + STRATEGY_INTERVALS[mode]
                        now = demo_now_ms()
                        quote_age = now - book_time
                        if not -1000 <= quote_age <= ENTRY_QUOTE_MAX_AGE_MS:
                            checks[symbol[:-4]] = '跳过 · 下单前盘口已过期或时间异常'
                            self.checks = checks.copy()
                            continue
                        order_window = ENTRY_WAIT_SECONDS[mode] * 1000 if time_in_force == 'GTX' else 0
                        if not 0 <= now - signal_at < STRATEGY_INTERVALS[mode] - order_window:
                            checks[symbol[:-4]] = '跳过 · 信号即将过期，等待下一根已收盘 K 线'
                            self.checks = checks.copy()
                            continue
                        execution = '限价吃单' if time_in_force == 'IOC' else '只挂不吃'
                        checks[symbol[:-4]] = f'正在提交 · {execution}'
                        self.checks = checks.copy()
                        tick = Decimal(filters['PRICE_FILTER']['tickSize'])
                        if tick <= 0:
                            raise ValueError('合约价格步长无效')
                        detail = f'{execution} · 信号幅度 {abs(score):.3%} · ATR {volatility:.3%} · 计划止损 {stop_loss:.3%}'
                        detail += (f' · {signal_context["detail"]} · 点差 {spread:.3%} · '
                                   f'预估双边手续费 {2 * fee:.3%} · 下一次不利资金费 {funding_cost:.3%}')
                        if fixed_notional is not None:
                            detail += f' · 目标仓位 {fixed_notional:f} USDT · 本单名义金额 {notional:.2f} USDT'
                        if time_in_force == 'GTX':
                            maker_reason = '吃单成本偏高' if cost > stop_loss / 2 else '对手盘一档数量不足'
                            detail += f' · {maker_reason}，改为限价挂单'
                        if fixed_leverage:
                            try:
                                mark, planned_stop = entry_stop_preflight(
                                    symbol, reference, now, price, stop_loss, tick, direction)
                            except (ValueError, KeyError, TypeError, ArithmeticError) as error:
                                checks[symbol[:-4]] = f'跳过 · 止损预检未通过：{error}'
                                self.checks = checks.copy()
                                continue
                            detail += f' · 下单前模拟盘标记价 {mark:f} · 计划止损触发价 {planned_stop:f}'
                        if fast_trend:
                            detail += f' · 收盘至准备发单 {(now - signal_at) / 1000:.1f} 秒'
                        decision = dict(reason=reason, detail=detail, signal_at=signal_at)
                        if fast_trend:
                            decision['setup'] = fast_entry_setup(signal_context, volatility)
                            decision['setup']['target_return'] = str(target_return)
                            decision['evidence'] = evidence
                            planned_take = protection_price(price, target_return, tick, direction=direction, kind='take')
                            decision['detail'] += f' · 计划止盈 {planned_take:f}（{target_source} · {target_return:.3%}）'
                            decision['detail'] += ' · ' + fast_evidence_label(evidence)
                            decision['detail'] += f' · 行情扫描 {self.scan_ms} ms · 下单前报价等待 {quote_ms} ms'
                            decision['detail'] += (f' · 名义仓位 {notional:.2f} USDT'
                                                   f' · 计划风险 {notional * (stop_loss + cost):.2f} USDT'
                                                   f'（上限 {equity * ENTRY_EQUITY_RISK[mode] * risk_scale:.2f} USDT）')
                            if time_in_force == 'IOC' and (budget / price // step) * step > amount:
                                decision['detail'] += ' · 按一档可成交深度缩量'
                        entry_attempts[symbol] = (candle, direction, now, price, time_in_force, cost)
                        with self.control:
                            if (symbol[:-4] not in self.assets or self.stop.is_set()
                                    or self.entries_paused.is_set() or self.wake.is_set()):
                                break
                            order = submit_demo_limit_entry(symbol, amount, price, leverage_target, mode,
                                                            stop_loss, tick, self.wake, decision,
                                                            side=entry_side(direction), time_in_force=time_in_force)
                        if fast_trend:
                            entry_outcomes[f'{time_in_force} {order["status"]}'] += 1
                        if order['status'] in DEMO_ORDER_PENDING:
                            pending_symbols[symbol] = order['status']
                            occupied.add(symbol)
                            pending.append(dict(symbol=symbol, side=entry_side(direction)))
                            free_margin = max(Decimal(0), free_margin - notional / leverage_target)
                            checks[symbol[:-4]] = ('限价挂单等待成交' if order['status'] == 'NEW' and time_in_force == 'GTX'
                                                   else f'开仓委托待核对 · {order["status"]}')
                            self.checks = checks.copy()
                            if order['status'] == 'PARTIALLY_FILLED':
                                pending_partial_entry = True
                                break
                            continue
                        if not Decimal(order.get('executedQty') or '0'):
                            checks[symbol[:-4]] = (order.get('outcome') or
                                                   order_outcome(dict(time_in_force=time_in_force), order)
                                                   or '委托结束，币安未回报成交')
                            self.checks = checks.copy()
                            continue
                        holdings[symbol] = (direction*Decimal(order['executedQty']), int(order.get('updateTime') or 0)/1000, leverage_target, stop_loss, order.get('orderId'))
                        ticks[symbol] = tick
                        if fast_trend:
                            fast_setups[symbol] = decision['setup']
                        entry = Decimal(0)
                        try:
                            entry = Decimal(order.get('avgPrice') or '0')
                            if entry <= 0:
                                entry = Decimal(order.get('cumQuote') or '0') / Decimal(order['executedQty'])
                            if entry <= 0:
                                entry = opening_fill_price(symbol, order, direction=direction)
                            set_initial_protection(symbol, entry, stop_loss, tick,
                                                   dict(opening_id=order.get('orderId'), opened=order.get('updateTime'),
                                                        leverage=leverage_target, strategy=mode), direction=direction,
                                                   take_multiple=take_multiple, target_return=target_return)
                            checks[symbol[:-4]] = '开仓已成交 · 止盈止损已挂'
                        except (ValueError, OSError, KeyError, ArithmeticError, sqlite3.Error) as error:
                            protection_errors[symbol] = str(error)
                            checks[symbol[:-4]] = '保护单未全部确认 · ' + str(error)
                            self.checks = checks.copy()
                            minimum_stop = protection_price(entry, -stop_loss, tick, direction=direction, kind='stop')
                            try:
                                protected = minimum_stop > 0 and confirmed_strategy_stop(symbol, minimum_stop, direction=direction)
                            except (OSError, ValueError, KeyError, ArithmeticError):
                                protected = False
                            if not protected:
                                if cost_control:
                                    reentry_at[symbol] = demo_now_ms() + cooldown_ms
                                self.status = f'{symbol} 未确认止损，正在提交只减仓平仓'
                                exit_detail = str(error)
                                if entry > 0:
                                    exit_detail += f' · 开仓均价 {entry:f} · 按成交重算止损 {minimum_stop:f}'
                                journal('demo_protection_emergency_exit', dict(symbol=symbol, error=exit_detail))
                                filled_amount, opened, lev, _, opening_id = holdings[symbol]
                                close = close_strategy_position(symbol, filled_amount, opened, lev, opening_id, mode,
                                                                dict(reason='开仓后止损未确认，保护性退出', detail=exit_detail))
                                if close and close['status'] in DEMO_ORDER_PENDING:
                                    pending_symbols[symbol] = close['status']
                                    checks[symbol[:-4]] = f'平仓委托待核对 · {close["status"]}'
                                else:
                                    del holdings[symbol]
                                    forget_protection(symbol)
                                    checks[symbol[:-4]] = '未确认止损 · 已核对仓位关闭'
                        for _, other_symbol, *_ in candidates:
                            other_asset = other_symbol[:-4]
                            if checks[other_asset].endswith('等待下单检查'):
                                checks[other_asset] = '信号成立 · 等待下一轮仓位检查'
                        self.checks = checks.copy()
                        break
                    for jobs in quotes.values():
                        for job in jobs:
                            job.cancel()
                    if fast_trend:
                        entry_signal_count += scan_signals
                        entry_blockers.update(checks[symbol[:-4]].split('：', 1)[0]
                                              for _, symbol, *_ in candidates
                                              if checks[symbol[:-4]].startswith(('跳过', '等待复查')))
                        if log_scan:
                            counts = '、'.join(f'{label} {count}' for label, count in entry_blockers.most_common()) or '无'
                            outcomes = '、'.join(f'{label} {count}' for label, count in entry_outcomes.items()) or '无'
                            record_runtime('5 分钟入场检查', mode=mode,
                                           detail=f'候选累计 {entry_signal_count} · 占用 {len(occupied)}/{max_positions}'
                                                  f' · 下单回报 {outcomes} · 拦截 {counts}')
                            entry_signal_count = 0
                            entry_blockers.clear()
                            entry_outcomes.clear()
                self.status = '监控中 · ' + (f'持仓 {len(holdings)}/{max_positions}' if holdings else '等待入场信号')
                if pending_symbols:
                    self.status += f' · {len(pending_symbols)} 个标的有待处理委托'
                if protection_errors:
                    self.status += f' · {len(protection_errors)} 个持仓的保护单待核对'
                if self.entries_paused.is_set():
                    self.status = f'已停止开新仓 · 持仓 {len(holdings)}'
                    if pending_entries or any(symbol not in pending_closes for symbol in pending_symbols):
                        self.status += ' · 开仓挂单撤销待核对'
                    elif not holdings and not pending_symbols:
                        self.status += ' · 已空仓'
                if pending_partial_entry:
                    continue
                if holdings or protection_errors:
                    self.wake.wait(2)
                else:
                    self.wake.wait(3 if pending_symbols else scan_interval)
            self.status = '已停止' + (' · 仍有持仓，请手动管理' if holdings else '')
        except HTTPError as error:
            endpoint = urlsplit(error.url or '').path
            try:
                details = json.load(error)
                message = f"{details['code']} {details['msg']}"
            except (ValueError, TypeError, KeyError):
                message = f'HTTP {error.code}'
            self.status = f'已暂停 · 币安模拟盘 {endpoint} · {message}'
            record_runtime('策略已暂停', mode=mode, detail=f'币安模拟盘 {endpoint} · {message}')
        except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as error:
            self.status = f'已暂停 · {error}'
            record_runtime('策略已暂停', mode=mode, detail=str(error))
        finally:
            if quote_pool is not None:
                quote_pool.shutdown(wait=False, cancel_futures=True)
            if review_pool is not None:
                review_pool.shutdown(wait=False, cancel_futures=True)
            self.lock.release()


SHADOW_FEE = Decimal('.0005')
SHADOW_DATA_ERRORS = (OSError, ValueError, KeyError, TypeError, ArithmeticError)


class LiveMarketSimulation:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.error = None
        self.marks = {}
        self.state = (json.loads(path.read_text()) if path.exists() else
                      dict(enabled=True, cursor=self.events(tail=True), positions={}, trades=[], notes=[]))
        threading.Thread(target=self.run, daemon=True).start()

    def save(self, **changes):
        state = dict(self.state, **changes)
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False)
        temporary.replace(self.path)
        self.state = state

    def configure(self, enabled):
        with self.lock:
            if self.state['enabled'] != enabled:
                self.save(enabled=enabled, **({'cursor': self.events(tail=True)} if enabled and
                                             not self.state['positions'] else {}))
                self.wake.set()

    def snapshot(self):
        with self.lock:
            positions = [dict(p, floating=self.marks.get(key)) for key, p in self.state['positions'].items()]
            return dict(enabled=self.state['enabled'], positions=positions, trades=self.state['trades'][:30],
                        notes=self.state['notes'], error=self.error,
                        realized=str(sum((Decimal(t['net']) for t in self.state['trades']), Decimal(0))))

    @staticmethod
    def events(cursor=0, *, tail=False):
        path = journal_path()
        if not path.exists():
            return 0 if tail else []
        with closing(sqlite3.connect(path)) as database:
            if tail:
                return database.execute('SELECT COALESCE(MAX(id), 0) FROM events').fetchone()[0]
            return list(database.execute(
                """SELECT id, created_at, kind, payload FROM events WHERE id > ?
                   AND kind IN ('demo_order_response', 'demo_order_settlement_pending', 'demo_order_settled',
                                'demo_protection_position_closed', 'demo_protection_confirmed')
                   ORDER BY id LIMIT 100""", (cursor,)))

    @staticmethod
    def quote(symbol):
        book = request('Binance', '/fapi/v1/ticker/bookTicker', symbol=symbol)
        bid, ask = Decimal(book['bidPrice']), Decimal(book['askPrice'])
        now = time.time_ns() // 1_000_000
        if (book['symbol'] != symbol or not 0 < bid <= ask
                or not -1000 <= now - int(book['time']) <= 5000):
            raise ValueError('实盘盘口无效或已过期')
        return book, bid, ask

    def estimate_fill(self, symbol, buy, amount):
        try:
            book, bid, ask = self.quote(symbol)
            quantity = Decimal(book['askQty' if buy else 'bidQty'])
            if quantity.is_finite() and quantity >= amount:
                return ask if buy else bid, int(book['time'])
        except SHADOW_DATA_ERRORS:
            pass

        depth = request('Binance', '/fapi/v1/depth', symbol=symbol, limit=20)
        quoted_at = int(depth['T'])
        if not -1000 <= time.time_ns() // 1_000_000 - quoted_at <= 5000:
            raise ValueError('实盘盘口深度已过期')
        remaining, value = amount, Decimal(0)
        for raw_price, raw_quantity in depth['asks' if buy else 'bids']:
            price, quantity = Decimal(raw_price), Decimal(raw_quantity)
            if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity <= 0:
                raise ValueError('实盘盘口深度数据无效')
            filled = min(quantity, remaining)
            value += price * filled
            remaining -= filled
            if not remaining:
                return value / amount, quoted_at
        raise ValueError('实盘前 20 档深度不足')

    def apply(self, event_id, recorded_at, kind, payload):
        intent = payload.get('intent', {})
        response = payload.get('response', {})
        symbol = payload.get('symbol') or intent.get('symbol')
        if intent.get('external_close'):
            return
        protection = kind == 'demo_protection_confirmed'
        if protection:
            opening_id = (intent.get('context') or {}).get('opening_id')
        else:
            opening_id = payload.get('opening_id') or intent.get('opening_id') or response.get('orderId')
        if not symbol or opening_id is None:
            return
        key = f'{symbol}:{opening_id}'
        with self.lock:
            if event_id <= self.state['cursor']:
                return
            position = self.state['positions'].get(key)
            enabled = self.state['enabled']
        if protection:
            if position and not position.get('closed'):
                with self.lock:
                    self.save(positions={**self.state['positions'], key: dict(position, **{intent['kind']: intent['price']})})
            return

        closing = kind == 'demo_protection_position_closed' or bool(intent.get('reduce_only'))
        amount = Decimal(response.get('executedQty') or '0')
        if closing:
            if not position or position.get('closed') or (
                    kind != 'demo_protection_position_closed' and response.get('status') != 'FILLED'):
                return
            direction = position['direction']
            delta = Decimal(position['amount'])
        else:
            if not enabled or not strategy_limit_entry(intent) or not intent.get('strategy') or amount <= 0:
                return
            if position and position.get('closed'):
                return
            delta = amount - Decimal(position['source_amount'] if position else '0')
            if delta <= 0:
                return
            direction = 1 if intent['side'] == 'BUY' else -1
        try:
            source_time = int(response.get('updateTime') or recorded_at)
            if time.time_ns() // 1_000_000 - source_time > 15_000:
                raise ValueError('模拟盘成交回报已超过 15 秒')
            price, quoted_at = self.estimate_fill(symbol, (direction > 0) != closing, delta)
            if not -5000 <= quoted_at - source_time <= 15_000:
                raise ValueError('实盘报价时间与模拟盘成交相隔过久')
        except SHADOW_DATA_ERRORS as error:
            with self.lock:
                action = '平仓' if closing else '开仓'
                note = dict(symbol=symbol, time=recorded_at,
                            text=f'{action}未估算成交 · 模拟盘开仓单 {opening_id} · 数量 {delta:f} · {error}')
                changes = dict(notes=[note, *self.state['notes']][:30], cursor=event_id)
                if position:
                    changes['positions'] = {**self.state['positions'], key: dict(
                        position, **({'closed': recorded_at, 'reason': note['text']} if closing
                                     else {'source_amount': str(amount)}))}
                self.save(**changes)
            return

        with self.lock:
            if closing:
                reason = (intent.get('decision') or {}).get('reason') or '跟随模拟盘平仓'
                position = dict(position, exit=str(price), closed=quoted_at, reason=reason)
            else:
                if not self.state['enabled'] or event_id <= self.state['cursor']:
                    return
                previous_amount = Decimal(position['amount']) if position else Decimal(0)
                entry_value = Decimal(position['entry']) * previous_amount if position else Decimal(0)
                total = previous_amount + delta
                position = dict(position or {}, symbol=symbol, direction=direction, amount=str(total),
                                source_amount=str(amount), entry=str((entry_value + price*delta)/total),
                                opened=position['opened'] if position else quoted_at, mode=intent['strategy'])
            self.save(positions={**self.state['positions'], key: position}, cursor=event_id)

    def settle(self, key, position):
        symbol = position['symbol']
        direction = position['direction']
        amount, entry, price = (Decimal(position[field]) for field in ('amount', 'entry', 'exit'))
        rows = request('Binance', '/fapi/v1/fundingRate', symbol=symbol,
                       startTime=position['opened'] + 1, endTime=position['closed'], limit=1000
                       ) if position['closed'] > position['opened'] else []
        if not isinstance(rows, list) or len(rows) >= 1000:
            raise ValueError('资金费明细待核对')
        funding = -direction * amount * sum(
            (Decimal(row['fundingRate']) * Decimal(row['markPrice']) for row in rows), Decimal(0))
        fees = SHADOW_FEE * amount * (entry + price)
        net = direction * amount * (price - entry) - fees + funding
        with self.lock:
            positions = self.state['positions'].copy()
            positions.pop(key)
            self.save(positions=positions, trades=[dict(position, net=str(net), fees=str(fees),
                                                        funding=str(funding)), *self.state['trades']])

    def run(self):
        next_mark = 0
        while True:
            with self.lock:
                self.wake.clear()
                active = self.state['enabled'] or any(not p.get('closed') or p.get('exit')
                                                      for p in self.state['positions'].values())
                cursor = self.state['cursor']
            if not active:
                self.wake.wait()
                continue
            errors = []
            try:
                for event_id, recorded_at, kind, raw in self.events(cursor):
                    self.apply(event_id, recorded_at, kind, json.loads(raw))
                    with self.lock:
                        if event_id > self.state['cursor']:
                            self.save(cursor=event_id)
                if time.monotonic() >= next_mark:
                    next_mark = time.monotonic() + 5
                    with self.lock:
                        positions = self.state['positions'].copy()
                    marks = {}
                    for key, position in positions.items():
                        try:
                            if position.get('exit'):
                                self.settle(key, position)
                            elif not position.get('closed'):
                                _, bid, ask = self.quote(position['symbol'])
                                direction = position['direction']
                                price = bid if direction > 0 else ask
                                amount, entry = (Decimal(position[field]) for field in ('amount', 'entry'))
                                marks[key] = str(amount * (direction*(price-entry) - SHADOW_FEE*(price+entry)))
                        except SHADOW_DATA_ERRORS as error:
                            errors.append(f"{position['symbol']} · {error}")
                    with self.lock:
                        self.marks = marks
            except SHADOW_DATA_ERRORS + (sqlite3.Error,) as error:
                errors.append(str(error))
            with self.lock:
                self.error = '；'.join(errors) or None
            self.wake.wait(1)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if (self.headers.get('Origin') != 'http://127.0.0.1:8765'
                or self.headers.get('Host') != '127.0.0.1:8765'
                or self.headers.get('X-Quant-Demo') != 'strategy'):
            self.send(403, 'text/plain', b'Forbidden')
            return
        parsed = urlsplit(self.path)
        strategy = self.server.strategy
        if parsed.path == '/backtest/cancel':
            try:
                job = parse_qs(parsed.query, keep_blank_values=True).get('id', [None])[0]
                if job is not None and (len(job) != 32 or any(char not in '0123456789abcdef' for char in job)):
                    self.send(400, 'application/json', json.dumps({'error': '回测任务 ID 无效'}).encode())
                    return
                result = cancel_backtests(job)
            except (OSError, sqlite3.Error) as error:
                self.send(500, 'application/json', json.dumps({'error': str(error)}).encode())
                return
            self.send(200, 'application/json', json.dumps(result).encode())
            return
        if parsed.path == '/backtest/jobs':
            try:
                submitted = submit_backtests(parse_qs(parsed.query))
            except (ValueError, KeyError, OSError, sqlite3.Error) as error:
                self.send(400, 'application/json', json.dumps({'error': str(error)}).encode())
                return
            self.send(202, 'application/json', json.dumps(submitted).encode())
            return
        if parsed.path in {'/shadow/start', '/shadow/pause'}:
            try:
                self.server.shadow.configure(parsed.path == '/shadow/start')
            except (OSError, sqlite3.Error) as error:
                self.send(409, 'application/json', json.dumps({'error':str(error)}).encode())
                return
        elif parsed.path == '/strategy/protection':
            params = parse_qs(parsed.query)
            try:
                replace_protection(params['symbol'][0], params['kind'][0], params['price'][0])
            except (OSError, ValueError, KeyError, ArithmeticError, StopIteration, sqlite3.Error) as error:
                self.send(502, 'application/json', json.dumps({'error': str(error)}).encode())
                return
        elif parsed.path == '/strategy/stop':
            strategy.stop_and_flatten()
        elif parsed.path in {'/strategy/pause-entries', '/strategy/resume-entries'}:
            with strategy.control:
                if not strategy.lock.locked() or strategy.stop.is_set() or strategy.draining:
                    self.send(409, 'application/json', json.dumps({'error': '当前没有可调整的运行中策略'}).encode())
                    return
                paused = parsed.path == '/strategy/pause-entries'
                if paused != strategy.entries_paused.is_set():
                    if paused:
                        strategy.entries_paused.set()
                    else:
                        strategy.entries_paused.clear()
                    strategy.wake.set()
                    record_runtime('停止开新仓' if paused else '恢复开新仓', mode=strategy.mode)
        elif parsed.path == '/strategy/asset':
            params = parse_qs(parsed.query)
            try:
                enabled = params['enabled'][0]
                if enabled not in {'0', '1'}:
                    raise ValueError('标的操作无效')
                strategy.change_asset(params['asset'][0], enabled == '1')
            except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as error:
                self.send(409, 'application/json', json.dumps({'error': str(error)}).encode())
                return
            self.send(200, 'application/json', json.dumps({'assets': strategy.assets}).encode())
            return
        elif parsed.path == '/strategy/start':
            params = parse_qs(parsed.query)
            assets = set(params.get('asset', ()))
            mode = params.get('strategy', ['swing'])[0]
            if mode == 'scalp':
                self.send(400, 'application/json', json.dumps({'error': '1 分钟策略已下线，请选择 5 分钟双向策略'}).encode())
                return
            if mode == 'mu_relative_strength' and assets != {'MU'}:
                self.send(400, 'application/json', json.dumps({'error': '请仅勾选 MU'}).encode())
                return
            if mode not in STRATEGY_NAMES:
                self.send(400, 'application/json', '{"error":"未知策略"}'.encode())
                return
            sizing = params.get('sizing', ['capital'])[0]
            fixed_notional = None
            try:
                if sizing == 'fixed':
                    fixed_notional = Decimal(params.get('notional', [''])[0])
                    if (not fixed_notional.is_finite() or not Decimal('.01') <= fixed_notional <= Decimal('1000000000')
                            or fixed_notional != fixed_notional.quantize(Decimal('.01'))):
                        raise ValueError('固定名义金额无效')
                    fixed_notional = fixed_notional.quantize(Decimal('.01'))
                elif sizing != 'capital':
                    raise ValueError('未知仓位模式')
            except (ValueError, InvalidOperation):
                self.send(400, 'application/json', json.dumps({'error': '请选择仓位模式并输入有效的固定名义金额'}).encode())
                return
            if assets:
                try:
                    demo = futures_catalog(DEMO_VENUE)
                    extra = assets - set(ASSETS)
                    if extra:
                        live = futures_catalog('Binance')
                        pool = {symbol[:-4] for symbol, item in live.items() if tradable_future(item)
                                and equity_kind(symbol, item) in {'us', 'unclassified'}}
                        if not extra <= pool:
                            raise ValueError('有标的不在实盘候选池中，请刷新合约目录')
                    unavailable = sorted(asset for asset in assets if not tradable_future(demo.get(asset + 'USDT'))
                                         or (mode == 'us_open' and equity_kind(asset + 'USDT', demo.get(asset + 'USDT')) != 'us'))
                    if unavailable:
                        raise ValueError('模拟盘或当前策略不支持：' + '、'.join(unavailable))
                except (OSError, ValueError, KeyError) as error:
                    self.send(400, 'application/json', json.dumps({'error': str(error)}).encode())
                    return
            with strategy.control:
                if not assets or strategy.draining or strategy.switch_blocked or not strategy.lock.acquire(blocking=False):
                    self.send(409, 'application/json', '{"error":"请选择标的，并等当前策略停止且币安确认空仓、无挂单"}'.encode())
                    return
                strategy.stop.clear()
                strategy.entries_paused.clear()
                strategy.wake.clear()
                strategy.mode = mode
                strategy.fixed_notional = fixed_notional
                strategy.assets = sorted(assets)
                strategy.busy_assets = []
                strategy.checks = {}
                strategy.checked_at = None
                strategy.scan_ms = None
                strategy.status = '正在核对模拟账户'
                threading.Thread(target=strategy.run, args=(assets, mode, fixed_notional), daemon=True).start()
        else:
            self.send(404, 'text/plain', b'Not found')
            return
        self.send(200, 'application/json', b'{}')

    def do_GET(self):
        parsed = urlsplit(self.path)
        try:
            if parsed.path in {'/', '/backtest', '/strategies'}:
                main, _, pages = Path(__file__).with_name('index.html').read_bytes().partition(b'<!-- backtest-page -->')
                backtest, _, strategies = pages.partition(b'<!-- strategies-page -->')
                self.send(200, 'text/html', {'/': main, '/backtest': backtest, '/strategies': strategies}[parsed.path])
                return
            if parsed.path == '/version':
                self.send(200, 'application/json', json.dumps(dict(commit=deployed_commit())).encode())
                return
            if parsed.path == '/backtest/jobs':
                self.send(200, 'application/json', json.dumps(backtest_jobs(parse_qs(parsed.query))).encode())
                return
            if parsed.path == '/account/binance-demo':
                self.send(200, 'application/json', json.dumps(demo_account()).encode())
                return
            if parsed.path == '/strategy/markets':
                catalog = request(DEMO_VENUE, '/fapi/v1/exchangeInfo')['symbols']
                by_symbol = {item['symbol']: item for item in catalog}
                rows = []
                for asset in ASSETS:
                    symbol = asset + 'USDT'
                    item = by_symbol.get(symbol)
                    rows.append(dict(asset=asset, symbol=symbol,
                                     status=item['status'] if item else None,
                                     contract_type=item['contractType'] if item else None))
                self.send(200, 'application/json', json.dumps(rows).encode())
                return
            if parsed.path == '/market-universe':
                self.send(200, 'application/json', json.dumps(market_universe()).encode())
                return
            if parsed.path == '/opportunities':
                self.send(200, 'application/json', json.dumps(current_opportunities(), allow_nan=False).encode())
                return
            if parsed.path == '/shadow':
                self.send(200, 'application/json', json.dumps(self.server.shadow.snapshot()).encode())
                return
            if parsed.path == '/strategy':
                self.send(200, 'application/json', json.dumps(dict(status=self.server.strategy.status, mode=self.server.strategy.mode,
                                                                    running=self.server.strategy.lock.locked() or self.server.strategy.draining,
                                                                    draining=self.server.strategy.draining, assets=self.server.strategy.assets,
                                                                    busy_assets=self.server.strategy.busy_assets,
                                                                    entries_paused=self.server.strategy.entries_paused.is_set(),
                                                                    fixed_notional=(str(self.server.strategy.fixed_notional)
                                                                                    if self.server.strategy.fixed_notional is not None else None),
                                                                    switch_blocked=self.server.strategy.switch_blocked,
                                                                    checks=self.server.strategy.checks,
                                                                    checked_at=self.server.strategy.checked_at,
                                                                    scan_ms=self.server.strategy.scan_ms)).encode())
                return
            if parsed.path == '/strategy/decisions':
                params = parse_qs(parsed.query)
                page = int(params.get('page', ['1'])[0])
                query = asset_search_term(params.get('q', [''])[0])
                view = params.get('view', ['important'])[0]
                self.send(200, 'application/json', json.dumps(decision_history(page, query, view)).encode())
                return
            if parsed.path == '/strategy/protection-decisions':
                self.send(200, 'application/json', json.dumps(protection_decisions()).encode())
                return
            if parsed.path == '/strategy/logs':
                self.send(200, 'application/json', json.dumps(runtime_logs()).encode())
                return
            if parsed.path == '/account/demo-realized-return':
                self.send(200, 'application/json', json.dumps(demo_realized_return()).encode())
                return
            if parsed.path == '/account/demo-statistics':
                self.send(200, 'application/json', json.dumps(strategy_statistics()).encode())
                return
            if parsed.path == '/account/demo-orders':
                self.send(200, 'application/json', json.dumps(recent_orders()).encode())
                return
            if parsed.path == '/account/demo-history':
                params = parse_qs(parsed.query)
                page = int(params.get('page', ['1'])[0])
                query = asset_search_term(params.get('q', [''])[0])
                self.send(200, 'application/json', json.dumps(demo_history(page, query)).encode())
                return
            if parsed.path != '/scan':
                self.send(404,'text/plain',b'Not found')
                return
            q = parse_qs(parsed.query)
            venue = q['venue'][0]
            selected = [a for a in ASSETS if a in q.get('asset',[])]
            if venue not in HOSTS:
                raise ValueError('输入无效')
            rows = scan(venue, selected)
            journal('replay', dict(venue=venue, selected=selected, rows=rows))
            self.send(200,'application/json',json.dumps(rows).encode())
        except (ValueError, KeyError, OSError, sqlite3.Error) as error:
            self.log_error('扫描失败: %s', error)
            self.send(502,'application/json',json.dumps({'error':str(error)}).encode())

    def send(self, status, mime, body):
        self.send_response(status)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Type', mime+'; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def journal_path():
    directory = Path.home() / 'Library' / 'Application Support' / 'Quant Desk'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / 'journal.sqlite3'


def ensure_journal_events(database):
    database.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, '
                     'created_at INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)')


def demo_return_baseline(wallet_balance=None):
    path = journal_path()
    if wallet_balance is None and not path.exists():
        return None
    with closing(sqlite3.connect(path)) as database, database:
        table_exists = database.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'demo_return_baseline' AND type = 'table'"
        ).fetchone()
        if table_exists:
            row = database.execute('SELECT principal, started_at FROM demo_return_baseline WHERE id = 1').fetchone()
            if row is not None:
                return row
        if wallet_balance is None:
            return None
        balance = Decimal(wallet_balance)
        if not balance.is_finite() or balance <= 0:
            return None
        ensure_journal_events(database)
        database.execute(
            'CREATE TABLE IF NOT EXISTS demo_return_baseline '
            '(id INTEGER PRIMARY KEY CHECK (id = 1), principal TEXT NOT NULL, started_at INTEGER NOT NULL)'
        )
        database.execute('INSERT OR IGNORE INTO demo_return_baseline VALUES (1, ?, ?)',
                         (str(balance), demo_now_ms()))
        return database.execute('SELECT principal, started_at FROM demo_return_baseline WHERE id = 1').fetchone()


def demo_realized_return():
    baseline = demo_return_baseline()
    principal, started_at = baseline if baseline is not None else (None, None)
    pnl = Decimal(0)
    by_symbol = {}
    by_day = {}
    for order in recent_orders(limit=None):
        if not order.get('reduce_only') or order.get('pnl') is None:
            continue
        value = Decimal(order['pnl'])
        if has_order_fill(order):
            symbol = order['symbol']
            by_symbol[symbol] = by_symbol.get(symbol, Decimal(0)) + value
            if order.get('closed'):
                day = (int(order['closed']) + 8 * HOUR) // (24 * HOUR)
                daily = by_day.setdefault(day, {})
                daily[symbol] = daily.get(symbol, Decimal(0)) + value
        if baseline is None or int(order.get('closed') or 0) <= started_at:
            continue
        return_pnl = order.get('return_pnl')
        if return_pnl is not None:
            pnl += Decimal(return_pnl)
        elif int(order.get('opened') or 0) >= started_at:
            pnl += value
    symbols = [dict(symbol=symbol, net=str(net))
               for symbol, net in sorted(by_symbol.items(), key=lambda item: (-item[1], item[0]))]
    return dict(rate=float(pnl/Decimal(principal)) if baseline is not None else None,
                principal=principal, started_at=started_at,
                net=str(pnl) if baseline is not None else None, symbols=symbols,
                symbol_days={day: {symbol: str(value) for symbol, value in totals.items()}
                             for day, totals in by_day.items()})


def journal(kind, payload, *, timeout=5):
    with closing(sqlite3.connect(journal_path(), timeout=timeout)) as database, database:
        ensure_journal_events(database)
        database.execute('INSERT INTO events (created_at, kind, payload) VALUES (?, ?, ?)',
                         (int(time.time()*1000), kind, json.dumps(payload, ensure_ascii=False)))


def record_runtime(message, *, mode=None, detail=None, at=None, scope='system', symbol=None):
    event = dict(at=at if at is not None else demo_now_ms(), message=message,
                 mode=mode, detail=detail, scope=scope)
    if symbol is not None:
        event['symbol'] = symbol
    try:
        journal('demo_runtime', event, timeout=0.15)
    except (OSError, sqlite3.Error) as error:
        print(f'运行日志未写入：{error} · {message}', flush=True)


def runtime_logs():
    path = journal_path()
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as database:
        rows = database.execute(
            """
            SELECT payload FROM events
            WHERE kind = 'demo_runtime' AND (
                json_extract(payload, '$.scope') = 'system' OR
                json_extract(payload, '$.message') IN (
                    '签名时间已重新校准', '收到停止并平仓请求', '停机未完成',
                    '策略已停止', '收到启动请求', '账户核对完成', '行情扫描完成', '策略已暂停'
                )
            )
            ORDER BY id DESC LIMIT 200
            """
        )
        return [json.loads(payload) for (payload,) in rows]


def asset_search_term(value):
    term = ''.join(value.upper().split())[:40]
    if term in {'SK', '海力士', 'SK海力士'}:
        return 'SKHYNIX'
    return term


def protection_decisions(limit=200, *, query='', confirmed=False):
    path = journal_path()
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as database:
        rows = database.execute(
            """
            SELECT payload FROM events
            WHERE kind = 'demo_runtime' AND json_extract(payload, '$.scope') = 'protection'
              AND (? = '' OR instr(upper(json_extract(payload, '$.symbol')), ?) > 0)
              AND (NOT ? OR json_extract(payload, '$.message') LIKE '%已确认')
            ORDER BY id DESC LIMIT ?
            """, (query, query, confirmed, -1 if limit is None else limit)
        )
        return [json.loads(payload) for (payload,) in rows]


def recent_orders(limit=50, *, query=''):
    path = journal_path()
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as database:
        rows = database.execute(
            """
            SELECT created_at, kind, payload FROM events
            WHERE kind LIKE 'demo_order_%'
              AND (? = '' OR instr(upper(coalesce(json_extract(payload, '$.intent.symbol'),
                                                 json_extract(payload, '$.symbol'))), ?) > 0)
            ORDER BY id DESC
            """, (query, query)
        )
        orders = {}
        seen = set()
        closes = {}
        for timestamp, kind, raw in rows:
            payload = json.loads(raw)
            intent = payload.get('intent', payload)
            key = intent['clientOrderId']
            if key in seen:
                continue
            seen.add(key)
            response = payload.get('response', {})
            executed_at = response.get('updateTime') if (response.get('status') == 'FILLED' or
                (strategy_limit_entry(intent) and Decimal(response.get('executedQty') or '0') > 0)) else None
            is_close = intent.get('reduce_only')
            exchange_id = response.get('orderId')
            if is_close and exchange_id is not None:
                exchange_key = (intent['symbol'], str(exchange_id))
                priority = (payload.get('net_pnl') is not None, not intent.get('external_close'))
                previous = closes.get(exchange_key)
                if previous:
                    previous_key, previous_priority = previous
                    if priority <= previous_priority:
                        continue
                    del orders[previous_key]
                closes[exchange_key] = (key, priority)
            orders[key] = dict(intent, time=timestamp, filled=payload.get('filled_quote') or response.get('cumQuote'),
                               entry_value=payload.get('entry_value'),
                               entry_price=payload.get('entry_price'), exit_price=payload.get('exit_price'),
                               opened=(payload.get('opened_at') or intent.get('opened') if is_close
                                       else executed_at),
                               closed=(payload.get('closed_at') or executed_at) if is_close else None,
                               executed_qty=response.get('executedQty'), exchange_order_id=response.get('orderId'),
                               status_reason=payload.get('outcome') or order_outcome(intent, response, payload),
                               cancel_requested=payload.get('cancel_requested') or intent.get('cancel_requested'),
                               pnl=payload.get('net_pnl'), roi=payload.get('net_roi'), return_pnl=payload.get('return_pnl'),
                               fees=payload.get('fees'), funding=payload.get('funding'),
                               status=response.get('status') or {
                                   'demo_order_intent': '待确认',
                                   'demo_order_http_error': '接口报错',
                                   'demo_order_unknown': '待核对',
                               }.get(kind, '待核对'))
        return list(orders.values())[:limit]


def journal_page(rows, page):
    page_size = 9
    pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    start = (page - 1) * page_size
    return page, pages, rows[start:start + page_size]


def demo_history(page, query=''):
    orders = [order for order in recent_orders(limit=None, query=query)
              if order.get('reduce_only') and order['status'] == 'FILLED']
    orders.sort(key=lambda order: int(order['closed'] or order['time']), reverse=True)
    page, pages, orders = journal_page(orders, page)
    return dict(page=page, pages=pages, orders=orders)


def decision_history(page, query, view):
    orders = recent_orders(limit=None, query=query)
    if view == 'closed':
        orders = [order for order in orders if order.get('reduce_only') and has_order_fill(order)]
        protections = []
    else:
        if view != 'all':
            orders = [order for order in orders if has_order_fill(order)]
        protections = protection_decisions(limit=None, query=query, confirmed=view != 'all')
    records = [dict(order=order, at=int(order['closed'] or order['opened'] or order['time'])) for order in orders]
    records.extend(dict(event=event, at=int(event['at'])) for event in protections)
    records.sort(key=lambda record: record['at'], reverse=True)
    page, pages, records = journal_page(records, page)
    return dict(page=page, pages=pages, records=records)


def order_outcome(intent, response, payload=None):
    payload = payload or {}
    status = response.get('status')
    if status == 'NEW' and intent.get('entry_expires_at'):
        if payload.get('cancel_requested') or intent.get('cancel_requested'):
            return '程序已请求撤单，等待币安确认'
        return '仅挂单已进入订单簿，等待成交'
    if status == 'PARTIALLY_FILLED':
        return f'已成交 {response["executedQty"]}，剩余委托待核对'
    if status == 'CANCELED':
        if payload.get('cancel_requested') or intent.get('cancel_requested'):
            return '币安确认已撤单。此前程序请求撤单，最终发起方未确认'
        return '币安回报已撤单，记录中没有撤单原因'
    if status == 'EXPIRED' and intent.get('time_in_force') == 'IOC':
        return 'IOC 限价单到期，未能立即成交的数量已撤销'
    if status == 'EXPIRED_IN_MATCH':
        return '币安回报撮合过程中失效（EXPIRED_IN_MATCH）'
    if status == 'EXPIRED':
        return '币安回报委托到期，未提供具体原因'
    if status == 'REJECTED':
        return '币安拒绝委托，未提供具体原因'
    if status == '交易所未找到':
        return '币安未找到该委托，核对时该合约无挂单、无持仓'
    if not status:
        return intent.get('error') or '等待币安确认委托状态'
    return None


def cancel_entry_order(intent, response, reason):
    if intent.get('cancel_requested') != reason:
        journal('demo_order_response', dict(intent=intent, response=response, cancel_requested=reason))
    try:
        result = demo_signed('/fapi/v1/order', method='DELETE', symbol=intent['symbol'],
                             origClientOrderId=intent['clientOrderId'])
    except (OSError, ValueError):
        return response, None
    if not valid_demo_order_response(intent, result):
        return response, None
    return result, reason if result['status'] == 'CANCELED' else None


def reconcile_demo_orders(*, cancel_entries=False):
    pending = []
    for order in recent_orders(limit=None):
        if order['status'] not in DEMO_ORDER_PENDING:
            continue
        symbol, client_id = order['symbol'], order['clientOrderId']
        lookup = dict(symbol=symbol, origClientOrderId=client_id)
        try:
            response = demo_signed('/fapi/v1/order', **lookup)
        except HTTPError as error:
            try:
                code = int(json.load(error)['code'])
            except (ValueError, TypeError, KeyError):
                code = None
            age = int(time.time()*1000) - order['time']
            if (error.code != 400 or code != -2013 or not strategy_limit_entry(order)
                    or order['status'] not in {'待确认', '待核对'}
                    or not 60000 <= age < 3*24*HOUR):
                pending.append(order)
                continue
            try:
                open_orders = demo_signed('/fapi/v1/openOrders', symbol=symbol)
                account = demo_signed('/fapi/v3/account')
                if open_orders or any(p['symbol'] == symbol and Decimal(p['positionAmt'])
                                      for p in account['positions']):
                    pending.append(order)
                    continue
            except (OSError, ValueError, KeyError, ArithmeticError):
                pending.append(order)
                continue
            journal('demo_order_response', dict(intent=order, response=dict(status='交易所未找到'),
                                                reconciliation_code=code))
            continue
        except (OSError, ValueError):
            pending.append(order)
            continue
        if not valid_demo_order_response(order, response):
            pending.append(order)
            continue
        outcome = None
        cancel_requested = order.get('cancel_requested')
        if response['status'] in DEMO_ORDER_ACTIVE and not order.get('reduce_only') and strategy_limit_entry(order):
            expires_at = order.get('entry_expires_at')
            reason = None
            if Decimal(response['executedQty']) > 0:
                reason = '委托已部分成交，程序撤销剩余数量并管理已成交仓位'
            elif cancel_entries:
                reason = '已停止开新仓，程序撤销未成交开仓委托'
            elif cancel_requested:
                reason = cancel_requested
            elif expires_at and demo_now_ms() >= expires_at:
                reason = f'限价挂单等待 {order.get("entry_wait_seconds", 60)} 秒仍未成交，程序撤单'
            elif not expires_at:
                reason = '程序在对账时撤销仍未完成的短时限价单'
            if reason:
                response, outcome = cancel_entry_order(order, response, reason)
                cancel_requested = reason
        status = response['status']
        if status in DEMO_ORDER_ACTIVE:
            pending.append(dict(order, status=status, executed_qty=response['executedQty'],
                                cancel_requested=cancel_requested))
            if status == order['status'] and response['executedQty'] == order.get('executed_qty'):
                continue
        kind = 'demo_order_response'
        if order.get('reduce_only') and order.get('opening_id') is not None and status == 'FILLED':
            kind = 'demo_order_settlement_pending'
        journal(kind, dict(intent=order, response=response, outcome=outcome, cancel_requested=cancel_requested))
    return pending


def valid_demo_order_response(intent, response):
    if (not isinstance(response, dict) or response.get('clientOrderId') != intent['clientOrderId']
            or response.get('symbol') != intent['symbol'] or response.get('side') != intent['side']
            or response.get('status') not in DEMO_ORDER_ACTIVE | DEMO_ORDER_TERMINAL):
        return False
    try:
        quantity = Decimal(response['executedQty'])
    except (KeyError, TypeError, ArithmeticError):
        return False
    return quantity.is_finite() and quantity >= 0 and (quantity > 0 or response['status'] != 'FILLED')


def strategy_statistics():
    groups = {key: [] for key in STRATEGY_NAMES}
    for order in recent_orders(limit=None):
        groups.setdefault(order.get('strategy') or 'manual', []).append(order)

    results = []
    today = int(time.time()) // 86400
    for key, orders in groups.items():
        filled = [order for order in orders if has_order_fill(order)]
        closed = [order for order in filled if order.get('reduce_only')]
        settled = [order for order in closed if order.get('pnl') is not None]
        pnl = sum((Decimal(order['pnl']) for order in settled), Decimal(0))
        wins = sum(Decimal(order['pnl']) > 0 for order in settled)
        daily = {}
        opened_days = [int(order['opened']) // 86400000 for order in filled if order.get('opened')]
        first = min(opened_days, default=today)
        for order in settled:
            if order.get('closed'):
                day = int(order['closed']) // 86400000
                daily[day] = daily.get(day, Decimal(0)) + Decimal(order['pnl'])
        samples = [float(daily.get(day, 0)) for day in range(first, today)]
        sharpe = None
        if len(samples) >= 10 and len(settled) >= 2 and len(settled) == len(closed):
            deviation = statistics.stdev(samples)
            if deviation:
                sharpe = statistics.mean(samples) / deviation * 365**.5
        results.append(dict(name=STRATEGY_NAMES.get(key, '手动订单' if key == 'manual' else key),
                            opens=len(filled)-len(closed), settled=len(settled),
                            pending=len(closed)-len(settled), net=str(pnl),
                            win_rate=wins/len(settled) if settled else None,
                            sharpe=sharpe, days=len(samples)))
    return results


def trade_quote(fill):
    quote = fill.get('quoteQty')
    return Decimal(quote) if quote else Decimal(fill['qty']) * Decimal(fill['price'])


external_demo_close_wake = threading.Event()


def demo_trade_window(symbol, start, end):
    rows = demo_signed('/fapi/v1/userTrades', symbol=symbol, startTime=start, endTime=end, limit=1000)
    if len(rows) < 1000:
        return sorted(rows, key=lambda fill: (int(fill['time']), int(fill['id'])))
    if start == end:
        raise ValueError('同一毫秒的成交明细超出接口上限')
    middle = (start + end) // 2
    return demo_trade_window(symbol, start, middle) + demo_trade_window(symbol, middle+1, end)


def find_demo_position_fills(order, now):
    symbol = order['symbol']
    opening_id = str(order['exchange_order_id'])
    expected = Decimal(order['executed_qty'])
    opening = demo_signed('/fapi/v1/userTrades', symbol=symbol, orderId=opening_id, limit=1000)
    if (not opening or len(opening) >= 1000 or sum(Decimal(fill['qty']) for fill in opening) != expected
            or any(str(fill['orderId']) != opening_id or fill['side'] != order['side']
                   or fill.get('positionSide', 'BOTH') != 'BOTH' for fill in opening)):
        return None
    first = min(int(fill['time']) for fill in opening)
    opening_key = min((int(fill['time']), int(fill['id'])) for fill in opening)
    remaining = Decimal(0)
    exits = []
    cursor = first
    while cursor <= now:
        end = min(now, cursor+7*24*HOUR-1)
        for fill in demo_trade_window(symbol, cursor, end):
            if (int(fill['time']), int(fill['id'])) < opening_key or fill.get('positionSide', 'BOTH') != 'BOTH':
                continue
            quantity = Decimal(fill['qty'])
            if str(fill['orderId']) == opening_id:
                remaining += quantity
            elif fill['side'] != ('SELL' if order['side'] == 'BUY' else 'BUY') or quantity > remaining:
                return None
            else:
                exits.append(fill)
                remaining -= quantity
                if not remaining:
                    if sum(Decimal(exit_fill['qty']) for exit_fill in exits) != expected:
                        return None
                    return opening, exits
        cursor = end+1
    return None


def sync_external_demo_close(order, known, now):
    fills = find_demo_position_fills(order, now)
    if fills is None:
        return False
    opening, exits = fills
    groups = {}
    for fill in exits:
        groups.setdefault(str(fill['orderId']), []).append(fill)
    missing = {key: group for key, group in groups.items()
               if known.get((order['symbol'], key), {}).get('pnl') is None}
    if not missing:
        return True
    if any(fill['commissionAsset'] != 'USDT' for fill in opening + exits):
        raise ValueError('外部平仓手续费币种需要换算')
    symbol = order['symbol']
    start, end = min(int(fill['time']) for fill in opening), int(exits[-1]['time'])
    funding_rows = demo_signed('/fapi/v1/income', symbol=symbol, incomeType='FUNDING_FEE',
                               startTime=start+1, endTime=end-1, limit=1000) if end-start > 1 else []
    if len(funding_rows) >= 1000 or any(row['asset'] != 'USDT' or not start < int(row['time']) < end
                                       for row in funding_rows):
        raise ValueError('外部平仓资金费明细待核对')
    funded_exits = {}
    for row in funding_rows:
        at = int(row['time'])
        eligible = [fill for fill in exits if int(fill['time']) > at]
        exposed = (sum(Decimal(fill['qty']) for fill in opening if int(fill['time']) < at)
                   - sum(Decimal(fill['qty']) for fill in exits if int(fill['time']) <= at))
        eligible_qty = sum(Decimal(fill['qty']) for fill in eligible)
        if exposed != eligible_qty or not exposed:
            raise ValueError('资金费时点仓位待核对')
        for fill in eligible:
            funded_exits.setdefault(str(fill['orderId']), []).append(
                (at, Decimal(row['income']) * Decimal(fill['qty']) / exposed))

    opening_qty = sum(Decimal(fill['qty']) for fill in opening)
    opening_value = sum(trade_quote(fill) for fill in opening)
    baseline = demo_return_baseline()
    for close_id, group in missing.items():
        closed_at = max(int(fill['time']) for fill in group)
        quantity = sum(Decimal(fill['qty']) for fill in group)
        share = quantity/opening_qty
        entry_value = opening_value*share
        exit_value = sum(trade_quote(fill) for fill in group)
        gross = sum(Decimal(fill['realizedPnl']) for fill in group)
        fees = share*sum(Decimal(fill['commission']) for fill in opening) + sum(Decimal(fill['commission']) for fill in group)
        funding = sum((value for _, value in funded_exits.get(close_id, [])), Decimal(0))
        net = gross-fees+funding
        return_pnl = None
        if baseline is not None and closed_at > baseline[1]:
            since = baseline[1]
            return_pnl = str(sum(Decimal(fill['realizedPnl'])-Decimal(fill['commission'])
                                 for fill in group if int(fill['time']) > since)
                             - share*sum(Decimal(fill['commission']) for fill in opening if int(fill['time']) > since)
                             + sum(value for at, value in funded_exits.get(close_id, []) if at > since))
        previous = known.get((symbol, close_id))
        client_id = f'demo-external-{symbol}-{close_id}'
        decision = ((previous or {}).get('decision') or
                    dict(reason='币安模拟盘外部平仓', detail='按交易所成交与资金费对账'))
        intent = dict(clientOrderId=client_id, symbol=symbol, side=group[0]['side'], quantity=str(quantity),
                      reduce_only=True, opening_id=order['exchange_order_id'], external_close=True,
                      strategy=order.get('strategy'), leverage=order.get('leverage'), decision=decision)
        response = dict(orderId=int(close_id), status='FILLED', executedQty=str(quantity), updateTime=closed_at)
        journal('demo_order_settled', dict(intent=intent, response=response, net_pnl=str(net),
                net_roi=float(net/entry_value), fees=str(fees), funding=str(funding), return_pnl=return_pnl,
                entry_value=str(entry_value), filled_quote=str(exit_value), entry_price=str(entry_value/quantity),
                exit_price=str(exit_value/quantity), opened_at=start, closed_at=closed_at))
    return True


def external_demo_close_watch():
    next_check = {}
    while True:
        external_demo_close_wake.clear()
        try:
            now = demo_now_ms()
            orders = recent_orders(limit=None)
            known, settled = {}, {}
            for order in orders:
                if order.get('reduce_only') and order['exchange_order_id'] is not None:
                    known[(order['symbol'], str(order['exchange_order_id']))] = order
                    if order.get('opening_id') is not None and order.get('pnl') is not None:
                        key = (order['symbol'], str(order['opening_id']))
                        settled[key] = settled.get(key, Decimal(0)) + Decimal(order['executed_qty'])
            due = []
            for order in orders:
                if (order.get('reduce_only') or order['status'] not in DEMO_ORDER_TERMINAL or not has_order_fill(order)
                        or not order.get('opened') or order['exchange_order_id'] is None
                        or int(order['opened']) < now-89*24*HOUR):
                    continue
                key = (order['symbol'], str(order['exchange_order_id']))
                deadline = next_check.get(key, 0)
                if deadline <= now and settled.get(key, 0) < Decimal(order['executed_qty']):
                    due.append((deadline, order, key))
            for _, order, key in sorted(due, key=lambda item: (item[0], -int(item[1]['opened'])))[:4]:
                age = now-int(order['opened'])
                next_check[key] = now + (15_000 if age < 2*24*HOUR else 5*60_000)
                try:
                    sync_external_demo_close(order, known, now)
                except (OSError, ValueError, KeyError, TypeError, ArithmeticError, sqlite3.Error) as error:
                    print(f'外部平仓 {key[0]} {key[1]} 待核对：{error}', flush=True)
        except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as error:
            print(f'外部平仓对账失败：{error}', flush=True)
        external_demo_close_wake.wait(5)


def position_breakeven_costs(symbol, opening_id, amount, cache):
    cached = cache.get(symbol)
    if cached and time.monotonic() < cached[0]:
        return cached[1]
    taker_rate = demo_taker_fee_rate(symbol)
    if opening_id is None or taker_rate is None or taker_rate >= 1:
        raise ValueError('开仓订单或平仓费率未确认')
    fills = demo_signed('/fapi/v1/userTrades', symbol=symbol, orderId=opening_id, limit=1000)
    if (not fills or len(fills) >= 1000 or sum(Decimal(fill['qty']) for fill in fills) != abs(amount)
            or any(str(fill['orderId']) != str(opening_id) or fill['side'] != entry_side(amount)
                   or fill['commissionAsset'] != 'USDT' for fill in fills)):
        raise ValueError('开仓成交或手续费未核对')
    first = min(int(fill['time']) for fill in fills)
    now = demo_now_ms()
    if not 0 <= now-first < 89*24*HOUR:
        raise ValueError('开仓时间超出资金费可核对范围')
    rows = demo_signed('/fapi/v1/income', symbol=symbol, incomeType='FUNDING_FEE',
                       startTime=first+1, endTime=now, limit=1000) if now > first else []
    if len(rows) >= 1000 or any(row['asset'] != 'USDT' or not first < int(row['time']) <= now for row in rows):
        raise ValueError('资金费明细未核对')
    open_fee = sum((Decimal(fill['commission']) for fill in fills), Decimal(0))
    funding = sum((Decimal(row['income']) for row in rows), Decimal(0))
    if not open_fee.is_finite() or not funding.is_finite():
        raise ValueError('费用或资金费无效')
    costs = (open_fee, taker_rate, funding)
    cache[symbol] = (time.monotonic()+30, costs)
    return costs


def net_breakeven_price(entry, amount, tick, open_fee, taker_rate, funding):
    direction = 1 if amount > 0 else -1
    price = (entry + direction*(open_fee-funding)/abs(amount))/(1-direction*taker_rate)
    rounding = ROUND_UP if direction > 0 else ROUND_DOWN
    return (price/tick).to_integral_value(rounding=rounding)*tick


def opening_fill_price(symbol, order, *, direction=1):
    quantity = Decimal(order['executedQty'])
    account = demo_signed('/fapi/v3/account')
    position = next((item for item in account['positions'] if item['symbol'] == symbol
                     and Decimal(item['positionAmt']) == direction*quantity
                     and item.get('positionSide', 'BOTH') == 'BOTH'), None)
    if position:
        entry = Decimal(position.get('entryPrice') or '0')
        if entry <= 0 and position.get('notional') is not None:
            entry = (abs(Decimal(position['notional'])) - direction*Decimal(position['unrealizedProfit'])) / quantity
        if entry > 0:
            return entry
    timestamp = int(order['updateTime'])
    fills = demo_signed('/fapi/v1/userTrades', symbol=symbol,
                        startTime=max(0, timestamp-60000), endTime=timestamp+1000, limit=1000)
    matched = [fill for fill in fills if str(fill['orderId']) == str(order['orderId'])]
    if sum(Decimal(fill['qty']) for fill in matched) != quantity:
        raise ValueError('开仓成交明细未齐')
    return sum(trade_quote(fill) for fill in matched) / quantity


def retry_pending_settlements():
    path = journal_path()
    if not path.exists():
        return
    with closing(sqlite3.connect(path)) as database:
        events = database.execute(
            "SELECT kind, payload FROM events WHERE kind IN "
            "('demo_order_settlement_pending','demo_order_settled') ORDER BY id DESC"
        )
        seen = set()
        pending = []
        for kind, raw in events:
            event = json.loads(raw)
            key = event['intent']['clientOrderId']
            if key in seen:
                continue
            seen.add(key)
            if kind == 'demo_order_settlement_pending' or (
                kind == 'demo_order_settled' and not event.get('exit_price') and event['intent'].get('opening_id')
            ):
                pending.append(event)
    for event in pending[-3:]:
        intent, response = event['intent'], event['response']
        try:
            settlement = settle_demo_trade(intent['symbol'], intent['opening_id'], response, intent.get('opened'))
        except (OSError, ValueError, KeyError, ArithmeticError):
            continue
        journal('demo_order_settled', dict(intent=intent, response=response, **settlement))


def settle_demo_trade(symbol, opening_id, order, opened=None):
    if opened and order.get('updateTime') and int(order['updateTime'])-int(opened) < 6*86400000:
        fills = demo_signed('/fapi/v1/userTrades', symbol=symbol, startTime=max(0, int(opened)-60000),
                            endTime=int(order['updateTime'])+1000, limit=1000)
        opening = [fill for fill in fills if str(fill['orderId']) == str(opening_id)]
        closing = [fill for fill in fills if str(fill['orderId']) == str(order['orderId'])]
    else:
        opening = demo_signed('/fapi/v1/userTrades', symbol=symbol, orderId=opening_id)
        closing = demo_signed('/fapi/v1/userTrades', symbol=symbol, orderId=order['orderId'])
    quantity = Decimal(order['executedQty'])
    closing_side = order.get('side', 'SELL')
    if closing_side not in {'BUY', 'SELL'}:
        raise ValueError('平仓成交方向无效')
    for fills, side in ((opening, 'SELL' if closing_side == 'BUY' else 'BUY'), (closing, closing_side)):
        if (not fills or len(fills) >= 1000 or sum(Decimal(fill['qty']) for fill in fills) != quantity
                or any(fill['side'] != side or fill['commissionAsset'] != 'USDT' for fill in fills)):
            raise ValueError('成交明细不完整或手续费需要换汇')
    start = min(int(fill['time']) for fill in opening)
    end = max(int(fill['time']) for fill in closing)
    if end < start:
        raise ValueError('成交时间顺序异常')
    funding_rows = demo_signed('/fapi/v1/income', symbol=symbol, incomeType='FUNDING_FEE',
                               startTime=start, endTime=end, limit=1000)
    if len(funding_rows) >= 1000 or any(row['asset'] != 'USDT' or not start < int(row['time']) < end
                                      for row in funding_rows):
        raise ValueError('资金费需要进一步核对')
    fees = sum(Decimal(fill['commission']) for fill in opening + closing)
    funding = sum((Decimal(row['income']) for row in funding_rows), Decimal(0))
    gross = sum(Decimal(fill['realizedPnl']) for fill in closing)
    entry_value = sum(trade_quote(fill) for fill in opening)
    exit_value = sum(trade_quote(fill) for fill in closing)
    net = gross - fees + funding
    baseline = demo_return_baseline()
    return_pnl = None
    if baseline is not None and end > baseline[1]:
        since = baseline[1]
        gross_since = sum(Decimal(fill['realizedPnl']) for fill in closing if int(fill['time']) > since)
        fees_since = sum(Decimal(fill['commission']) for fill in opening + closing if int(fill['time']) > since)
        funding_since = sum(Decimal(row['income']) for row in funding_rows if int(row['time']) > since)
        return_pnl = str(gross_since - fees_since + funding_since)
    return dict(net_pnl=str(net), net_roi=float(net / entry_value), fees=str(fees), funding=str(funding), entry_value=str(entry_value), filled_quote=str(exit_value),
                entry_price=str(entry_value/quantity), exit_price=str(exit_value/quantity), opened_at=start, closed_at=end,
                return_pnl=return_pnl)


def submit_demo_limit_entry(symbol, amount, price, leverage, strategy, stop_loss, tick, stop_event, decision=None, *, side='BUY', time_in_force='GTX'):
    if stop_event.is_set():
        return dict(status='CANCELED', executedQty='0', outcome='已停止开新仓，委托未发送')
    client_id = 'quant-demo-' + uuid.uuid4().hex[:20]
    intent = dict(clientOrderId=client_id, symbol=symbol, side=side, quantity=str(amount),
                  notional=str(amount*price), leverage=leverage, strategy=strategy,
                  maker_only=time_in_force == 'GTX', time_in_force=time_in_force,
                  limit_price=str(price), stop_loss=str(stop_loss), price_tick=str(tick), decision=decision)
    if time_in_force == 'GTX':
        wait_seconds = ENTRY_WAIT_SECONDS[strategy]
        intent['entry_wait_seconds'] = wait_seconds
        intent['entry_expires_at'] = demo_now_ms() + wait_seconds * 1000
    journal('demo_order_intent', intent)
    lookup = dict(symbol=symbol, origClientOrderId=client_id)
    try:
        order = demo_signed('/fapi/v1/order', method='POST', symbol=symbol, side=side, positionSide='BOTH',
                            type='LIMIT', timeInForce=time_in_force, price=format(price, 'f'),
                            quantity=format(amount, 'f'), newClientOrderId=client_id,
                            **({'newOrderRespType': 'RESULT'} if time_in_force == 'IOC' else {}))
    except HTTPError as error:
        try:
            code = int(json.load(error)['code'])
        except (ValueError, TypeError, KeyError):
            code = None
        if code == -5022:
            outcome = '仅挂单到达币安时会立即成交，交易所拒绝该委托（-5022）'
            order = dict(status='REJECTED', executedQty='0', outcome=outcome)
            journal('demo_order_response', dict(intent=intent, response=order, outcome=outcome))
            return order
        journal('demo_order_unknown', dict(intent, error=f'币安错误码 {code or error.code}'))
        return dict(status='待核对', clientOrderId=client_id)
    except (OSError, ValueError):
        journal('demo_order_unknown', intent)
        return dict(status='待核对', clientOrderId=client_id)

    if not valid_demo_order_response(intent, order):
        journal('demo_order_unknown', dict(intent, error='币安委托回报字段无法核验'))
        return dict(status='待核对', clientOrderId=client_id)
    journal('demo_order_response', dict(intent=intent, response=order))
    if intent.get('entry_expires_at'):
        return order
    deadline = time.monotonic()+12
    cancel_reason = '限价单等待 12 秒仍未成交，程序撤单'
    outcome = None
    while order.get('status') not in DEMO_ORDER_TERMINAL and not Decimal(order.get('executedQty') or '0'):
        remaining = deadline-time.monotonic()
        if remaining <= 0 or stop_event.wait(min(1, remaining)):
            break
        try:
            latest = demo_signed('/fapi/v1/order', **lookup)
        except (OSError, ValueError):
            cancel_reason = '委托查询失败，程序撤销仍待确认的限价单'
            break
        if not valid_demo_order_response(intent, latest):
            cancel_reason = '委托回报无法核验，程序撤销仍待确认的限价单'
            break
        order = latest
    if order.get('status') not in DEMO_ORDER_TERMINAL:
        if stop_event.is_set():
            cancel_reason = '策略控制状态变更，程序撤销剩余开仓委托'
        elif Decimal(order.get('executedQty') or '0') > 0:
            cancel_reason = '委托已部分成交，程序撤销剩余数量并管理已成交仓位'
        order, outcome = cancel_entry_order(intent, order, cancel_reason)
    else:
        cancel_reason = None
    if order.get('status') not in DEMO_ORDER_TERMINAL | DEMO_ORDER_ACTIVE:
        journal('demo_order_unknown', intent)
        return dict(status='待核对', clientOrderId=client_id)
    journal('demo_order_response', dict(intent=intent, response=order, outcome=outcome, cancel_requested=cancel_reason))
    return dict(order, outcome=outcome)


def submit_demo_order(symbol, side, quantity, *, notional=None, leverage=None, reduce_only=False, opened=None, opening_id=None, strategy=None, stop_loss=None, price_tick=None, decision=None, defer_confirmation=False):
    if side not in {'BUY', 'SELL'} or not symbol.isascii() or not symbol.isalnum():
        raise ValueError('请输入合约代码和 BUY 或 SELL')
    try:
        amount = Decimal(quantity)
    except InvalidOperation:
        raise ValueError('数量无效') from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError('数量必须大于零')
    order_id = 'quant-demo-' + uuid.uuid4().hex[:20]
    intent = dict(clientOrderId=order_id, symbol=symbol, side=side, quantity=str(amount),
                  notional=notional, leverage=leverage, opened=opened, reduce_only=reduce_only, opening_id=opening_id, strategy=strategy,
                  stop_loss=stop_loss, price_tick=price_tick, decision=decision)
    journal('demo_order_intent', intent)
    print(f'币安模拟盘 · {symbol} {side} {amount} · 客户端订单号 {order_id}', flush=True)
    try:
        order = demo_signed('/fapi/v1/order', method='POST', symbol=symbol,
                           side=side, type='MARKET', quantity=str(amount),
                           newClientOrderId=order_id, newOrderRespType='RESULT',
                           **({'reduceOnly': 'true'} if reduce_only else {}))
        order.setdefault('side', side)
        journal('demo_order_response', dict(intent=intent, response=order))
        if reduce_only and order.get('status') == 'FILLED' and opening_id is not None:
            try:
                settlement = settle_demo_trade(symbol, opening_id, order, opened)
            except (OSError, ValueError, KeyError, ArithmeticError) as error:
                journal('demo_order_settlement_pending', dict(intent=intent, response=order, error=str(error)))
            else:
                journal('demo_order_settled', dict(intent=intent, response=order, **settlement))
        return order
    except HTTPError as error:
        try:
            details = json.load(error)
            message = f"{details['code']} {details['msg']}"
        except (ValueError, KeyError, TypeError):
            message = f'HTTP {error.code}'
        uncertain = error.code >= 500 or error.code in {408, 429}
        journal('demo_order_unknown' if uncertain else 'demo_order_http_error', dict(intent=intent, error=message))
        if defer_confirmation and uncertain:
            return dict(status='待核对', clientOrderId=order_id)
        raise ValueError(message) from None
    except (OSError, ValueError):
        journal('demo_order_unknown', intent)
        if defer_confirmation:
            return dict(status='待核对', clientOrderId=order_id)
        raise ValueError(f'未收到明确回报，请先核对客户端订单号 {order_id}，避免重复提交') from None


if __name__ == '__main__':
    if len(sys.argv) == 7 and sys.argv[1] == '--backtest-worker':
        if sys.platform.startswith('linux'):
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            limit = 384 * 1024 * 1024
            if soft != resource.RLIM_INFINITY:
                limit = min(limit, soft)
            resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
        job, asset, source, start, end = sys.argv[2:]
        execute_backtest_job(job, asset, source, int(start), int(end))
        sys.exit(0)
    if len(sys.argv) > 1:
        if len(sys.argv) != 5 or sys.argv[1] != '--demo-order':
            sys.exit('用法：启动.command --demo-order 合约代码 BUY或SELL 数量')
        try:
            print(json.dumps(submit_demo_order(*sys.argv[2:]), ensure_ascii=False, indent=2))
        except ValueError as error:
            sys.exit(str(error))
        sys.exit(0)
    server = ThreadingHTTPServer(('127.0.0.1',8765),Handler)
    try:
        cancel_backtests(before=BACKTEST_CANCEL_BEFORE)
        resume_backtests()
    except (OSError, sqlite3.Error) as error:
        print(f'回测任务恢复失败：{error}', file=sys.stderr, flush=True)
    server.strategy = DemoStrategy()
    server.shadow = LiveMarketSimulation(journal_path().with_name('live-market-mirror.json'))
    threading.Thread(target=watch_opportunities, daemon=True).start()
    if os.getenv('BINANCE_DEMO_API_KEY') and os.getenv('BINANCE_DEMO_API_SECRET'):
        threading.Thread(target=protection_watch, daemon=True).start()
        threading.Thread(target=external_demo_close_watch, daemon=True).start()
    print('http://127.0.0.1:8765', flush=True)
    webbrowser.open('http://127.0.0.1:8765')
    server.serve_forever()
