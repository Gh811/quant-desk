import csv, io, zipfile, subprocess, json, statistics
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal as D
from concurrent.futures import ThreadPoolExecutor
import channel_engine as e
def ms(x): return int(datetime.fromisoformat(x).replace(tzinfo=timezone.utc).timestamp()*1000)
months=[f'2026-{m:02d}' for m in range(5,11)]
from leadership_rebalance_replay import ASSETS
assets='BTC ETH XAU SOXL KORU MU NVDA SKHYNIX SAMSUNG QQQ SPCX META TSLA AMZN INTC OPENAI CRCL SPY PLTR'.split()
kinds=('klines','markPriceKlines','fundingRate')
def fetch(task):
 a,k,m=task
 frame=k+'/' if k in {'1d','4h','1h','15m'} else '' if k=='fundingRate' else '1m/'
 endpoint='klines' if k in {'1d','4h','1h','15m'} else 'markPriceKlines' if k=='mark-day' else k
 symbol=a+'USDT'
 name=f'{symbol}-{frame.rstrip("/") or k}-{m}.zip'
 p=(Path(__file__).resolve().parents[1] / 'market-data')/'halfyear-data'/k/name
 p.parent.mkdir(parents=True,exist_ok=True)
 if not p.exists():
  return task, '未缓存'
 return task,p
def read(p):
 with zipfile.ZipFile(p) as z,z.open(z.namelist()[0]) as f:
  for row in csv.reader(io.TextIOWrapper(f)):
   if row[0].isdigit(): yield row
# Probe earliest month first so later-listed contracts do not waste full downloads.
paths={}
with ThreadPoolExecutor(18) as pool:
 for task,p in pool.map(fetch,[(a,k,m) for a in assets for k in kinds for m in months]): paths[task]=p
eligible=assets
print('earliest',eligible,'excluded',[(a,{k:paths[a,k,months[0]] for k in kinds if not isinstance(paths[a,k,months[0]],Path)}) for a in assets if a not in eligible],flush=True)
with ThreadPoolExecutor(18) as pool:
 for task,p in pool.map(fetch,[(a,k,m) for a in eligible for k in kinds for m in months[1:]] + [(a,'1d',m) for a in eligible for m in ('2026-03','2026-04')] + [(a,f,'2026-04') for a in eligible for f in ('15m','1h','4h')]):paths[task]=p
with ThreadPoolExecutor(12) as pool:
 for task,p in pool.map(fetch,[(a,'mark-day','2026-06-29') for a in eligible]):paths[task]=p
start,end=ms('2026-05-01'),ms('2026-11-01')
def load(a):
 frames={f:[] for f in ('5m','15m','1h')}
 buffers={}
 available=[m for m in months if all(isinstance(paths.get((a,k,m)),Path) for k in kinds)]
 if not available: raise ValueError('无完整归档月份')
 active_months=months[months.index(available[0]):]
 if available != active_months: raise ValueError('上市后归档月份缺失')
 first=None
 minutes={k:e.MinuteBars(0) for k in kinds[:2]}
 for k,series in minutes.items():
  for m in active_months:
   batch=read(paths[a,k,m])
   if m=='2026-06':
    from heapq import merge
    backup=(Path(__file__).resolve().parents[1] / 'market-data' / 'halfyear-data')/('mark-day' if k=='markPriceKlines' else 'daily')/f'{a}USDT-1m-2026-06-29.zip'
    if backup.exists():
     batch=merge(batch,read(backup),key=lambda row:int(row[0]))
   for row in batch:
    t=int(row[0])
    if len(series) and t==series.start+60000*(len(series)-1): continue
    if not len(series):
     series.start=t
    if t!=series.start+60000*len(series) or int(row[6])!=t+59999: raise ValueError(f'{k} 数据缺口 {t}')
    bar=(t,*map(float,row[1:5]))
    series.append(bar)
    if k!='klines': continue
    for f,dest in frames.items():
     width=e.TIMEFRAME_MINUTES[f]*60000
     if t%width==0: buffers[f]=[t,bar[1],bar[2],bar[3],bar[4],float(row[7]),float(row[10])]
     else:
      if f not in buffers: continue
      b=buffers[f];b[2]=max(b[2],bar[2]);b[3]=min(b[3],bar[3]);b[4]=bar[4];b[5]+=float(row[7]);b[6]+=float(row[10])
     if (t+60000)%width==0:
      if f not in buffers: continue
      b=buffers[f];dest.append((tuple(b[:5]),(0,0,0,0,0,0,0,b[5],0,0,b[6])))
  if len(series)!=(end-series.start)//60000: raise ValueError(f'{k} 长度不完整')
 funding=[]
 for m in active_months:
  for row in read(paths[a,'fundingRate',m]):
   t=int(row[0])//60000*60000
   if minutes['markPriceKlines'].start<=t<end: funding.append((t,D(row[2]),D(str(minutes['markPriceKlines'][(t-minutes['markPriceKlines'].start)//60000][0][1]))))
 frames['1m']=minutes['klines']
 return frames,minutes['markPriceKlines'],funding
markets={}
for a in eligible:
 try:
  markets[a]=load(a);print('loaded',a,flush=True)
 except (KeyError,ValueError,TypeError,OSError,zipfile.BadZipFile) as exc: print('invalid',a,str(exc),flush=True)
class Slice:
 def __init__(self,data,left,right): self.data=data;self.left=left;self.right=right
 def __len__(self): return self.right-self.left
 def __getitem__(self,i):
  index=self.left+i
  if index<0: return ((self.data.start+index*60000,0,0,0,0),None)
  return self.data[index]

from datetime import timedelta
left=end-180*24*3600000
i=(left-start)//60000
benchmark=markets.pop('QQQ')[0]['1h']
universe={a:({**d,'1m':Slice(d['1m'],(left-d['1m'].start)//60000,len(d['1m']))},Slice(m,(left-m.start)//60000,len(m)),f) for a,(d,m,f) in markets.items()}

for mode,rule in [('intraday','hourly_rsi_15m_both_reset'),('fast','continuation')]:
 for new in (True,):
  r=e.replay_portfolio(mode,universe,left,end,D('.0005'),D('.0001'),rules=rule,
   target_r=D('1.25'),max_positions=5,holding_hours=24 if mode=='intraday' else None,
   relative_strength_benchmark=benchmark if mode=='intraday' else None,leverage_override=5,
   reentry_bars=3,rebalance_entry=True,staged_take=True,
   maker_entry_assets={a for a in universe if e.equity_kind(a+'USDT',None)=='us'},
   maker_rebalance=True,maker_offset=D(0),thesis_stop=new, staggered_listing=True)
  assert abs(sum(t['net'] for t in r['trades'])-r['net'])<1e-6
  from collections import Counter
  print(mode,new,{k:r[k] for k in ('count','net','drawdown','win_rate','profit_factor','fees')},
        {k:r['entry_checks'].get(k,0) for k in ('分批补仓成交','回均价减仓成交','TP1 成交','TP2 成交','TP3 尾仓跟踪启动')},
        Counter(t['reason'].split(' · ')[0] for t in r['trades']), Counter(t['symbol'] for t in r['trades'] if end-86400000<=t['opened']<end),flush=True)