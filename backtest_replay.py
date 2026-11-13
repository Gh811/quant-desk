import statistics
from collections import Counter, deque
from decimal import Decimal

from strategy_signals import (
    FAST_TREND_TARGET,
    HOUR,
    SHORT_COST_CAP,
    SHORT_REENTRY_COOLDOWN_BARS,
    SIGNAL_TIMEFRAMES,
    STRATEGY_INTERVALS,
    STRATEGY_NAMES,
    TIMEFRAME_MINUTES,
    US_EQUITY_ASSETS,
    entry_notional_budget,
    entry_stop_fraction,
    equity_kind,
    evaluate_entry_signal,
    fast_entry_evidence,
    fast_entry_setup,
    fast_position_assessment,
    fast_position_levels,
    fast_position_market,
    fast_signal_family,
    fast_structure_stop,
    fast_take_target,
    trend_metrics,
)


BACKTEST_MAX_POSITIONS = 5
FAST_CORRELATED_ASSETS = {'BTC', 'ETH', 'SOL'}
FAST_CRYPTO_ASSETS = FAST_CORRELATED_ASSETS | {'BNB', 'ZEC'}


BACKTEST_RULES = {'current': '当前策略', 'confirmation': '严格确认', 'retest': '严格确认＋回踩'}


def backtest_entry_signal(symbol, mode, now, read, rules):
    candle, score, volatility, reason, context = evaluate_entry_signal(symbol, mode, now, read)
    if rules == 'current' or score is None:
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


def replay_portfolio(mode, markets, start, end, fee, slippage, event_at=None, *, rules='current', contracts=None):
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
        return amount*(direction*(price-entry)-fee*(price+entry)) + position['funding']

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
    assets = sorted(asset for asset in markets if mode != 'us_open' or asset in US_EQUITY_ASSETS)

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
                    asset+'USDT', mode, stamp, lambda frame, cutoff, required: read(asset, frame, cutoff, required), rules)
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
                entry = opening * (1 + direction*slippage)
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
                cost = 2*(fee+slippage)
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
                entry_checks['模拟开仓'] += 1
                margin = notional/leverage
                free_margin -= margin
                positions[asset] = dict(opened=stamp, entry=entry, direction=direction, amount=notional/entry,
                                        risk=stop, trail=entry*(1-direction*stop), margin=margin,
                                        take=entry*(1+direction*target_return),
                                        initial_take=entry*(1+direction*target_return),
                                        setup=fast_entry_setup(context, volatility) if fast_trend else None,
                                        entry_slippage=abs(entry-opening)*notional/entry,
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
                position['review'] = fast_position_assessment(market, position['opened'], direction, position['setup'])
            if fast_trend:
                review, detail = position.get('review', (None, ''))
                mark = Decimal(str(mark_open))
                current = direction*(mark/entry-1)
                position['peak'] = max(position['peak'], current)
                stop_return, take_return = fast_position_levels(stop, current, position['peak'], review == 'weak')
                if review == 'exit':
                    reason, price = f'入场条件失效 · {detail}', Decimal(str(trade_open))
                elif review == 'weak' and current <= stop_return:
                    reason, price = f'行情走弱触及收紧保护线 · {detail}', Decimal(str(trade_open))
                if stop_return is not None and reason is None:
                    candidate = entry*(1+direction*stop_return)
                    breakeven = (entry*(1+direction*fee)-direction*position['funding']/amount)/(1-direction*fee)
                    if direction*(mark-breakeven) > 0:
                        candidate = (max if direction > 0 else min)(candidate, breakeven)
                    if direction*(mark-candidate) > 0:
                        trail = position['trail'] = (max if direction > 0 else min)(trail, candidate)
                review_window = stamp // interval
                if take_return is not None and reason is None and position.get('take_reviewed') != review_window:
                    take = position['take'] = (min if direction > 0 else max)(take, entry*(1+direction*take_return))
                    position['take_reviewed'] = review_window
            if reason is None:
                if direction*(adverse-trail) <= 0:
                    reason = '移动止损' if trail != entry*(1-direction*stop) else '止损'
                    price = (min if direction > 0 else max)(trail, Decimal(str(trade_open)))
                elif direction*(favorable-take) >= 0:
                    reason, price = '动态止盈' if take != position['initial_take'] else '止盈', take
                elif max_hours is not None and stamp-position['opened'] >= max_hours*HOUR:
                    reason, price = '持仓到期', Decimal(str(trade_open))
                elif stamp+60000 >= end:
                    reason, price = '回测结束结算', Decimal(str(trade_close))
            if reason:
                price_before_slippage = price
                price *= 1-direction*slippage
                fees = fee*amount*(entry+price)
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
                    breakeven = (entry*(1+direction*fee)-direction*position['funding']/amount)/(1-direction*fee)
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
