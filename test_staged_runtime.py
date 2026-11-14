import runpy
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

scope = runpy.run_path(str(Path(__file__).with_name('app.py')))
g = scope['restore_strategy_positions'].__globals__
symbol = 'MUUSDT'
mode = 'mu_relative_strength'

with TemporaryDirectory() as tmp:
    g['journal_path'] = lambda: Path(tmp)/'trades.sqlite'
    root = dict(clientOrderId='root', symbol=symbol, side='BUY', time_in_force='GTX',
                stop_loss='.015', price_tick='.01', leverage=5, strategy=mode, quantity='1',
                decision=dict(staged=dict(base_stop='.01', budget='302', step='.01',
                                          min_qty='.01', max_qty='1000', min_notional='5',
                                          contract_type='TRADIFI_PERPETUAL', funding_cost='0')))

    def response(intent, oid, qty, status, quote='0', stamp=1000):
        g['journal']('demo_order_response', dict(intent=intent, response=dict(orderId=oid,
            executedQty=qty, status=status, updateTime=stamp, cumQuote=quote)))

    response(root, 10, '1', 'FILLED', '100')
    g['position_opened_at'] = lambda p: (1000, 10)
    p = dict(symbol=symbol, positionAmt='1', positionSide='BOTH', entryPrice='100')
    account = dict(positions=[p], totalMarginBalance='10000', availableBalance='9000')
    g['demo_signed'] = lambda path, **k: account if path.endswith('/account') else []
    g['demo_now_ms'] = lambda: 6000
    g['confirmed_strategy_stop'] = lambda *a, **kw: True
    g['demo_entry_book'] = lambda *a: (dict(time=6000, askQty='2', bidQty='2'), D('99.49'), D('99.50'))
    g['request'] = lambda *a, **k: dict(symbol=symbol, time=6000, markPrice='99.50')
    g['demo_taker_fee_rate'] = lambda *a: D('.0005')
    submit = Mock(return_value=dict(status='EXPIRED', executedQty='1.2'))
    g['submit_demo_order'] = submit
    restore = g['restore_strategy_positions']
    manage = g['manage_staged_position']
    def setup():
        return restore({symbol:p}, {'MU'}, mode)[2][symbol]
    def execute(state, mark, allow=True):
        return manage(symbol, D(p['positionAmt']), 1, 10, D('.01'), D(mark), D(p['entryPrice']),
                      state, mode, allow_add=allow)

    first = setup()
    assert first['hard_stop'] == D('98.50')
    assert not execute(first, '99.50', False)
    assert not execute(first, '99.90')
    assert execute(first, '99.50')
    args, k = submit.call_args
    assert D(args[2]) == 2 and not k['reduce_only'] and k['ioc_price'] == D('99.50')
    add = dict(root, clientOrderId='add', opening_id=10, decision=dict(stage='add'))
    response(add, 11, '1.2', 'EXPIRED', '119.4', 2000)
    p.update(positionAmt='2.2', entryPrice='99.72727273')
    expanded = setup()
    assert expanded['trim_remaining'] == D('1.2') and expanded['hard_stop'] == first['hard_stop']
    assert not execute(expanded, '99.50')
    g['demo_entry_book'] = lambda *a: (dict(time=6000, askQty='2', bidQty='2'), D('100.49'), D('100.50'))
    g['request'] = lambda *a, **k: dict(symbol=symbol, time=6000, markPrice='100.50')
    g['position_breakeven_costs'] = lambda *a, **k: (D('.1'), D('.0005'), D('0'))
    assert execute(expanded, '100.50', False)
    args, k = submit.call_args
    assert D(args[2]) == D('1.2') and k['reduce_only'] and k['decision']['stage'] == 'trim'
    trim = dict(root, clientOrderId='trim', side='SELL', reduce_only=True,
                opening_id=10, decision=dict(stage='trim'))
    response(trim, 12, '.5', 'EXPIRED', '50.245', 3000)
    p.update(positionAmt='1.7')
    partial = setup()
    assert partial['trim_remaining'] == D('.7')
    execute(partial, '100.50')
    args, _ = submit.call_args
    assert D(args[2]) == D('.7')

    new_plan = dict(root['decision']['staged'], budget='200', combined_budget='300', trim_total_third=True)
    new_root = dict(root, decision=dict(staged=new_plan), filled='100', executed_qty='1')
    new_add = dict(add, executed_qty='1.2')
    new_trim = dict(trim, executed_qty='.5')
    new_setup = g['staged_position_setup']
    p.update(positionAmt='1', entryPrice='100')
    g['demo_entry_book'] = lambda *a: (dict(time=6000, askQty='2', bidQty='2'), D('99.49'), D('99.50'))
    g['request'] = lambda *a, **k: dict(symbol=symbol, time=6000, markPrice='99.50')
    assert execute(new_setup(new_root, [], D('.01'), D(1)), '99.50')
    args, _ = submit.call_args
    assert D(args[2]) == 2  # The add can exceed the unused half of the nominal plan.
    account['availableBalance'] = '20'
    assert execute(new_setup(new_root, [], D('.01'), D(1)), '99.50')
    args, _ = submit.call_args
    assert D(args[2]) == D('.95')  # Available margin still caps the add.
    account['availableBalance'] = '9000'

    p.update(positionAmt='2.2', entryPrice='99.72727273')
    g['demo_entry_book'] = lambda *a: (dict(time=6000, askQty='2', bidQty='2'), D('100.49'), D('100.50'))
    g['request'] = lambda *a, **k: dict(symbol=symbol, time=6000, markPrice='100.50')
    assert execute(new_setup(new_root, [new_add], D('.01'), D('2.2')), '100.50', False)
    args, k = submit.call_args
    assert D(args[2]) == D('.73') and k['reduce_only']
    assert new_setup(new_root, [new_add, new_trim], D('.01'), D('1.7'))['trim_remaining'] == D('.23')
    assert new_setup(new_root, [new_add, dict(new_trim, executed_qty='.73')], D('.01'),
                     D('1.47'))['trim_remaining'] == 0
    assert new_setup(new_root, [dict(new_add, executed_qty='.2')], D('.01'),
                     D('1.2'))['trim_remaining'] == D('.4')
    assert new_setup(new_root, [dict(new_add, executed_qty='0')], D('.01'), D(1))['trim_remaining'] == 0
    p.update(positionAmt='1.7')

    def fill(oid, amount, side, price, fee, realized, stamp):
        return dict(orderId=oid, id=oid, qty=amount, side=side, time=stamp, price=price,
                    commissionAsset='USDT', commission=fee, realizedPnl=realized)
    fills = [
       fill(10,'1','BUY','100','.05','0',1000),
       fill(11,'1.2','BUY','99.5','.06','0',2000),
       fill(12,'.5','SELL','100.49','.03','.381363635',3000),
       fill(13,'1.7','SELL','99','.08','-1.236363641',5500),
    ]
    def signed(path, **k):
        if path.endswith('income'):
            return [dict(asset='USDT', income='-.22', time=2500),
                    dict(asset='USDT', income='.11', time=1500)]
        if 'orderId' in k and path.endswith('order'):
            return dict(status='FILLED', executedQty='1.7')
        return [f for f in fills if 'orderId' not in k or str(f['orderId']) == str(k['orderId'])]
    g['demo_signed'] = signed
    g['demo_return_baseline'] = lambda: None
    order, add_order, trim_order = (next(o for o in g['recent_orders']() if o['clientOrderId'] == name)
                                    for name in ('root','add','trim'))
    assert g['sync_external_demo_close'](order, {(symbol,'12'):trim_order}, 6000, [add_order])
    closes = {o['exchange_order_id']:o for o in g['recent_orders']() if o.get('pnl') is not None}
    assert len(closes) == 2 and closes[12]['decision']['stage'] == 'trim'
    assert sum(D(o['fees']) for o in closes.values()) == D('.22')
    assert sum(D(o['funding']) for o in closes.values()) == D('-.11')
    assert setup()['trim_remaining'] == D('.7')
    assert g['sync_external_demo_close'](order, {(symbol,str(key)):o for key,o in closes.items()}, 6000, [add_order])
    assert len([o for o in g['recent_orders']() if o.get('pnl') is not None]) == 2
print('Passed: staging off, IOC sizing, restart ownership and hard stop, partial trim and idempotent fees/funding')
