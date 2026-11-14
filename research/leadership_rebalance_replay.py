import csv
import json
import argparse
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

import channel_engine as engine

ROOT = Path(__file__).resolve().parents[1] / 'market-data'
ASSETS = 'AMZN GOOGL KORU META MU NVDA QQQ SAMSUNG SKHYNIX SOXL SPCX TSLA'.split()
MINUTE = 60000
HOUR = 60 * MINUTE

def stamp(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()*1000)

def rows(asset, kind, month):
    suffix = kind if kind == 'fundingRate' else '1m'
    name = f'{asset}USDT-{suffix}-2026-{month}.zip'
    path = (ROOT/'sep'/kind/name if month == '09' else ROOT/'earlier-replays'/f'portfolio-{kind}-{name}')
    if asset == 'BTC' and month == '10':
        path = ROOT/'earlier-replays'/{
            'klines': 'quant-oct-test.zip',
            'markPriceKlines': 'quant-btc-markPriceKlines-oct.zip',
            'fundingRate': 'quant-BTCUSDT-fundingRate-2026-10.zip',
        }[kind]
    with ZipFile(path) as archive, archive.open(archive.namelist()[0]) as data:
        for row in csv.reader(line.decode() for line in data):
            if row and row[0].isdigit():
                yield row

def load(asset, begin, end):
    # Keep two weeks of bars available before the research window.
    raw_start = stamp('2026-09-01')
    trades = engine.MinuteBars(begin)
    marks = engine.MinuteBars(begin)
    frames = {'5m': [], '15m': [], '1h': []}
    buffers = {}
    for kind, series in [('klines', trades), ('markPriceKlines', marks)]:
        expected = raw_start
        for month in ('09', '10'):
            for row in rows(asset, kind, month):
                time = int(row[0])
                if time != expected or int(row[6]) != time+MINUTE-1:
                    raise ValueError(f'{asset} {kind} 不连续，时间 {time}')
                expected += MINUTE
                op, high, low, close = map(float, row[1:5])
                if time >= begin:
                    series.append((time, op, high, low, close))
                if kind == 'klines':
                    volume, buy = float(row[7]), float(row[10])
                    for frame, bars in frames.items():
                        width = engine.TIMEFRAME_MINUTES[frame]*MINUTE
                        if time % width == 0:
                            buffers[frame] = [time, op, high, low, close, volume, buy]
                        else:
                            bar = buffers[frame]
                            bar[2] = max(bar[2], high)
                            bar[3] = min(bar[3], low)
                            bar[4] = close
                            bar[5] += volume
                            bar[6] += buy
                        if (time+MINUTE) % width == 0:
                            bar = buffers[frame]
                            bars.append((tuple(bar[:5]), (0,0,0,0,0,0,0,bar[5],0,0,bar[6])))
        if expected != end:
            raise ValueError(f'{asset} {kind} 结束时间不足')
    funding = []
    for month in ('09', '10'):
        for row in rows(asset, 'fundingRate', month):
            time = int(row[0])//MINUTE*MINUTE
            if begin <= time < end:
                price = Decimal(str(marks[(time-begin)//MINUTE][0][1]))
                funding.append((time, Decimal(row[2]), price))
    frames['1m'] = trades
    return frames, marks, funding

def compare_fast_exits(markets, begin, end):
    for staged in (False,True):
        report=engine.replay_portfolio('fast',markets,begin,end,Decimal('.0005'),Decimal('.0001'),
            rules='continuation',staged_take=staged,max_positions=5)
        checks=report['entry_checks']
        trades=report['trades']
        assert abs(sum(t['net'] for t in trades)-report['net'])<1e-6
        print(staged,{k:report[k] for k in ('count','net','drawdown')},
              {k:checks.get(k,0) for k in ('TP1 成交','TP2 成交','TP3 尾仓跟踪启动')},
              {d:sum(t['direction']==d for t in trades) for d in (-1,1)},flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--staged', action='store_true', help='比较三档止盈与提高入场频次的设置')
    parser.add_argument('--maker-rebalance', action='store_true', help='比较补仓只挂单及净保本减仓')
    parser.add_argument('--fast-exits', action='store_true', help='对照5分钟双向策略的三档止盈')
    args = parser.parse_args()
    begin, end = stamp('2026-09-20'), stamp('2026-11-01')
    assets = ASSETS + ['SNDK'] if args.staged or args.maker_rebalance else ASSETS
    if args.fast_exits:
        assets = ASSETS + ['SNDK', 'BTC', 'ETH', 'SOL', 'XAU']
    markets = {asset: load(asset, begin, end) for asset in assets}
    if args.fast_exits:
        compare_fast_exits(markets, begin, end)
        return
    benchmark = markets.pop('QQQ')[0]['1h']
    if args.staged or args.maker_rebalance:
        original_markets = {asset: markets[asset] for asset in ASSETS if asset != 'QQQ'}
        maker_assets = set(markets) - {'SKHYNIX', 'SAMSUNG'}
        configs = (
            ('基线', 'hourly_rsi_15m', 3, False, (), '1.25', False),
            ('三档止盈', 'hourly_rsi_15m', 3, True, (), '1.25', False),
            ('三档止盈 · 冷却 15 分钟', 'hourly_rsi_15m', 1, True, (), '1.25', False),
            ('三档止盈 · 冷却 15 分钟 · RSI≤35', 'hourly_rsi_15m_frequent', 1, True, (), '1.25', False),
            ('三档止盈 · 美股 GTX', 'hourly_rsi_15m', 3, True, maker_assets, '1.25', False),
            ('三档止盈 · 美股 GTX · 5 分钟入场', 'hourly_rsi_5m', 3, True, maker_assets, '1.25', False),
            ('三档止盈 · 美股 GTX · 5 分钟入场 · 冷却 5 分钟', 'hourly_rsi_5m', 1, True, maker_assets, '1.25', False),
            ('三档止盈 · 美股 GTX · TP1 0.75R · 加入 SNDK', 'hourly_rsi_15m', 3, True, maker_assets, '.75', True),
            ('三档止盈 · 美股 GTX · TP1 0.75R · 加入 SNDK · 5 分钟入场', 'hourly_rsi_5m', 3, True, maker_assets, '.75', True),
            ('三档止盈 · TP1 0.75R · 加入 SNDK · 5 分钟入场', 'hourly_rsi_5m', 3, True, (), '.75', True),
        )
        if args.maker_rebalance:
            configs = (
                ('原补仓规则', 'hourly_rsi_15m', 3, True, maker_assets, '1.25', False),
                ('GTX 补仓 · 净保本减仓', 'hourly_rsi_15m', 3, True, maker_assets, '1.25', False),
                ('GTX 补仓 · 5 分钟入场 · 加入 SNDK', 'hourly_rsi_5m', 3, True, maker_assets, '.75', True),
                ('GTX 补仓 · 5 分钟入场 · 加入 SNDK · 首仓贴近盘口', 'hourly_rsi_5m', 3, True, maker_assets, '.75', True),
            )
        for index, (label, rule, cooldown, staged, makers, target, expanded) in enumerate(configs):
            report = engine.replay_portfolio(
                'intraday', markets if expanded else original_markets, begin, end, Decimal('.0005'), Decimal('.0001'),
                rules=rule, target_r=Decimal(target), max_positions=5, leverage_override=5,
                holding_hours=24, relative_strength_benchmark=benchmark, reentry_bars=cooldown,
                rebalance_entry=True, staged_take=staged, maker_entry_assets=makers,
                maker_rebalance=args.maker_rebalance and index > 0,
                maker_offset=Decimal(0) if args.maker_rebalance and index == 3 else Decimal('.0001'))
            metrics = {key: report[key] for key in ('count', 'net', 'win_rate', 'drawdown', 'profit_factor',
                                                     'fees', 'slippage', 'funding', 'peak_positions')}
            metrics.update(tp1=report['entry_checks'].get('TP1 成交', 0),
                           tp2=report['entry_checks'].get('TP2 成交', 0),
                           tp3=report['entry_checks'].get('TP3 尾仓跟踪启动', 0),
                           runner_exits=sum(t['reason'] == 'TP3 尾仓跟踪' for t in report['trades']),
                           unfilled=report['entry_checks'].get('挂单未成交', 0),
                           maker_adds=report['entry_checks'].get('补仓 GTX 成交', 0),
                           maker_add_unfilled=report['entry_checks'].get('补仓 GTX 未成交', 0),
                           reductions=report['entry_checks'].get('回均价减仓成交', 0),
                           first_half=sum(t['net'] for t in report['trades'] if t['closed'] < (begin+end)//2),
                           second_half=sum(t['net'] for t in report['trades'] if t['closed'] >= (begin+end)//2))
            assert abs(sum(t['net'] for t in report['trades']) - report['net']) < 1e-6
            print(json.dumps(dict(config=label, **metrics), ensure_ascii=False), flush=True)
        return
    for symbols in (['MU','NVDA'], list(markets)):
        for rebalance in (False, True):
            report = engine.replay_portfolio(
                'intraday', {asset: markets[asset] for asset in symbols}, begin, end,
                Decimal('.0005'), Decimal('.0001'), rules='hourly_rsi_15m',
                target_r=Decimal('1.25'), max_positions=5, leverage_override=5,
                holding_hours=24, relative_strength_benchmark=benchmark, reentry_bars=3, rebalance_entry=rebalance)
            metrics = {k: report[k] for k in ('count','net','win_rate','drawdown','peak_positions')}
            print(json.dumps(dict(assets=symbols, rebalance=rebalance, adds=report['entry_checks'].get('分批补仓成交', 0),
                                  reductions=report['entry_checks'].get('回均价减仓成交', 0), **metrics)), flush=True)

if __name__ == '__main__':
    main()
