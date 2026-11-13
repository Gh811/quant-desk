import math
import statistics
from decimal import Decimal


US_EQUITY_ASSETS = set('SNDK SOXL KORU MU NVDA QQQ GOOGL SPCX META TSLA AMZN INTC'.split())
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


def trend_metrics(bars, direction=1):
    closes = [bar[4] for bar in bars]
    fast = statistics.mean(closes[-12:])
    slow = statistics.mean(closes[-48:])
    true_ranges = [
        max(high-low, abs(high-prev[4]), abs(low-prev[4]))
        for prev, (_, _, high, low, _) in zip(bars[-15:-1], bars[-14:])
    ]
    atr_fraction = statistics.mean(true_ranges) / closes[-1]
    return direction*closes[-1] > direction*fast > direction*slow, Decimal(str(atr_fraction))


STRATEGY_NAMES = {
    'swing': '中长线 · 1 小时双向突破与回踩',
    'intraday': '短线 · 15 分钟双向突破与回踩',
    'fast': '短线 · 5 分钟双向突破与回踩',
    'us_open': '美股双向测试',
}
SIGNAL_TIMEFRAMES = {
    'fast': ('5m', '15m', '1h'),
    'intraday': ('15m', '1h', '4h'),
    'swing': ('1h', '4h', '1d'),
    'us_open': ('15m', '1h', '4h'),
}
TIMEFRAME_MINUTES = {'5m': 5, '15m': 15, '1h': 60, '4h': 240, '1d': 1440}
STRATEGY_INTERVALS = {mode: TIMEFRAME_MINUTES[frames[0]] * 60000 for mode, frames in SIGNAL_TIMEFRAMES.items()}
STRATEGY_LOOKBACK = {'fast': 12, 'intraday': 20, 'swing': 24, 'us_open': 20}
STRATEGY_MAX_ATR = {'fast': Decimal('.008'), 'intraday': Decimal('.012'), 'swing': Decimal('.02'),
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
    if mode != 'fast':
        margin_cap /= slots
    risk_cap = equity * ENTRY_EQUITY_RISK[mode] * risk_scale / (stop + cost)
    return max(Decimal(0), min(capital_cap, margin_cap, risk_cap))


def entry_signal(symbol, strategy, bars):
    lookback = STRATEGY_LOOKBACK[strategy]
    close = bars[-1][4]
    direction = 1
    trend, volatility = trend_metrics(bars)
    if not trend:
        trend, _ = trend_metrics(bars, -1)
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
        fast_average = statistics.mean(bar[4] for bar in bars[-12:])
        previous_average = statistics.mean(bar[4] for bar in bars[-13:-1])
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


def evaluate_entry_signal(symbol, mode, now, read):
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
                return candle, None, volatility, '波动突增 · 等待突破后回踩确认', {}
            if retest is not None:
                score = bars[-1][4]/retest - 1
                reason = '高波动回踩做多' if direction > 0 else '高波动反抽做空'
    if score is None or score == 0:
        return candle, None, volatility, reason, {}

    signal_at = candle + STRATEGY_INTERVALS[mode]
    direction = 1 if score > 0 else -1
    confirmation, _ = read(confirm, signal_at, 48)
    if not trend_metrics(confirmation, direction)[0]:
        return candle, None, volatility, f'{reason} · {confirm} 趋势未确认', {}
    background, _ = read(backdrop, signal_at, 48)
    if trend_metrics(background, -direction)[0]:
        return candle, None, volatility, f'{reason} · {backdrop} 趋势反向', {}
    aligned = trend_metrics(background, direction)[0]

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
        detail=f'{confirm} 同向 · {backdrop}{" 同向" if aligned else " 中性"} · '
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


def fast_position_assessment(market, opened, direction, setup):
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
            return 'exit', f'5 分钟收盘 {close:f} 穿过入场结构极值 {extreme:f}'
        if lost_boundary and opposite:
            return 'exit', f'5 分钟收盘 {close:f} 穿过入场触发位 {boundary:f}，{opposite}趋势反向'
        if failed_pair and confirmation is not None and not trend_metrics(confirmation, direction)[0]:
            return 'exit', f'连续两根 5 分钟 K 线收盘失守触发位 {boundary:f}，15 分钟同向趋势消失'
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
