"""Local classroom bakery tracker. Python standard library only."""
import argparse
import csv
import html
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
START = date(2026, 11, 23)
LAST_MESSAGE_DAY = date(2026, 11, 29)
END = date(2026, 12, 2)
PRODUCTS = {'cupcakes': (4000, 30), 'cake': (12000, 120)}

def item_label(p, q):
    return f"{q} dozen cupcakes" if p=='cupcakes' else f"{q} cake" + ('s' if q!=1 else '')

def product(value):
    m = re.fullmatch(r'(\d+)\s+(dozen cupcakes|cakes?)', value.strip(), re.I)
    if not m or not 1 <= int(m[1]) <= 1000:
        raise ValueError('Unrecognized product or quantity; review the source message.')
    return ('cupcakes' if m[2].lower() == 'dozen cupcakes' else 'cake', int(m[1]))

def pickup(value):
    m = re.search(r'(Nov|Dec)\s+(\d+)', value)
    if not m:
        m = re.search(r'(\d+)\s+(Nov|Dec)', value)
        if not m: raise ValueError('Unrecognized pickup date.')
        return date(2026, 11 if m[2] == 'Nov' else 12, int(m[1])).isoformat()
    return date(2026, 11 if m[1] == 'Nov' else 12, int(m[2])).isoformat()

class Bakery:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, customer TEXT NOT NULL,
                product TEXT NOT NULL CHECK(product IN ('cupcakes','cake')), qty INTEGER NOT NULL CHECK(qty>0),
                pickup TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('Accepted','Picked up','Cancelled')));
            CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY,helper INTEGER NOT NULL DEFAULT 0 CHECK(helper IN(0,1)));
            CREATE TABLE IF NOT EXISTS imports(day TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, source TEXT UNIQUE NOT NULL,
                day TEXT NOT NULL, customer TEXT NOT NULL, kind TEXT NOT NULL, product TEXT, qty INTEGER,
                pickup TEXT, raw TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                order_id INTEGER REFERENCES orders(id));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,day TEXT NOT NULL,detail TEXT NOT NULL);
            ''')
            if not db.execute("SELECT 1 FROM meta WHERE key='today'").fetchone():
                db.execute("INSERT INTO meta VALUES('today',?)", (START.isoformat(),))
                for r in csv.DictReader((ROOT/'existing_orders.csv').read_text(encoding='utf-8-sig').splitlines()):
                    p,q=product(r['item'])
                    db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?)',(int(r['order'].lstrip('#')),r['customer'],p,q,pickup(r['pickup_day']),'Accepted'))
                for i in range((END-START).days):
                    db.execute('INSERT INTO days(day) VALUES(?)',((START+timedelta(days=i)).isoformat(),))

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def today(db):return db.execute("SELECT value FROM meta WHERE key='today'").fetchone()[0]

    def event(self,db,msg): db.execute('INSERT INTO events(day,detail) VALUES(?,?)',(self.today(db),msg))

    @staticmethod
    def used(db,day,exclude=None):
        return sum(PRODUCTS[r['product']][1]*r['qty'] for r in db.execute("SELECT * FROM orders WHERE pickup=? AND status!='Cancelled' AND id!=?",(day,exclude or -1)))

    @staticmethod
    def limit(db,day):
        r=db.execute('SELECT helper FROM days WHERE day=?',(day,)).fetchone()
        if not r: raise ValueError('Pickup day is outside this classroom simulation.')
        return 480+240*r[0]

    def check(self,db,r,exclude=None):
        if not r['pickup'] or (date.fromisoformat(r['pickup'])-date.fromisoformat(self.today(db))).days<2:
            return 'Needs 2 days notice; this pickup is too soon.'
        required=PRODUCTS[r['product']][1]*r['qty']
        left=self.limit(db,r['pickup'])-self.used(db,r['pickup'],exclude)
        if required>left:
            return f'Needs {required/60:g} h; only {left/60:g} h available. Book a helper if notice allows.'
        return ''

    def fetch(self,db):
        today=self.today(db)
        if db.execute('SELECT 1 FROM imports WHERE day=?',(today,)).fetchone(): return 'Messages already loaded; nothing duplicated.'
        dt=date.fromisoformat(today)
        if dt>LAST_MESSAGE_DAY:
            db.execute('INSERT INTO imports VALUES(?)',(today,))
            return 'No more message files. Finish the remaining pickups.'
        filename=f'InstagramOrders_{dt:%A}.html'
        raw=(ROOT/filename).read_text(encoding='utf-8')
        messages=re.findall(r'<p class="message">(.*?)</p>',raw,re.S)
        for i,raw_message in enumerate(messages):
            message=html.unescape(raw_message)
            fields=dict(line.split(':',1) for line in message.splitlines() if ':' in line)
            name=fields.get('Name','').strip()
            value=fields.get('Order','').strip()
            kind='Cancel' if value.lower()=='cancel' else 'Change' if value.lower().startswith('change to ') else 'New'
            p,q,day=None,None,None
            error=''
            try:
                day=pickup(fields.get('Pickup Date',''))
                if kind!='Cancel':p,q=product(re.sub(r'^Change to\s+','',value,flags=re.I))
            except ValueError as e:error=str(e)
            cur=db.execute('INSERT INTO requests(source,day,customer,kind,product,qty,pickup,raw,status,reason) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (f'{filename}:{i}',today,name,kind,p,q,day,message,'Incompatible' if error else 'Requested',error))
            rid=cur.lastrowid
            if error or kind=='New':continue
            matches=db.execute("SELECT * FROM orders WHERE lower(customer)=lower(?) AND pickup=? AND status='Accepted'",(name,day)).fetchall()
            if len(matches)!=1:
                error='No unique accepted order matches this customer and pickup date.'
            elif (date.fromisoformat(day)-dt).days<2:
                error='Needs 2 days notice; the accepted order is unchanged.'
            else:
                old=matches[0]
                r=db.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
                if kind=='Change':error=self.check(db,r,old['id'])
                if not error:
                    if kind=='Cancel':db.execute("UPDATE orders SET status='Cancelled' WHERE id=?",(old['id'],))
                    else:db.execute('UPDATE orders SET product=?,qty=? WHERE id=?',(p,q,old['id']))
                    note=f"{name}: {kind.lower()} applied to #{old['id']} (was {item_label(old['product'],old['qty'])})."
                    db.execute("UPDATE requests SET status='Applied',order_id=?,reason=? WHERE id=?",(old['id'],note,rid))
                    self.event(db,note)
            if error:db.execute("UPDATE requests SET status='Incompatible',reason=? WHERE id=?",(error,rid))
        db.execute('INSERT INTO imports VALUES(?)',(today,))
        self.event(db,f'{dt:%A} messages loaded: {len(messages)} requests. Valid changes and cancellations applied.')
        return f'{dt:%A} messages loaded: {len(messages)} requests. Check the activity log for changes and cancellations.'

    def accept(self,db,rid):
        r=db.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
        if not r or r['status']!='Requested' or r['kind']!='New':raise ValueError('This request is no longer awaiting acceptance.')
        error=self.check(db,r)
        if error:raise ValueError(error)
        oid=db.execute("INSERT INTO orders(customer,product,qty,pickup,status) VALUES(?,?,?,?,'Accepted')",(r['customer'],r['product'],r['qty'],r['pickup'])).lastrowid
        db.execute("UPDATE requests SET status='Accepted',order_id=? WHERE id=?",(oid,rid))
        self.event(db,f"Accepted #{oid}: {r['customer']}, {item_label(r['product'],r['qty'])}, pickup {r['pickup']}.")

    def advance(self,db):
        today=self.today(db)
        if today>=END.isoformat():raise ValueError('The simulation is complete.')
        if not db.execute('SELECT 1 FROM imports WHERE day=?',(today,)).fetchone():raise ValueError("Load today's messages before ending the day.")
        accepted=rejected=0
        for r in db.execute("SELECT * FROM requests WHERE status='Requested' ORDER BY id").fetchall():
            reason=self.check(db,r)
            if reason:
                db.execute("UPDATE requests SET status='Rejected',reason=? WHERE id=?",(reason,r['id']));rejected+=1
                self.event(db,f"Rejected {r['customer']}: {reason}")
            else:self.accept(db,r['id']);accepted+=1
        db.execute("UPDATE requests SET status='Rejected' WHERE status='Incompatible'")
        count=db.execute("UPDATE orders SET status='Picked up' WHERE pickup=? AND status='Accepted'",(today,)).rowcount
        self.event(db,f'Day closed: {accepted} new acceptances, {rejected} pending orders rejected, {count} pickups at 5 PM.')
        db.execute("UPDATE meta SET value=? WHERE key='today'",((date.fromisoformat(today)+timedelta(days=1)).isoformat(),))
        return f'Day closed. Accepted {accepted}, rejected {rejected}, completed {count} pickups.'

    def action(self,kind,args):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.today(db)>=END.isoformat():raise ValueError('The simulation is complete. Your results are saved.')
            if kind=='fetch':return self.fetch(db)
            if kind=='accept':self.accept(db,int(args['id']));return 'Order accepted. Capacity and totals updated.'
            if kind=='reject':
                rid=int(args['id'])
                if not db.execute("UPDATE requests SET status='Rejected',reason='Rejected by baker' WHERE id=? AND status IN ('Requested','Incompatible')",(rid,)).rowcount:raise ValueError('Request already resolved.')
                self.event(db,f'Request {rid} rejected by baker.');return 'Request rejected.'
            if kind=='helper':
                day=args['day']
                if (date.fromisoformat(day)-date.fromisoformat(self.today(db))).days<2:raise ValueError('Helpers require 2 days notice.')
                self.limit(db,day)
                if not db.execute('UPDATE days SET helper=1 WHERE day=? AND helper=0',(day,)).rowcount:raise ValueError('A helper is already booked for this day.')
                self.event(db,f'Helper booked for {day}: $100, +4 hours.');return 'Helper booked. Capacity is now 12 hours.'
            if kind=='advance':return self.advance(db)
            raise ValueError('Unknown action.')

    def preview(self):
        # A full database snapshot: forecast has no writes to the real database.
        with self.connect() as source:
            db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row;source.backup(db)
        try:
            before=db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
            result=self.advance(db)
            events=[r[0] for r in db.execute('SELECT detail FROM events WHERE id>? ORDER BY id',(before,))]
            return {'summary':result,'events':events}
        finally:db.close()

    def state(self):
        with self.connect() as db:
            today=self.today(db)
            orders=[dict(r) for r in db.execute('SELECT * FROM orders ORDER BY pickup,id')]
            for o in orders:o.update(value=o['qty']*PRODUCTS[o['product']][0],minutes=o['qty']*PRODUCTS[o['product']][1])
            requests=[dict(r) for r in db.execute('SELECT * FROM requests ORDER BY id')]
            for r in requests:
                r['issue']=self.check(db,r) if r['status']=='Requested' else r['reason']
                r['eligible']=r['status']=='Requested' and not r['issue']
                r['value']=r['qty']*PRODUCTS[r['product']][0] if r['product'] else 0
                r['minutes']=r['qty']*PRODUCTS[r['product']][1] if r['product'] else 0
                r['left']=self.limit(db,r['pickup'])-self.used(db,r['pickup']) if r['pickup'] else 0
            days=[]
            for d in db.execute('SELECT * FROM days ORDER BY day'):
                row=dict(d);row.update(used=self.used(db,d['day']),limit=self.limit(db,d['day']),count=sum(o['pickup']==d['day'] and o['status']!='Cancelled' for o in orders))
                row['can_book']=not d['helper'] and (date.fromisoformat(d['day'])-date.fromisoformat(today)).days>=2 and today<END.isoformat()
                days.append(row)
            value=sum(o['value'] for o in orders if o['status']!='Cancelled')
            sales=sum(o['value'] for o in orders if o['status']=='Picked up')
            costs=sum(d['helper']*10000 for d in days)
            return {'today':today,'finished':today>=END.isoformat(),'loaded':bool(db.execute('SELECT 1 FROM imports WHERE day=?',(today,)).fetchone()),'orders':orders,'requests':requests,'days':days,'value':value,'sales':sales,'costs':costs,'events':[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')]}

PAGE = r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Home Bakery | Order desk</title>
<style>
:root{--ink:#242638;--muted:#626676;--line:#dce0e7;--accent:#3f4e96;--paper:#fff;--bg:#f3f4f8}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}header{background:#242638;color:white;padding:24px max(24px,calc((100vw - 1360px)/2));display:flex;align-items:center;justify-content:space-between}header small{color:#cbcfe4;letter-spacing:.16em}h1{font-size:28px;margin:3px 0}header a{color:white}main{max-width:1408px;margin:auto;padding:26px 24px 50px}h2{font-size:21px;margin:0 0 5px}p{margin:4px 0 15px}.muted,small{color:var(--muted)}small{font-size:14px}.toolbar{display:flex;justify-content:space-between;gap:18px;align-items:center;margin-bottom:20px}.actions{display:flex;gap:10px;flex-wrap:wrap}button,select{font:inherit;border:1px solid #bbc1d1;background:white;color:var(--ink);padding:10px 15px;border-radius:7px;cursor:pointer}button:hover:not(:disabled){background:#e9ecfa}button.primary{background:var(--accent);color:white;border-color:var(--accent)}button:disabled{color:#727786;background:#eff0f4;cursor:not-allowed;opacity:.75}button:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #8799e7;outline-offset:3px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:18px 0 28px}.stat{padding:19px;background:white;border:1px solid var(--line);border-radius:10px}.stat strong{display:block;font-size:29px;letter-spacing:-1px}.panel{background:white;border:1px solid var(--line);border-radius:12px;margin-bottom:24px;padding:22px}.sectionhead{display:flex;justify-content:space-between;align-items:center;gap:16px}.tablewrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:var(--muted);font-weight:600;background:#f7f8fb}td,th{padding:14px 12px;border-bottom:1px solid var(--line);vertical-align:top}td strong{font-size:16px}td small{display:block;margin-top:4px}td .actions{flex-wrap:nowrap}td button{font-size:14px;padding:7px 12px}.tag{display:inline-block;border-radius:4px;font-size:12px;padding:3px 7px;background:#eef0f6;color:#414965;margin:4px 0}.good{color:#29624a;background:#eaf5ed}.bad{color:#933c2a;background:#fff0ea}.days{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;margin-top:18px}.day{padding:12px;border:1px solid var(--line);border-radius:8px}.day.selected{border:2px solid var(--accent);padding:11px;background:#f7f8ff}.day .selectday{border:0;background:none;font-weight:700;padding:0;text-align:left;width:100%}.day button.helper{font-size:13px;width:100%;padding:8px 3px;margin-top:9px}.day small{display:block;font-size:13px}.meter{height:6px;border-radius:5px;background:#e6e9f0;margin:10px 0}.meter span{display:block;height:6px;background:#6372af;border-radius:5px}.notice{padding:12px 16px;background:#eaf0ff;border:1px solid #cbd6f4;border-radius:8px;margin-bottom:18px}.notice.error{background:#fff0ea;border-color:#e1b5a7}.empty{text-align:center;padding:34px;color:var(--muted)}details{margin:8px 0}summary{cursor:pointer}pre{white-space:pre-wrap;font:14px/1.6 system-ui}.log{max-height:260px;overflow:auto}.log p{padding:9px 0;border-bottom:1px solid var(--line);font-size:14px}dialog{border:1px solid var(--line);border-radius:12px;max-width:650px;width:calc(100% - 32px);padding:26px;box-shadow:0 20px 100px #0004}dialog::backdrop{background:#20263888}dialog .body{max-height:55vh;overflow:auto}dialog li{margin:8px 0}.footnote{font-size:14px;color:var(--muted)}@media(max-width:1050px){.days{grid-template-columns:repeat(4,1fr)}.toolbar{align-items:flex-start;flex-direction:column}}@media(max-width:650px){.stats{grid-template-columns:repeat(2,1fr)}.days{grid-template-columns:repeat(2,1fr)}main{padding:18px 12px}.panel{padding:15px}.sectionhead{align-items:flex-start;flex-direction:column}header{padding:20px}header a{font-size:14px}h1{font-size:24px}}
</style><header><div><small>HOME BAKERY</small><h1>Order desk</h1></div><a href="/README.html" target="_blank">How to use this app</a></header>
<main><div class="toolbar"><div><h2 id="today">Loading your bakery...</h2><span id="loadstate" class="muted"></span></div><div class="actions"><button id="fetch" onclick="act('fetch')">Get today's messages</button><button id="next" class="primary" onclick="preview()">Accept eligible & end day</button></div></div>
<div id="notice" role="status" class="notice" hidden></div><div id="stats" class="stats"></div>
<section class="panel"><div class="sectionhead"><div><h2>Requests to review</h2><p class="muted">Check notice and remaining hours before saying yes.</p></div><label>Show <select id="requestfilter" onchange="renderRequests()"><option value="pending">Pending review</option><option value="all">All requests</option><option value="Applied">Applied changes / cancellations</option><option value="Rejected">Rejected</option></select></label></div><div id="requests" class="tablewrap"></div></section>
<section class="panel"><h2>Daily capacity</h2><p class="muted">Baked on pickup day. 8 hours per day, or 12 with a helper. Pickups at 5 PM.</p><div id="days" class="days"></div></section>
<section class="panel"><div class="sectionhead"><h2 id="orderheading">Orders</h2><label>Status <select id="orderfilter" onchange="renderOrders()"><option value="all">All statuses</option><option>Accepted</option><option>Picked up</option><option>Cancelled</option></select></label></div><div id="orders" class="tablewrap"></div></section>
<section class="panel"><h2>Activity log</h2><p class="muted">Changes, cancellations and decisions stay visible here.</p><div id="events" class="log"></div></section>
<p class="footnote">Classroom simulation · Local files only, no Instagram connection · All requests and helper bookings need 2 calendar days notice.<br>Cupcakes: $40 / dozen, 30 min. Cake: $120, 2 hours. Totals cover Nov 23-Dec 1; they are not a profit calculation.</p></main>
<dialog id="confirm"><h2 id="confirmtitle"></h2><div id="confirmbody" class="body"></div><div class="actions"><button onclick="document.getElementById('confirm').close()">Keep reviewing</button><button id="confirmyes" class="primary">Confirm</button></div></dialog>
<script>
const token='__TOKEN__';let state,selected='2026-11-25',busy=false;
const el=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=n=>new Intl.NumberFormat('en-CA',{style:'currency',currency:'CAD',maximumFractionDigits:0}).format(n/100),hours=n=>(n/60).toLocaleString('en-CA',{maximumFractionDigits:1})+' h';
const day=d=>new Date(d+'T12:00:00').toLocaleDateString('en-CA',{weekday:'short',month:'short',day:'numeric'});
function notify(s,error=false){el('notice').hidden=false;el('notice').className='notice'+(error?' error':'');el('notice').textContent=s}
async function api(path,args){let r=await fetch(path,args?{method:'POST',headers:{'Content-Type':'application/json','X-Bakery-Token':token},body:JSON.stringify(args)}:{});let d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d}
async function refresh(){state=await api('/api/state');render()}
async function act(action,args={}){if(busy)return;busy=true;el('confirm').close();notify('Saving...');try{let r=await api('/api/action',{action,...args});await refresh();notify(r.message)}catch(e){notify(e.message,true)}finally{busy=false}}
function confirmAction(title,body,action,args={}){el('confirmtitle').textContent=title;el('confirmbody').innerHTML=body;el('confirmyes').onclick=()=>act(action,args);el('confirm').showModal()}
async function preview(){try{const p=await api('/api/preview');confirmAction('Close today and continue?',`<p>Remaining requests are checked in arrival order. Incompatible requests are rejected. Today's orders become picked up at 5 PM. This cannot be undone.</p><ul>${p.events.map(s=>`<li>${esc(s)}</li>`).join('')}</ul>`,'advance')}catch(e){notify(e.message,true)}}
function helper(d){confirmAction('Book a helper?',`<p>${esc(day(d))}: pay <b>$100</b> for 4 extra hours. Capacity increases from 8 to 12 hours. One helper per day; booking cannot be undone.</p>`,'helper',{day:d})}
function reject(id){confirmAction('Reject this request?','<p>The request will stay in the history. This decision cannot be undone.</p>','reject',{id})}
function render(){el('today').textContent=state.finished?'Simulation complete':day(state.today)+', 2026';el('loadstate').textContent=state.finished?'All results saved locally.':state.loaded?'Today\'s messages loaded. Review requests, then end the day.':'Start by loading today\'s messages.';el('fetch').disabled=state.finished||state.loaded;el('next').disabled=state.finished||!state.loaded;
el('stats').innerHTML=[['Confirmed order value',money(state.value),'Accepted + picked up'],['Fulfilled sales',money(state.sales),'Picked-up orders only'],['Helper costs',money(state.costs),'All booked helpers'],['Value less helper costs',money(state.value-state.costs),'Excludes ingredients and other costs']].map(([a,b,d])=>`<div class="stat"><small>${a}</small><strong>${b}</strong><small>${d}</small></div>`).join('');
renderRequests();renderDays();renderOrders();el('events').innerHTML=state.events.length?state.events.map(e=>`<p><b>${esc(day(e.day))}</b> · ${esc(e.detail)}</p>`).join(''):'<div class="empty">Your decisions will appear here.</div>'}
function renderRequests(){let f=el('requestfilter').value;let rows=state.requests.filter(r=>f==='all'||(f==='pending'?['Requested','Incompatible'].includes(r.status):r.status===f));el('requests').innerHTML=rows.length?`<table><thead><tr><th>Customer / request</th><th>Pickup / value</th><th>Capacity & rule check</th><th>Decision</th></tr></thead><tbody>${rows.map(r=>`<tr><td><strong>${esc(r.customer)}</strong> <span class="tag">${esc(r.kind)}</span><small>${r.product?`${r.qty} ${r.product==='cupcakes'?'dozen cupcakes':'cake(s)'}`:'Cancel existing order'}</small><details><summary>Original message</summary><pre>${esc(r.raw)}</pre></details></td><td>${esc(r.pickup?day(r.pickup):'Unknown date')}<small>${r.product?money(r.value):'See original order'}</small></td><td>${r.kind==='New'?`Needs ${hours(r.minutes)} · ${hours(r.left)} available<small>${r.eligible?hours(r.left-r.minutes)+' left after acceptance':''}</small>`:''}<span class="tag ${r.eligible?'good':r.issue&&r.status!=='Applied'?'bad':''}">${esc(r.eligible?'Eligible · notice and capacity OK':r.issue||r.status)}</span></td><td>${['Requested','Incompatible'].includes(r.status)?`<div class="actions"><button class="primary" ${r.eligible?'':'disabled'} onclick="act('accept',{id:${r.id}})">Accept</button><button onclick="reject(${r.id})">Reject</button></div>`:`<span class="tag">${esc(r.status)}</span>`}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">'+(state.loaded?'No requests in this view.':'Load today\'s messages to begin.')+'</div>'}
function renderDays(){el('days').innerHTML=state.days.map(d=>`<div class="day ${selected===d.day?'selected':''}"><button class="selectday" aria-pressed="${selected===d.day}" onclick="selectDay('${d.day}')">${day(d.day)}</button><div class="meter"><span style="width:${100*d.used/d.limit}%"></span></div><b>${hours(d.limit-d.used)} left</b><small>${hours(d.used)} / ${hours(d.limit)} used</small><small>${d.count} orders</small><button class="helper" ${d.can_book?'':'disabled'} onclick="helper('${d.day}')">${d.helper?'Helper booked':'Hire helper · $100'}</button><small>${d.helper?'+4 h included':d.can_book?'Adds 4 hours':'Booking window closed'}</small></div>`).join('')}
function selectDay(d){selected=d;renderDays();renderOrders()}
function renderOrders(){let f=el('orderfilter').value;let rows=state.orders.filter(o=>o.pickup===selected&&(f==='all'||o.status===f));el('orderheading').textContent='Orders · '+day(selected);el('orders').innerHTML=rows.length?`<table><thead><tr><th>Order / customer</th><th>Product</th><th>Oven time</th><th>Value</th><th>Status</th></tr></thead><tbody>${rows.map(o=>`<tr><td>#${o.id} · ${esc(o.customer)}</td><td>${o.qty} ${o.product==='cupcakes'?'dozen cupcakes':'cake(s)'}</td><td>${hours(o.minutes)}</td><td>${money(o.value)}</td><td><span class="tag">${o.status}</span></td></tr>`).join('')}</tbody></table>`:'<div class="empty">No orders in this view.</div>'}
refresh().catch(e=>notify('Could not load your bakery: '+e.message,true));
</script></html>'''

def handler(bakery):
    token=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def respond(self,status,body,kind='application/json'):
            data=(json.dumps(body) if kind=='application/json' else body).encode('utf-8')
            self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data)
        def do_GET(self):
            try:
                path=urlparse(self.path).path
                if path=='/':self.respond(200,PAGE.replace('__TOKEN__',token),'text/html')
                elif path=='/README.html':self.respond(200,(ROOT/'README.html').read_text(encoding='utf-8'),'text/html')
                elif path=='/api/state':self.respond(200,bakery.state())
                elif path=='/api/preview':self.respond(200,bakery.preview())
                else:self.respond(404,{'error':'Not found'})
            except ValueError as e:self.respond(400,{'error':str(e)})
        def do_POST(self):
            if self.path!='/api/action':self.respond(404,{'error':'Not found'});return
            if self.headers.get('X-Bakery-Token')!=token:self.respond(403,{'error':'Reload this page before saving.'});return
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<10000:raise ValueError('Invalid request size.')
                args=json.loads(self.rfile.read(n))
                msg=bakery.action(args['action'],args)
                self.respond(200,{'message':msg})
            except (ValueError,KeyError,TypeError) as e:self.respond(400,{'error':str(e)})
    return Handler

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--db',default=str(ROOT/'bakery.db'));parser.add_argument('--port',type=int,default=8000);args=parser.parse_args()
    bakery=Bakery(args.db)
    print(f'Bakery app: http://127.0.0.1:{args.port} | Database: {args.db}',flush=True)
    ThreadingHTTPServer(('127.0.0.1',args.port),handler(bakery)).serve_forever()
