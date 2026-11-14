from array import array
from datetime import datetime

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
    'swing': '中长线 · 1 小时双向突破与回踩',
    'intraday': '短线 · 15 分钟双向突破与回踩',
    'scalp': '1 分钟双向 · 高频测试',
    'fast': '短线 · 5 分钟双向突破与回踩',
    'us_open': '美股双向测试',
}
SIGNAL_TIMEFRAMES = {
    'scalp': ('1m', '5m', '15m'),
    'fast': ('5m', '15m', '1h'),
    'intraday': ('15m', '1h', '4h'),
    'swing': ('1h', '4h', '1d'),
    'us_open': ('15m', '1h', '4h'),
}
TIMEFRAME_MINUTES = {'1m': 1, '5m': 5, '15m': 15, '1h': 60, '4h': 240, '1d': 1440}
STRATEGY_INTERVALS = {mode: TIMEFRAME_MINUTES[frames[0]] * 60000 for mode, frames in SIGNAL_TIMEFRAMES.items()}
STRATEGY_LOOKBACK = {'scalp': 6, 'fast': 12, 'intraday': 20, 'swing': 24, 'us_open': 20}
STRATEGY_MAX_ATR = {'scalp': Decimal('.008'), 'fast': Decimal('.008'), 'intraday': Decimal('.012'), 'swing': Decimal('.02'),
                    'us_open': Decimal('.015')}
ENTRY_EQUITY_RISK = {'fast': Decimal('.005'), 'intraday': Decimal('.00325'), 'swing': Decimal('.0075'),
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


BOLLINGER_MACD_RULES = {'bb_macd_reentry': '布林带回归与 MACD', 'bb_macd_pullback': '布林带顺势回踩与 MACD',
                        'bb_macd_breakout': '布林带突破与 MACD', 'bb_macd_trend': '布林带中轨趋势延续', 'bb_macd_flow': '布林带与成交确认', 'bb_macd_flow_long': '布林带与成交确认（多头）'}
BACKTEST_RULES = {'rsi20':'趋势回撤 RSI 20', 'rsi35':'趋势回撤 RSI 35', 'fade24': '24 小时假突破', 'fade48': '48 小时假突破', 'channel24': '24 小时通道突破', 'channel48': '48 小时通道突破', 'swing_pullback':'顺势回踩', 'swing_aligned':'日线同向回踩', 'current': '当前策略', 'continuation': '突破延续研究', 'reversion': '区间回归研究', 'momentum': '多周期动量回撤', 'confirmation': '严格确认', 'retest': '严格确认＋回踩'}
BACKTEST_RULES.update(BOLLINGER_MACD_RULES)


def range_reversion_signal(now, read):
    bars, _ = read('5m', now, 22)
    previous, current = bars[-2:]
    closes = [bar[4] for bar in bars[:20]]
    center = statistics.mean(closes)
    width = 2 * statistics.pstdev(closes)
    _, atr = trend_metrics(bars)
    trend_bars, _ = read('15m', now, 17)
    trend = [bar[4] for bar in trend_bars]
    path = sum(abs(b-a) for a,b in zip(trend,trend[1:]))
    efficiency = abs(trend[-1]-trend[0])/path if path else 0
    upper, lower = center + width, center - width
    close = current[4]
    direction = (1 if previous[4] < lower < close < center and close > current[1]
                 else -1 if center < close < upper < previous[4] and close < current[1] else 0)
    if not direction or efficiency > .35 or not 0 < atr <= STRATEGY_MAX_ATR['fast']:
        return current[0], None, atr, '未出现横盘回归信号', {}
    boundary = lower if direction > 0 else upper
    extreme = (min if direction > 0 else max)(previous[3 if direction > 0 else 2],
                                             current[3 if direction > 0 else 2])
    return current[0], direction, atr, '区间回归做多' if direction > 0 else '区间回归做空', dict(
        close=Decimal(str(close)), boundary=Decimal(str(boundary)), stressed=True, priority=1-efficiency,
        extreme=Decimal(str(extreme)), center=Decimal(str(center)))


def momentum_pullback(now, read):
    bars, raw = read('5m', now, 22)
    hourly, _ = read('1h', now, 25)
    prev, cur = bars[-2:]
    center = statistics.mean(bar[4] for bar in bars[-22:-2])
    _, atr = trend_metrics(bars)
    momentum = hourly[-1][4] / hourly[-13][4] - 1
    direction = 1 if momentum > 0 else -1
    aligned = direction * (hourly[-1][4] - statistics.mean(bar[4] for bar in hourly[-24:])) > 0
    volume = float(raw[-1][7])
    typical = statistics.median(float(row[7]) for row in raw[:-1])
    active_flow = float(raw[-1][10])/volume if volume > 0 else .5
    active_flow = active_flow if direction > 0 else 1-active_flow
    trigger = prev[2 if direction > 0 else 3]
    if not (aligned and 0 < atr <= STRATEGY_MAX_ATR['fast']
            and direction*(prev[4]-center) <= 0 < direction*(cur[4]-trigger)
            and direction*(cur[4]-center) > 0 and volume >= typical > 0
            and active_flow >= .52):
        return cur[0], None, atr, '未出现顺势回撤再启动', {}
    extreme = (min if direction > 0 else max)(prev[3 if direction > 0 else 2],cur[3 if direction > 0 else 2])
    return cur[0], direction, atr, '顺势回撤再启动', dict(
        close=Decimal(str(cur[4])), boundary=Decimal(str(trigger)), stressed=False,
        priority=active_flow+min(volume/typical,2)/2, extreme=Decimal(str(extreme)))


def channel_breakout(now, read, lookback):
    bars, _ = read('1h', now, lookback + 1)
    current = bars[-1]
    prior = bars[:-1]
    upper = max(bar[2] for bar in prior)
    lower = min(bar[3] for bar in prior)
    close = current[4]
    side = 1 if close > upper else -1 if close < lower else 0
    _, volatility = trend_metrics(bars)
    if not side or not 0 < volatility <= STRATEGY_MAX_ATR['swing']:
        return current[0], None, volatility, '等待价格通道突破', {}
    boundary = upper if side > 0 else lower
    return current[0], side, volatility, '价格通道突破', dict(
        close=Decimal(str(close)), boundary=Decimal(str(boundary)), stressed=False,
        priority=abs(close / boundary - 1) / float(volatility))


def failed_breakout(now, read, lookback):
    bars, _ = read('1h', now, lookback + 2)
    previous, current = bars[-2:]
    upper = max(bar[2] for bar in bars[:-2])
    lower = min(bar[3] for bar in bars[:-2])
    close = current[4]
    side = (1 if previous[4] < lower < close and close > current[1]
            else -1 if previous[4] > upper > close and close < current[1] else 0)
    _, volatility = trend_metrics(bars)
    if not side or not 0 < volatility <= STRATEGY_MAX_ATR['swing']:
        return current[0], None, volatility, '等待突破失败确认', {}
    boundary = lower if side > 0 else upper
    return current[0], side, volatility, '通道假突破反转', dict(
        close=Decimal(str(close)), boundary=Decimal(str(boundary)), stressed=False,
        priority=abs(close / boundary - 1) / float(volatility))


def trend_rsi_pullback(now, read, threshold):
    bars, _ = read('1h', now, 50)
    closes = [bar[4] for bar in bars]
    _, volatility = trend_metrics(bars)
    recent_changes = [b - a for a, b in zip(closes[-4:-1], closes[-3:-1])]
    total = sum(map(abs, recent_changes))
    rsi = 100 * sum(max(0, change) for change in recent_changes) / total if total else 50
    prior_mean = statistics.mean(closes[-49:-1])
    earlier_mean = statistics.mean(closes[-50:-2])
    current = bars[-1]
    if not (0 < volatility <= STRATEGY_MAX_ATR['swing'] and closes[-1] > prior_mean >= earlier_mean
            and rsi <= threshold and closes[-1] > closes[-2] and closes[-1] > current[1]):
        return current[0], None, volatility, '等待多头回撤修复', {}
    return current[0], 1, volatility, '趋势内 RSI 回撤修复', dict(
        close=Decimal(str(current[4])), boundary=Decimal(str(current[3])),
        stressed=False, priority=(threshold-rsi)/100)


def bollinger_macd_signal(now, read, rule):
    bars, raw = read('1h', now, 50)
    closes = [bar[4] for bar in bars]
    current = bars[-1]
    _, volatility = trend_metrics(bars)
    history = closes[-22:-2]
    center = statistics.mean(history)
    deviation = statistics.pstdev(history)
    bands = 1.5 if rule in {'bb_macd_pullback', 'bb_macd_flow', 'bb_macd_flow_long'} else 2
    lower, upper = center - bands*deviation, center + bands*deviation
    fast = slow = closes[0]
    signal = 0
    histogram = [0]
    for close in closes[1:]:
        fast += (close-fast) * (2/13)
        slow += (close-slow) * (2/27)
        macd = fast-slow
        signal += (macd-signal) * (2/10)
        histogram.append(macd-signal)
    last, previous = closes[-1], closes[-2]
    side = 0
    if rule == 'bb_macd_trend':
        trend = statistics.mean(closes[-49:-1])
        if trend < center and lower < previous < center < last < upper and histogram[-1] > max(histogram[-2], 0):
            side = 1
        elif trend > center and upper > previous > center > last > lower and histogram[-1] < min(histogram[-2], 0):
            side = -1
    elif rule == 'bb_macd_breakout':
        if previous <= upper < last and histogram[-1] > max(0, histogram[-2]):
            side = 1
        elif previous >= lower > last and histogram[-1] < min(0, histogram[-2]):
            side = -1
    else:
        long = previous < lower < last < center and histogram[-1] > histogram[-2]
        short = center < last < upper < previous and histogram[-1] < histogram[-2]
        if rule == 'bb_macd_pullback':
            trend = statistics.mean(closes[-49:-1]) - statistics.mean(closes[-50:-2])
            long, short = long and trend > 0, short and trend < 0
        if rule in {'bb_macd_flow', 'bb_macd_flow_long'}:
            volume = float(raw[-1][7])
            typical = statistics.median(float(row[7]) for row in raw[-21:-1])
            buy_ratio = float(raw[-1][10]) / volume if volume > 0 else .5
            long = long and volume >= typical > 0 and buy_ratio >= .52
            short = short and rule == 'bb_macd_flow' and volume >= typical > 0 and buy_ratio <= .48
        side = 1 if long else -1 if short else 0
    if not side or not 0 < volatility <= STRATEGY_MAX_ATR['swing']:
        return current[0], None, volatility, '等待布林带与 MACD 确认', {}
    if rule == 'bb_macd_trend':
        boundary = center
    else:
        boundary = (upper if side > 0 else lower) if rule == 'bb_macd_breakout' else (lower if side > 0 else upper)
    return current[0], side, volatility, '布林带与 MACD 确认', dict(
        close=Decimal(str(last)), boundary=Decimal(str(boundary)), stressed=False,
        priority=abs(histogram[-1]-histogram[-2])/last)


from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo('America/New_York')
INTRADAY_RESEARCH_RULES = {
    'session_momentum': '美股尾盘趋势延续',
    'short_reversion': '15 分钟趋势内均值回归',
    'volume_squeeze': '低波动放量突破',
    'range_reclaim': '日内区间扫低收复',
    'open_gap_reclaim': '美股开盘缺口修复',
    'persistent_momentum': '多日趋势内短线恢复',
    'hourly_rsi_5m': '小时趋势 5 分钟回撤',
    'hourly_rsi_15m': '小时趋势 15 分钟回撤',
    'hourly_rsi_15m_flow': '小时趋势 15 分钟回撤与成交确认',
    'hourly_rsi_15m_both_reset': '小时趋势 15 分钟双向回撤与均线收复',
    'hourly_rsi_15m_both': '小时趋势 15 分钟双向回撤',
    'hourly_rsi_15m_frequent': '小时趋势 15 分钟回撤（RSI≤35）',
}
BACKTEST_RULES.update(INTRADAY_RESEARCH_RULES)


def intraday_research_signal(now, read, rule):
    bars, raw = read('5m' if rule == 'hourly_rsi_5m' else '15m', now, 49)
    current = bars[-1]
    close = current[4]
    previous = bars[-2][4]
    _, volatility = trend_metrics(bars)
    closes = [bar[4] for bar in bars]
    side = 0
    boundary = close
    if rule == 'session_momentum':
        local = datetime.fromtimestamp(now/1000, NEW_YORK)
        minute = local.hour*60 + local.minute
        if local.weekday() < 5 and 14*60+30 <= minute <= 15*60:
            open_index = -(minute - (9*60+30))//15
            day_open = bars[open_index][1]
            upper = max(bar[2] for bar in bars[-9:-1])
            lower = min(bar[3] for bar in bars[-9:-1])
            if close > upper and close/day_open > 1.004:
                side, boundary = 1, upper
            elif close < lower and close/day_open < .996:
                side, boundary = -1, lower
    elif rule in {'hourly_rsi_5m', 'hourly_rsi_15m', 'hourly_rsi_15m_flow', 'hourly_rsi_15m_frequent', 'hourly_rsi_15m_both', 'hourly_rsi_15m_both_reset'}:
        hours, _ = read('1h', now, 50)
        hourly = [bar[4] for bar in hours]
        mean = statistics.mean(hourly[-49:-1])
        trend = mean - statistics.mean(hourly[-50:-2])
        changes = [b-a for a,b in zip(closes[-4:-2], closes[-3:-1])]
        total = sum(map(abs, changes))
        rsi = 100 * sum(max(0, change) for change in changes) / total if total else 50
        volume = float(raw[-1][7])
        buy = float(raw[-1][10])/volume if volume > 0 else .5
        rsi_limit = 35 if rule in {'hourly_rsi_15m_frequent', 'hourly_rsi_15m_both', 'hourly_rsi_15m_both_reset'} else 20
        if (hourly[-1] > mean and trend > 0 and rsi <= rsi_limit and close > max(previous, current[1])
                and (rule != 'hourly_rsi_15m_flow' or buy >= .55)):
            side, boundary = 1, current[3]
        elif (rule in {'hourly_rsi_15m_both', 'hourly_rsi_15m_both_reset'} and hourly[-1] < mean and trend < 0
              and rsi >= 100-rsi_limit and close < min(previous, current[1])):
            side, boundary = -1, current[2]
        if not side and rule == 'hourly_rsi_15m_both_reset':
            average = statistics.mean(closes[-7:-1])
            if hourly[-1] > mean and trend > 0 and previous <= average < close and close > current[1]:
                side, boundary = 1, current[3]
            elif hourly[-1] < mean and trend < 0 and previous >= average > close and close < current[1]:
                side, boundary = -1, current[2]
        _, volatility = trend_metrics(hours)
    elif rule == 'persistent_momentum':
        hours, _ = read('1h', now, 49)
        hourly = [bar[4] for bar in hours]
        mean = statistics.mean(hourly[-48:-1])
        momentum = hourly[-1]/hourly[0]-1
        recent = statistics.mean(closes[-9:-1])
        upper = max(bar[2] for bar in bars[-5:-1])
        lower = min(bar[3] for bar in bars[-5:-1])
        atr = float(volatility)
        if momentum >= .015 and hourly[-1] > mean and previous < recent and 0 < close-upper <= atr*close:
            side, boundary = 1, upper
        elif momentum <= -.015 and hourly[-1] < mean and previous > recent and 0 < lower-close <= atr*close:
            side, boundary = -1, lower
        _, volatility = trend_metrics(hours)
    elif rule == 'open_gap_reclaim':
        local = datetime.fromtimestamp(now/1000, NEW_YORK)
        if local.weekday() < 5 and local.hour == 10 and local.minute == 0:
            opening = bars[-2][1]
            reference = read('15m', int((local.replace(hour=16, minute=0).timestamp()-86400)*1000), 1)[0][-1][4]
            if local.weekday() != 0 and opening/reference < .995 and close > bars[-2][2] and close < reference:
                side, boundary = 1, bars[-2][2]
            elif local.weekday() != 0 and opening/reference > 1.005 and close < bars[-2][3] and close > reference:
                side, boundary = -1, bars[-2][3]
    elif rule == 'range_reclaim':
        prior = bars[-17:-1]
        lower = min(bar[3] for bar in prior)
        upper = max(bar[2] for bar in prior)
        spread = current[2] - current[3]
        if spread > 0 and current[3] < lower < close and (close-current[3])/spread >= .75:
            if close < (lower+upper)/2 and close > current[1]:
                side, boundary = 1, lower
    elif rule == 'volume_squeeze':
        recent = bars[-9:-1]
        previous_range = statistics.mean(bar[2]-bar[3] for bar in bars[-33:-9])
        recent_range = statistics.mean(bar[2]-bar[3] for bar in recent)
        median_volume = statistics.median(float(row[7]) for row in raw[-33:-1])
        volume = float(raw[-1][7])
        buy_share = float(raw[-1][10])/volume if volume > 0 else .5
        upper = max(bar[2] for bar in recent)
        lower = min(bar[3] for bar in recent)
        if 0 < recent_range <= .7*previous_range and volume >= 1.5*median_volume > 0:
            if close > upper and buy_share >= .55:
                side, boundary = 1, upper
            elif close < lower and buy_share <= .45:
                side, boundary = -1, lower
    else:
        prior = closes[-22:-2]
        center = statistics.mean(prior)
        width = 1.5*statistics.pstdev(prior)
        trend = center - statistics.mean(closes[-49:-2])
        lower, upper = center - width, center + width
        volume = float(raw[-1][7])
        buy = float(raw[-1][10])/volume if volume > 0 else .5
        if trend > 0 and previous < lower < close < center and buy >= .52:
            side, boundary = 1, lower
        elif trend < 0 and center < close < upper < previous and buy <= .48:
            side, boundary = -1, upper
    if not side or not 0 < volatility <= STRATEGY_MAX_ATR['intraday']:
        return current[0], None, volatility, '等待尾盘趋势或回归信号', {}
    return current[0], side, volatility, INTRADAY_RESEARCH_RULES[rule], dict(
        close=Decimal(str(close)), boundary=Decimal(str(boundary)), stressed=False,
        priority=abs(close/boundary-1)/float(volatility))


def backtest_entry_signal(symbol, mode, now, read, rules, *, us_equity=False):
    if rules in INTRADAY_RESEARCH_RULES:
        return intraday_research_signal(now, read, rules)
    if rules in BOLLINGER_MACD_RULES:
        return bollinger_macd_signal(now, read, rules)
    if rules in {'rsi20', 'rsi35'}:
        return trend_rsi_pullback(now, read, int(rules[3:]))
    if rules in {'fade24', 'fade48'}:
        return failed_breakout(now, read, int(rules[4:]))
    if rules in {'channel24', 'channel48'}:
        return channel_breakout(now, read, int(rules[7:]))
    if rules == 'momentum':
        return momentum_pullback(now, read)
    if rules == 'reversion':
        return range_reversion_signal(now, read)
    candle, score, volatility, reason, context = evaluate_entry_signal(symbol, mode, now, read, us_equity=us_equity,
                                                                      continuation=rules == 'continuation')
    if rules.startswith('swing_') and score:
        side = 1 if score > 0 else -1
        if reason not in {'回踩做多', '反抽做空'}:
            return candle, None, volatility, '仅在回踩确认后入场', {}
        daily, _ = read('1d', candle + STRATEGY_INTERVALS[mode], 48)
        if rules == 'swing_aligned' and not trend_metrics(daily, side)[0]:
            return candle, None, volatility, '日线未同向', {}
        return candle, score, volatility, reason, context
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


def replay_portfolio(mode, markets, start, end, fee, slippage, event_at=None, *, rules='current', contracts=None, maker_fee=None, weight_opportunity=False, target_r=None, max_positions=BACKTEST_MAX_POSITIONS, holding_hours=None, failed_rebound_hours=None, relative_strength_benchmark=None, entry_factor=None, split_entry=False, leverage_override=None, reentry_bars=SHORT_REENTRY_COOLDOWN_BARS, rebalance_entry=False, staged_take=False, maker_entry_assets=(), maker_offset=Decimal('.0001'), maker_rebalance=False, maker_add_offset=Decimal('.0001'), thesis_stop=False, staggered_listing=False):
    split_entry = split_entry or rebalance_entry
    if split_entry and (maker_fee is not None or max_positions != 5):
        raise ValueError('分批入场研究只使用 15 分钟信号、吃单与 5 仓位上限')
    entry_fee = fee if maker_fee is None else maker_fee
    entry_slip = slippage if maker_fee is None else Decimal(0)
    starts = {asset: {frame: series[0][0][0] for frame, series in data.items()}
              for asset, (data, _, _) in markets.items()}
    funding = {asset: {stamp: (rate, price) for stamp, rate, price in rows}
               for asset, (_, _, rows) in markets.items()}

    def read(asset, frame, cutoff, required):
        duration = TIMEFRAME_MINUTES[frame] * 60000
        if frame == '1m' and '1m_signal' in starts[asset]:
            frame = '1m_signal'
        end_index = (cutoff - starts[asset][frame]) // duration
        if end_index < required:
            raise ValueError('回测预热数据不足')
        return tuple(zip(*markets[asset][0][frame][end_index-required:end_index]))

    def relative_strength(asset, stamp):
        # Both legs end on the same fully closed hourly bar.
        horizon = 24 * HOUR
        asset_bars, _ = read(asset, '1h', stamp, 25)
        benchmark = relative_strength_benchmark
        index = (stamp - benchmark[0][0][0]) // HOUR
        if index < 25 or index > len(benchmark):
            raise ValueError('相对强弱基准缺少已收盘行情')
        benchmark_first, benchmark_last = benchmark[index-25][0], benchmark[index-1][0]
        if (benchmark_last[0] != asset_bars[-1][0] or benchmark_first[0] != asset_bars[0][0]
                or benchmark_last[0] - benchmark_first[0] != horizon):
            raise ValueError('相对强弱基准与标的时间不一致')
        return asset_bars[-1][4] / asset_bars[0][4] - benchmark_last[4] / benchmark_first[4]

    def value(position, price):
        direction, amount, entry = (position[key] for key in ('direction', 'amount', 'entry'))
        return amount*(direction*(price-entry)-(position['entry_fee_unit'] + fee*price)) + position['funding']

    def settle_leg(position, quantity, quote, *, maker=False):
        nonlocal balance, fees_paid, estimated_slippage, funding_net
        direction, amount, entry = (position[key] for key in ('direction', 'amount', 'entry'))
        price = quote if maker else quote * (1-direction*slippage)
        fraction = quantity / amount
        funding_share = position['funding'] * fraction
        entry_slippage = position['entry_slippage'] * fraction
        fees = quantity * (position['entry_fee_unit'] + (0 if maker else fee*price))
        net = direction*quantity*(price-entry)-fees+funding_share
        balance += net
        fees_paid += fees
        funding_net += funding_share
        estimated_slippage += entry_slippage + quantity*abs(price-quote)
        position.update(amount=amount-quantity, funding=position['funding']-funding_share,
                        margin=position['margin']*(1-fraction),
                        entry_slippage=position['entry_slippage']-entry_slippage,
                        partial_net=position.get('partial_net', Decimal(0))+net)

    initial = Decimal(10000)
    balance = high_water = equity = initial
    drawdown = fees_paid = estimated_slippage = funding_net = Decimal(0)
    positions = {}
    peak_exposure = Decimal(0)
    weighted_entries = 0
    trades = []
    learning_samples = {side: deque(maxlen=500) for side in (1, -1)}
    event_skips = capacity_skips = peak_positions = 0
    entry_checks = Counter()
    curve = [dict(time=start, equity=float(initial))]
    interval = 300000 if rules == 'hourly_rsi_5m' else STRATEGY_INTERVALS[mode]
    fast_trend = mode == 'fast' and rules not in {'reversion', 'momentum'}
    cost_control = mode != 'swing'
    take_multiple = target_r if target_r is not None else (FAST_TREND_TARGET if fast_trend else 2)
    cost_cap = SHORT_COST_CAP if cost_control else Decimal('.75')
    reentry_at = {}
    max_hours = {'intraday': 4, 'swing': 48, 'us_open': 1.5}.get(mode)
    if rules == 'reversion':
        max_hours = Decimal('.75')
    elif rules == 'momentum':
        max_hours = Decimal('1.5')
    if holding_hours is not None:
        max_hours = holding_hours
    assets = sorted(asset for asset in markets if mode != 'us_open' or equity_kind(asset+'USDT', (contracts or {}).get(asset)) == 'us')
    us_assets = {asset for asset in assets if equity_kind(asset+'USDT', (contracts or {}).get(asset)) == 'us'}

    for index, stamp in enumerate(range(start, end, 60000)):
        if stamp % interval == 0 and equity > 0:
            candidates = []
            for asset in assets:
                if staggered_listing and any(stamp < starts[asset][frame] + 50*TIMEFRAME_MINUTES[frame]*60000
                                             for frame in ('15m', '1h')):
                    continue
                if asset in positions:
                    entry_checks['持仓中'] += 1
                    continue
                if stamp < reentry_at.get(asset, 0):
                    entry_checks['平仓冷却'] += 1
                    continue
                _, score, volatility, reason, context = backtest_entry_signal(
                    asset+'USDT', mode, stamp, lambda frame, cutoff, required: read(asset, frame, cutoff, required),
                    rules, us_equity=asset in us_assets)
                if score is not None and relative_strength_benchmark is not None:
                    strength = relative_strength(asset, stamp)
                    if (1 if score > 0 else -1) * strength <= 0:
                        score, reason = None, '24 小时相对强弱未同向'
                    else:
                        context = {**context, 'priority': context['priority'] + abs(strength)}
                if score is not None and entry_factor is not None:
                    direction = 1 if score > 0 else -1
                    if entry_factor(asset, stamp) * direction <= 0:
                        score, reason = None, '跨市场残差动量未同向'
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

            free_margin = max(Decimal(0), equity - sum(p['margin'] + p.get('reserve', Decimal(0))/p.get('leverage', 1) for p in positions.values()))
            for _, asset, score, volatility, reason, context, evidence in sorted(candidates, key=lambda row: (-row[0], row[1])):
                if max_positions is not None and len(positions) >= max_positions:
                    capacity_skips += 1
                    continue
                direction = 1 if score > 0 else -1
                if (mode == 'fast' and asset in FAST_CORRELATED_ASSETS and
                        any(other in FAST_CORRELATED_ASSETS and p['direction'] == direction for other, p in positions.items())):
                    continue
                opening = Decimal(str(markets[asset][0]['1m'][index][0][1]))
                entry = opening * (1 + direction*entry_slip)
                post_only = asset in maker_entry_assets
                if maker_fee is not None or post_only:
                    offset = maker_offset if post_only else Decimal('.0001')
                    entry = opening * (1 - direction*offset)
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
                if split_entry:
                    stop *= Decimal('1.5')
                if thesis_stop:
                    swing_bars, _ = read(asset, '15m', stamp, 6)
                    extreme = (min if direction > 0 else max)(bar[3 if direction > 0 else 2] for bar in swing_bars)
                    stop = max(stop, direction*(1-Decimal(str(extreme))/entry) + volatility*Decimal('.35'))
                cost = fee + slippage if post_only else entry_fee + fee + entry_slip + slippage
                if cost > stop*cost_cap:
                    entry_checks['预估成本过高'] += 1
                    continue
                target_return = take_multiple*stop
                if rules == 'reversion':
                    target_return = direction * (context['center'] / entry - 1)
                    if target_return < max(stop * Decimal('1.1'), 3 * cost):
                        entry_checks['回归空间不足'] += 1
                        continue
                if fast_trend and not thesis_stop:
                    try:
                        target_return, _ = fast_take_target(entry, direction, stop, volatility, cost,
                                                            context.get('obstacles', ()))
                    except ValueError:
                        entry_checks['止盈空间不足'] += 1
                        continue
                leverage = leverage_override or (5 if mode == 'fast' else min(5 if mode == 'swing' else 3, max(1, int(Decimal('.05')/stop))))
                risk_scale = Decimal(str(evidence['risk_scale'])) if evidence else 1
                notional = entry_notional_budget(mode, equity, free_margin, leverage,
                                                 max(1, (max_positions or BACKTEST_MAX_POSITIONS)-len(positions)), stop, cost, risk_scale)
                if rules in BOLLINGER_MACD_RULES or rules in INTRADAY_RESEARCH_RULES or rules in {'reversion', 'momentum', 'current', 'swing_pullback', 'swing_aligned', 'channel24', 'channel48', 'fade24', 'fade48', 'rsi20', 'rsi35'}:
                    # Reserve risk using entry notional and the original stop plus fees.
                    committed = sum(p['entry'] * p['amount'] + p.get('reserve', 0) for p in positions.values())
                    reserved = sum((p['entry'] * p['amount'] + p.get('reserve', 0)) * (p['risk'] + entry_fee + fee + entry_slip + slippage)
                                   for p in positions.values())
                    remaining = min(equity - committed, (equity * Decimal('.02') - reserved) / (stop + cost))
                    boost = weight_opportunity and context['priority'] >= .8 and target_return >= stop * Decimal('1.5')
                    base_notional = notional
                    notional = min(remaining, free_margin * leverage * Decimal('.95'),
                                   base_notional * (Decimal('1.15') if boost else 1))
                    if notional > base_notional:
                        weighted_entries += 1
                if notional <= 0:
                    entry_checks['风险预算不足'] += 1
                    continue
                if maker_fee is not None or post_only:
                    bar = markets[asset][0]['1m'][index][0]
                    touch = Decimal(str(bar[3 if direction > 0 else 2]))
                    if direction*(touch-entry) >= 0:
                        entry_checks['挂单未成交'] += 1
                        continue
                entry_checks['模拟开仓'] += 1
                planned_notional = notional
                if split_entry:
                    notional /= 3 if rebalance_entry else 2
                margin = notional/leverage
                free_margin -= margin
                positions[asset] = dict(opened=stamp, entry=entry, direction=direction, amount=notional/entry,
                                        risk=stop, trail=entry*(1-direction*stop), margin=margin,
                                        take=entry*(1+direction*target_return),
                                        initial_take=entry*(1+direction*target_return),
                                        setup=fast_entry_setup(context, volatility) if fast_trend else None,
                                        entry_fee_unit=Decimal(0) if post_only else entry*entry_fee,
                                        post_only=post_only,
                                        entry_slippage=abs(entry-opening)*notional/entry if maker_fee is None and not post_only else Decimal(0),
                                        peak=Decimal(0), funding=Decimal(0), signal=reason, evidence=evidence)
                if split_entry:
                    positions[asset].update(reserve=planned_notional-notional, leverage=leverage,
                                            add_trigger=entry*(1-direction*stop/3))
                if staged_take:
                    positions[asset]['take_stage'] = 0
                peak_positions = max(peak_positions, len(positions))
                if equity > 0:
                    peak_exposure = max(peak_exposure, sum(p['entry']*p['amount'] for p in positions.values()) / equity)

        for asset, position in list(positions.items()):
            data, marks, _ = markets[asset]
            _, mark_open, mark_high, mark_low, mark_close = marks[index][0]
            _, trade_open, trade_high, trade_low, trade_close = data['1m'][index][0]
            entry, direction, amount, stop = (position[k] for k in ('entry','direction','amount','risk'))
            if stamp in funding[asset] and stamp > position['opened']:
                rate, price = funding[asset][stamp]
                position['funding'] -= direction*amount*rate*price
            if split_entry and position['reserve'] and stamp > position['opened']:
                mark = Decimal(str(mark_open))
                opening = Decimal(str(trade_open))
                maker_add = maker_rebalance and rebalance_entry and position['post_only']
                price = opening*(1-direction*maker_add_offset if maker_add else 1+direction*entry_slip)
                eligible = (direction*(mark-position['add_trigger']) <= 0
                        and direction*(mark-position['trail']) > 0
                        and direction*(opening-position['trail']) > 0
                        and direction*(price-position['trail']) > 0
                        and (not rebalance_entry or (direction*(price-entry) < 0 and 2*amount*price <= position['reserve']))
                        and (max_hours is None or stamp-position['opened'] < max_hours*HOUR))
                if eligible and maker_add:
                    mark_adverse = Decimal(str(mark_low if direction > 0 else mark_high))
                    trade_adverse = Decimal(str(trade_low if direction > 0 else trade_high))
                    eligible = direction*(mark_adverse-position['trail']) > 0 and direction*(trade_adverse-price) < 0
                    entry_checks['补仓 GTX 成交' if eligible else '补仓 GTX 未成交'] += 1
                if eligible:
                    added = 2*amount if rebalance_entry else position['reserve']/price
                    total = amount+added
                    if rebalance_entry:
                        position['reduce_qty'] = total/3 if maker_rebalance else added
                        position['add_at'] = stamp
                    if position['post_only']:
                        add_fee = Decimal(0) if maker_add else price*added*entry_fee
                        position['entry_fee_unit'] = (amount*position['entry_fee_unit'] + add_fee)/total
                    entry = (entry*amount+price*added)/total
                    if not position['post_only']:
                        position['entry_fee_unit'] = entry*entry_fee
                    amount = total
                    stop = direction*(1-position['trail']/entry)
                    take = entry*(1+direction*take_multiple*stop)
                    position.update(entry=entry, amount=amount, risk=stop, peak=Decimal(0),
                                    take=take, initial_take=take,
                                    margin=position['margin']+position['reserve']/position['leverage'],
                                    reserve=Decimal(0))
                    if not maker_add:
                        position['entry_slippage'] += added*abs(price-opening)
                    entry_checks['分批补仓成交'] += 1
            reduce_qty = position.get('reduce_qty', Decimal(0))
            if reduce_qty and stamp > position['add_at'] and (max_hours is None or stamp-position['opened'] < max_hours*HOUR):
                # Allocation to this leg uses weighted-average cost and proportional funding.
                close_price = Decimal(str(trade_open))*(1-direction*slippage)
                funding_share = position['funding'] * reduce_qty / amount
                reduce_fees = reduce_qty*(position['entry_fee_unit'] + fee*close_price)
                reduce_net = direction*reduce_qty*(close_price-entry)-reduce_fees+funding_share
                mark = Decimal(str(mark_open))
                if direction*(mark-position['trail']) > 0 and direction*(position['take']-mark) > 0 and reduce_net >= 0:
                    fraction = reduce_qty/amount
                    fees_paid += reduce_fees
                    funding_net += funding_share
                    allocated_slippage = position['entry_slippage']*fraction
                    estimated_slippage += allocated_slippage + reduce_qty*abs(close_price-Decimal(str(trade_open)))
                    balance += reduce_net
                    amount -= reduce_qty
                    position.update(amount=amount, reduce_qty=Decimal(0), partial_net=reduce_net,
                                    funding=position['funding']-funding_share,
                                    margin=position['margin']*(1-fraction),
                                    entry_slippage=position['entry_slippage']-allocated_slippage)
                    entry_checks['回均价减仓成交'] += 1
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
            maker_fill_bar = (maker_fee is not None or position['post_only']) and stamp == position['opened']
            if position['post_only'] and maker_fill_bar:
                # A minute bar cannot establish when the order joined the queue or filled.
                # Count adverse stop touches, and suppress favorable exits in the fill minute.
                favorable = entry
            if fast_trend and not maker_fill_bar and not position.get('take_stage') and not thesis_stop:
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
                    breakeven = (entry+direction*(position['entry_fee_unit']-position['funding']/amount))/(1-direction*fee)
                    breakeven /= 1-direction*slippage
                    if direction*(mark-breakeven) > 0:
                        candidate = (breakeven if candidate is None else
                                     (max if direction > 0 else min)(candidate, breakeven))
                    if candidate is not None and direction*(mark-candidate) > 0:
                        trail = position['trail'] = (max if direction > 0 else min)(trail, candidate)
                review_window = stamp // interval
                if not staged_take and take_return is not None and reason is None and position.get('take_reviewed') != review_window:
                    take = position['take'] = (min if direction > 0 else max)(take, entry*(1+direction*take_return))
                    position['take_reviewed'] = review_window
            if reason is None:
                take_stage = position.get('take_stage', 0)
                if take_stage:
                    breakeven = (entry+direction*(position['entry_fee_unit']-position['funding']/amount))/(1-direction*fee)
                    breakeven /= 1-direction*slippage
                    trail = position['trail'] = (max if direction > 0 else min)(trail, breakeven)
                # Once TP3 arms the runner, let the trailing stop manage its exit.
                if holding_hours is not None and stamp-position['opened'] >= holding_hours*HOUR and take_stage < 3:
                    reason, price = '持仓到期', Decimal(str(trade_open))
                elif (failed_rebound_hours is not None and stamp % HOUR == 0
                      and stamp-position['opened'] >= failed_rebound_hours*HOUR
                      and direction*(Decimal(str(trade_open))/entry-1) <= -stop/3):
                    reason, price = '回撤修复失败', Decimal(str(trade_open))
                elif direction*(adverse-trail) <= 0:
                    reason = ('TP3 尾仓跟踪' if take_stage == 3 else '分批止盈后保护止损' if take_stage
                              else '移动止损' if trail != entry*(1-direction*stop) else '止损')
                    price = trail if maker_fill_bar else (min if direction > 0 else max)(trail, Decimal(str(trade_open)))
                elif staged_take and not maker_fill_bar and take_stage < 3:
                    trade_bar = data['1m'][index][0]
                    trade_extreme = Decimal(str(trade_bar[2 if direction > 0 else 3]))
                    risk_distance = entry * stop
                    levels = (take, take + direction*risk_distance/2, take + direction*risk_distance*(Decimal('1.5') if thesis_stop else 1))
                    # The existing stop wins an ambiguous minute. New stops take effect on the next minute.
                    for stage in range(take_stage, len(levels)):
                        level = levels[stage]
                        maker_exit = position['post_only'] and stage < 2
                        if maker_exit:
                            if direction*(trade_extreme-level) <= 0:
                                break
                        elif min(direction*(favorable-level), direction*(trade_extreme-level)) < 0:
                            break
                        position['take_stage'] = stage+1
                        position['reserve'] = Decimal(0)
                        if stage < 2:
                            position.setdefault('take_start_amount', amount)
                            quantity = position['amount']/2
                            settle_leg(position, quantity, level, maker=maker_exit)
                            position['reduce_qty'] = Decimal(0)
                            entry_checks[f'TP{stage+1} 成交'] += 1
                        else:
                            position['runner_peak'] = level
                            entry_checks['TP3 尾仓跟踪启动'] += 1
                    amount = position['amount']
                elif not staged_take and not maker_fill_bar and direction*(favorable-take) >= 0:
                    reason, price = '动态止盈' if take != position['initial_take'] else '止盈', take
                if reason is None and max_hours is not None and stamp-position['opened'] >= max_hours*HOUR and position.get('take_stage', 0) < 3:
                    reason, price = '持仓到期', Decimal(str(trade_open))
                if reason is None and stamp+60000 >= end:
                    reason, price = '回测结束结算', Decimal(str(trade_close))
            if reason:
                price_before_slippage = price
                price *= 1-direction*slippage
                fees = amount*(position['entry_fee_unit']+fee*price)
                fees_paid += fees
                estimated_slippage += position['entry_slippage'] + amount*abs(price-price_before_slippage)
                funding_net += position['funding']
                net = direction*amount*(price-entry)-fees+position['funding']
                balance += net
                family = fast_signal_family(position['signal'])
                if fast_trend and family and reason != '回测结束结算':
                    result_r = (net + position.get('partial_net', 0)) / (entry * position.get('take_start_amount', amount) * stop)
                    learning_samples[direction].append((stamp + 60000, asset+'USDT', direction, family,
                                                        float(max(Decimal(-3), min(Decimal(3), result_r)))))
                trades.append(dict(symbol=asset+'USDT', opened=position['opened'], closed=stamp, entry=float(entry), exit=float(price),
                                   direction=direction, net=float(net + position.get('partial_net', 0)), reason=reason, signal=position['signal'],
                                   evidence=position['evidence']))
                if cost_control:
                    reentry_at[asset] = stamp + reentry_bars * interval
                del positions[asset]
            else:
                close = Decimal(str(mark_close))
                peak = max(position['peak'], direction*(close/entry-1))
                position['peak'] = peak
                if staged_take and position['take_stage'] == 3:
                    tighten = max if direction > 0 else min
                    runner_peak = position['runner_peak'] = tighten(position['runner_peak'], close)
                    runner_stop = runner_peak-direction*entry*stop*Decimal('.75')
                    position['trail'] = tighten(trail, runner_stop)
                elif not thesis_stop and (not staged_take or not position['take_stage']) and not fast_trend and rules not in {'reversion', 'momentum'} and peak >= stop:
                    breakeven = (entry+direction*(position['entry_fee_unit']-position['funding']/amount))/(1-direction*fee)
                    candidate = (max if direction > 0 else min)(entry*(1+direction*(peak-stop)), breakeven)
                    if direction*(close-candidate) > 0 and direction*(candidate-trail) > 0:
                        position['trail'] = candidate
                        if split_entry:
                            position['reserve'] = Decimal(0)
        equity = balance + sum(value(p, Decimal(str(markets[asset][0]['1m'][index][0][4])))
                               for asset, p in positions.items())
        high_water = max(high_water, equity)
        drawdown = max(drawdown, (high_water-equity)/high_water)
        if (stamp+60000) % HOUR == 0 or stamp+60000 >= end:
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
                peak_exposure=float(peak_exposure), weighted_entries=weighted_entries,
                entry_checks=dict(entry_checks.most_common()))


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

