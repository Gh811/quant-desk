const el = id => document.getElementById(id);
const accountMode = el('account-mode');
const demoSync = el('demo-sync');
const accountFields = [...document.querySelectorAll('[data-account]')];
let accountSnapshot;
let accountSyncError;
let realizedReturnSnapshot;
const oneDecimal = new Intl.NumberFormat('zh-CN', {minimumFractionDigits:1, maximumFractionDigits:1});
function colorPnL(cell, value) {
  cell.classList.toggle('gain', Number(value) > 0);
  cell.classList.toggle('loss', Number(value) < 0);
}
const pnlAmount = new Intl.NumberFormat('zh-CN', {maximumFractionDigits:4, signDisplay:'exceptZero'});
const pnlPeriod = el('pnl-period');
const pnlDate = el('pnl-date');
const dayMs = 86400000;
pnlDate.value = new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
pnlPeriod.addEventListener('change', showSymbolPnL);
pnlDate.addEventListener('change', showSymbolPnL);
function selectedSymbolPnL(snapshot) {
  if (pnlPeriod.value === 'all') return snapshot?.symbols;
  if (!snapshot?.symbol_days || !pnlDate.value) return null;
  const anchor = new Date(pnlDate.value + 'T00:00:00Z');
  let first = anchor.getTime() / dayMs;
  let last = first + 1;
  if (pnlPeriod.value === 'week') {
    first -= (anchor.getUTCDay() + 6) % 7;
    last = first + 7;
  } else if (pnlPeriod.value === 'month') {
    first = Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth(), 1) / dayMs;
    last = Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth() + 1, 1) / dayMs;
  }
  const totals = new Map();
  for (const [day, symbols] of Object.entries(snapshot.symbol_days)) {
    if (Number(day) < first || Number(day) >= last) continue;
    for (const [symbol, net] of Object.entries(symbols)) {
      totals.set(symbol, (totals.get(symbol) ?? 0) + Number(net));
    }
  }
  return Array.from(totals, ([symbol, net]) => ({symbol, net}))
    .sort((a, b) => b.net - a.net || a.symbol.localeCompare(b.symbol));
}
function showSymbolPnL() {
  const panel = el('pnl-analysis');
  panel.hidden = accountMode.value !== 'demo';
  pnlDate.hidden = el('pnl-date-label').hidden = pnlPeriod.value === 'all';
  const rows = selectedSymbolPnL(realizedReturnSnapshot);
  const total = el('pnl-period-total');
  const net = rows?.reduce((sum, row) => sum + Number(row.net), 0);
  total.textContent = net == null ? '' : '净盈亏 ' + pnlAmount.format(net) + ' USDT';
  colorPnL(total, net);
  const chart = el('pnl-chart');
  const status = el('pnl-chart-status');
  status.textContent = rows?.length ? '' : rows ? '所选周期暂无已核对的平仓记录' : '暂无所选周期数据';
  status.hidden = Boolean(rows?.length);
  const maxAbs = Math.max(0, ...(rows || []).map(row => Math.abs(Number(row.net))));
  chart.replaceChildren(...(rows || []).map(({symbol, net}) => {
    const value = Number(net);
    const item = document.createElement('li');
    const amount = orderElement('strong', '', pnlAmount.format(value));
    amount.title = pnlAmount.format(value) + ' USDT';
    colorPnL(amount, value);
    const positive = orderElement('span', 'pnl-bar-track');
    const negative = orderElement('span', 'pnl-bar-track negative');
    positive.setAttribute('aria-hidden', 'true');
    negative.setAttribute('aria-hidden', 'true');
    if (value !== 0) {
      const bar = orderElement('span', 'pnl-bar');
      bar.style.height = Math.abs(value) / maxAbs * 100 + '%';
      (value > 0 ? positive : negative).append(bar);
    }
    item.append(amount, positive, negative, orderElement('span', '', symbol));
    return item;
  }));
}
function showRealizedReturn() {
  showSymbolPnL();
  const data = accountMode.value === 'demo' ? realizedReturnSnapshot : null;
  const net = el('realized-pnl');
  net.textContent = data?.net == null ? '—' : (Number(data.net) > 0 ? '+' : '') + oneDecimal.format(data.net) + ' USDT';
  colorPnL(net, data?.net);
  const field = el('realized-return');
  field.textContent = data?.rate == null ? '—' : (data.rate*100).toFixed(2) + '%';
  colorPnL(field, data?.rate);
  const basis = el('realized-base');
  basis.textContent = data?.principal == null ? '' : '起始本金 ' + oneDecimal.format(data.principal) + ' USD 等值';
  basis.title = data?.started_at ? '起算时间 ' + date(data.started_at) : '';
}
function showAccount() {
  const demo = accountMode.value === 'demo';
  const snapshot = demo && accountSnapshot;
  demoSync.hidden = !demo;
  for (const field of accountFields) {
    const key = field.dataset.account;
    const unit = key === 'count' ? '' : key === 'latency' ? ' ms' : ' USD 等值';
    const value = snapshot && snapshot[key];
    field.textContent = snapshot ? (key === 'count' || key === 'latency' ? value : oneDecimal.format(value)) + unit : '—';
    if (key === 'pnl') colorPnL(field, value);
  }
  showRealizedReturn();
  renderOrderCards();
  renderMarketRows();
  updateAccountStatus();
}
function updateAccountStatus() {
  const status = el('account-status');
  if (accountMode.value !== 'demo') {
    status.dataset.tone = '';
    status.textContent = '实盘未连接';
    return;
  }
  const age = accountSnapshot ? Math.floor((Date.now()-accountSnapshot.syncedAt)/1000) : null;
  const delayed = age !== null && age >= 15;
  status.dataset.tone = accountSyncError || delayed ? 'error' : age === null ? '' : 'live';
  status.textContent = (age === null
    ? (demoSync.disabled ? '正在连接币安模拟账户' : '币安模拟账户未连接')
    : '币安模拟盘 · ' + (delayed ? '更新延迟 · ' : '') + age + ' 秒前同步')
    + (accountSyncError ? ' · ' + accountSyncError : '');
}
accountMode.addEventListener('change', () => {
  showAccount();
  if (accountMode.value === 'demo' && !demoSync.disabled) demoSync.click();
});
demoSync.onclick = async () => {
  if (demoSync.disabled || accountMode.value !== 'demo') return;
  demoSync.disabled = true;
  updateAccountStatus();
  try {
    const response = await fetch('./account/binance-demo', {cache:'no-store', signal:AbortSignal.timeout(18000)});
    const data = await response.json();
    if (!response.ok) throw Error(data.error);
    accountSnapshot = {...data, syncedAt: Date.now()};
    accountSyncError = null;
    showAccount();
  } catch (error) {
    accountSyncError = '同步失败：' + error.message;
  } finally {
    demoSync.disabled = false;
    updateAccountStatus();
  }
};
const name = asset => ({XAU:'黄金', SKHYNIX:'SK 海力士', SAMSUNG:'三星电子', GOOGL:'Google（GOOGL）', INTC:'英特尔（INTC）'})[asset] || asset;
const defaultAssets = 'ETH BTC XAU SNDK SOXL KORU MU NVDA SOL SKHYNIX SAMSUNG QQQ GOOGL SPCX META TSLA AMZN BNB ZEC INTC'.split(' ');
function addAssetCheckbox(asset, checked) {
  const choice = document.createElement('div');
  choice.className = 'asset-choice';
  const label = document.createElement('label');
  const checkbox = Object.assign(document.createElement('input'), {type:'checkbox', name:'asset', value:asset, checked, disabled:true});
  const assetName = orderElement('span', '', name(asset));
  assetName.title = name(asset);
  label.append(checkbox, assetName);
  const remove = orderElement('button', 'watch-remove');
  Object.assign(remove, {type:'button', textContent:'移出', disabled:true});
  remove.setAttribute('aria-label', '移出 ' + name(asset));
  remove.addEventListener('click', () => setAssetWatch(asset, false));
  choice.append(label, remove);
  el('assets').append(choice);
}
function row(table, values) {
  const item = el(table).insertRow();
  values.forEach(value => { item.insertCell().textContent = value; });
  return item;
}
const date = timestamp => timestamp ? new Date(Number(timestamp)).toLocaleString() : '—';
function readSaved(key) {
  try { return JSON.parse(localStorage.getItem('quant-desk:' + key)); }
  catch { return null; }
}
function writeSaved(key, value) {
  try { localStorage.setItem('quant-desk:' + key, JSON.stringify(value)); }
  catch { el('strategy-status').textContent = '标的选择保存失败'; }
}
const config = el('config');
const preferences = readSaved('config');
const poolWatch = new Set(Array.isArray(preferences?.poolWatch)
  ? preferences.poolWatch.filter(asset => typeof asset === 'string' && /^[A-Z0-9]{1,24}$/.test(asset)) : []);
const removedAssets = new Set(Array.isArray(preferences?.removedAssets) ? preferences.removedAssets : []);
for (const asset of defaultAssets) {
  if (!removedAssets.has(asset)) addAssetCheckbox(asset, !['SAMSUNG','XAU','BNB','ZEC','INTC'].includes(asset));
}
const strategyMode = el('strategy-mode');
const strategySizing = el('strategy-sizing');
const strategyNotional = el('strategy-notional');
strategySizing.value = preferences?.sizing === 'fixed' ? 'fixed' : 'capital';
strategyNotional.value = preferences?.notional ?? '';
const strategyNames = Object.fromEntries([...strategyMode.options].map(option => [option.value, option.textContent]));
if (Object.hasOwn(strategyNames, preferences?.strategy)) strategyMode.value = preferences.strategy;
if (Array.isArray(preferences?.assets)) {
  for (const checkbox of config.querySelectorAll('[name=asset]')) {
    checkbox.checked = preferences.assets.includes(checkbox.value);
  }
}
function savePreferences() {
  writeSaved('config', {assets:[...config.querySelectorAll('[name=asset]:checked')].map(input=>input.value),
    poolWatch:[...poolWatch], removedAssets:[...removedAssets], strategy:strategyMode.value,
    sizing:strategySizing.value, notional:strategyNotional.value});
}
const usStrategyAssets = new Set('SNDK SOXL KORU MU NVDA QQQ GOOGL SPCX META TSLA AMZN INTC'.split(' '));
if (strategyMode.value === 'us_open') {
  for (const checkbox of config.querySelectorAll('[name=asset]')) {
    if (!usStrategyAssets.has(checkbox.value)) checkbox.checked = false;
  }
}
config.addEventListener('change', savePreferences);
strategyMode.addEventListener('change', () => {
  if (strategyMode.value === 'us_open') {
    for (const checkbox of config.querySelectorAll('[name=asset]')) {
      checkbox.checked = marketByAsset.get(checkbox.value)?.demo_tradeable &&
        marketByAsset.get(checkbox.value)?.equity === 'us';
    }
    renderMarketRows();
  }
  renderStrategyControls();
  savePreferences();
});
strategySizing.addEventListener('change', () => {
  savePreferences();
  renderStrategyControls();
});
strategyNotional.addEventListener('input', savePreferences);
function orderElement(tag, className, text) {
  return Object.assign(document.createElement(tag), {className, textContent:text || ''});
}
const pageSize = 9;
el('pool-scroll').style.setProperty('--pool-page-size', pageSize);
function pageItems(items, navigationId, render) {
  const navigation = el(navigationId);
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  const page = Math.min(Number(navigation.dataset.page || 1), pageCount);
  renderPagination(navigationId, page, pageCount, number => {
    navigation.dataset.page = number;
    render();
  });
  const start = (page - 1) * pageSize;
  return items.slice(start, start + pageSize);
}
function renderPagination(navigationId, page, pageCount, selectPage) {
  const navigation = el(navigationId);
  navigation.dataset.page = page;
  navigation.hidden = pageCount === 1;

  let numbers = [];
  if (pageCount > 1 && pageCount <= 9) {
    numbers = Array.from({length:pageCount}, (_, index) => index + 1);
  } else if (pageCount > 9) {
    const start = Math.max(2, Math.min(page - 2, pageCount - 5));
    numbers = [1, ...Array.from({length:5}, (_, index) => start + index), pageCount];
  }
  const pagesKey = numbers.join(',');
  if (navigation.dataset.pages !== pagesKey) {
    const focused = navigation.contains(document.activeElement);
    const buttons = [];
    let previousNumber = 0;
    for (const number of numbers) {
      if (number - previousNumber > 1) {
        const gap = orderElement('span', '', '…');
        gap.setAttribute('aria-hidden', 'true');
        buttons.push(gap);
      }
      const button = orderElement('button', '', String(number));
      button.type = 'button';
      button.dataset.page = number;
      button.setAttribute('aria-label', '第 ' + number + ' 页');
      button.onclick = () => selectPage(number);
      buttons.push(button);
      previousNumber = number;
    }
    navigation.replaceChildren(...buttons);
    navigation.dataset.pages = pagesKey;
    if (focused) navigation.querySelector('button[data-page="' + page + '"]')?.focus({preventScroll:true});
  }
  const current = navigation.querySelector('button[data-page="' + page + '"]');
  const previous = navigation.querySelector('[aria-current]');
  if (previous !== current) {
    previous?.removeAttribute('aria-current');
    current?.setAttribute('aria-current', 'page');
  }
}
function tradePrice(value) {
  if (value == null || value === '' || !Number.isFinite(Number(value))) return '—';
  const magnitude = Math.abs(Number(value));
  const digits = magnitude >= 1 ? 2 : magnitude >= .01 ? 4 : 8;
  return new Intl.NumberFormat('zh-CN', {maximumFractionDigits:digits}).format(value) + ' USDT';
}
function showPricePrecision(node, value) {
  node.classList.add('trade-price');
  if (value != null && value !== '') node.title = String(value) + ' USDT';
}
function holdingMetrics(position, leverage) {
  const metrics = orderElement('dl', 'order-metrics position-metrics');
  const money = value => value == null ? '—' : oneDecimal.format(value) + ' USDT';
  const quantity = position.quantity + (position.notional == null ? '' : '（' + money(position.notional) + '）');
  for (const [label, value, pnl, rawPrice] of [
    ['开仓时间', date(position.opened)],
    ['开仓均价', tradePrice(position.entry_price), null, position.entry_price],
    ['当前标记价', tradePrice(position.mark_price), null, position.mark_price],
    ['持仓数量', quantity],
    ...(leverage == null ? [] : [['杠杆', leverage + '×']]),
    ['浮盈亏', money(position.pnl), position.pnl],
    ['浮盈亏率', position.roi == null ? '—' : oneDecimal.format(position.roi*100) + '%', position.roi],
    ['预计平仓手续费', money(position.fee)]
  ]) {
    const field = document.createElement('div');
    const data = orderElement('dd', '', value);
    if (rawPrice != null) showPricePrecision(data, rawPrice);
    if (pnl != null) colorPnL(data, pnl);
    field.append(orderElement('dt', '', label), data);
    metrics.append(field);
  }
  return metrics;
}
function positionDirection(quantity) {
  const badge = orderElement('span', 'position-direction', quantity > 0 ? '多' : quantity < 0 ? '空' : '—');
  badge.dataset.direction = quantity > 0 ? 'long' : quantity < 0 ? 'short' : 'unknown';
  return badge;
}
function holdingStatus() {
  const badge = orderElement('span', 'market-pill', '持仓中');
  badge.dataset.tone = 'live';
  return badge;
}
function orderCard(order, position, snapshot) {
  const money = value => value == null || value === '' ? '—' : oneDecimal.format(value) + ' USDT';
  const card = orderElement('article', 'order-card');
  const direction = position ? Number(position.quantity) : ({SELL:1, BUY:-1}[order.side] || 0);
  if (direction) card.dataset.side = direction > 0 ? 'buy' : 'sell';
  const header = orderElement('header', 'order-card-header');
  const badges = orderElement('div', 'order-badges');
  badges.append(positionDirection(direction));
  if (!position) {
    const closed = orderElement('span', 'market-pill', '已平仓');
    closed.dataset.tone = 'submit';
    badges.append(closed);
    const status = orderElement('span', 'market-pill', order.status === 'FILLED' ? '已成交' : order.status);
    status.dataset.tone = order.status === 'FILLED' ? 'live' : 'warn';
    badges.append(status);
  } else {
    badges.append(holdingStatus());
  }
  header.append(orderElement('h3', '', order.symbol), badges);

  const metrics = orderElement('dl', 'order-metrics');
  const times = orderElement('dl', 'order-times');
  const add = (list, label, value, pnl, rawPrice) => {
    const field = document.createElement('div');
    const data = orderElement('dd', '', value);
    if (rawPrice != null) showPricePrecision(data, rawPrice);
    if (pnl != null) colorPnL(data, pnl);
    field.append(orderElement('dt', '', label), data);
    list.append(field);
  };
  const pending = order.reduce_only && order.status === 'FILLED' && order.pnl == null;
  if (!position) {
    add(metrics, '委托名义金额', money(order.notional));
    add(metrics, '已成交金额', money(order.filled));
    add(metrics, '杠杆', order.leverage == null ? '—' : order.leverage + '×');
    const profit = orderElement('div', 'order-profit');
    const amount = orderElement('strong', '', pending ? '待核对' : money(order.pnl));
    colorPnL(amount, order.pnl);
    profit.append(orderElement('small', '', '净收益'), amount);
    card.append(header, orderElement('p', 'order-strategy', strategyNames[order.strategy] || '—'), profit);
    add(metrics, '开仓均价', tradePrice(order.entry_price), null, order.entry_price);
    add(metrics, '平仓均价', tradePrice(order.exit_price), null, order.exit_price);
    add(metrics, '净收益率', order.roi == null ? '—' : oneDecimal.format(order.roi*100) + '%', order.roi);
    add(metrics, '手续费', money(order.fees));
    add(metrics, '资金费净额', money(order.funding));
  } else {
    card.append(header, orderElement('p', 'order-strategy', strategyNames[order.strategy] || '—'));
  }
  add(times, '开仓时间', date(order.opened));
  if (!position) add(times, '平仓时间', date(order.closed));
  add(times, '记录时间', date(order.time));
  const details = document.createElement('details');
  details.append(orderElement('summary', '', '客户端订单号'), orderElement('code', '', order.clientOrderId || '—'));
  if (position) card.append(holdingMetrics(position, order.leverage), protectionPanel(position, snapshot));
  else card.append(metrics, times);
  card.append(details);
  return card;
}
let protectionBusy = false;
let protectionMessage = '';
async function updateProtection(symbol, kind, current) {
  const label = kind === 'stop' ? '止损' : '止盈';
  const input = prompt(symbol + ' · 输入新的' + label + '触发价', current || '');
  if (input == null || !input.trim() || protectionBusy) return;
  protectionBusy = true;
  protectionMessage = '正在向币安提交' + label + '更新…';
  renderOrderCards();
  try {
    const params = new URLSearchParams({symbol, kind, price:input.trim()});
    const response = await fetch('./strategy/protection?' + params, {
      method:'POST', headers:{'X-Quant-Demo':'strategy'}
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error);
    protectionMessage = symbol + ' · ' + label + '已更新';
  } catch (error) {
    protectionMessage = symbol + ' · 更新未确认：' + error.message;
  } finally {
    protectionBusy = false;
    renderOrderCards();
    demoSync.click();
  }
}
function protectionPanel(position, snapshot) {
  const card = orderElement('div', 'order-protection');
  card.append(orderElement('h4', '', '止盈止损'));
  card.append(orderElement('small', '', '策略管理的仓位以策略保护范围为准。条件单触发成交后才核算交易手续费。'));
  for (const [kind, type, label] of [
    ['stop','STOP_MARKET','止损'], ['take','TAKE_PROFIT_MARKET','止盈']
  ]) {
    const line = orderElement('div', 'protection-line');
    const orders = (position.protection || []).filter(order => order.type === type);
    line.append(orderElement('span', '', label));
    for (const order of orders) {
      line.append(orderElement('strong', '', order.trigger));
      const status = orderElement('span', 'market-pill', order.status === 'NEW' ? '已挂单' : order.status);
      status.dataset.tone = order.status === 'NEW' ? 'live' : 'warn';
      line.append(status);
    }
    if (!orders.length) line.append(orderElement('span', 'market-pill', snapshot.protection_error ? '未能核对' : '未挂单'));
    if (Number(position.quantity) !== 0 && !snapshot.protection_error) {
      const own = orders.filter(order => order.owned);
      const button = orderElement('button', '', own.length ? '改价' : '挂单');
      button.type = 'button';
      button.disabled = protectionBusy || own.length > 1 || orders.length !== own.length;
      button.onclick = () => updateProtection(position.symbol, kind, own[0]?.trigger);
      line.append(button);
    }
    card.append(line);
  }
  return card;
}
async function loadStatistics() {
  try {
    const response = await fetch('./account/demo-statistics');
    const rows = await response.json();
    if (!response.ok) throw Error(rows.error);
    const cards = rows.map(stat => {
      const card = orderElement('article', 'order-card');
      const heading = orderElement('h3', '', stat.name);
      const metrics = orderElement('dl', 'order-metrics');
      const fields = [
        ['成交开仓', stat.opens], ['已核对平仓', stat.settled], ['平仓待核对', stat.pending],
        ['累计净收益', stat.settled ? oneDecimal.format(stat.net) + ' USDT' : '—'],
        ['胜率', stat.win_rate == null ? '—' : oneDecimal.format(stat.win_rate * 100) + '%'],
        ['已实现损益夏普', stat.sharpe == null ? '—' : stat.sharpe.toFixed(2)]
      ];
      for (const [label, value] of fields) {
        const field = document.createElement('div');
        const data = orderElement('dd', '', String(value));
        if (label === '累计净收益' && stat.settled) colorPnL(data, stat.net);
        field.append(orderElement('dt', '', label), data);
        metrics.append(field);
      }
      card.append(heading, metrics);
      return card;
    });
    el('strategy-stats').replaceChildren(...cards);
    el('stats-status').textContent = '';
  } catch (error) {
    el('stats-status').textContent = '读取统计失败：' + error.message;
  }
}
let renderedOrders;
let latestOrders = [];
function renderOrderCards() {
  const snapshot = accountMode.value === 'demo' ? accountSnapshot : null;
  const message = el('protection-status');
  message.hidden = !snapshot || (!protectionMessage && !snapshot.protection_error);
  message.textContent = [protectionMessage, snapshot?.protection_error &&
    '币安条件单查询失败：' + snapshot.protection_error].filter(Boolean).join(' · ');

  const holdings = snapshot?.holdings || [];
  const visibleHoldings = pageItems(holdings, 'positions-pages', renderOrderCards);
  const signature = JSON.stringify([latestOrders, visibleHoldings, snapshot?.protection_error, protectionBusy]);
  if (signature === renderedOrders) return;
  const current = el('current-orders');
  const open = new Set([...current.querySelectorAll('details[open] code')]
    .map(node => node.textContent));
  const activeCards = visibleHoldings.map(position => {
    const side = Number(position.quantity) > 0 ? 'BUY' : 'SELL';
    const entry = latestOrders.find(order => !order.reduce_only && order.side === side &&
      order.symbol === position.symbol &&
      (order.status === 'FILLED' || ((order.maker_only || order.time_in_force === 'IOC') && Number(order.executed_qty) > 0)) &&
      (position.opening_order_id != null
        ? String(position.opening_order_id) === String(order.exchange_order_id)
        : position.opened && Number(position.opened) === Number(order.opened)));
    if (entry) {
      const card = orderCard(entry, position, snapshot);
      card.querySelector('details').open = open.has(entry.clientOrderId);
      return card;
    }
    const card = orderElement('article', 'order-card protection-card');
    const header = orderElement('header', 'order-card-header');
    const badges = orderElement('div', 'order-badges');
    badges.append(positionDirection(Number(position.quantity)), holdingStatus());
    header.append(orderElement('h3', '', position.symbol), badges);
    card.append(header,
      holdingMetrics(position),
      protectionPanel(position, snapshot));
    return card;
  });
  current.replaceChildren(...(activeCards.length ? activeCards :
    [orderElement('p', 'order-strategy', snapshot ? '当前无持仓' : '等待账户同步')]));

  renderedOrders = signature;
}
let historyRequest;
let renderedHistory;
async function loadHistory(page) {
  if (page == null && historyRequest) return;
  const requestedPage = page ?? el('history-pages').dataset.page ?? 1;
  const request = fetch('./account/demo-history?page=' + requestedPage, {signal:AbortSignal.timeout(6000)});
  historyRequest = request;
  const status = el('order-status');
  try {
    const response = await request;
    const history = await response.json();
    if (historyRequest !== request) return;
    if (!response.ok) throw Error(history.error || 'HTTP ' + response.status);
    renderPagination('history-pages', history.page, history.pages, loadHistory);
    const signature = JSON.stringify(history.orders);
    if (signature !== renderedHistory) {
      const grid = el('demo-orders');
      const open = new Set([...grid.querySelectorAll('details[open] code')].map(node => node.textContent));
      const cards = history.orders.map(order => {
        const card = orderCard(order);
        card.querySelector('details').open = open.has(order.clientOrderId);
        return card;
      });
      grid.replaceChildren(...cards);
      renderedHistory = signature;
    }
    status.textContent = history.orders.length ? '' : '暂无本程序记录的已平仓交易';
  } catch (error) {
    if (historyRequest === request) status.textContent = '读取交易历史失败：' + error.message;
  } finally {
    if (historyRequest === request) historyRequest = null;
  }
}
let protectionDecisions = [];
let protectionDecisionsPending = false;
async function loadProtectionDecisions() {
  if (protectionDecisionsPending) return;
  protectionDecisionsPending = true;
  try {
    const response = await fetch('./strategy/protection-decisions', {cache:'no-store', signal:AbortSignal.timeout(6000)});
    if (!response.ok) throw Error('HTTP ' + response.status);
    const events = await response.json();
    if (JSON.stringify(events) !== JSON.stringify(protectionDecisions)) {
      protectionDecisions = events;
      renderDecisions();
    }
  } catch (error) {
    console.error('读取保护单决策失败：', error);
  } finally {
    protectionDecisionsPending = false;
  }
}
function renderDecisions() {
  const records = [
    ...latestOrders.map(order => ({order, at:Number(order.time || order.closed || order.opened || 0)})),
    ...protectionDecisions.map(event => ({event, at:Number(event.at)}))
  ].sort((a, b) => b.at - a.at);
  const items = pageItems(records, 'decisions-pages', renderDecisions).map(({order, event}) => {
    if (event) {
      const item = orderElement('article', 'decision-item');
      item.append(orderElement('strong', '', event.symbol + ' · ' + event.message),
        orderElement('p', '', event.detail),
        orderElement('small', '', [strategyNames[event.mode], date(event.at)].filter(Boolean).join(' · ')));
      return item;
    }
    const item = orderElement('article', 'decision-item');
    const action = order.reduce_only
      ? (order.side === 'BUY' ? '平空' : '平多')
      : (order.side === 'SELL' ? '开空' : '开多');
    const decision = order.decision;
    const orderLabels = {FILLED:'已成交', NEW:'已挂单', PARTIALLY_FILLED:'部分成交 · 待核对',
      CANCELED:'已撤单', EXPIRED:'已到期', EXPIRED_IN_MATCH:'撮合失效', REJECTED:'已拒单'};
    const endedWithFill = ['CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH'].includes(order.status) && Number(order.executed_qty) > 0;
    const status = endedWithFill ? '部分成交 · 剩余委托已结束' : (orderLabels[order.status] || order.status);
    item.append(orderElement('strong', '', order.symbol + ' · ' + action + ' · ' + status),
      orderElement('p', '', decision?.reason || '旧订单未记录决策原因'),
      orderElement('small', '', [strategyNames[order.strategy], date(decision?.signal_at || order.closed || order.opened || order.time),
        order.limit_price ? '限价 ' + order.limit_price : '', decision?.detail].filter(Boolean).join(' · ')));
    if (order.status_reason || (order.status === 'NEW' && order.entry_expires_at)) {
      item.append(orderElement('p', '', [order.status_reason, order.status === 'NEW' && order.entry_expires_at &&
        '计划撤单时间 ' + date(order.entry_expires_at)].filter(Boolean).join(' · ')));
    }
    return item;
  });
  el('decision-events').replaceChildren(...(items.length ? items : [orderElement('p', '', '暂无决策记录')]));
}
const runtimeTimeFormat = new Intl.DateTimeFormat('zh-CN', {
  year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit',
  second:'2-digit', fractionalSecondDigits:3, hourCycle:'h23'
});
let runtimeRequestPending = false;
let renderedRuntimeLogs;
let latestRuntimeLogs = [];
function renderRuntimeLogs() {
  const items = pageItems(latestRuntimeLogs, 'runtime-pages', renderRuntimeLogs).map(event => {
    const item = orderElement('article', 'decision-item');
    const time = orderElement('time', '', runtimeTimeFormat.format(event.at));
    time.dateTime = new Date(event.at).toISOString();
    const meta = orderElement('small', '');
    meta.append(time);
    if (strategyNames[event.mode]) meta.append(' · ' + strategyNames[event.mode]);
    item.append(orderElement('strong', '', event.message));
    if (event.detail) item.append(orderElement('p', '', event.detail));
    item.append(meta);
    return item;
  });
  el('runtime-events').replaceChildren(...items);
}
async function loadRuntimeLogs() {
  if (runtimeRequestPending) return;
  runtimeRequestPending = true;
  const status = el('runtime-status');
  try {
    const response = await fetch('./strategy/logs', {cache:'no-store', signal:AbortSignal.timeout(6000)});
    if (!response.ok) throw Error('HTTP ' + response.status);
    const events = await response.json();
    const signature = JSON.stringify(events);
    if (signature !== renderedRuntimeLogs) {
      latestRuntimeLogs = events;
      renderRuntimeLogs();
      renderedRuntimeLogs = signature;
    }
    status.textContent = events.length ? '' : '暂无运行记录';
  } catch (error) {
    status.textContent = '读取运行日志失败：' + error.message;
  } finally {
    runtimeRequestPending = false;
  }
}
async function loadOrders() {
  try {
    const response = await fetch('./account/demo-orders');
    const orders = await response.json();
    if (!response.ok) throw Error(orders.error);
    latestOrders = orders;
    renderOrderCards();
    if (!el('decisions-panel').hidden) renderDecisions();
    const returns = await fetch('./account/demo-realized-return');
    if (returns.ok) {
      realizedReturnSnapshot = await returns.json();
      showRealizedReturn();
    } else if (!realizedReturnSnapshot) {
      el('pnl-chart-status').textContent = '盈亏数据暂不可用';
    }
  } catch (error) {
    if (!realizedReturnSnapshot) el('pnl-chart-status').textContent = '盈亏数据暂不可用';
    console.error('读取订单失败：', error);
  }
}
let strategyChecks = {};
let statusRequest = null;
let statusReceivedAt = null;
let lastProgressAt = Date.now();
let progressKey = '';
let strategyState = null;
let strategyAction = null;
let strategyStartError = '';
let strategyStatusFailed = false;
let marketCatalog = null;
let marketByAsset = new Map();
function assetBusy(asset) {
  return Boolean(strategyState?.running && strategyState.busy_assets?.includes(asset));
}
function assetEditingLocked() {
  return !strategyState || strategyState.draining || strategyState.switch_blocked || strategyAction !== null;
}
let assetUpdateRevision = 0;
async function changeRunningAsset(asset, enabled) {
  if (assetEditingLocked()) return false;
  strategyAction = 'asset';
  renderStrategyControls();
  let error;
  try {
    const params = new URLSearchParams({asset, enabled:enabled ? '1' : '0'});
    const response = await fetch('./strategy/asset?' + params, {
      method:'POST', headers:{'X-Quant-Demo':'strategy'}, signal:AbortSignal.timeout(30000)
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'HTTP ' + response.status);
    assetUpdateRevision++;
    if (strategyState.running) strategyState.assets = data.assets;
  } catch (failure) {
    error = failure.message;
  } finally {
    strategyAction = null;
    renderStrategyControls();
    if (error) el('strategy-status').textContent = error;
  }
  return !error;
}
function renderStrategyControls() {
  const start = el('strategy-start');
  const stop = el('strategy-stop');
  const entries = el('strategy-entries');
  const running = Boolean(strategyState?.running);
  const stopping = strategyAction === 'stop' || Boolean(strategyState?.draining);
  const blocked = Boolean(strategyState?.switch_blocked);
  const locked = !strategyState || running || stopping || blocked || strategyAction !== null;
  strategyMode.disabled = locked;
  strategySizing.disabled = locked;
  el('strategy-notional-field').hidden = strategySizing.value !== 'fixed';
  strategyNotional.disabled = locked || strategySizing.value !== 'fixed';
  start.disabled = locked || !marketCatalog || ![...config.querySelectorAll('[name=asset]:checked')]
    .some(input=>marketByAsset.get(input.value)?.demo_tradeable &&
      (strategyMode.value !== 'us_open' || marketByAsset.get(input.value)?.equity === 'us'));
  start.dataset.running = String(running && !stopping);
  if (stopping) start.textContent = '正在停止策略…';
  else if (running) start.textContent = '✓ 策略运行中';
  else if (strategyAction === 'start') start.textContent = '正在启动策略…';
  else if (blocked) start.textContent = '平仓待确认';
  else if (strategyState) start.textContent = '启动模拟策略';
  else start.textContent = strategyStatusFailed ? '后台状态暂不可用' : '正在同步状态…';
  const pausedAfterError = strategyState?.status?.startsWith('已暂停');
  stop.disabled = !(running || blocked || pausedAfterError) || stopping || strategyAction !== null;
  stop.textContent = stopping ? '正在停止并平仓…' : '停止并平仓';
  entries.disabled = typeof strategyState?.entries_paused !== 'boolean' || !running || stopping || strategyAction !== null;
  entries.textContent = strategyState?.entries_paused ? '恢复开新仓' : '停止开新仓';
  for (const checkbox of config.querySelectorAll('[name=asset]')) {
    const market = marketByAsset.get(checkbox.value);
    checkbox.disabled = assetEditingLocked() || (checkbox.checked && assetBusy(checkbox.value)) || !market?.demo_tradeable || (strategyMode.value === 'us_open' && market.equity !== 'us');
    checkbox.parentElement.dataset.unavailable = String(!market?.demo_tradeable);
    checkbox.parentElement.title = market?.demo_tradeable ? '' : '模拟盘未确认可交易';
    checkbox.closest('.asset-choice').querySelector('button').disabled = assetEditingLocked() || assetBusy(checkbox.value);
  }
}
function setMarketPill(cell, label, tone) {
  if (cell.firstElementChild?.textContent === label && cell.firstElementChild.dataset.tone === tone) return;
  const pill = document.createElement('span');
  pill.className = 'market-pill updated';
  pill.dataset.tone = tone;
  pill.textContent = label;
  cell.replaceChildren(pill);
}
function renderStrategyCheck(item) {
  const label = strategyChecks[item.dataset.asset];
  const tones = [
    [/委托待核对|挂单等待成交/, 'warn'],
    [/信号成立.*等待下单检查/, 'signal'],
    [/均线|趋势未确认/, 'trend'],
    [/突破.*最高价|等待突破/, 'breakout'],
    [/ATR/, 'volatility'],
    [/正在提交|设置杠杆/, 'submit'],
    [/正在读取行情/, 'busy'],
    [/已成交/, 'live'],
    [/已平仓|仓位关闭/, 'handled'],
    [/保护单待核对/, 'warn'],
    [/持仓/, 'holding'],
    [/不可用|不可交易|未匹配/, 'error'],
    [/跳过/, 'skip'],
    [/信号成立|入场候选/, 'signal'],
    [/已处理/, 'handled']
  ];
  const tone = tones.find(([pattern]) => pattern.test(label || ''))?.[1] || '';
  setMarketPill(item.cells[4], label || '—', tone);
}
function renderStatusConnection() {
  const age = statusReceivedAt === null ? null : Math.floor((Date.now() - statusReceivedAt)/1000);
  const pulse = el('market-pulse');
  const idleSeconds = Math.floor((Date.now() - lastProgressAt)/1000);
  if (age !== null && age < 10 && strategyState?.running && idleSeconds >= 90) {
    pulse.dataset.tone = 'error';
    pulse.textContent = '本地服务在线 · 策略 ' + idleSeconds + ' 秒无新进度';
    return;
  }
  pulse.dataset.tone = age !== null && age < 10 ? 'live' : age === null && statusRequest ? '' : 'error';
  pulse.textContent = age === null
    ? (statusRequest ? '正在连接' : '尚未取得策略状态')
    : age < 10 ? '自动更新 · ' + age + ' 秒前' : '状态更新延迟 · ' + age + ' 秒未收到';
}
function strategyStatus() {
  if (!statusRequest) {
    statusRequest = refreshStrategyStatus().finally(() => {
      statusRequest = null;
      renderStrategyControls();
      renderStatusConnection();
    });
  }
  return statusRequest;
}
async function showDeployedVersion() {
  try {
    const response = await fetch('./version', {cache:'no-store', signal:AbortSignal.timeout(30000)});
    if (!response.ok) return;
    const {commit} = await response.json();
    if (typeof commit !== 'string' || !/^[a-f0-9]{40}$/.test(commit)) return;
    const version = el('desk-version');
    const link = version.querySelector('a');
    link.textContent = commit.slice(0, 7);
    link.href = 'https://github.com/Gh811/quant-desk/commit/' + commit;
    link.title = commit;
    version.hidden = false;
  } catch {
    // The deployment label is optional on local or older servers.
  }
}
showDeployedVersion();
async function refreshStrategyStatus() {
  const revision = assetUpdateRevision;
  try {
    const response = await fetch('./strategy', {cache:'no-store', signal:AbortSignal.timeout(6000)});
    if (!response.ok) throw Error('HTTP ' + response.status);
    const state = await response.json();
    if (revision !== assetUpdateRevision) return;
    strategyChecks = state.checks || {};
    const wasRunning = strategyState?.running;
    const busyChanged = JSON.stringify(strategyState?.busy_assets) !== JSON.stringify(state.busy_assets);
    strategyState = state;
    if ((Boolean(wasRunning) !== state.running || busyChanged) && marketCatalog) renderPool();
    strategyStatusFailed = false;
    if (wasRunning && !state.running && accountMode.value === 'demo' && !demoSync.disabled) demoSync.click();
    const nextKey = JSON.stringify([state.status, state.checked_at, strategyChecks, state.running]);
    if (nextKey !== progressKey) {
      progressKey = nextKey;
      lastProgressAt = Date.now();
    }
    statusReceivedAt = Date.now();
    for (const item of el('market-rows').rows) renderStrategyCheck(item);
    const checks = Object.values(strategyChecks);
    const pending = checks.filter(label => label === '正在读取行情').length;
    el('scan-status').textContent = state.running && pending
      ? '正在检查 ' + (checks.length - pending) + '/' + checks.length
      : state.checked_at
        ? '最近一轮 ' + date(state.checked_at) + ' · 耗时 ' + (state.scan_ms/1000).toFixed(1) + ' 秒'
        : '';
    if (state.running || state.switch_blocked) {
      strategyMode.value = state.mode;
      strategySizing.value = state.fixed_notional == null ? 'capital' : 'fixed';
      if (state.fixed_notional != null) strategyNotional.value = state.fixed_notional;
    }
    if (state.running && Array.isArray(state.assets)) {
      let selectionChanged = false;
      const existing = new Set([...config.querySelectorAll('[name=asset]')].map(input=>input.value));
      for (const asset of state.assets) {
        if (!existing.has(asset)) {
          addAssetCheckbox(asset, true);
          selectionChanged = true;
        }
      }
      for (const checkbox of config.querySelectorAll('[name=asset]')) {
        const selected = state.assets.includes(checkbox.value);
        if (checkbox.checked !== selected) {
          checkbox.checked = selected;
          selectionChanged = true;
        }
      }
      if (selectionChanged) renderMarketRows();
    } else if (wasRunning) {
      for (const input of config.querySelectorAll('[name=asset]')) {
        if (removedAssets.has(input.value)) input.closest('.asset-choice').remove();
      }
      renderMarketRows();
    }
    if (state.running) strategyStartError = '';
    el('strategy-status').textContent = state.running
      ? strategyNames[state.mode] + ' · ' + state.status : strategyStartError || state.status;
  } catch (error) {
    strategyStatusFailed = true;
    el('strategy-status').textContent = '无法读取策略状态：' + error.message;
  }
}
for (const control of ['start','stop','entries']) {
  const button = el('strategy-' + control);
  button.onclick = async () => {
    if (button.disabled || strategyAction) return;
    const action = control === 'entries' ? (strategyState.entries_paused ? 'resume-entries' : 'pause-entries') : control;
    if (action === 'stop' && !confirm('停止后台策略并平掉币安模拟盘的所有单向持仓？')) return;
    const params = new URLSearchParams(new FormData(config));
    params.set('strategy', strategyMode.value);
    if (action === 'start') {
      params.set('sizing', strategySizing.value);
      if (strategySizing.value === 'fixed') {
        if (!strategyNotional.value || !strategyNotional.checkValidity()) {
          el('strategy-status').textContent = '请输入有效的单笔目标金额（USDT）';
          strategyNotional.focus();
          return;
        }
        params.set('notional', strategyNotional.value);
      }
    }
    strategyStartError = '';
    strategyAction = action;
    renderStrategyControls();
    let actionError;
    try {
      const response = await fetch('./strategy/' + action + '?' + params, {
        method:'POST', headers:{'X-Quant-Demo':'strategy'},
        signal:AbortSignal.timeout(action === 'start' ? 30000 : 6000)
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw Error(detail?.error || 'HTTP ' + response.status);
      }
    } catch (error) {
      actionError = error.message;
    } finally {
      if (statusRequest) await statusRequest;
      await strategyStatus();
      strategyAction = null;
      renderStrategyControls();
      if (actionError && action === 'start' && !strategyState?.running) {
        strategyStartError = '启动失败 · ' + actionError;
        el('strategy-status').textContent = strategyStartError;
      } else if (actionError && action !== 'start') {
        el('strategy-status').textContent = actionError;
      }
    }
  };
}
function renderMarketRows() {
  if (!marketCatalog) return;
  const selected = new Set([...config.querySelectorAll('[name=asset]:checked')].map(checkbox => checkbox.value));
  for (const asset of poolWatch) selected.add(asset);
  for (const asset of ['BNB','ZEC','INTC']) if (!removedAssets.has(asset)) selected.add(asset);
  const heldSymbols = new Set((accountSnapshot?.holdings || [])
    .filter(position => Number(position.quantity) !== 0)
    .map(position => position.symbol));
  const markets = marketCatalog.filter(market => selected.has(market.asset));
  markets.sort((a, b) => Number(heldSymbols.has(b.symbol)) - Number(heldSymbols.has(a.symbol)));
  el('market-rows').replaceChildren();
  for (const market of markets) {
    const tradeable = market.demo_tradeable;
    const status = tradeable ? '可交易' : market.status ?? (market.live_tradeable ? '仅实盘目录可交易' : '模拟盘未匹配');
    const item = row('market-rows', [name(market.asset), market.symbol, status, market.contract_type ?? '—', strategyChecks[market.asset] || '—']);
    item.dataset.asset = market.asset;
    setMarketPill(item.cells[2], status, tradeable ? 'live' : market.status ? 'warn' : '');
    renderStrategyCheck(item);
  }
}
config.addEventListener('change', async event => {
  const input = event.target;
  if (input.name === 'asset' && strategyState?.running) {
    const enabled = input.checked;
    input.checked = strategyState.assets.includes(input.value);
    if (await changeRunningAsset(input.value, enabled)) input.checked = enabled;
    savePreferences();
  }
  renderMarketRows();
  renderStrategyControls();
});
async function loadMarketStatus() {
  try {
    const response = await fetch('./market-universe', {signal:AbortSignal.timeout(20000)});
    const universe = await response.json();
    if (!response.ok) throw Error(universe.error);
    marketCatalog = universe.markets;
    marketByAsset = new Map(marketCatalog.map(market => [market.asset, market]));
    const existing = new Set([...config.querySelectorAll('[name=asset]')].map(input=>input.value));
    for (const market of marketCatalog) {
      if ((poolWatch.has(market.asset) || (strategyState?.running && strategyState.assets.includes(market.asset))) &&
          !existing.has(market.asset))
        addAssetCheckbox(market.asset, strategyState?.running ? strategyState.assets.includes(market.asset) :
          (preferences?.assets?.includes(market.asset) || false));
    }
    for (const input of config.querySelectorAll('[name=asset]')) {
      const market = marketByAsset.get(input.value);
      if (!universe.demo_error && (!market?.demo_tradeable || (strategyMode.value === 'us_open' && market.equity !== 'us')))
        input.checked = false;
    }
    renderStrategyControls();
    renderMarketRows();
    renderPool();
    el('market-status').textContent = universe.demo_error ? '模拟盘目录核验失败 · ' + universe.demo_error : '币安模拟盘目录 · ' + date(Date.now());
    el('pool-status').textContent = universe.live_error ? '实盘目录暂不可用 · ' + universe.live_error
      : marketCatalog.filter(isPoolMarket).length + ' 个候选 · 加入观察后可在左侧选择模拟盘支持的合约';
  } catch (error) {
    el('market-status').textContent += ' · 本次未能验证：' + error.message;
    el('pool-status').textContent = '候选池读取失败 · ' + error.message;
  }
}
const pinnedPoolAssets = new Set(['BNB', 'ZEC', 'ETH', 'BTC']);
function isPoolMarket(market) {
  return market.pool || pinnedPoolAssets.has(market.asset) || removedAssets.has(market.asset);
}
function renderPool() {
  const query = el('pool-search').value.trim().toUpperCase();
  const rows = (marketCatalog || []).filter(market=>isPoolMarket(market) &&
    (market.symbol.includes(query) || name(market.asset).toUpperCase().includes(query)));
  rows.sort((a, b) => Number(pinnedPoolAssets.has(b.asset)) - Number(pinnedPoolAssets.has(a.asset)));
  el('pool-rows').replaceChildren();
  for (const market of pageItems(rows, 'pool-pages', renderPool)) {
    const item = row('pool-rows', [market.symbol, pinnedPoolAssets.has(market.asset) ? '加密货币' :
      market.equity === 'us' ? '美股 / ETF' : '传统资产 · 分类待确认',
      market.live_tradeable ? '可交易' : '未确认', market.demo_tradeable ? '可交易' : '不可选', '']);
    item.cells[0].className = 'asset-code';
    setMarketPill(item.cells[2], market.live_tradeable ? '可交易' : '未确认', market.live_tradeable ? 'live' : 'warn');
    setMarketPill(item.cells[3], market.demo_tradeable ? '可交易' : '不可选', market.demo_tradeable ? 'live' : 'warn');
    const builtIn = defaultAssets.includes(market.asset);
    const watched = !removedAssets.has(market.asset) && (builtIn || poolWatch.has(market.asset));
    const button = orderElement('button', 'compact-button', watched ? '移出列表' : '加入列表');
    button.classList.toggle('watch-remove', watched);
    button.type = 'button';
    button.disabled = watched && (assetEditingLocked() || assetBusy(market.asset));
    button.addEventListener('click', () => setAssetWatch(market.asset, !watched));
    item.cells[4].append(button);
  }
  if (!rows.length) row('pool-rows', ['暂无匹配合约', '—', '—', '—', '—']);
}
async function setAssetWatch(asset, watched) {
  const input = [...config.querySelectorAll('[name=asset]')].find(input=>input.value === asset);
  if (!watched) {
    if (assetEditingLocked() || assetBusy(asset)) return;
    if (strategyState.running && strategyState.assets.includes(asset) && !(await changeRunningAsset(asset, false))) return;
    poolWatch.delete(asset);
    if (defaultAssets.includes(asset)) removedAssets.add(asset);
    input?.closest('.asset-choice').remove();
  } else {
    removedAssets.delete(asset);
    poolWatch.add(asset);
    if (!input) addAssetCheckbox(asset, false);
  }
  savePreferences();
  renderStrategyControls();
  renderMarketRows();
  renderPool();
}
el('pool-refresh').addEventListener('click', async () => {
  const button = el('pool-refresh');
  button.disabled = true;
  try { await loadMarketStatus(); }
  finally { button.disabled = false; }
});
el('pool-search').addEventListener('input', () => {
  el('pool-pages').dataset.page = 1;
  renderPool();
});
let opportunityRows = [];
function renderOpportunities() {
  el('opportunity-rows').replaceChildren();
  for (const market of pageItems(opportunityRows, 'opportunity-pages', renderOpportunities)) {
    const item = row('opportunity-rows', [market.symbol, market.direction, market.level, market.reason, market.reference || '—',
      market.stop ? market.stop + ' / ' + market.target : '—', market.demo_tradeable ? '可交易' : '不可选']);
    item.cells[0].className = 'asset-code';
    setMarketPill(item.cells[2], market.level, market.level === '信号成立' ? 'signal' : 'trend');
  }
  el('opportunity-results').hidden = !opportunityRows.length;
}
let opportunityHistorySnapshot = '';
let opportunityHistory = [];
let opportunityCopyTimer;
const opportunityHistoryTime = new Intl.DateTimeFormat('zh-CN', {hour:'2-digit', minute:'2-digit', hourCycle:'h23', timeZone:'Asia/Shanghai'});
function renderOpportunityHistory(history) {
  const cutoff = Date.now() - 86400000;
  const visible = (history || []).filter(item => item.candle_at > cutoff);
  const snapshot = JSON.stringify([history === null, visible]);
  if (snapshot === opportunityHistorySnapshot) return;
  opportunityHistorySnapshot = snapshot;
  const cards = visible.map(item => {
    const card = orderElement('button', '');
    card.type = 'button';
    card.dataset.direction = item.direction;
    card.setAttribute('aria-label', '复制 ' + item.symbol + ' · 做' + item.direction);
    const time = orderElement('time', '', opportunityHistoryTime.format(item.candle_at));
    time.dateTime = new Date(item.candle_at).toISOString();
    card.append(orderElement('span', 'asset-code', item.symbol), '做' + item.direction,
                orderElement('span', '', '参考开仓 ' + item.reference), time);
    card.onclick = async () => {
      const status = el('opportunity-history-status');
      clearTimeout(opportunityCopyTimer);
      try {
        await navigator.clipboard.writeText(item.symbol);
        status.textContent = '已复制 ' + item.symbol;
      } catch {
        status.textContent = '复制失败 · ' + item.symbol;
      }
      opportunityCopyTimer = setTimeout(() => { status.textContent = ''; }, 2500);
    };
    return card;
  });
  el('opportunity-history').replaceChildren(...cards);
  clearTimeout(opportunityCopyTimer);
  el('opportunity-history-status').textContent = history === null ? '历史记录暂不可用' : visible.length ? '' : '最近 24 小时暂无记录';
}
let opportunityRequest = false;
let opportunitySnapshot = '';
let opportunityCandle = null;
async function refreshOpportunities() {
  if (opportunityRequest) return;
  opportunityRequest = true;
  try {
    const response = await fetch('./opportunities', {cache:'no-store', signal:AbortSignal.timeout(6000)});
    const report = await response.json();
    if (!response.ok) throw Error(report.error || 'HTTP ' + response.status);
    opportunityHistory = report.history ?? null;
    renderOpportunityHistory(opportunityHistory);
    if (report.candle_at !== opportunityCandle) {
      opportunityCandle = report.candle_at;
      el('opportunity-pages').dataset.page = 1;
    }
    const snapshot = JSON.stringify([report.candle_at, report.rows]);
    if (snapshot !== opportunitySnapshot) {
      opportunitySnapshot = snapshot;
      opportunityRows = report.rows;
      renderOpportunities();
    }
    const status = {
      waiting:'等待本轮行情就绪', scanning:'后台正在扫描',
      ready:opportunityRows.length ? '信号时点 · ' + date(report.candle_at) : '当前暂无符合条件的机会',
      partial:opportunityRows.length ? '部分行情暂不可用 · ' + date(report.candle_at) : '部分行情暂不可用 · 暂无已确认机会',
      unavailable:'行情暂不可用 · 后台将重试'
    }[report.status];
    const label = el('opportunity-status');
    if (label.textContent !== status) label.textContent = status;
  } catch {
    opportunitySnapshot = '';
    renderOpportunityHistory(opportunityHistory);
    if (opportunityRows.length) {
      opportunityRows = [];
      renderOpportunities();
    }
    el('opportunity-status').textContent = '机会扫描连接中断 · 正在重连';
  } finally {
    opportunityRequest = false;
  }
}
refreshOpportunities();
setInterval(refreshOpportunities, 3000);
renderStrategyControls();
loadMarketStatus();
strategyStatus();
setInterval(strategyStatus, 2500);
setInterval(renderStatusConnection, 1000);
demoSync.click();
setInterval(() => {
  if (accountMode.value === 'demo' && !demoSync.disabled) demoSync.click();
}, 5000);
setInterval(updateAccountStatus, 1000);
loadOrders();
setInterval(loadOrders, 10000);
let shadowRequest = false;
let shadowAction = false;
let shadowState;
const shadowPercent = new Intl.NumberFormat('zh-CN', {style:'percent', minimumFractionDigits:2, maximumFractionDigits:2, signDisplay:'exceptZero'});
function renderShadow() {
  const status = el('shadow-status');
  const state = shadowState;
  if (!state) return;
  el('shadow-start').disabled = shadowAction || state.enabled;
  el('shadow-pause').disabled = shadowAction || !state.enabled;
  status.textContent = [
    state.enabled ? '跟随中 · ' + (strategyState?.running ? strategyNames[strategyState.mode] : '等待模拟盘策略成交')
      : '跟随已关闭 · 已有虚拟仓位继续跟随平仓',
    state.error
  ].filter(Boolean).join(' · ');
  const baseline = realizedReturnSnapshot?.principal;
  const principal = baseline == null ? null : Number(baseline);
  const realized = Number(state.realized);
  const rate = principal > 0 && Number.isFinite(principal) ? realized / principal : null;
  const returnField = el('shadow-return');
  returnField.textContent = Number.isFinite(rate) ? shadowPercent.format(rate) : '—';
  colorPnL(returnField, rate);
  const open = state.positions.filter(position => !position.closed);
  const floating = open.some(position => position.floating == null)
    ? null : open.reduce((sum, position) => sum + Number(position.floating), 0);
  const pending = state.positions.some(position => position.closed);
  const equity = principal == null || floating == null || pending ? null : principal + realized + floating;
  for (const [id, value, signed] of [
    ['shadow-principal', principal, false],
    ['shadow-realized', realized, true],
    ['shadow-floating', floating, true],
    ['shadow-equity', equity, false]
  ]) {
    const field = el(id);
    field.textContent = Number.isFinite(value)
      ? (signed ? pnlAmount : oneDecimal).format(value) + ' USDT' : '—';
    if (signed) colorPnL(field, value);
  }
  el('shadow-metrics-note').textContent = pending
    ? '有平仓尚未核对，当前权益待更新。收益率仅统计已结算的虚拟交易。'
    : '本金沿用模拟盘起算本金。收益率 = 累计虚拟净盈亏 ÷ 初始本金。单边手续费估算 0.05%。';
  const holdings = state.positions.map(p => {
    const card = orderElement('article', 'order-card');
    card.append(orderElement('strong', '', p.symbol + ' · ' + (p.direction > 0 ? '虚拟多仓' : '虚拟空仓')),
      orderElement('p', '', '入场 ' + p.entry + ' · 数量 ' + p.amount),
      orderElement('p', '', '止损 ' + (p.stop ?? '待同步') + ' · 止盈 ' + (p.take ?? '待同步')),
      orderElement('small', '', p.closed ? (p.exit ? '已估算出场，资金费待核对' : p.reason) :
        (p.floating == null ? '浮盈亏待更新' : '估算浮盈亏 ' + pnlAmount.format(Number(p.floating)) + ' USDT')));
    return card;
  });
  el('shadow-summary').replaceChildren(...holdings);
  const missed = el('shadow-missed');
  const latestMiss = state.notes[0];
  missed.hidden = !latestMiss;
  missed.textContent = latestMiss ? '最近未跟随 · ' + date(latestMiss.time) + ' · ' +
    latestMiss.symbol + ' · ' + latestMiss.text : '';
  const checks = state.notes.map(note => orderElement('p', '', date(note.time) + ' · ' + note.symbol + ' · ' + note.text));
  const trades = state.trades.map(t => {
    const item = orderElement('article', 'decision-item');
    const net = orderElement('strong', '', t.symbol + ' · ' + (t.direction > 0 ? '多' : '空') +
      ' · ' + pnlAmount.format(Number(t.net)) + ' USDT');
    colorPnL(net, t.net);
    item.append(net, orderElement('p', '', t.reason + ' · ' + t.entry + ' → ' + t.exit),
      orderElement('small', '', date(t.closed) + ' · 手续费 ' + pnlAmount.format(Number(t.fees)) +
        ' · 资金费 ' + pnlAmount.format(Number(t.funding))));
    return item;
  });
  el('shadow-records').replaceChildren(...checks, ...trades);
}
async function loadShadow() {
  if (shadowRequest) return;
  shadowRequest = true;
  try {
    const response = await fetch('./shadow', {cache:'no-store', signal:AbortSignal.timeout(6000)});
    if (!response.ok) throw Error('HTTP ' + response.status);
    shadowState = await response.json();
    renderShadow();
  } catch (error) {
    el('shadow-status').textContent = '读取实盘行情模拟状态失败 · ' + error.message;
  } finally {
    shadowRequest = false;
  }
}
for (const action of ['start', 'pause']) {
  el('shadow-' + action).onclick = async () => {
    if (shadowAction) return;
    shadowAction = true;
    renderShadow();
    let errorMessage;
    try {
      const response = await fetch('./shadow/' + action, {
        method:'POST', headers:{'X-Quant-Demo':'strategy'}, signal:AbortSignal.timeout(6000)
      });
      if (!response.ok) throw Error((await response.json()).error || 'HTTP ' + response.status);
    } catch (error) {
      errorMessage = error.message;
    } finally {
      shadowAction = false;
      renderShadow();
      await loadShadow();
      if (errorMessage) el('shadow-status').textContent = errorMessage;
    }
  };
}
loadShadow();
setInterval(loadShadow, 3000);
const positionTabs = [...document.querySelectorAll('.position-tabs button')];
for (const tab of positionTabs) {
  tab.onclick = () => {
    for (const item of positionTabs) {
      item.setAttribute('aria-pressed', String(item === tab));
      el(item.getAttribute('aria-controls')).hidden = item !== tab;
    }
    if (tab.getAttribute('aria-controls') === 'decisions-panel') {
      renderDecisions();
      loadProtectionDecisions();
    }
    if (tab.getAttribute('aria-controls') === 'runtime-panel') loadRuntimeLogs();
    if (tab.getAttribute('aria-controls') === 'history-panel') loadHistory();
  };
}
setInterval(() => {
  if (!el('runtime-panel').hidden) loadRuntimeLogs();
  if (!el('decisions-panel').hidden) loadProtectionDecisions();
}, 2500);
setInterval(() => {
  if (!el('history-panel').hidden) loadHistory();
}, 10000);
const statsToggle = el('stats-toggle');
const statsPanel = el('stats-panel');
statsToggle.onclick = () => {
  statsPanel.hidden = !statsPanel.hidden;
  statsToggle.setAttribute('aria-expanded', String(!statsPanel.hidden));
  statsToggle.textContent = statsPanel.hidden ? '查看策略统计' : '收起策略统计';
  if (!statsPanel.hidden) loadStatistics();
};
setInterval(() => {
  if (!statsPanel.hidden) loadStatistics();
}, 15000);
