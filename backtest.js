const form = document.getElementById('backtest');
const status = document.getElementById('status');
const results = document.getElementById('results');
const color = index => 'hsl(' + ((index * 137 + 211) % 360) + ' 58% 36%)';
const amount = new Intl.NumberFormat('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const percent = v => v == null ? '—' : (v*100).toFixed(2)+'%';
const formatTime = t => new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(t);
function element(tag, value) {
  const node = document.createElement(tag);
  if (value != null) node.textContent = value;
  return node;
}
function table(headings, rows, pnlColumn) {
  const container = element('div');
  container.className = 'scroll';
  const grid = element('table');
  const head = grid.createTHead().insertRow();
  headings.forEach(text => head.append(element('th',text)));
  const body = grid.createTBody();
  for (const row of rows) {
    const tr = body.insertRow();
    row.forEach((value,i) => {
      const cell = tr.insertCell();
      cell.textContent = i === pnlColumn ? amount.format(value) : value;
      if (i === pnlColumn) cell.className = value > 0 ? 'gain' : value < 0 ? 'loss' : '';
    });
  }
  container.append(grid);
  return container;
}
function draw(canvas, data, start, end) {
  const points = data.flatMap(item => item.curve.map(point => point.equity));
  const low = Math.min(...points), high = Math.max(...points);
  const w = canvas.clientWidth, h = canvas.clientHeight, scale = window.devicePixelRatio || 1;
  if (!w) return;
  canvas.width=w*scale; canvas.height=h*scale;
  const ctx=canvas.getContext('2d');
  ctx.scale(scale,scale);
  const x=t=>66+(t-start)/(end-start)*(w-82);
  const y=v=>16+(high-v)/(high-low || 1)*(h-48)+(high===low ? (h-48)/2 : 0);
  ctx.font='12px system-ui'; ctx.fillStyle='#52647a'; ctx.strokeStyle='#d8e2ed';
  for (const level of new Set([low, high, (low+high)/2])) {
    ctx.beginPath(); ctx.moveTo(66,y(level)); ctx.lineTo(w-16,y(level)); ctx.stroke();
    ctx.fillText(amount.format(level),0,y(level)+4);
  }
  ctx.fillText(formatTime(start),66,h-8);
  ctx.textAlign='right'; ctx.fillText(formatTime(end),w-16,h-8);
  data.forEach((item,i)=>{
    ctx.strokeStyle=color(i);ctx.lineWidth=2;ctx.beginPath();
    item.curve.forEach((p,j)=>j?ctx.lineTo(x(p.time),y(p.equity)):ctx.moveTo(x(p.time),y(p.equity)));
    ctx.stroke();
  });
}
let chart;
function render(data) {
  results.replaceChildren();
  const panel=element('div');panel.className='panel';
  const metric = value => value == null ? '—' : amount.format(value);
  panel.append(element('h2',data.assets[0]+' · '+data.source_label),element('p','实际回测区间 '+formatTime(data.start)+' 至 '+formatTime(data.end)),table(['策略','净盈亏 USDT','毛盈亏','手续费','滑点','资金费净额','收益率','最大回撤','胜率','平均盈亏比','盈利因子','每笔净收益 USDT','交易笔数','峰值仓位','满仓信号','事件过滤信号'],
    data.results.map(r=>[r.name,r.net,amount.format(r.gross),amount.format(r.fees),amount.format(r.slippage),amount.format(r.funding),percent(r.roi),percent(r.drawdown),percent(r.win_rate),metric(r.payoff_ratio),metric(r.profit_factor),metric(r.expectancy),r.count,r.peak_positions,r.capacity_skips,r.event_skips]),1));
  panel.append(element('h2','估算权益曲线'));
  const canvas=element('canvas');canvas.setAttribute('aria-label','回测权益随时间变化');
  const legend=element('div');legend.className='legend';
  data.results.forEach((r,i)=>{const label=element('small',r.name);label.style.color=color(i);legend.append(label)});
  panel.append(canvas,legend);results.append(panel);
  chart=()=>draw(canvas,data.results,data.start,data.end);
  chart();
  for (const r of data.results) {
    const review = element('div');
    review.className = 'panel';
    review.append(element('h2','交易分布'), table(['方向','笔数','净盈亏 USDT','胜率'],
      [[1,'做多'],[-1,'做空']].map(([direction,label]) => {
        const trades = r.trades.filter(trade => trade.direction === direction);
        return [label,trades.length,trades.reduce((sum,trade) => sum + trade.net,0),
          percent(trades.length ? trades.filter(trade => trade.net > 0).length/trades.length : null)];
      }),2));
    if (r.entry_checks) {
      review.append(element('h2','入场检查'), table(['检查结果','次数'], Object.entries(r.entry_checks)));
    }
    results.append(review);
    const details=element('details');
    details.open = true;
    details.append(element('summary',r.name+' · 成交明细（'+r.count+'）'));
    if (r.trades.length) {
      const headings = ['标的','入场','出场','方向','入场价','出场价','净盈亏 USDT','退出原因'];
      if (r.mode === 'fast') headings.push('入场时历史估计');
      const rows = r.trades.map(t=>{
        const row = [t.symbol,formatTime(t.opened),formatTime(t.closed),t.direction>0?'多':'空',t.entry,t.exit,t.net,t.reason];
        if (r.mode === 'fast') {
          const e = t.evidence;
          row.push(e?.samples ? percent(e.probability) + ' / ' + metric(e.expectancy) + 'R / ' + e.samples + ' 笔' : '暂无样本');
        }
        return row;
      });
      details.append(table(headings, rows, 6));
    } else details.append(element('p','区间内无成交'));
    results.append(details);
  }
}
const assetGroup = document.getElementById('asset');
const search = document.getElementById('search-assets');
const historyAsset = document.getElementById('history-asset');
const historySource = document.getElementById('history-source');
const defaults = 'ETH BTC XAU SNDK SOXL KORU MU NVDA SOL SKHYNIX SAMSUNG QQQ GOOGL SPCX META TSLA AMZN BNB ZEC INTC'.split(' ');
let markets = [];
const selected = {live:new Set(['BTC']), demo:new Set(['BTC'])};
const submit = form.querySelector('button[type=submit]');
function source() { return form.elements.source.value; }
function updateBacktestAssets() {
  const feed = source();
  const list = markets.length ? markets : defaults.map(asset => ({asset, symbol:asset+'USDT'}));
  const query = search.value.trim().toUpperCase();
  assetGroup.replaceChildren(...list.filter(market => market.symbol.includes(query)).map(market => {
    const label = element('label');
    const input = element('input');
    Object.assign(input, {type:'checkbox', value:market.asset, checked:selected[feed].has(market.asset)});
    if (market[feed+'_tradeable'] === false) label.title = '提交后检查该来源的历史行情';
    input.onchange = () => input.checked ? selected[feed].add(market.asset) : selected[feed].delete(market.asset);
    label.append(input, market.asset);
    return label;
  }));
}
async function fetchData(url, options = {}) {
  const response = await fetch(url, {cache:'no-store', signal:AbortSignal.timeout(15000), ...options});
  const data = await response.json();
  if (!response.ok) throw Error(data.error || 'HTTP '+response.status);
  return data;
}
if (!new URLSearchParams(location.search).has('id')) fetchData('./market-universe').then(data => {
  markets = data.markets;
  updateBacktestAssets();
}).catch(() => {});
search.oninput = updateBacktestAssets;
document.getElementById('select-assets').onclick = () => {
  for (const input of assetGroup.querySelectorAll('input:not(:disabled)')) selected[source()].add(input.value);
  updateBacktestAssets();
};
document.getElementById('clear-assets').onclick = () => {
  selected[source()].clear();
  updateBacktestAssets();
};
form.elements.days.onchange = () => {
  const customDays = form.elements.days.value === 'days';
  document.getElementById('custom-days').hidden = !customDays;
  form.elements.day_count.disabled = !customDays;
  form.elements.day_count.required = customDays;
  const custom = form.elements.days.value === 'custom';
  document.getElementById('custom-range').hidden = !custom;
  for (const field of ['start','end']) form.elements[field].required = custom;
};
const history = document.getElementById('history');
const previous = document.getElementById('history-prev');
const next = document.getElementById('history-next');
const route = new URLSearchParams(location.search);
const detailId = route.get('id');
let page = Number(route.get('page')) || 1, requestId = 0, historySnapshot = '';
async function refreshHistory() {
  const id = ++requestId;
  const params = new URLSearchParams({source:historySource.value, page});
  const asset = historyAsset.value.trim().toUpperCase().replace(/USDT$/, '');
  if (asset) params.set('asset', asset);
  try {
    const report = await fetchData('./backtest/jobs?'+params);
    if (id !== requestId) return;
    const snapshot = JSON.stringify([historySource.value, asset, report]);
    if (snapshot === historySnapshot) return;
    historySnapshot = snapshot;
    page = report.page;
    const labels = {queued:'排队中', running:'运行中', done:'已完成', failed:'失败'};
    const grid = table(['标的','行情来源','请求区间','状态','进度或错误','结果'], report.jobs.map(job =>
      [job.asset, job.source === 'demo' ? '币安模拟盘' : '币安实盘', formatTime(job.start)+' 至 '+formatTime(job.end), labels[job.state], job.detail || '—', '—']));
    for (const [index, job] of report.jobs.entries()) {
      if (job.state !== 'done') continue;
      const link = element('a','查看');
      const query = new URLSearchParams({id:job.id, source:historySource.value, page, asset:historyAsset.value.trim()});
      link.href = './backtest?' + query;
      grid.querySelector('tbody').rows[index].cells[5].replaceChildren(link);
    }
    history.replaceChildren(report.jobs.length ? grid : element('p','暂无回测记录'));
    const pages = Math.max(1, Math.ceil(report.total/25));
    previous.disabled = page <= 1;
    next.disabled = page >= pages;
    document.getElementById('history-page').textContent = page+' / '+pages;
  } catch (error) {
    if (id === requestId) {
      historySnapshot = '';
      history.replaceChildren(element('p','记录读取失败 · '+error.message));
    }
  }
}
function resetHistory() { page = 1; refreshHistory(); }
form.elements.source.onchange = () => {
  updateBacktestAssets();
};
historySource.onchange = resetHistory;
historyAsset.oninput = resetHistory;
previous.onclick = () => { page--; refreshHistory(); };
next.onclick = () => { page++; refreshHistory(); };
if (['live','demo'].includes(route.get('source'))) {
  form.elements.source.value = route.get('source');
  historySource.value = route.get('source');
}
form.elements.days.onchange();
historyAsset.value = route.get('asset') || '';
if (detailId) {
  document.getElementById('backtest-list').hidden = true;
  const back = document.getElementById('backtest-return');
  const listRoute = new URLSearchParams(route);
  listRoute.delete('id');
  back.href = './backtest?' + listRoute + '#history';
  back.textContent = '← 返回回测记录';
  const detailStatus = document.getElementById('detail-status');
  detailStatus.hidden = false;
  detailStatus.textContent = '正在读取回测结果…';
  fetchData('./backtest/jobs?' + new URLSearchParams({id:detailId})).then(data => {
    document.getElementById('backtest-title').textContent = data.assets[0] + ' 回测详情';
    document.title = data.assets[0] + ' 回测详情 · Quant Desk';
    detailStatus.hidden = true;
    render(data);
  }).catch(error => { detailStatus.textContent = '结果读取失败 · ' + error.message; });
} else {
  updateBacktestAssets();
  refreshHistory();
  setInterval(refreshHistory, 4000);
}
window.addEventListener('resize',()=>chart?.());
form.onsubmit = async event => {
  event.preventDefault();
  const feed = source();
  submit.disabled = true;
  try {
    const assets = [...selected[feed]];
    if (!assets.length) throw Error('请选择至少一个标的');
    const span = form.elements.days.value;
    const params = new URLSearchParams({source:feed, days:span === 'days' ? form.elements.day_count.value : span});
    if (form.elements.days.value === 'custom') {
      for (const field of ['start','end']) params.set(field, form.elements[field].value);
    }
    for (const asset of assets) params.append('asset', asset);
    const data = await fetchData('./backtest/jobs?'+params, {
      method:'POST', headers:{'X-Quant-Demo':'strategy'}
    });
    status.textContent = '已提交 '+data.jobs.length+' 个标的 · 可继续提交其他区间';
    if (source() === feed) {
      historySource.value = feed;
      historyAsset.value = '';
      resetHistory();
    }
  } catch (error) {
    status.textContent = '提交失败 · '+error.message;
  } finally {
    submit.disabled = false;
  }
};
