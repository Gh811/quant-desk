import hmac
import json
import math
import os
import time
import threading
import sys
import sqlite3
from contextlib import closing
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
import statistics
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

ASSETS = 'ETH BTC SNDK SOXL KORU MU NVDA SOL SKHYNIX QQQ GOOGL SPCX META TSLA AMZN'.split()
US_EQUITY_ASSETS = set('SNDK SOXL KORU MU NVDA QQQ GOOGL SPCX META TSLA AMZN'.split())
HOSTS = {'OKX': 'https://openapi.okx.com', 'Binance': 'https://fapi.binance.com'}
DEMO_VENUE = 'Binance 模拟盘'
HOSTS[DEMO_VENUE] = 'https://demo-fapi.binance.com'
HOUR = 3600000
DEMO_ORDER_ACTIVE = {'NEW', 'PARTIALLY_FILLED'}
DEMO_ORDER_PENDING = {'待确认', '待核对'} | DEMO_ORDER_ACTIVE
DEMO_ORDER_TERMINAL = {'FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}
DEMO_CLOCK_TTL_NS = 60 * 1_000_000_000
TRADIFI_QUOTE_MAX_AGE_MS = 30_000
TRADIFI_QUOTE_MAX_DEVIATION = Decimal('.03')
ENTRY_WAIT_SECONDS = {'fast': 60, 'intraday': 90, 'swing': 120, 'us_open': 60}
ENTRY_QUOTE_MAX_AGE_MS = 5_000
ENTRY_RETRY_COOLDOWN_SECONDS = {'fast': 20, 'intraday': 30, 'swing': 60, 'us_open': 30}
demo_clock_lock = threading.Lock()
demo_clock_sample = None

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


def select_demo_entry(direction, book, bid, ask, budget, step, stop_loss, cost):
    price = ask if direction > 0 else bid
    amount = (budget / price // step) * step
    try:
        depth = Decimal(book.get('askQty' if direction > 0 else 'bidQty') or '0')
    except (ValueError, TypeError, InvalidOperation):
        depth = Decimal(0)
    if depth.is_finite() and 0 < amount <= depth and cost <= stop_loss / 2:
        return price, amount, 'IOC'
    price = bid if direction > 0 else ask
    return price, (budget / price // step) * step, 'GTX'


def entry_retry_block(previous, current, cooldown_seconds):
    if previous is None or previous[:2] != current[:2]:
        return None
    _, direction, now, price, time_in_force, cost = current
    _, _, last_at, last_price, last_time_in_force, last_cost = previous
    if now - last_at < cooldown_seconds * 1000:
        return '短暂冷却，之后继续复查盘口'
    if direction * (price - last_price) > 0:
        return '价格较上次委托不利，等待更合适的盘口'
    if (price == last_price and not (last_time_in_force == 'GTX' and time_in_force == 'IOC')
            and cost >= last_cost * Decimal('.9')):
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
        return fee, Decimal(0)
    funding_rate = Decimal(reference['lastFundingRate'])
    if not funding_rate.is_finite() or funding_at < now - 1000:
        raise ValueError('资金费率或结算时间异常')
    return fee, max(Decimal(0), direction * funding_rate) if funding_at <= now + funding_window else Decimal(0)


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
        if 400 <= error.code < 500 and error.code not in {408, 429}:
            journal('demo_protection_resolved', dict(clientAlgoId=client_id, rejected=error.code))
        raise
    journal('demo_protection_response', dict(intent=intent, response=response))
    if not any(order['clientAlgoId'] == client_id and order['algoStatus'] == 'NEW'
               for order in open_protection(symbol)):
        raise ValueError('币安未确认保护单仍在挂单列表，请先核对')
    return client_id


def cancel_protection(order):
    client_id = order['clientAlgoId']
    journal('demo_protection_cancel_intent', dict(symbol=order['symbol'], clientAlgoId=client_id))
    result = demo_signed('/fapi/v1/algoOrder', method='DELETE', clientAlgoId=client_id)
    journal('demo_protection_cancel_response', dict(clientAlgoId=client_id, response=result))
    if any(item['clientAlgoId'] == client_id for item in open_protection(order['symbol'])):
        raise ValueError('币安仍显示旧保护单，请核对挂单列表')


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


def set_initial_protection(symbol, entry, stop_loss, tick, context, *, direction=1):
    if entry <= 0 or tick <= 0:
        raise ValueError('成交均价或价格步长无效')
    with protection_lock:
        for kind, change in (('stop', -stop_loss), ('take', 2*stop_loss)):
            price = protection_price(entry, change, tick, direction=direction, kind=kind)
            place_protection(symbol, kind, price, context, direction=direction)


def align_strategy_protection(symbol, entry, mark, stop_loss, stop, tick, context, *, direction=1, take=None):
    if min(entry, mark, tick) <= 0:
        raise ValueError('保护单价格数据无效')
    if take is None:
        take = protection_price(entry, 2*stop_loss, tick, direction=direction, kind='take')
    if min(stop, take) <= 0 or not direction*stop < direction*mark < direction*take:
        raise ValueError('标记价格已到达保护区间边界')
    with protection_lock:
        orders = open_protection(symbol)
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
                place_protection(symbol, kind, target, context, direction=direction)
                keep = None
            for order in matching:
                if order is not keep:
                    cancel_protection(order)


def replace_protection(symbol, kind, raw_price):
    if symbol not in {asset + 'USDT' for asset in ASSETS} or kind not in PROTECTION_TYPES:
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
        intent = dict(context, clientOrderId=client_id, symbol=symbol, side=order.get('side') or event.get('side', 'SELL'),
                      quantity=order['executedQty'], reduce_only=True,
                      decision=dict(reason='交易所止损触发' if event['kind'] == 'stop' else '交易所止盈触发',
                                    detail='触发价 ' + event['price']))
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
ENTRY_EQUITY_RISK = {'fast': Decimal('.005'), 'intraday': Decimal('.0065'), 'swing': Decimal('.0075'),
                     'us_open': Decimal('.004')}


def entry_stop_fraction(asset, mode, volatility):
    if mode == 'us_open':
        return min(Decimal('.025'), max(Decimal('.008'), Decimal('2.5') * volatility))
    if mode == 'fast' and asset in US_EQUITY_ASSETS:
        return min(Decimal('.02'), max(Decimal('.0065'), Decimal('2.5') * volatility))
    low, high = (Decimal('.005'), Decimal('.02')) if mode == 'swing' else (Decimal('.0035'), Decimal('.01'))
    return min(high, max(low, 2 * volatility))


def closed_demo_bars(symbol, interval, cutoff, required):
    minutes = TIMEFRAME_MINUTES[interval]
    duration = minutes * 60000
    raw = request(DEMO_VENUE, '/fapi/v1/klines', symbol=symbol, interval=interval, limit=65)
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
    lookback = STRATEGY_LOOKBACK[strategy]
    if bars is None:
        bars, _ = closed_demo_bars(symbol, SIGNAL_TIMEFRAMES[strategy][0], demo_now_ms(), 49)
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


def scan_entry_signal(symbol, mode):
    base, confirm, backdrop = SIGNAL_TIMEFRAMES[mode]
    bars, raw = closed_demo_bars(symbol, base, demo_now_ms(), 49)
    candle, score, volatility, reason = entry_signal(symbol, mode, bars)
    if score is None:
        return candle, None, volatility, reason, {}

    signal_at = candle + STRATEGY_INTERVALS[mode]
    direction = 1 if score > 0 else -1
    confirmation, _ = closed_demo_bars(symbol, confirm, signal_at, 48)
    if not trend_metrics(confirmation, direction)[0]:
        return candle, None, volatility, f'{reason} · {confirm} 趋势未确认', {}
    background, _ = closed_demo_bars(symbol, backdrop, signal_at, 48)
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
    if relative_volume < (1.2 if reason in {'突破做多', '跌破做空'} else 0.8):
        return candle, None, volatility, f'{reason} · 相对成交量偏低（{relative_volume:.2f}×）', {}
    if side_flow < 0.45:
        return candle, None, volatility, f'{reason} · 同向主动成交偏弱（{side_flow:.0%}）', {}
    if abs(score) > 1.5 * float(volatility):
        return candle, None, volatility, f'{reason} · 触发幅度过大，避免追价', {}

    close = bars[-1][4]
    return candle, score, volatility, reason, dict(
        priority=int(aligned) + min(relative_volume, 2.0) / 2 + side_flow
                 + min(abs(score) / float(volatility), 0.75),
        close=Decimal(str(close)), boundary=Decimal(str(close / (1 + score))),
        detail=f'{confirm} 同向 · {backdrop}{" 同向" if aligned else " 中性"} · '
               f'相对成交量 {relative_volume:.2f}× · 同向主动成交 {side_flow:.0%}')



def restore_strategy_positions(positions, assets, mode):
    if not positions:
        return {}, {}
    orders = recent_orders(limit=None)
    holdings, ticks = {}, {}
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
    return holdings, ticks


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
        self.status = '未启动'
        self.mode = 'swing'
        self.assets = []
        self.checks = {}
        self.checked_at = None
        self.scan_ms = None

    def stop_and_flatten(self):
        with self.control:
            if self.draining:
                return
            self.draining = True
            self.switch_blocked = True
            self.stop.set()
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

    def run(self, assets, mode):
        short_term = mode != 'swing'
        max_positions = self.FAST_MAX_POSITIONS if mode == 'fast' else self.MAX_POSITIONS
        max_hold_hours = {'intraday': 4, 'swing': 48, 'us_open': 1.5}.get(mode)
        holdings = {}
        entry_attempts = {}
        peaks = {}
        protection_errors = {}
        ticks = {}
        breakeven_costs = {}
        trailing_stops = {}
        take_reviewed = {}
        prepared_leverage = set()
        prepare_at_start = mode == 'fast'
        recovering = True
        last_scan_log = None
        try:
            record_runtime('收到启动请求', mode=mode, detail='、'.join(sorted(assets)))
            while not self.stop.is_set():
                if recovering:
                    self.status = '正在读取模拟账户持仓'
                pending = reconcile_demo_orders()
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
                if recovering:
                    demo_return_baseline(account.get('totalWalletBalance'))
                to_restore = {symbol: position for symbol, position in positions.items()
                              if symbol not in holdings or Decimal(position['positionAmt']) != holdings[symbol][0]}
                if to_restore:
                    restored, restored_ticks = restore_strategy_positions(to_restore, assets, mode)
                    holdings.update(restored)
                    ticks.update(restored_ticks)
                if recovering:
                    recovering = False
                    record_runtime('账户核对完成', mode=mode, detail=f'已接管 {len(holdings)} 个持仓')
                closed_symbols = holdings.keys() - positions.keys()
                for symbol in closed_symbols:
                    amount, opened, leverage, _, opening_id = holdings.pop(symbol)
                    peaks.pop(symbol, None)
                    ticks.pop(symbol, None)
                    protection_errors.pop(symbol, None)
                    breakeven_costs.pop(symbol, None)
                    trailing_stops.pop(symbol, None)
                    take_reviewed.pop(symbol, None)
                    self.checks = {**self.checks, symbol[:-4]: '已平仓 · 待下轮扫描'}
                    journal('demo_protection_position_closed', dict(symbol=symbol, opening_id=opening_id))
                unmatched = {symbol for symbol, position in positions.items()
                             if symbol not in holdings or Decimal(position['positionAmt']) != holdings[symbol][0]}
                if len(positions) != len(active) or unmatched - owned_pending:
                    details = '：' + '、'.join(sorted(unmatched)) if unmatched else ''
                    raise ValueError('账户持仓无法与所选策略的开仓成交核对' + details)
                self.checks = {**self.checks, **{symbol[:-4]: ('限价挂单等待成交' if symbol in resting_entries
                                                            else f'委托待核对 · {status}')
                               for symbol, status in pending_symbols.items() if symbol[:-4] in assets}}
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
                    entry_value = notional - direction*pnl
                    timed_out = (max_hold_hours is not None and opened > 0
                                 and demo_now_ms()/1000 - opened >= max_hold_hours * 3600)
                    price_return = pnl / entry_value if entry_value > 0 else Decimal(0)
                    peak = max(peaks.get(symbol, price_return), price_return)
                    peaks[symbol] = peak
                    fixed_exit = price_return <= -stop_loss or price_return >= 2 * stop_loss
                    entry_price = entry_value/abs(amount)
                    mark = notional/abs(amount)
                    tick = ticks[symbol]
                    minimum_stop = protection_price(entry_price, -stop_loss, tick, direction=direction, kind='stop')
                    stop = trailing_stops.get(symbol, minimum_stop)
                    if peak >= stop_loss and not (timed_out or fixed_exit):
                        candidate = protection_price(entry_price, peak-stop_loss, tick, direction=direction, kind='stop')
                        try:
                            costs = position_breakeven_costs(symbol, opening_id, amount, breakeven_costs)
                            breakeven = net_breakeven_price(entry_price, amount, tick, *costs)
                        except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
                            breakeven = None
                        if (breakeven is not None and direction*(candidate-breakeven) >= 0
                                and direction*(mark-candidate) > 0 and direction*(candidate-stop) > 0):
                            stop = candidate
                    trailing = stop != minimum_stop and direction*(mark-stop) <= 0
                    missing_stop = False
                    if not (timed_out or fixed_exit or trailing):
                        take = None
                        if stop != minimum_stop and price_return >= stop_loss:
                            window = demo_now_ms() // STRATEGY_INTERVALS[mode]
                            if take_reviewed.get(symbol) != window:
                                try:
                                    candle, signal, _, assessment = entry_signal(symbol, mode)
                                    take_reviewed[symbol] = min(window, (candle + STRATEGY_INTERVALS[mode]) // STRATEGY_INTERVALS[mode])
                                except (OSError, ValueError, KeyError, IndexError, ArithmeticError):
                                    pass
                                else:
                                    weakened = assessment == '均线趋势未确认' or (signal is not None and signal*direction < 0)
                                    target = price_return + stop_loss/4
                                    if weakened and candle + STRATEGY_INTERVALS[mode] > opened*1000 and target < 2*stop_loss:
                                        closer = protection_price(entry_price, target, tick, direction=direction, kind='take')
                                        if direction*(closer-mark) > 0:
                                            take = closer
                        try:
                            align_strategy_protection(symbol, entry_price, mark, stop_loss, stop, tick,
                                                      dict(opening_id=opening_id, opened=int(opened*1000),
                                                           leverage=leverage, strategy=mode), direction=direction, take=take)
                            if stop != minimum_stop:
                                trailing_stops[symbol] = stop
                            protection_errors.pop(symbol, None)
                            self.checks = {**self.checks, symbol[:-4]: '持仓监控中'}
                        except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error) as error:
                            protection_errors[symbol] = str(error)
                            self.checks = {**self.checks, symbol[:-4]: '持仓中 · 保护单待核对'}
                            try:
                                missing_stop = not confirmed_strategy_stop(symbol, minimum_stop, direction=direction)
                            except (OSError, ValueError, KeyError, ArithmeticError):
                                missing_stop = True
                    if timed_out or fixed_exit or trailing or missing_stop:
                        if symbol in pending_entries:
                            continue
                        self.status = f'正在平仓 {symbol}'
                        if timed_out:
                            reason, threshold = '达到最长持仓时间', f'{max_hold_hours:g} 小时'
                        elif price_return <= -stop_loss:
                            reason, threshold = '达到止损线', f'{-stop_loss:.3%}'
                        elif price_return >= 2*stop_loss:
                            reason, threshold = '达到止盈线', f'{2*stop_loss:.3%}'
                        elif trailing:
                            reason, threshold = '触发移动止盈', f'移动止损价 {stop:f}'
                        else:
                            reason, threshold = '无法确认有效止损，保护性退出', protection_errors.get(symbol, '')
                        decision = dict(reason=reason, detail=f'判断时浮动收益率 {price_return:.3%} · {threshold}')
                        order = close_strategy_position(symbol, amount, opened, leverage, opening_id, mode, decision)
                        if order and order['status'] in DEMO_ORDER_PENDING:
                            pending_symbols[symbol] = order['status']
                            self.checks = {**self.checks, symbol[:-4]: f'平仓委托待核对 · {order["status"]}'}
                            continue
                        del holdings[symbol]
                        del peaks[symbol]
                        ticks.pop(symbol, None)
                        protection_errors.pop(symbol, None)
                        breakeven_costs.pop(symbol, None)
                        trailing_stops.pop(symbol, None)
                        take_reviewed.pop(symbol, None)
                        self.checks = {**self.checks, symbol[:-4]: f'已平仓 · {reason}'}
                        closed = True
                if (not closed and not closed_symbols and not self.stop.is_set()
                        and (prepare_at_start or len(holdings) < max_positions)):
                    self.status = '正在核对模拟盘普通挂单'
                    open_orders = demo_signed('/fapi/v1/openOrders')
                    occupied = positions.keys() | pending_entries | {order['symbol'] for order in open_orders
                                                                     if not order.get('reduceOnly')}
                    for order in open_orders:
                        pending_symbols.setdefault(order['symbol'], order.get('status') or '交易所有挂单')
                    catalog = request(DEMO_VENUE, '/fapi/v1/exchangeInfo')['symbols']
                    started = time.monotonic()
                    self.status = '正在检查行情与入场信号'
                    candidates = []
                    listed = {item['symbol']: item for item in catalog}
                    checks = {}
                    with ThreadPoolExecutor(max_workers=max(1, len(assets))) as pool:
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
                        for asset in sorted(assets):
                            symbol = asset + 'USDT'
                            item = listed.get(symbol)
                            if symbol in pending_symbols:
                                checks[asset] = ('限价挂单等待成交' if symbol in resting_entries
                                                 else f'委托待核对 · {pending_symbols[symbol]}')
                            elif symbol in holdings:
                                checks[asset] = ('持仓中 · 保护单待核对' if symbol in protection_errors else '持仓监控中')
                            elif not item:
                                checks[asset] = '模拟盘目录未匹配'
                            elif (item['status'] != 'TRADING' or item['contractType'] not in
                                  {'PERPETUAL', 'TRADIFI_PERPETUAL'}):
                                checks[asset] = '模拟盘合约当前不可交易'
                            else:
                                checks[asset] = '正在读取行情'
                                jobs[pool.submit(scan_entry_signal, symbol, mode)] = (asset, item)
                        self.checks = checks.copy()
                        for job in as_completed(jobs):
                            asset, item = jobs[job]
                            symbol = item['symbol']
                            try:
                                candle, score, volatility, reason, signal_context = job.result()
                                if score is not None:
                                    candidates.append((signal_context['priority'], symbol, candle, volatility,
                                                       reason, item, score, signal_context))
                                    reason = f'信号成立 · {reason} · 等待下单检查'
                            except (OSError, ValueError, KeyError, IndexError, TypeError, ArithmeticError) as error:
                                reason = f'行情不可用 · {error}'
                            checks[asset] = reason
                            self.checks = checks.copy()
                    self.checked_at = demo_now_ms()
                    self.scan_ms = round((time.monotonic()-started)*1000)
                    if last_scan_log is None or self.checked_at - last_scan_log >= 60000:
                        unavailable = sum(label.startswith('行情不可用') for label in checks.values())
                        record_runtime('行情扫描完成', mode=mode, at=self.checked_at,
                                       detail=f'读取 {len(jobs)} 个标的 · 行情不可用 {unavailable} 个 · 耗时 {self.scan_ms} ms')
                        last_scan_log = self.checked_at
                    free_margin = Decimal(account['availableBalance'])
                    for _, symbol, candle, volatility, reason, item, score, signal_context in sorted(
                            candidates, key=lambda candidate: candidate[0], reverse=True):
                        if self.stop.is_set() or len(occupied) >= max_positions:
                            break
                        filters = {f['filterType']: f for f in item['filters']}
                        lot = filters['LOT_SIZE']
                        direction = 1 if score > 0 else -1
                        try:
                            funding_window = (max_hold_hours or 1) * HOUR
                            fee, funding_cost = demo_entry_cost(symbol, direction, funding_window)
                            book, bid, ask = demo_entry_book(symbol, item['contractType'])
                            book_time = int(book['time'])
                            midpoint = (bid + ask) / 2
                            spread = (ask - bid) / midpoint
                            cost = spread + 2 * fee + funding_cost
                            if direction * (midpoint / signal_context['close'] - 1) > volatility / 2:
                                raise ValueError('盘口已远离信号收盘价，避免追价')
                            if direction * (midpoint / signal_context['boundary'] - 1) < -volatility / 4:
                                raise ValueError('价格已反向穿过触发位')
                        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
                            checks[symbol[:-4]] = f'跳过 · 价格核验未通过：{error}'
                            self.checks = checks.copy()
                            continue
                        step = Decimal(lot['stepSize'])
                        equity = Decimal(account['totalMarginBalance'])
                        stop_loss = entry_stop_fraction(symbol[:-4], mode, volatility)
                        if cost > stop_loss * Decimal('.75'):
                            checks[symbol[:-4]] = (f'跳过 · 点差与交易成本过高：预估 {cost:.3%}'
                                                   f' · 上限 {stop_loss * Decimal(".75"):.3%}')
                            self.checks = checks.copy()
                            continue
                        leverage_cap = 3 if short_term else 5
                        leverage_target = (self.FAST_LEVERAGE if mode == 'fast'
                                           else min(leverage_cap, max(1, int(Decimal('.05') / stop_loss))))
                        margin_budget = free_margin * Decimal('.95') / (max_positions - len(occupied))
                        budget = max(Decimal(0), min((equity if short_term else 3 * equity), leverage_target * margin_budget))
                        risk_budget = equity * ENTRY_EQUITY_RISK[mode] / (stop_loss + cost)
                        price, amount, time_in_force = select_demo_entry(
                            direction, book, bid, ask, min(budget, risk_budget), step, stop_loss, cost)
                        attempt = (candle, direction, demo_now_ms(), price, time_in_force, cost)
                        retry_block = entry_retry_block(entry_attempts.get(symbol), attempt,
                                                        ENTRY_RETRY_COOLDOWN_SECONDS[mode])
                        if retry_block:
                            checks[symbol[:-4]] = f'等待复查 · {retry_block}'
                            self.checks = checks.copy()
                            continue
                        notional = amount * price
                        minimum = Decimal(filters.get('MIN_NOTIONAL', {}).get('notional', '0'))
                        if not Decimal(lot['minQty']) <= amount <= Decimal(lot['maxQty']) or notional < minimum or not amount:
                            checks[symbol[:-4]] = '跳过 · 数量或名义金额不符合合约要求'
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
                        if self.stop.is_set():
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
                        if time_in_force == 'GTX':
                            maker_reason = '吃单成本偏高' if cost > stop_loss / 2 else '对手盘一档数量不足'
                            detail += f' · {maker_reason}，改为限价挂单'
                        decision = dict(reason=reason,
                                        detail=detail, signal_at=signal_at)
                        entry_attempts[symbol] = (candle, direction, now, price, time_in_force, cost)
                        order = submit_demo_limit_entry(symbol, amount, price, leverage_target, mode,
                                                        stop_loss, tick, self.stop, decision,
                                                        side=entry_side(direction), time_in_force=time_in_force)
                        if order['status'] in DEMO_ORDER_PENDING:
                            pending_symbols[symbol] = order['status']
                            occupied.add(symbol)
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
                        entry = Decimal(0)
                        try:
                            entry = Decimal(order.get('avgPrice') or '0')
                            if entry <= 0:
                                entry = Decimal(order.get('cumQuote') or '0') / Decimal(order['executedQty'])
                            if entry <= 0:
                                entry = opening_fill_price(symbol, order, direction=direction)
                            set_initial_protection(symbol, entry, stop_loss, tick,
                                                   dict(opening_id=order.get('orderId'), opened=order.get('updateTime'),
                                                        leverage=leverage_target, strategy=mode), direction=direction)
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
                                self.status = f'{symbol} 未确认止损，正在提交只减仓平仓'
                                journal('demo_protection_emergency_exit', dict(symbol=symbol, error=str(error)))
                                filled_amount, opened, lev, _, opening_id = holdings[symbol]
                                close = close_strategy_position(symbol, filled_amount, opened, lev, opening_id, mode,
                                                                dict(reason='开仓后止损未确认，保护性退出', detail=str(error)))
                                if close and close['status'] in DEMO_ORDER_PENDING:
                                    pending_symbols[symbol] = close['status']
                                    checks[symbol[:-4]] = f'平仓委托待核对 · {close["status"]}'
                                else:
                                    del holdings[symbol]
                                    ticks.pop(symbol, None)
                                    protection_errors.pop(symbol, None)
                                    checks[symbol[:-4]] = '未确认止损 · 已核对仓位关闭'
                        for _, other_symbol, *_ in candidates:
                            other_asset = other_symbol[:-4]
                            if checks[other_asset].endswith('等待下单检查'):
                                checks[other_asset] = '信号成立 · 等待下一轮仓位检查'
                        self.checks = checks.copy()
                        break
                self.status = '监控中 · ' + (f'持仓 {len(holdings)}/{max_positions}' if holdings else '等待入场信号')
                if pending_symbols:
                    self.status += f' · {len(pending_symbols)} 个标的有待处理委托'
                if protection_errors:
                    self.status += f' · {len(protection_errors)} 个持仓的保护单待核对'
                if pending_partial_entry:
                    continue
                self.stop.wait(3 if pending_symbols else 10 if mode == 'fast' else 30)
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
            self.lock.release()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if (self.headers.get('Origin') != 'http://127.0.0.1:8765'
                or self.headers.get('Host') != '127.0.0.1:8765'
                or self.headers.get('X-Quant-Demo') != 'strategy'):
            self.send(403, 'text/plain', b'Forbidden')
            return
        parsed = urlsplit(self.path)
        strategy = self.server.strategy
        if parsed.path == '/strategy/protection':
            params = parse_qs(parsed.query)
            try:
                replace_protection(params['symbol'][0], params['kind'][0], params['price'][0])
            except (OSError, ValueError, KeyError, ArithmeticError, StopIteration, sqlite3.Error) as error:
                self.send(502, 'application/json', json.dumps({'error': str(error)}).encode())
                return
        elif parsed.path == '/strategy/stop':
            strategy.stop_and_flatten()
        elif parsed.path == '/strategy/start':
            params = parse_qs(parsed.query)
            assets = set(params.get('asset', ())) & set(ASSETS)
            mode = params.get('strategy', ['swing'])[0]
            if mode not in STRATEGY_NAMES:
                self.send(400, 'application/json', '{"error":"未知策略"}'.encode())
                return
            if mode == 'us_open':
                assets &= US_EQUITY_ASSETS
            with strategy.control:
                if not assets or strategy.draining or strategy.switch_blocked or not strategy.lock.acquire(blocking=False):
                    self.send(409, 'application/json', '{"error":"请选择标的，并等当前策略停止且币安确认空仓、无挂单"}'.encode())
                    return
                strategy.stop.clear()
                strategy.mode = mode
                strategy.assets = sorted(assets)
                strategy.checks = {}
                strategy.checked_at = None
                strategy.scan_ms = None
                strategy.status = '正在核对模拟账户'
                threading.Thread(target=strategy.run, args=(assets, mode), daemon=True).start()
        else:
            self.send(404, 'text/plain', b'Not found')
            return
        self.send(200, 'application/json', b'{}')

    def do_GET(self):
        parsed = urlsplit(self.path)
        try:
            if parsed.path == '/':
                self.send(200, 'text/html', Path(__file__).with_name('index.html').read_bytes())
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
            if parsed.path == '/strategy':
                self.send(200, 'application/json', json.dumps(dict(status=self.server.strategy.status, mode=self.server.strategy.mode,
                                                                    running=self.server.strategy.lock.locked() or self.server.strategy.draining,
                                                                    draining=self.server.strategy.draining, assets=self.server.strategy.assets,
                                                                    switch_blocked=self.server.strategy.switch_blocked,
                                                                    checks=self.server.strategy.checks,
                                                                    checked_at=self.server.strategy.checked_at,
                                                                    scan_ms=self.server.strategy.scan_ms)).encode())
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
                page = int(parse_qs(parsed.query).get('page', ['1'])[0])
                self.send(200, 'application/json', json.dumps(demo_history(page)).encode())
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
    for order in recent_orders(limit=None):
        if not order.get('reduce_only') or order.get('pnl') is None:
            continue
        value = Decimal(order['pnl'])
        if has_order_fill(order):
            symbol = order['symbol']
            by_symbol[symbol] = by_symbol.get(symbol, Decimal(0)) + value
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
                net=str(pnl) if baseline is not None else None, symbols=symbols)


def journal(kind, payload, *, timeout=5):
    with closing(sqlite3.connect(journal_path(), timeout=timeout)) as database, database:
        ensure_journal_events(database)
        database.execute('INSERT INTO events (created_at, kind, payload) VALUES (?, ?, ?)',
                         (int(time.time()*1000), kind, json.dumps(payload, ensure_ascii=False)))


def record_runtime(message, *, mode=None, detail=None, at=None):
    event = dict(at=at if at is not None else demo_now_ms(), message=message,
                 mode=mode, detail=detail, scope='system')
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


def recent_orders(limit=50):
    path = journal_path()
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as database:
        rows = database.execute(
            "SELECT created_at, kind, payload FROM events WHERE kind LIKE 'demo_order_%' ORDER BY id DESC"
        )
        orders = {}
        for timestamp, kind, raw in rows:
            payload = json.loads(raw)
            intent = payload.get('intent', payload)
            key = intent['clientOrderId']
            if key in orders:
                continue
            response = payload.get('response', {})
            executed_at = response.get('updateTime') if (response.get('status') == 'FILLED' or
                (strategy_limit_entry(intent) and Decimal(response.get('executedQty') or '0') > 0)) else None
            is_close = intent.get('reduce_only')
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
            if limit is not None and len(orders) == limit:
                break
        return list(orders.values())


def demo_history(page):
    page_size = 9
    orders = [order for order in recent_orders(limit=None)
              if order.get('reduce_only') and order['status'] == 'FILLED']
    orders.sort(key=lambda order: int(order['closed'] or order['time']), reverse=True)
    pages = max(1, (len(orders) + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    start = (page - 1) * page_size
    return dict(page=page, pages=pages, orders=orders[start:start + page_size])


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


def reconcile_demo_orders():
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
        if response['status'] in DEMO_ORDER_ACTIVE and strategy_limit_entry(order):
            expires_at = order.get('entry_expires_at')
            reason = None
            if Decimal(response['executedQty']) > 0:
                reason = '委托已部分成交，程序撤销剩余数量并管理已成交仓位'
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
            cancel_reason = '用户停止策略并清仓，程序撤销剩余委托'
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
    if len(sys.argv) > 1:
        if len(sys.argv) != 5 or sys.argv[1] != '--demo-order':
            sys.exit('用法：启动.command --demo-order 合约代码 BUY或SELL 数量')
        try:
            print(json.dumps(submit_demo_order(*sys.argv[2:]), ensure_ascii=False, indent=2))
        except ValueError as error:
            sys.exit(str(error))
        sys.exit(0)
    server = ThreadingHTTPServer(('127.0.0.1',8765),Handler)
    server.strategy = DemoStrategy()
    if os.getenv('BINANCE_DEMO_API_KEY') and os.getenv('BINANCE_DEMO_API_SECRET'):
        threading.Thread(target=protection_watch, daemon=True).start()
    print('http://127.0.0.1:8765', flush=True)
    webbrowser.open('http://127.0.0.1:8765')
    server.serve_forever()
