"""
Sportsbook pages on the stats website (see sportsbook.py for markets, odds and settling).

Bettors (no login: a cookie remembers them; a new visitor picks a name and gets
sportsbook.START_BALANCE in fake money):
    GET  /book                     open matchups
    GET  /book/matchups/<id>       every bet on one matchup
    GET  /book/bet/<selection>     bet slip;  POST places the bet
    GET  /book/parlay              parlay slip (picks collected with the + buttons);  POST places the parlay,
                                   round robins and/or each pick alone
    POST /book/parlay/add/<sel>, /book/parlay/remove/<sel>, /book/parlay/clear
    POST /book/alt/<market>        bet or slip a line picked on an over/under's slider
    GET  /book/feed                live feed of open bets (?view=history: settled ones);  POST /book/tail/<kind>/<id> copies one
    GET  /book/me                  my balance and bets
    GET  /book/leaderboard         everyone's money
    GET  /book/join;  POST /book/join
    POST /book/login, /book/logout log in with name + password (any device), log out
    POST /book/password            set or change my password
    POST /book/code                get an account back with its code (older accounts with no password)

Admin (user "admin", password ADMIN_PASSWORD in .env; off when it isn't set):
    GET  /admin                    login, house profit/loss, matchups, create a matchup
    GET  /admin/matchups/<id>      edit lines and odds, lock, add props/custom bets, settle
    GET  /admin/bettors            adjust balances
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import time
from urllib.parse import urlencode

from aiohttp import web

from .db import Database
from .embeds import Names
from . import players
from .site import _e, _page, _time
from .sportsbook import (MAX_PARLAY_LEGS, MAX_SLIP_BETS, START_BALANCE, Book, BookError, Models,
                         american_from_decimal, bettable_stats, decimal_odds, fmt_odds, stat_label)

BETTOR_COOKIE = "nflu_bettor"
ADMIN_COOKIE = "nflu_admin"
ADMIN_USER = "admin"
ADMIN_SESSION_SECONDS = 7 * 24 * 3600
# Wrong passwords allowed per address before logins from it are refused for a while
LOGIN_TRIES, LOGIN_WINDOW = 5, 600

CSS = """<style>
.bar{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;margin-bottom:14px}
.bar .money{font-size:20px;font-weight:700}
.mk{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)}
.mk:last-child{border-bottom:none}.mk .t{flex:1 1 220px}
a.sel{display:inline-flex;gap:8px;align-items:baseline;border:1px solid var(--line);border-radius:8px;
padding:5px 10px;background:var(--chip);color:var(--text)}a.sel:hover{border-color:var(--accent);text-decoration:none}
a.sel b,.odds{color:var(--accent);font-variant-numeric:tabular-nums}
span.sel{display:inline-flex;gap:8px;border:1px dashed var(--line);border-radius:8px;padding:5px 10px;color:var(--muted)}
.msg{background:var(--chip);border-left:4px solid var(--accent);padding:8px 12px;border-radius:6px;margin-bottom:14px}
.err{border-left-color:#c0392b}
.won{color:var(--win);font-weight:700}.lost{color:#c0392b}
form.stack{display:grid;gap:10px;max-width:420px}
input.n{width:90px}textarea{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--text);width:100%}
button.sec{background:var(--chip);color:var(--text);border:1px solid var(--line)}
button.bad{background:#c0392b}
.pick td{padding:4px 10px}.pick label{margin-right:10px;white-space:nowrap}
.pick-wrap{display:inline-flex;gap:4px;align-items:center;margin:2px 0}
form.addp{display:inline;margin:0}form.addp button{padding:5px 9px;line-height:1.2}
form.addp button.in{background:var(--win)}
.legs td{vertical-align:middle}
button.pk{display:inline-flex;gap:8px;align-items:baseline;border:1px solid var(--line);border-radius:8px;
padding:7px 12px;background:var(--chip);color:var(--text);font-weight:400}button.pk:hover{border-color:var(--accent)}
button.pk b{color:var(--accent);font-variant-numeric:tabular-nums}
.alt{flex-direction:column;align-items:stretch}.alt .row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.alt .ln{font-size:22px;font-weight:700;min-width:70px;font-variant-numeric:tabular-nums}
.ends{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:-6px}
input.big{-webkit-appearance:none;appearance:none;width:100%;height:14px;border-radius:7px;background:var(--line);
margin:16px 0;padding:0;border:none;cursor:pointer}
input.big::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:36px;height:36px;border-radius:50%;
background:var(--accent);border:4px solid var(--card);box-shadow:0 1px 6px rgba(0,0,0,.45)}
input.big::-moz-range-thumb{width:30px;height:30px;border-radius:50%;background:var(--accent);
border:4px solid var(--card);box-shadow:0 1px 6px rgba(0,0,0,.45)}
.pickbox select.add{display:block;width:100%;margin:6px 0}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.seg label{margin:0!important;position:relative}.seg input{position:absolute;opacity:0;width:1px;height:1px}
.seg span{display:block;padding:10px 18px;min-width:64px;text-align:center;cursor:pointer;user-select:none}
.seg label+label span{border-left:1px solid var(--line)}
.seg input:checked+span{background:var(--muted);color:var(--card);font-weight:700}
.seg input[value="1"]:checked+span{background:var(--accent);color:#fff}
.seg input[value="2"]:checked+span{background:#c0392b;color:#fff}
.seg input:focus-visible+span{outline:2px solid var(--accent)}
.teams{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:10px 0}
.teams ol{margin:6px 0 0;padding-left:22px;min-height:1.4em}.teams .t1 b{color:var(--accent)}.teams .t2 b{color:#c0392b}
.tabs{display:flex;gap:6px;margin:0 0 12px}.tabs a{padding:6px 14px;border:1px solid var(--line);border-radius:999px}
@keyframes flash{from{background:rgba(255,200,0,.35)}to{background:transparent}}
tr.new td{animation:flash 3s}
.tabs a.on{background:var(--accent);color:#fff;border-color:var(--accent)}
button.ad.in{background:var(--win)}
.filters{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;position:sticky;top:0;z-index:5}
.filters .fq{flex:1 1 220px}
#betcard{position:fixed;z-index:50;width:320px;max-width:calc(100vw - 20px);background:var(--card);color:var(--text);
border:1px solid var(--line);border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.35);display:none}
#betcard .bh{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line);cursor:move;
touch-action:none;user-select:none;font-weight:700}#betcard .bh span{flex:1}
#betcard .bh button{padding:2px 9px;background:var(--chip);color:var(--text);border:1px solid var(--line)}
#betcard .bb{padding:12px;display:grid;gap:10px}#betcard .pick{font-size:18px;font-weight:700}
#betcard .q{display:flex;gap:6px;flex-wrap:wrap}#betcard .q button{padding:4px 10px}
#betcard .bm{min-height:1.2em}
.sel.locked{opacity:.55;cursor:not-allowed}
#cartbtn{position:fixed;right:16px;bottom:16px;z-index:40;border-radius:999px;padding:12px 18px;
box-shadow:0 6px 20px rgba(0,0,0,.3);background:var(--chip);color:var(--text);border:1px solid var(--line)}
#cartbtn.has{background:var(--accent);color:#fff;border-color:var(--accent)}
#cartbtn .cn{display:inline-block;min-width:1.4em;padding:0 5px;border-radius:999px;background:rgba(255,255,255,.25)}
#cart{position:fixed;right:16px;bottom:74px;z-index:45;width:360px;max-width:calc(100vw - 32px);max-height:70vh;
display:none;flex-direction:column;background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:12px;
box-shadow:0 12px 40px rgba(0,0,0,.35)}
#cart .ch{display:flex;gap:8px;align-items:center;padding:10px 12px;border-bottom:1px solid var(--line)}
#cart .ch b{white-space:nowrap}#cart .ch .cc{flex:1}#cart .ch .x{padding:2px 9px}
#cart .cl{overflow:auto;flex:1}#cart .ci{display:flex;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line)}
#cart .ci>div{flex:1}#cart .ci button{padding:2px 9px}
#cart .cf{display:grid;gap:8px;padding:10px 12px}#cart .cs{width:100%}
.opts label{display:flex;gap:8px;align-items:center;margin:4px 0}
</style>"""

_STATUS = {"open": "Open", "locked": "Betting closed", "settled": "Settled", "void": "Void"}

# Bettor pages check /book/version every few seconds and reload when the admin changed anything,
# unless the bettor is typing a stake (then a banner offers the reload instead).
LIVE_JS = """<div id="live" class="msg" style="display:none;position:sticky;bottom:10px">Odds or bets just changed.
<a href="" onclick="location.reload();return false">Reload to see them</a></div>
<script>(function(){var v=null;function busy(){var a=document.activeElement;if(a&&a.tagName=='INPUT')return true;
var s=document.getElementById('stake'),c=document.getElementById('betcard');
var k=document.getElementById('cart');
return !!(s&&s.value)||!!(c&&c.style.display==='block')||!!(k&&k.style.display==='flex');}
function tick(){if(document.hidden)return;fetch('/book/version',{cache:'no-store'}).then(function(r){return r.json()})
.then(function(d){if(v===null){v=d.v;return;}if(d.v!==v){if(busy()){document.getElementById('live').style.display='block';}
else{location.reload();}}}).catch(function(){});}
tick();setInterval(tick,5000);document.addEventListener('visibilitychange',tick);})();</script>"""


# Everyone's bets: when anything changes, fetch the list again and swap it in, lighting up new rows
FEED_JS = """<script>(function(){var v=null,box=document.getElementById('feed');
function keys(){var o={};box.querySelectorAll('tr[data-k]').forEach(function(t){o[t.dataset.k]=1});return o;}
function tick(){if(document.hidden)return;fetch('/book/version',{cache:'no-store'}).then(function(r){return r.json()})
.then(function(d){var n=d.v+'|'+d.f;if(v===null){v=n;return;}if(n===v)return;v=n;var old=keys();
return fetch(location.href,{headers:{'X-Requested-With':'fetch'},cache:'no-store'}).then(function(r){return r.text()})
.then(function(h){box.innerHTML=h;box.querySelectorAll('time[datetime]').forEach(function(t){
var x=new Date(t.getAttribute('datetime'));if(!isNaN(x))t.textContent=x.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'});});
box.querySelectorAll('tr[data-k]').forEach(function(t){if(!old[t.dataset.k])
t.classList.add('new')});});}).catch(function(){});}
tick();setInterval(tick,4000);document.addEventListener('visibilitychange',tick);})();</script>"""


# Over/under sliders: moving one shows that line's odds and points the buttons at it
SLIDER_JS = """<script>(function(){function fo(o){return(o>0?'+':'')+o}
document.querySelectorAll('form.alt').forEach(function(f){
var r=f.querySelector('input[type=range]');function u(){var L=JSON.parse(f.dataset.l),slip=JSON.parse(f.dataset.slip),x=L[+r.value];f.querySelector('.ln').textContent=x[0];
f.elements.line.value=x[0];var lb=f.querySelectorAll('.lb'),b=f.querySelectorAll('button.pk b'),ad=f.querySelectorAll('button.ad');
['Over','Under'].forEach(function(w,j){lb[j].textContent=w+' '+x[0];b[j].textContent=fo(x[1+j]);
var inn=x[3+j]!==null&&slip.indexOf(x[3+j])>=0;ad[j].textContent=inn?'\u2713':'+';ad[j].className=(inn?'in':'sec')+' ad';});}
r.addEventListener('input',u);u();});})();</script>"""


# Search box + filter buttons (see BookSite._filter_bar)
FILTER_JS = """<script>(function(){var bar=document.currentScript.previousElementSibling,q=bar.querySelector('.fq'),
t=bar.querySelector('.ft'),n=bar.querySelector('.fn'),kind='';
function run(){var s=q.value.trim().toLowerCase(),tm=t?t.value:'',shown=0,all=document.querySelectorAll('.fr');
all.forEach(function(r){var ok=(!kind||r.dataset.k===kind)&&(!tm||r.dataset.t===tm)&&
s.split(/\\s+/).every(function(w){return r.dataset.q.indexOf(w)>=0});r.style.display=ok?'':'none';if(ok)shown++;});
document.querySelectorAll('.fg').forEach(function(g){g.style.display=g.querySelector('.fr:not([style*="none"])')?'':'none'});
n.textContent=(s||kind||tm)?shown+' of '+all.length:'';}
bar.querySelectorAll('[data-f]').forEach(function(a){a.addEventListener('click',function(e){e.preventDefault();
kind=a.dataset.f;bar.querySelectorAll('[data-f]').forEach(function(b){b.className=b===a?'on':''});run();});});
q.addEventListener('input',run);if(t)t.addEventListener('change',run);})();</script>"""

# The bet card: tapping odds opens a small card you can drag anywhere (it remembers where) to bet
# without leaving the page. Links still work without JavaScript (they open /book/bet/<id>).
BETCARD_JS = """<script>(function(){var c=document.getElementById('betcard'),cs=c.dataset.csrf,sel=null;
var H={'X-Requested-With':'fetch'};function $(x){return c.querySelector(x)}
function f(v){return '$'+v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}
function fo(o){return(o>0?'+':'')+o}function stake(){return parseFloat(($('.st').value||'').replace(/[$,]/g,''))||0}
function upd(){if(!sel)return;var s=stake(),d=sel.decimal;$('.win').textContent=f(s*(d-1));$('.ret').textContent=f(s*d);}
function msg(t,bad){var m=$('.bm');m.textContent=t;m.className='bm small '+(bad?'lost':'won');}
function money(b){var e=document.querySelector('.bar .money');if(e&&b!=null)e.textContent=f(b);}
function slipCount(n){document.querySelectorAll('a[href="/book/parlay"]').forEach(function(a){
if(/slip/i.test(a.textContent))a.textContent=a.textContent.replace(/\\(\\d+\\)/,'('+n+')');});}
function place(){try{var p=JSON.parse(localStorage.getItem('betcard-pos'));if(p){c.style.left=Math.min(p[0],innerWidth-60)+'px';
c.style.top=Math.min(p[1],innerHeight-60)+'px';return;}}catch(e){}c.style.left=Math.max(10,innerWidth-340)+'px';c.style.top='90px';}
function open(id){fetch('/book/api/sel/'+id,{headers:H,cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
if(!d.ok){location.href='/book/bet/'+id;return;}sel=d;$('.mt').textContent=d.matchup;$('.tt').textContent=d.title;
$('.pick').innerHTML='';$('.pick').append(d.label+' ');var o=document.createElement('span');o.className='odds';
o.textContent=fo(d.odds);$('.pick').append(o);$('.bal').textContent=f(d.balance);$('.ad').textContent=d.in_slip?
'\\u2713 In your parlay slip':'+ Add to parlay';$('.ad').disabled=d.in_slip;msg('');
$('.go').disabled=!d.open||!!d.why;if(d.why){$('.ad').disabled=true;msg(d.why,true);}
else if(!d.open)msg('Betting on this is closed.',true);c.style.display='block';place();upd();$('.st').focus();});}
document.addEventListener('click',function(e){var a=e.target.closest('a.sel[href^="/book/bet/"]');
if(a&&!e.ctrlKey&&!e.metaKey){e.preventDefault();open(a.getAttribute('href').split('/').pop());return;}
var b=e.target.closest('form.alt button[value$=":bet"]');if(b){e.preventDefault();var fm=new FormData(b.form);
fm.set('pick',b.value);fetch(b.form.action,{method:'POST',body:fm,headers:H}).then(function(r){return r.json()})
.then(function(d){if(d.ok)open(d.sel);else alertless(d.msg);});}});
function alertless(t){c.style.display='block';place();msg(t,true);}
$('.x').addEventListener('click',function(){c.style.display='none'});
document.addEventListener('keydown',function(e){if(e.key==='Escape')c.style.display='none'});
$('.st').addEventListener('input',upd);
c.querySelectorAll('.q button').forEach(function(b){b.addEventListener('click',function(){
$('.st').value=b.dataset.v==='max'?String(sel?sel.balance:0):String(stake()+(+b.dataset.v));upd();});});
$('.go').addEventListener('click',function(){if(!sel)return;var fd=new FormData();fd.set('csrf',cs);fd.set('odds',sel.odds);
fd.set('stake',$('.st').value);$('.go').disabled=true;fetch('/book/bet/'+sel.id,{method:'POST',body:fd,headers:H})
.then(function(r){return r.json()}).then(function(d){$('.go').disabled=false;msg(d.msg,!d.ok);money(d.balance);
if(d.ok){sel.balance=d.balance;$('.bal').textContent=f(d.balance);$('.st').value='';upd();}else if(d.odds){sel.odds=d.odds;
sel.decimal=d.decimal;$('.pick .odds').textContent=fo(d.odds);upd();}});});
$('.ad').addEventListener('click',function(){if(!sel)return;var fd=new FormData();fd.set('csrf',cs);fd.set('next','/book');
fetch('/book/parlay/add/'+sel.id,{method:'POST',body:fd,headers:H}).then(function(r){return r.json()}).then(function(d){
msg(d.msg,!d.ok);if(d.ok){$('.ad').textContent='\\u2713 In your parlay slip';$('.ad').disabled=true;slipCount(d.count);
if(window.bookCart)window.bookCart.refresh();}});});
var h=$('.bh'),dx=0,dy=0,drag=false;h.addEventListener('pointerdown',function(e){if(e.target.closest('button'))return;
drag=true;dx=e.clientX-c.offsetLeft;dy=e.clientY-c.offsetTop;h.setPointerCapture(e.pointerId);});
h.addEventListener('pointermove',function(e){if(!drag)return;var x=Math.max(0,Math.min(innerWidth-60,e.clientX-dx)),
y=Math.max(0,Math.min(innerHeight-40,e.clientY-dy));c.style.left=x+'px';c.style.top=y+'px';});
h.addEventListener('pointerup',function(){if(!drag)return;drag=false;try{localStorage.setItem('betcard-pos',
JSON.stringify([c.offsetLeft,c.offsetTop]));}catch(e){}});})();</script>"""

# The parlay cart: a button that follows you on every sportsbook page, opening a panel with the picks
# in your slip. The + buttons add and remove picks without reloading, and the parlay is placed from
# the panel; the full slip page (/book/parlay) is still there for round robins.
CART_HTML = """<button id="cartbtn" type="button" aria-label="Parlay slip">🧾 Parlay <b class="cn">0</b></button>
<div id="cart" role="dialog" aria-label="Parlay slip"><div class="ch"><b>Parlay slip</b>
<span class="small muted cc"></span><button type="button" class="sec x" aria-label="Close">✕</button></div>
<div class="cl"></div><div class="cf"><div class="co"></div>
<label class="small">Stake ($) <input class="cs" inputmode="decimal" placeholder="100" autocomplete="off"></label>
<div class="small">To win <b class="cw">$0.00</b> · paid back <b class="cr">$0.00</b></div>
<button type="button" class="cp">Place parlay</button><div class="cm small"></div>
<div class="small"><a href="/book/parlay">Round robins and more →</a> · <a href="#" class="cx">Clear</a></div></div></div>"""

CART_JS = """<script>(function(){var c=document.getElementById('cart'),btn=document.getElementById('cartbtn'),
cs=document.getElementById('betcard').dataset.csrf,H={'X-Requested-With':'fetch'},S=null;function $(x){return c.querySelector(x)}
function f(v){return '$'+v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}function fo(o){return(o>0?'+':'')+o}
function post(url,data){var fd=new FormData();fd.set('csrf',cs);fd.set('next','/book');for(var k in data)fd.set(k,data[k]);
return fetch(url,{method:'POST',body:fd,headers:H}).then(function(r){return r.json()});}
function show(o){c.style.display=o?'flex':'none';try{sessionStorage.setItem('cart-open',o?'1':'')}catch(e){}}
function upd(){if(!S)return;var s=parseFloat(($('.cs').value||'').replace(/[$,]/g,''))||0,d=S.decimal;
$('.cw').textContent=f(s*(d-1));$('.cr').textContent=f(s*d);}
function mark(ids){document.querySelectorAll('form.addp').forEach(function(fm){var m=fm.action.match(/\\/parlay\\/(add|remove)\\/(\\d+)/);
if(!m)return;var inn=ids.indexOf(+m[2])>=0,b=fm.querySelector('button');fm.action='/book/parlay/'+(inn?'remove':'add')+'/'+m[2];
if(!/tail/.test(fm.action)){b.textContent=inn?'\\u2713':'+';b.className=inn?'in':'sec';}});
document.querySelectorAll('form.alt').forEach(function(fm){fm.dataset.slip=JSON.stringify(ids);
var r=fm.querySelector('input[type=range]');if(r)r.dispatchEvent(new Event('input'));});
document.querySelectorAll('a[href="/book/parlay"]').forEach(function(a){if(/slip \\(/i.test(a.textContent))
a.textContent=a.textContent.replace(/\\(\\d+\\)/,'('+ids.length+')');});}
function draw(d){S=d;var n=d.legs.length;btn.querySelector('.cn').textContent=n;btn.className=n?'has':'';
$('.cc').textContent=n?n+' pick'+(n==1?'':'s'):'';var l=$('.cl');l.innerHTML='';
if(!n){l.innerHTML='<p class="small muted" style="margin:10px 12px">Empty. Tap <b>+</b> next to any pick to add it.</p>';}
d.legs.forEach(function(g){var row=document.createElement('div');row.className='ci';var t=document.createElement('div');
t.innerHTML='<div class="small muted"></div><div></div>';t.firstChild.textContent=g.matchup+' \\u00b7 '+g.title;
t.lastChild.textContent=g.label+' ';var o=document.createElement('span');o.className='odds';o.textContent=fo(g.odds);
t.lastChild.appendChild(o);if(!g.open){var z=document.createElement('span');z.className='lost small';z.textContent=' betting closed';
t.lastChild.appendChild(z);}var x=document.createElement('button');x.type='button';x.className='sec';x.textContent='\\u2715';
x.title='Remove';x.onclick=function(){post('/book/parlay/remove/'+g.id).then(refresh)};row.appendChild(t);row.appendChild(x);l.appendChild(row);});
$('.co').innerHTML=n>=2?'<b>'+n+'-pick parlay</b> <span class="odds">'+fo(d.american)+'</span>':
'<span class="small muted">'+(n?'Add one more pick to make a parlay.':'')+'</span>';$('.cp').disabled=n<2;
$('.cf').style.display=n?'grid':'none';mark(d.legs.map(function(g){return g.id}));upd();}
function refresh(openIt){return fetch('/book/api/slip',{headers:H,cache:'no-store'}).then(function(r){return r.json()})
.then(function(d){if(d.ok){draw(d);if(openIt===true)show(true);}});}
window.bookCart={refresh:refresh};
btn.onclick=function(){show(c.style.display!=='flex')};$('.x').onclick=function(){show(false)};$('.cs').oninput=upd;
$('.cx').onclick=function(e){e.preventDefault();post('/book/parlay/clear').then(refresh)};
$('.cp').onclick=function(){if(!S)return;$('.cp').disabled=true;post('/book/parlay',{stake:$('.cs').value,seen:S.seen,
size:S.legs.length}).then(function(d){var m=$('.cm');m.textContent=d.msg;m.className='cm small '+(d.ok?'won':'lost');
if(d.ok){$('.cs').value='';var e=document.querySelector('.bar .money');if(e)e.textContent=f(d.balance);}refresh();});};
document.addEventListener('submit',function(e){var fm=e.target;if(!fm.matches('form.addp')||/tail/.test(fm.action))return;
e.preventDefault();post(fm.action,{}).then(function(d){if(!d.ok){alertMsg(d.msg)}refresh(/\\/add\\//.test(fm.action));});});
document.addEventListener('click',function(e){var b=e.target.closest('form.alt button[value$=":parlay"]');if(!b)return;
e.preventDefault();var fd=new FormData(b.form);fd.set('pick',b.value);fetch(b.form.action,{method:'POST',body:fd,headers:H})
.then(function(r){return r.json()}).then(function(d){if(!d.ok){alertMsg(d.msg);return;}if(d.sel){var L=JSON.parse(b.form.dataset.l);
L.forEach(function(x){if(x[0]==d.line)x[d.side==='over'?3:4]=d.sel});b.form.dataset.l=JSON.stringify(L);}refresh(d.in_slip);});},true);
function alertMsg(t){show(true);var m=$('.cm');m.textContent=t;m.className='cm small lost';$('.cf').style.display='grid';}
refresh();try{if(sessionStorage.getItem('cart-open'))show(true)}catch(e){}})();</script>"""

BETCARD_HTML = """<div id="betcard" role="dialog" aria-label="Bet slip" data-csrf="{csrf}">
<div class="bh" title="Drag to move"><span>⠿ Bet slip</span><button class="x" type="button" aria-label="Close">✕</button></div>
<div class="bb"><div><div class="small muted mt"></div><div class="small tt"></div><div class="pick"></div></div>
<label class="small">Stake ($) <span class="muted">· you have <b class="bal"></b></span><br>
<input class="st" inputmode="decimal" placeholder="100" autocomplete="off"></label>
<div class="q"><button type="button" class="sec" data-v="10">+$10</button><button type="button" class="sec" data-v="50">+$50</button>
<button type="button" class="sec" data-v="100">+$100</button><button type="button" class="sec" data-v="500">+$500</button>
<button type="button" class="sec" data-v="max">Max</button></div>
<div class="small">To win <b class="win">$0.00</b> · paid back <b class="ret">$0.00</b></div>
<button type="button" class="go">Place bet</button><button type="button" class="sec ad">+ Add to parlay</button>
<div class="bm small"></div></div></div>"""


def _money(v: float) -> str:
    return f"-${-v:,.2f}" if v < 0 else f"${v:,.2f}"


def _int(v, default=None):
    try:
        return int(str(v).strip().lstrip("+"))
    except (TypeError, ValueError):
        return default


def _float(v, default=None):
    try:
        return float(str(v).strip().replace(",", "").lstrip("$"))
    except (TypeError, ValueError):
        return default


def _redirect(path: str, msg: str | None = None, err: bool = False) -> web.HTTPSeeOther:
    if msg:
        path += ("&" if "?" in path else "?") + urlencode({"err" if err else "msg": msg})
    return web.HTTPSeeOther(path)


def _flash(request: web.Request) -> str:
    if m := request.query.get("msg"):
        return f'<div class="msg">{_e(m)}</div>'
    if m := request.query.get("err"):
        return f'<div class="msg err">{_e(m)}</div>'
    return ""


def _ajax(request: web.Request) -> bool:
    """A request from the bet card's script, which wants JSON back instead of a page."""
    return request.headers.get("X-Requested-With") == "fetch"


def _secure(request: web.Request) -> bool:
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


class BookSite:
    def __init__(self, db: Database, secret: str, admin_password: str | None = None):
        self.db = db
        self.book = Book(db)
        self.admin_password = admin_password if admin_password is not None else os.getenv("ADMIN_PASSWORD", "")
        self.key = hashlib.sha256(f"nflu-book:{secret}".encode()).digest()
        # Changing ADMIN_PASSWORD logs every admin session out
        self.admin_key = hashlib.sha256(f"nflu-admin:{secret}:{self.admin_password}".encode()).digest()
        self.failures: dict[str, list[float]] = {}

    def routes(self) -> list[web.RouteDef]:
        return [
            web.get("/book", self.index),
            web.get("/book/join", self.join_form),
            web.post("/book/join", self.join),
            web.post("/book/code", self.use_code),
            web.post("/book/login", self.bettor_login),
            web.post("/book/logout", self.bettor_logout),
            web.post("/book/password", self.set_password),
            web.get("/book/matchups/{id}", self.matchup_page),
            web.get("/book/bet/{id}", self.slip),
            web.post("/book/bet/{id}", self.bet),
            web.get("/book/parlay", self.parlay_page),
            web.post("/book/parlay", self.parlay_place),
            web.post("/book/parlay/add/{id}", self.parlay_add),
            web.post("/book/parlay/remove/{id}", self.parlay_remove),
            web.post("/book/parlay/clear", self.parlay_clear),
            web.post("/book/alt/{id}", self.alt_pick),
            web.get("/book/feed", self.feed),
            web.get("/book/api/sel/{id}", self.api_selection),
            web.get("/book/api/slip", self.api_slip),
            web.post("/book/tail/{kind}/{id}", self.tail),
            web.get("/book/me", self.me),
            web.get("/book/leaderboard", self.leaderboard),
            web.get("/book/version", self.version),
            web.get("/admin", self.admin),
            web.post("/admin/login", self.admin_login),
            web.post("/admin/logout", self.admin_logout),
            web.post("/admin/matchups", self.admin_create),
            web.get("/admin/matchups/{id}", self.admin_matchup),
            web.post("/admin/matchups/{id}", self.admin_matchup_action),
            web.post("/admin/markets/{id}", self.admin_market_action),
            web.get("/admin/bettors", self.admin_bettors),
            web.post("/admin/bettors/{id}", self.admin_bettor_action),
        ]

    # ------------------------------------------------------------- security
    def _sign(self, key: bytes, value: str) -> str:
        return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:32]

    def _csrf(self, request: web.Request, cookie: str) -> str:
        return self._sign(self.key, "csrf:" + (request.cookies.get(cookie) or ""))

    def _check_csrf(self, request: web.Request, form, cookie: str) -> None:
        if not hmac.compare_digest(str(form.get("csrf", "")), self._csrf(request, cookie)):
            raise web.HTTPForbidden(text="This form expired. Go back, reload the page and try again.")

    def _hidden(self, request: web.Request, cookie: str) -> str:
        return f'<input type="hidden" name="csrf" value="{self._csrf(request, cookie)}">'

    def _bettor(self, request: web.Request):
        return self.book.bettor_by_token(request.cookies.get(BETTOR_COOKIE))

    def _is_admin(self, request: web.Request) -> bool:
        if not self.admin_password:
            return False
        exp, _, sig = (request.cookies.get(ADMIN_COOKIE) or "").partition(".")
        return (exp.isdigit() and int(exp) > time.time()
                and hmac.compare_digest(sig, self._sign(self.admin_key, exp)))

    def _need_admin(self, request: web.Request) -> None:
        if not self._is_admin(request):
            raise web.HTTPSeeOther("/admin")

    # --------------------------------------------------------------- shared
    def _names(self):
        """username_key -> 'Real Name' (or their Roblox username), for keys with or without games."""
        names = Names(self.db)
        known = {u.lower(): u for u in self.db.known_usernames()}
        return lambda key: names(known.get(key, key))

    def _title(self, m: dict, name=None) -> str:
        """A market's title; props use the player's current name, so names set later show up."""
        if m.get("kind") == "prop" and m.get("player_key"):
            return f"{(name or self._names())(m['player_key'])}: {stat_label(m['stat'])}"
        return m.get("market_title") or m["title"]

    def _bar(self, request: web.Request, bettor) -> str:
        if not bettor:
            return ""
        return (f'<div class="card bar"><span>👋 {_e(bettor["name"])}</span>'
                f'<span class="money">{_money(bettor["balance"])}</span>'
                f'<a href="/book">Bets</a><a href="/book/parlay">Parlay slip ({len(self._slip_ids(bettor))})</a>'
                f'<a href="/book/me">My bets</a><a href="/book/feed">Everyone\'s bets</a>'
                f'<a href="/book/leaderboard">Leaderboard</a>'
                f'<form method="post" action="/book/logout" style="margin:0 0 0 auto">'
                f'{self._hidden(request, BETTOR_COOKIE)}<button class="sec">Log out</button></form></div>'
                + BETCARD_HTML.replace("{csrf}", self._csrf(request, BETTOR_COOKIE)) + BETCARD_JS
                + ("" if request.path == "/book/parlay" else CART_HTML + CART_JS))

    def _slip_ids(self, bettor) -> set[int]:
        return {r["selection_id"] for r in self.db.q("SELECT selection_id FROM book_slip WHERE bettor_id = ?",
                                                     (bettor["id"],))}

    def _parlay_ctx(self, request: web.Request, bettor) -> dict:
        """What the + (add to parlay) buttons on a page need."""
        blocked: dict[int, str] = {}
        for mu in self.book.matchups(("open", "locked")):
            blocked.update(self.book.blocked(bettor["id"], mu["id"]))
        return {"csrf": self._hidden(request, BETTOR_COOKIE), "next": request.path,
                "slip": self._slip_ids(bettor), "blocked": blocked}

    @staticmethod
    def _sel_button(s: dict, open_: bool, ctx: dict | None = None) -> str:
        inner = f'<span>{_e(s["label"])}</span><b>{fmt_odds(s["odds"])}</b>'
        if open_ and ctx and (why := ctx["blocked"].get(s["id"])):
            return f'<span class="sel locked" title="{_e(why)}">🔒 {inner}</span>'
        if open_:
            link = f'<a class="sel" href="/book/bet/{s["id"]}" title="Bet on this alone">{inner}</a>'
            if not ctx:
                return link
            inside = s["id"] in ctx["slip"]
            action = "remove" if inside else "add"
            tip = "In your parlay slip (tap to take it out)" if inside else "Add to parlay"
            return (f'<span class="pick-wrap">{link}<form class="addp" method="post" '
                    f'action="/book/parlay/{action}/{s["id"]}">{ctx["csrf"]}'
                    f'<input type="hidden" name="next" value="{_e(ctx["next"])}">'
                    f'<button class="{"in" if inside else "sec"}" title="{tip}" aria-label="{tip}">'
                    f'{"✓" if inside else "+"}</button></form></span>')
        mark = {"win": " ✅", "lose": "", "push": " ↩", "void": " ↩"}.get(s.get("outcome") or "", "")
        return f'<span class="sel">{inner}{mark}</span>'

    def _market_row(self, m: dict, mu: dict, name, ctx: dict | None = None) -> str:
        open_ = m["status"] == "open" and mu["status"] == "open"
        note = (f'<div class="small muted">{_e(m["result"])}</div>' if m["result"]
                else "" if open_ else f'<div class="small muted">{_STATUS[m["status"]]}</div>')
        sels = " ".join(self._sel_button(s, open_, ctx) for s in m["selections"])
        return f'<div class="mk"><div class="t">{_e(self._title(m, name))}{note}</div><div>{sels}</div></div>'

    def _page(self, title: str, body: str, live: bool = False) -> web.Response:
        return _page(title, CSS + body + (LIVE_JS if live else ""))

    def _join_html(self, nxt: str = "/book", error: str = "", name: str = "") -> str:
        err = f'<div class="msg err">{_e(error)}</div>' if error else ""
        nx = f'<input type="hidden" name="next" value="{_e(nxt)}">'
        return (f"<h1>Sportsbook</h1>{err}<div class='grid'><div class='card'><h2 style='margin-top:0'>Log in</h2>"
                f'<form class="stack" method="post" action="/book/login">{nx}'
                f'<input name="name" maxlength="24" required placeholder="Your name" autocomplete="username" '
                f'list="league-players" value="{_e(name)}">{self._players_list()}'
                f'<input name="password" type="password" required placeholder="Password" '
                f'autocomplete="current-password"><button>Log in</button></form>'
                f"<p class='small muted'>League players: pick your name and type the password the bot's "
                f"<b>/password</b> command shows you in Discord (only you can see it).</p></div>"
                f"<div class='card'><h2 style='margin-top:0'>New here?</h2><p>Bet fake money on league games. "
                f"Make an account and you start with <b>{_money(START_BALANCE)}</b>.</p>"
                f'<form class="stack" method="post" action="/book/join">{nx}'
                f'<input name="name" maxlength="24" required placeholder="Pick a name" autocomplete="username">'
                f'<input name="password" type="password" required minlength="4" placeholder="Pick a password" '
                f'autocomplete="new-password"><button>Start betting</button></form></div></div>'
                f"<div class='card'><p class='small'>Made an account before passwords existed? Log in with its "
                f"name and a new password to claim it (your money and bets stay), or enter its account code.</p>"
                f'<form class="inline" method="post" action="/book/code">'
                f'<input type="hidden" name="next" value="{_e(nxt)}">'
                f'<input name="code" maxlength="12" required placeholder="ABCD-2345" autocomplete="off">'
                f"<button class='sec'>Use my account</button></form></div>")

    def _players_list(self) -> str:
        """Autocomplete for the login name: every league player who has a sportsbook password."""
        names = sorted(p.name for p in players.players() if players.book_password(self.db, p))
        return ('<datalist id="league-players">' + "".join(f'<option value="{_e(n)}">' for n in names)
                + "</datalist>")

    # -------------------------------------------------------------- bettors
    async def index(self, request: web.Request) -> web.Response:
        bettor = self._bettor(request)
        if not bettor:
            return self._page("Sportsbook", _flash(request) + self._join_html())
        cards, name, ctx = [], self._names(), self._parlay_ctx(request, bettor)
        live = self.book.matchups(("open", "locked"))
        for mu in live:
            markets = self.book.markets(mu["id"])
            markets = [m for m in markets if not m["parent_id"]]
            main = [m for m in markets if m["kind"] in ("moneyline", "spread") or (m["kind"] == "total"
                                                                                  and not m["stat"])]
            n_props = len(markets) - len(main)
            text = " ".join([mu["title"], mu["team1"], mu["team2"]] + [name(k) for k in mu["players1"] + mu["players2"]])
            cards.append(
                f'<div class="card fr" data-k="{mu["status"]}" data-q="{_e(text.lower())}">'
                f'<h2 style="margin-top:0"><a href="/book/matchups/{mu["id"]}">#{mu["id"]} {_e(mu["title"])}</a> '
                f'<span class="chip">{_STATUS[mu["status"]]}</span></h2>'
                f'<p class="small muted" style="margin-top:-6px">{self._lineups(mu, name)}</p>'
                + "".join(self._market_row(m, mu, name, ctx) for m in main)
                + f'<p class="small"><a href="/book/matchups/{mu["id"]}">All {len(markets)} bets'
                  f'{f" ({n_props} player props and more)" if n_props else ""} →</a></p></div>')
        recent = self.book.matchups(("settled",))[:5]
        past = "".join(f'<li><a href="/book/matchups/{mu["id"]}">{_e(mu["title"])}</a></li>' for mu in recent)
        tools = self._filter_bar("Search games or players", [("", "All"), ("open", "Open"),
                                                              ("locked", "Betting closed")]) if len(live) > 1 else ""
        body = (self._bar(request, bettor) + _flash(request) + "<h1>Sportsbook</h1>" + tools
                + ("".join(cards) or "<p class='muted'>No games are open for betting right now. Check back soon.</p>")
                + (f"<h2>Recently settled</h2><ul>{past}</ul>" if past else ""))
        return self._page("Sportsbook", body, live=True)

    async def join_form(self, request: web.Request) -> web.Response:
        if self._bettor(request):
            raise web.HTTPSeeOther("/book")
        return self._page("Join", self._join_html(self._safe_next(request.query.get("next"))))

    @staticmethod
    def _safe_next(nxt: str | None) -> str:
        return nxt if nxt and nxt.startswith("/book") and "//" not in nxt else "/book"

    async def join(self, request: web.Request) -> web.Response:
        form = await request.post()
        nxt = self._safe_next(form.get("next"))
        if self._bettor(request):
            raise web.HTTPSeeOther(nxt)
        name = str(form.get("name", ""))
        if (p := players.find(name)) and players.book_password(self.db, p):
            return self._page("Join", self._join_html(
                nxt, f"{p.name} is a league player. Log in with your name and the password /password shows "
                     f"you in Discord.", p.name))
        try:
            _, token = self.book.create_bettor(name, str(form.get("password", "")))
        except BookError as e:
            return self._page("Join", self._join_html(nxt, str(e)))
        raise self._login_bettor(request, token, _redirect(
            nxt, f"Welcome! You have {_money(START_BALANCE)} to bet. Log in with your name and password "
                 f"on any device."))

    async def bettor_login(self, request: web.Request) -> web.Response:
        form = await request.post()
        nxt = self._safe_next(form.get("next"))
        name = str(form.get("name", ""))
        ip = "login:" + (request.headers.get("CF-Connecting-IP") or request.remote or "?")
        if self._too_many(ip):
            return self._page("Log in", self._join_html(nxt, "Too many wrong passwords. Wait 10 minutes.", name))
        player = players.find(name)
        preset = players.book_password(self.db, player) if player else None
        try:
            b, claimed = self.book.login(player.name if player else name, str(form.get("password", "")), preset)
        except BookError as e:  # claiming an old account with too short a password
            return self._page("Log in", self._join_html(nxt, str(e), name))
        if not b:
            self._too_many(ip, failed=True)
            await asyncio.sleep(1)
            return self._page("Log in", self._join_html(nxt, "Wrong name or password.", name))
        msg = (f"Welcome back, {b['name']}! This account had no password, so the one you typed is its "
               f"password now." if claimed else f"Welcome back, {b['name']}!")
        raise self._login_bettor(request, b["token"], _redirect(nxt, msg))

    async def bettor_logout(self, request: web.Request) -> web.Response:
        form = await request.post()
        self._check_csrf(request, form, BETTOR_COOKIE)
        resp = _redirect("/book", "Logged out.")
        resp.del_cookie(BETTOR_COOKIE, path="/")
        raise resp

    async def set_password(self, request: web.Request) -> web.Response:
        bettor = self._require_bettor(request)
        form = await request.post()
        self._check_csrf(request, form, BETTOR_COOKIE)
        try:
            self.book.set_password(bettor["id"], str(form.get("password", "")))
        except BookError as e:
            raise _redirect("/book/me", str(e), err=True)
        raise _redirect("/book/me", "Password saved. Log in with your name and it on any device.")

    @staticmethod
    def _login_bettor(request: web.Request, token: str, resp: web.HTTPException) -> web.HTTPException:
        resp.set_cookie(BETTOR_COOKIE, token, max_age=10 * 365 * 24 * 3600, httponly=True, samesite="Lax",
                        secure=_secure(request), path="/")
        return resp

    def _too_many(self, key: str, failed: bool = False) -> bool:
        """Per-address limit on wrong passwords / account codes (LOGIN_TRIES per LOGIN_WINDOW)."""
        now = time.time()
        recent = [t for t in self.failures.get(key, []) if now - t < LOGIN_WINDOW]
        if failed:
            recent.append(now)
        self.failures[key] = recent
        return len(recent) >= LOGIN_TRIES

    async def use_code(self, request: web.Request) -> web.Response:
        form = await request.post()
        nxt = self._safe_next(form.get("next"))
        ip = "code:" + (request.headers.get("CF-Connecting-IP") or request.remote or "?")
        if self._too_many(ip):
            return self._page("Join", self._join_html(nxt, "Too many wrong codes. Wait 10 minutes."))
        b = self.book.bettor_by_code(str(form.get("code", "")))
        if not b:
            self._too_many(ip, failed=True)
            await asyncio.sleep(1)
            return self._page("Join", self._join_html(nxt, "No account has that code."))
        raise self._login_bettor(request, b["token"], _redirect(nxt, f"Welcome back, {b['name']}!"))

    def _require_bettor(self, request: web.Request):
        bettor = self._bettor(request)
        if not bettor:
            raise web.HTTPSeeOther("/book/join?" + urlencode({"next": request.path_qs}))
        return bettor

    async def matchup_page(self, request: web.Request) -> web.Response:
        bettor = self._require_bettor(request)
        mu = self.book.matchup(_int(request.match_info["id"], 0))
        if not mu:
            raise web.HTTPNotFound(text="No such matchup")
        name = self._names()
        everything = self.book.markets(mu["id"])
        alts: dict[int, list[dict]] = {}
        for m in everything:
            if m["parent_id"]:
                alts.setdefault(m["parent_id"], []).append(m)
        markets = [m for m in everything if not m["parent_id"]]
        groups: dict[str, list[dict]] = {}
        for m in markets:
            head = ("Game" if m["kind"] in ("moneyline", "spread") or (m["kind"] == "total" and not m["stat"]) else
                    "Game props" if m["kind"] in ("total", "yesno") else
                    "Specials" if m["kind"] == "custom" else name(m["player_key"]))
            groups.setdefault(head, []).append(m)
        ctx = self._parlay_ctx(request, bettor)
        models = Models(self.db)

        side = {k: "1" for k in mu["players1"]} | {k: "2" for k in mu["players2"]}

        def row(m: dict) -> str:
            if (m["kind"] in ("prop", "total") and m["status"] == "open" and mu["status"] == "open"
                    and not any(x["id"] in ctx["blocked"] for x in m["selections"])):
                html = self._slider_row(m, name, ctx, self.book.ladder(m, models, alts.get(m["id"], [])))
            else:
                html = self._market_row(m, mu, name, ctx)
            kind = "game" if m["kind"] in ("moneyline", "spread", "total", "yesno") else m["kind"]
            text = " ".join([self._title(m, name)] + [x["label"] for x in m["selections"]]).lower()
            return (f'<div class="fr" data-k="{kind}" data-t="{side.get(m["player_key"], "")}" '
                    f'data-q="{_e(text)}">{html}</div>')
        cards = "".join(f'<div class="card fg"><h2 style="margin-top:0">{_e(head)}</h2>'
                        + "".join(row(m) for m in ms) + "</div>" for head, ms in groups.items())
        lineups = self._lineups(mu, name)
        if ctx["blocked"].keys() & {x["id"] for m in markets for x in m["selections"]}:
            lineups += ('<div class="msg">🔒 You\'re playing in this game, so the picks you could sway are '
                        'locked. Everything else is open.</div>')
        tools = self._filter_bar("Search players or stats, e.g. passing yards",
                                 [("", "All"), ("game", "Game"), ("prop", "Player props"), ("custom", "Specials")],
                                 [("", "Both teams"), ("1", mu["team1"]), ("2", mu["team2"])])
        body = (self._bar(request, bettor) + _flash(request)
                + f'<p class="small"><a href="/book">← all games</a></p><h1>#{mu["id"]} {_e(mu["title"])} '
                  f'<span class="chip">{_STATUS[mu["status"]]}</span></h1>'
                  f'<p class="small">{lineups}</p>'
                  f'<p class="small muted">Lines and odds come from each player\'s past games. Props settle on the '
                  f'stat in the game; a player who doesn\'t play has their props voided (stakes returned). '
                  f'Tap a pick to bet it alone, or <b>+</b> to add it to a <a href="/book/parlay">parlay</a>. '
                  f'Drag a slider for other lines: a higher line pays more on the over, less on the under.</p>'
                + tools + cards + (SLIDER_JS if "data-l=" in cards else ""))
        return self._page(mu["title"], body, live=True)

    @staticmethod
    def _lineups(mu: dict, name) -> str:
        """Each team (its in-game name once a game has named it) on its own line with its players."""
        return "<br>".join(f'<b>{_e(mu[t])}</b>: ' + _e(", ".join(name(k) for k in mu[p]))
                           for t, p in (("team1", "players1"), ("team2", "players2")))

    @staticmethod
    def _filter_bar(placeholder: str, kinds: list[tuple[str, str]], teams: list[tuple[str, str]] | None = None) -> str:
        """Search box and filter buttons over the page's .fr rows (data-q text, data-k kind, data-t team);
        a .fg card whose rows are all hidden hides too."""
        chips = "".join(f'<a href="#" data-f="{k}"{" class=on" if not k else ""}>{_e(lbl)}</a>' for k, lbl in kinds)
        team = ("" if not teams else '<select class="ft" aria-label="Team">'
                + "".join(f'<option value="{k}">{_e(lbl)}</option>' for k, lbl in teams) + "</select>")
        return (f'<div class="card filters"><input class="fq" type="search" placeholder="{_e(placeholder)}" '
                f'aria-label="Search"><div class="tabs" style="margin:0">{chips}</div>{team}'
                f'<span class="small muted fn"></span></div>' + FILTER_JS)

    def _slider_row(self, m: dict, name, ctx: dict, ladder: list[dict]) -> str:
        """An over/under with a big slider through its lines; the pick buttons follow the slider."""
        if len(ladder) < 2:
            return self._market_row(m, {"status": "open"}, name, ctx)
        i = next(n for n, r in enumerate(ladder) if r["line"] == m["line"])
        data = json.dumps([[r["line"], r["over"], r["under"], r["over_id"], r["under_id"]] for r in ladder])
        cur = ladder[i]

        def pick(side: str) -> str:
            sid = cur[side + "_id"]
            inside = sid in ctx["slip"]
            return (f'<span class="pick-wrap"><button class="pk" name="pick" value="{side}:bet" '
                    f'title="Bet on this alone"><span class="lb">{side.title()} {cur["line"]:g}</span>'
                    f'<b>{fmt_odds(cur[side])}</b></button>'
                    f'<button class="{"in" if inside else "sec"} ad" name="pick" value="{side}:parlay" '
                    f'title="Add to parlay" style="padding:7px 11px">{"✓" if inside else "+"}</button></span>')
        return (f'<form class="mk alt" method="post" action="/book/alt/{m["id"]}" data-l="{_e(data)}" '
                f'data-slip="{_e(json.dumps(sorted(ctx["slip"])))}">{ctx["csrf"]}'
                f'<input type="hidden" name="next" value="{_e(ctx["next"])}">'
                f'<input type="hidden" name="line" value="{cur["line"]:g}">'
                f'<div class="row"><div class="t">{_e(self._title(m, name))}</div>'
                f'<span class="ln">{cur["line"]:g}</span></div>'
                f'<input type="range" class="big" min="0" max="{len(ladder) - 1}" value="{i}" step="1" '
                f'aria-label="Line for {_e(self._title(m, name))}">'
                f'<div class="ends"><span>{ladder[0]["line"]:g}</span><span>{ladder[-1]["line"]:g}</span></div>'
                f'<div class="row">{pick("over")} {pick("under")}</div></form>')

    async def alt_pick(self, request: web.Request) -> web.Response:
        """A pick from an over/under's slider: bet it alone, or add it to (or take it out of) the slip."""
        bettor = self._require_bettor(request)
        form = await request.post()
        self._check_csrf(request, form, BETTOR_COOKIE)
        nxt = self._safe_next(form.get("next"))
        side, _, what = str(form.get("pick", "")).partition(":")
        line = _float(form.get("line"))
        try:
            if side not in ("over", "under") or what not in ("bet", "parlay") or line is None:
                raise BookError("Pick over or under.")
            m = self.book.market(self.book.alt_market(_int(request.match_info["id"], 0), line))
            sel = next(x for x in m["selections"] if x["side"] == side)
            if what == "bet":
                if _ajax(request):
                    return web.json_response({"ok": True, "sel": sel["id"]})
                raise web.HTTPSeeOther(f"/book/bet/{sel['id']}")
            inside = sel["id"] in self._slip_ids(bettor)
            if inside:
                self.book.slip_remove(bettor["id"], sel["id"])
                msg = f"Took {sel['label']} out of your parlay slip."
            else:
                self.book.slip_add(bettor["id"], sel["id"])
                n = len(self._slip_ids(bettor))
                msg = f"Added {sel['label']} to your parlay slip ({n} pick{'s' * (n != 1)})."
            if _ajax(request):
                return web.json_response({"ok": True, "msg": msg, "sel": sel["id"], "line": m["line"], "side": side,
                                          "in_slip": not inside})
        except BookError as e:
            if _ajax(request):
                return web.json_response({"ok": False, "msg": str(e)})
            raise _redirect(nxt, str(e), err=True)
        raise _redirect(nxt, msg)

    async def api_selection(self, request: web.Request) -> web.Response:
        """What the bet card shows for one pick."""
        bettor = self._bettor(request)
        s = self.book.selection(_int(request.match_info["id"], 0))
        if not bettor or not s:
            return web.json_response({"ok": False})
        return web.json_response({
            "ok": True, "id": s["id"], "label": s["label"], "odds": s["odds"], "decimal": decimal_odds(s["odds"]),
            "title": self._title(s), "matchup": f'#{s["matchup_id"]} {s["matchup_title"]}',
            "open": s["matchup_status"] == "open" and s["market_status"] == "open",
            "why": self.book._insider(bettor["id"], s),
            "in_slip": s["id"] in self._slip_ids(bettor), "balance": bettor["balance"]},
            headers={"Cache-Control": "no-store"})

    async def api_slip(self, request: web.Request) -> web.Response:
        """The parlay cart: the picks in the slip with their current odds."""
        bettor = self._bettor(request)
        if not bettor:
            return web.json_response({"ok": False})
        legs = self.book.slip(bettor["id"])
        dec = self.book.parlay_decimal([s["odds"] for s in legs])
        return web.json_response({
            "ok": True, "decimal": dec, "american": american_from_decimal(dec) if dec > 1 else 0,
            "seen": ",".join(f'{s["id"]}:{s["odds"]}' for s in legs), "balance": bettor["balance"],
            "legs": [{"id": s["id"], "label": s["label"], "odds": s["odds"], "title": self._title(s),
                      "matchup": f'#{s["matchup_id"]} {s["matchup_title"]}',
                      "open": s["matchup_status"] == "open" and s["market_status"] == "open"} for s in legs]},
            headers={"Cache-Control": "no-store"})

    async def slip(self, request: web.Request, error: str = "", stake: str = "") -> web.Response:
        bettor = self._require_bettor(request)
        s = self.book.selection(_int(request.match_info["id"], 0))
        if not s:
            raise web.HTTPNotFound(text="No such bet")
        dec = decimal_odds(s["odds"])
        open_ = s["matchup_status"] == "open" and s["market_status"] == "open"
        if open_ and (why := self.book._insider(bettor["id"], s)):
            error, open_ = error or why, False
        err = f'<div class="msg err">{_e(error)}</div>' if error else ""
        inside = s["id"] in self._slip_ids(bettor)
        parlay = ("" if not open_ else
                  f'<p class="small">✓ In your <a href="/book/parlay">parlay slip</a>.</p>' if inside else
                  f'<form method="post" action="/book/parlay/add/{s["id"]}" style="margin-top:12px">'
                  f'{self._hidden(request, BETTOR_COOKIE)}<input type="hidden" name="next" value="/book/parlay">'
                  f'<button class="sec">Add to parlay instead</button></form>')
        form = (f'<form class="stack" method="post" action="/book/bet/{s["id"]}">{self._hidden(request, BETTOR_COOKIE)}'
                f'<input type="hidden" name="odds" value="{s["odds"]}">'
                f'<label>Stake ($)<br><input id="stake" name="stake" inputmode="decimal" required '
                f'value="{_e(stake)}" placeholder="100"></label>'
                f'<div>To win <b id="win">$0.00</b>, paid back <b id="ret">$0.00</b></div>'
                f"<button>Place bet</button></form>"
                f"<script>(function(){{var i=document.getElementById('stake'),d={dec};"
                f"function f(v){{return '$'+v.toLocaleString(undefined,{{minimumFractionDigits:2,maximumFractionDigits:2}})}}"
                f"function u(){{var s=parseFloat((i.value||'').replace(/[$,]/g,''))||0;"
                f"document.getElementById('win').textContent=f(s*(d-1));document.getElementById('ret').textContent=f(s*d);}}"
                f"i.addEventListener('input',u);u();}})();</script>"
                if open_ else "<p class='muted'>Betting on this is closed.</p>")
        body = (self._bar(request, bettor) + err
                + f'<p class="small"><a href="/book/matchups/{s["matchup_id"]}">← {_e(s["matchup_title"])}</a></p>'
                  f'<h1>Bet slip</h1><div class="card"><div class="muted small">{_e(self._title(s))}</div>'
                  f'<div class="score">{_e(s["label"])} <span class="odds">{fmt_odds(s["odds"])}</span></div>'
                  f'<p class="small muted">Decimal {dec:.2f}: a $100 bet pays back {_money(100 * dec)}. '
                  f'You have {_money(bettor["balance"])}.</p>{form}{parlay}</div>')
        return self._page("Bet slip", body, live=True)

    async def bet(self, request: web.Request) -> web.Response:
        bettor = self._require_bettor(request)
        form = await request.post()
        self._check_csrf(request, form, BETTOR_COOKIE)
        sel_id = _int(request.match_info["id"], 0)
        stake = _float(form.get("stake"))
        try:
            if stake is None:
                raise BookError("Enter how much to bet.")
            self.book.place_bet(bettor["id"], sel_id, stake, _int(form.get("odds")))
        except BookError as e:
            if _ajax(request):
                s = self.book.selection(sel_id)
                return web.json_response({"ok": False, "msg": str(e), "balance": self.book.bettor(bettor["id"])["balance"],
                                          "odds": s["odds"] if s else None,
                                          "decimal": decimal_odds(s["odds"]) if s else None})
            return await self.slip(request, str(e), str(form.get("stake", "")))
        s = self.book.selection(sel_id)
        if _ajax(request):
            return web.json_response({"ok": True, "balance": self.book.bettor(bettor["id"])["balance"],
                                      "msg": f"Bet placed: {_money(stake)} on {s['label']} ({fmt_odds(s['odds'])}). "
                                             f"Good luck!"})
        raise _redirect(f"/book/matchups/{s['matchup_id']}",
                        f"Bet placed: {_money(stake)} on {s['label']} ({fmt_odds(s['odds'])}), "
                        f"{self._title(s)}.")

    def _bets_table(self, bets: list[dict]) -> str:
        if not bets:
            return "<p class='muted'>No bets yet.</p>"
        rows, name = [], self._names()
        for b in bets:
            if b["status"] == "pending":
                res = f'<span class="muted">Pending · to win {_money(b["stake"] * (decimal_odds(b["odds"]) - 1))}</span>'
            elif b["status"] == "won":
                res = f'<span class="won">Won {_money(b["payout"] - b["stake"])}</span>'
            elif b["status"] == "lost":
                res = f'<span class="lost">Lost {_money(b["stake"])}</span>'
            else:
                res = f'<span class="muted">{"Push" if b["status"] == "push" else "Void"}, stake returned</span>'
            result = f'<br><span class="small muted">{_e(b["result"])}</span>' if b["result"] else ""
            rows.append(f'<tr><td><a href="/book/matchups/{b["matchup_id"]}">{_e(b["matchup_title"])}</a><br>'
                        f'<span class="small">{_e(self._title(b, name))}</span></td>'
                        f'<td>{_e(b["label"])} <span class="odds">{fmt_odds(b["odds"])}</span></td>'
                        f'<td class="n">{_money(b["stake"])}</td><td>{res}{result}</td>'
                        f'<td class="small muted">{_time(b["placed_at"])}</td></tr>')
        return ('<div class="card scroll"><table><tr><th>Game</th><th>Pick</th><th class="n">Stake</th>'
                f'<th>Result</th><th>Placed</th></tr>{"".join(rows)}</table></div>')

    async def me(self, request: web.Request) -> web.Response:
        bettor = self._require_bettor(request)
        bets = self.book.bets_for(bettor["id"])
        parlays = self.book.parlays_for(bettor["id"])
        at_risk = sum(b["stake"] for b in bets + parlays if b["status"] == "pending")
        worth = bettor["balance"] + at_risk
        body = (self._bar(request, bettor) + _flash(request) + f"<h1>My bets</h1><div class='card'>"
                f'<span class="stat"><b>{_money(bettor["balance"])}</b><span class="muted">to bet</span></span>'
                f'<span class="stat"><b>{_money(at_risk)}</b><span class="muted">in open bets</span></span>'
                f'<span class="stat"><b class="{"won" if worth >= START_BALANCE else "lost"}">'
                f'{_money(worth - START_BALANCE)}</b><span class="muted">profit</span></span>'
                f"</div><div class='card'><h2 style='margin-top:0'>Password</h2>"
                + ("" if bettor["password"] else
                   "<p class='msg err'>You don't have a password yet. Set one so you can log in on any device "
                   "(and after the website's link changes).</p>")
                + f'<form class="inline" method="post" action="/book/password">{self._hidden(request, BETTOR_COOKIE)}'
                  f'<input type="hidden" name="name" value="{_e(bettor["name"])}" autocomplete="username">'
                  f'<input name="password" type="password" required minlength="4" autocomplete="new-password" '
                  f'placeholder="{"New password" if bettor["password"] else "Pick a password"}">'
                  f'<button>{"Change password" if bettor["password"] else "Set password"}</button></form>'
                  f'<p class="small muted">You log in as <b>{_e(bettor["name"])}</b>.</p></div>'
                + (f"<h2>Parlays</h2>{self._parlays_table(parlays)}<h2>Single bets</h2>" if parlays else "")
                + self._bets_table(bets))
        return self._page("My bets", body, live=True)

    def _parlays_table(self, parlays: list[dict]) -> str:
        name = self._names()
        icon = {"win": "✅", "lose": "❌", "push": "↩", "void": "↩", None: "⏳"}
        rows = []
        for p in parlays:
            if p["status"] == "pending":
                res = f'<span class="muted">Pending · to win {_money(p["stake"] * (p["decimal"] - 1))}</span>'
            elif p["status"] == "won":
                res = f'<span class="won">Won {_money(p["payout"] - p["stake"])}</span>'
            elif p["status"] == "lost":
                res = f'<span class="lost">Lost {_money(p["stake"])}</span>'
            else:
                res = '<span class="muted">Void, stake returned</span>'
            legs = "<br>".join(
                f'{icon.get(leg["outcome"], "")} <a href="/book/matchups/{leg["matchup_id"]}">{_e(self._title(leg, name))}'
                f'</a>: {_e(leg["label"])} <span class="odds">{fmt_odds(leg["odds"])}</span>'
                + (f' <span class="small muted">({_e(leg["result"])})</span>' if leg["result"] else "")
                for leg in p["legs"])
            rows.append(f'<tr><td class="small">{len(p["legs"])}-leg parlay '
                        f'<span class="odds">{fmt_odds(american_from_decimal(p["decimal"]))}</span><br>{legs}</td>'
                        f'<td class="n">{_money(p["stake"])}</td><td>{res}</td>'
                        f'<td class="small muted">{_time(p["placed_at"])}</td></tr>')
        return ('<div class="card scroll"><table><tr><th>Picks</th><th class="n">Stake</th><th>Result</th>'
                f'<th>Placed</th></tr>{"".join(rows)}</table></div>')

    # -------------------------------------------------------------- parlays
    async def parlay_page(self, request: web.Request, error: str = "", stake: str = "",
                          chosen: set[int] | None = None) -> web.Response:
        bettor = self._require_bettor(request)
        legs = self.book.slip(bettor["id"])
        name = self._names()
        csrf = self._hidden(request, BETTOR_COOKIE)
        err = f'<div class="msg err">{_e(error)}</div>' if error else _flash(request)
        if not legs:
            body = (self._bar(request, bettor) + err + "<h1>Parlay slip</h1><div class='card'><p>Your slip is "
                    "empty. Open a <a href='/book'>game</a> and tap <b>+</b> next to the picks you want. A parlay "
                    "multiplies the odds of every pick and pays only if they all win.</p></div>")
            return self._page("Parlay slip", body)
        rows = []
        for s in legs:
            open_ = s["matchup_status"] == "open" and s["market_status"] == "open"
            rows.append(f'<tr><td><a href="/book/matchups/{s["matchup_id"]}">{_e(s["matchup_title"])}</a><br>'
                        f'<span class="small">{_e(self._title(s, name))}</span></td>'
                        f'<td>{_e(s["label"])} <span class="odds">{fmt_odds(s["odds"])}</span>'
                        + ("" if open_ else ' <span class="lost small">betting closed</span>') + "</td>"
                        f'<td><form class="addp" method="post" action="/book/parlay/remove/{s["id"]}">{csrf}'
                        f'<input type="hidden" name="next" value="/book/parlay">'
                        f'<button class="sec" title="Remove">✕</button></form></td></tr>')
        dec = self.book.parlay_decimal([s["odds"] for s in legs])
        seen = ",".join(f'{s["id"]}:{s["odds"]}' for s in legs)
        n = len(legs)
        # For each bet size k: how many bets, and what they pay back per $1 if every pick wins
        # (the sum over every k-pick combination of its multiplied odds)
        decs = [decimal_odds(s["odds"]) for s in legs]
        sizes = {k: (math.comb(n, k), self._sym(decs, k)) for k in range(1, n + 1)}
        chosen = set(chosen or ()) or {n if n >= 2 else 1}

        def opt(k: int, text: str) -> str:
            count, _ = sizes[k]
            return (f'<label><input type="checkbox" name="size" value="{k}"{" checked" if k in chosen else ""}> '
                    f'{text} <span class="muted small">({count} bet{"s" * (count != 1)})</span></label>')
        options = ([opt(n, f"<b>{n}-pick parlay</b> {fmt_odds(american_from_decimal(dec))}")] if n >= 2 else [])
        options += [opt(k, f"Round robin: every {k}-pick parlay") for k in range(n - 1, 1, -1)]
        options.append(opt(1, "Each pick on its own (straight bets)"))
        table = json.dumps({k: list(v) for k, v in sizes.items()})
        form = (f'<form class="stack" method="post" action="/book/parlay">{csrf}'
                f'<input type="hidden" name="seen" value="{seen}">'
                f'<div class="opts"><b>Bet</b>{"".join(options)}</div>'
                f'<label>Stake per bet ($)<br><input id="stake" name="stake" inputmode="decimal" required '
                f'value="{_e(stake)}" placeholder="100"></label>'
                f'<label class="small"><input type="checkbox" name="keep" value="1"> Keep these picks in my slip '
                f'(to bet them again another way)</label>'
                f'<div><b id="nb">1 bet</b> · total <b id="cost">$0.00</b> · pays back up to <b id="ret">$0.00</b> '
                f'if every pick wins</div>'
                f'<button id="go">Place bets</button></form>'
                f"<script>(function(){{var t={table},i=document.getElementById('stake'),"
                f"bx=document.querySelectorAll('input[name=size]');"
                f"function f(v){{return '$'+v.toLocaleString(undefined,{{minimumFractionDigits:2,maximumFractionDigits:2}})}}"
                f"function u(){{var s=parseFloat((i.value||'').replace(/[$,]/g,''))||0,nb=0,ret=0;"
                f"bx.forEach(function(b){{if(b.checked){{nb+=t[b.value][0];ret+=t[b.value][1];}}}});"
                f"document.getElementById('nb').textContent=nb+' bet'+(nb==1?'':'s');"
                f"document.getElementById('cost').textContent=f(s*nb);document.getElementById('ret').textContent=f(s*ret);"
                f"document.getElementById('go').textContent=nb?'Place '+nb+' bet'+(nb==1?'':'s')+' ('+f(s*nb)+')':'Pick what to bet';}}"
                f"i.addEventListener('input',u);bx.forEach(function(b){{b.addEventListener('change',u)}});u();}})();</script>")
        body = (self._bar(request, bettor) + err + "<h1>Parlay slip</h1>"
                f'<div class="card scroll"><table class="legs">{"".join(rows)}</table></div>'
                f'<div class="card"><div class="score">{n} pick{"s" * (n != 1)}'
                + (f' · <span class="odds">{fmt_odds(american_from_decimal(dec))}</span>' if n >= 2 else "")
                + f'</div><p class="small muted">A parlay multiplies the odds of every pick and pays only if they all '
                  f"win; a pick that pushes or is voided drops out and the rest still count. A round robin bets "
                  f"every smaller parlay from your picks, so one miss doesn't sink everything. Tick as many as you "
                  f"like: each is a separate bet of your stake. Up to {MAX_PARLAY_LEGS} picks, one per bet. "
                  f"You have {_money(bettor['balance'])}.</p>{form}"
                  f'<form method="post" action="/book/parlay/clear" style="margin-top:12px">{csrf}'
                  f'<button class="sec">Clear slip</button></form></div>')
        return self._page("Parlay slip", body, live=True)

    @staticmethod
    def _sym(values: list[float], k: int) -> float:
        """Sum over every k-item combination of the product of its values."""
        e = [1.0] + [0.0] * k
        for v in values:
            for j in range(k, 0, -1):
                e[j] += e[j - 1] * v
        return e[k]

    async def _slip_change(self, request: web.Request, fn) -> web.Response:
        bettor = self._require_bettor(request)
        form = await request.post()
        self._check_csrf(request, form, BETTOR_COOKIE)
        nxt = self._safe_next(form.get("next"))
        try:
            msg = fn(bettor["id"])
        except BookError as e:
            if _ajax(request):
                return web.json_response({"ok": False, "msg": str(e)})
            raise _redirect(nxt, str(e), err=True)
        if _ajax(request):
            return web.json_response({"ok": True, "msg": msg or "", "count": len(self._slip_ids(bettor))})
        raise _redirect(nxt, msg) if msg else web.HTTPSeeOther(nxt)

    async def parlay_add(self, request: web.Request) -> web.Response:
        sel = _int(request.match_info["id"], 0)

        def add(bid: int) -> str:
            self.book.slip_add(bid, sel)
            n = len(self._slip_ids({"id": bid}))
            return f"Added to your parlay slip ({n} pick{'s' * (n != 1)})."
        return await self._slip_change(request, add)

    async def parlay_remove(self, request: web.Request) -> web.Response:
        sel = _int(request.match_info["id"], 0)
        return await self._slip_change(request, lambda bid: self.book.slip_remove(bid, sel))

    async def parlay_clear(self, request: web.Request) -> web.Response:
        return await self._slip_change(request, lambda bid: self.book.slip_remove(bid))

    async def parlay_place(self, request: web.Request) -> web.Response:
        bettor = self._require_bettor(request)
        form = await request.post()
        self._check_csrf(request, form, BETTOR_COOKIE)
        stake = _float(form.get("stake"))
        seen = []
        for part in str(form.get("seen", "")).split(","):
            sid, _, odds = part.partition(":")
            if _int(sid) is not None and _int(odds) is not None:
                seen.append((_int(sid), _int(odds)))
        sizes = [k for x in form.getall("size", []) if (k := _int(x)) is not None]
        try:
            if stake is None:
                raise BookError("Enter how much to bet.")
            if not sizes and "size" not in form and len(seen) >= 2:
                sizes = [len(seen)]  # an older page without the options: the whole slip as one parlay
            if not sizes:
                raise BookError("Pick what to bet: the parlay, a round robin or each pick alone.")
            res = self.book.place_slip(bettor["id"], stake, seen, sizes, keep=bool(form.get("keep")))
        except BookError as e:
            if _ajax(request):
                return web.json_response({"ok": False, "msg": str(e)})
            return await self.parlay_page(request, str(e), str(form.get("stake", "")), set(sizes))
        if _ajax(request):
            return web.json_response({"ok": True, "balance": self.book.bettor(bettor["id"])["balance"],
                                      "msg": f"Parlay placed: {_money(stake)}. Good luck! It's under My bets."})
        parts = ([f"{len(res['parlays'])} parlay{'s' * (len(res['parlays']) != 1)}"] if res["parlays"] else []) + (
            [f"{len(res['singles'])} straight bet{'s' * (len(res['singles']) != 1)}"] if res["singles"] else [])
        raise _redirect("/book/parlay" if form.get("keep") else "/book/me",
                        f"Placed {' and '.join(parts)} of {_money(stake)} ({_money(res['cost'])} in all). Good luck!")

    # ------------------------------------------------------- everyone's bets
    async def feed(self, request: web.Request) -> web.Response:
        """Everyone's bets. Live: open bets, newest first, updating on the page as bets come in, each with
        a Tail button that copies it. Bet history: settled bets with their results."""
        bettor = self._require_bettor(request)
        view = "history" if request.query.get("view") == "history" else "live"
        show = request.query.get("show", "all")
        show = show if show in ("parlays", "singles", "all") else "all"
        items = [d for d in self.book.feed(200, pending=view == "live")
                 if show == "all" or d["kind"] == ("parlay" if show == "parlays" else "bet")][:80]
        name, csrf = self._names(), self._hidden(request, BETTOR_COOKIE)
        icon = {"win": "✅", "lose": "❌", "push": "↩", "void": "↩", None: "⏳"}
        rows = []
        for d in items:
            if d["status"] == "pending":
                res = f'<span class="muted">Open · to win {_money(d["stake"] * (d["decimal"] - 1))}</span>'
            elif d["status"] == "won":
                res = f'<span class="won">Won {_money(d["payout"] - d["stake"])}</span>'
            elif d["status"] == "lost":
                res = f'<span class="lost">Lost {_money(d["stake"])}</span>'
            else:
                res = f'<span class="muted">{"Push" if d["status"] == "push" else "Void"}, stake returned</span>'
            legs = "<br>".join(
                (f'{icon.get(leg["outcome"], "")} ' if d["kind"] == "parlay" else "")
                + f'<a href="/book/matchups/{leg["matchup_id"]}">{_e(self._title(leg, name))}</a>: '
                  f'{_e(leg["label"])} <span class="odds">{fmt_odds(leg["odds"])}</span>' for leg in d["legs"])
            open_legs = [leg for leg in d["legs"] if leg["market_status"] == "open" and leg["matchup_status"] == "open"]
            if not open_legs or d["bettor_id"] == bettor["id"]:
                tail = ""
            elif d["kind"] == "bet":
                tail = f'<a class="sel" href="/book/bet/{open_legs[0]["selection_id"]}">Tail</a>'
            else:
                tail = (f'<form class="addp" method="post" action="/book/tail/parlay/{d["id"]}">{csrf}'
                        f'<button title="Copy these picks into your parlay slip">Tail</button></form>')
            what = (f'{len(d["legs"])}-leg parlay <span class="odds">{fmt_odds(american_from_decimal(d["decimal"]))}'
                    f'</span><br>' if d["kind"] == "parlay" else "")
            rows.append(f'<tr data-k="{d["kind"]}{d["id"]}"><td><b>{_e(d["name"])}</b><br>'
                        f'<span class="small muted">{_time(d["placed_at"])}</span></td>'
                        f'<td class="small">{what}{legs}</td><td class="n">{_money(d["stake"])}</td>'
                        f'<td>{res}</td>{"<td>" + tail + "</td>" if view == "live" else ""}</tr>')
        empty = "No open bets right now." if view == "live" else "No bets have been settled yet."
        table = ('<div class="card scroll"><table><tr><th>Bettor</th><th>Picks</th><th class="n">Stake</th>'
                 f'<th>Result</th>{"<th></th>" if view == "live" else ""}</tr>{"".join(rows)}</table></div>'
                 if rows else f"<p class='muted'>{empty}</p>")
        if _ajax(request):
            return web.Response(text=table, content_type="text/html", headers={"Cache-Control": "no-store"})
        q = lambda v, k: f"/book/feed?view={v}" + ("" if k == "all" else f"&show={k}")
        tabs = "".join(f'<a href="{q(v, show)}"{" class=on" if v == view else ""}>{lbl}</a>'
                       for v, lbl in (("live", "🔴 Live"), ("history", "Bet history")))
        kinds = "".join(f'<a href="{q(view, k)}"{" class=on" if k == show else ""}>{lbl}</a>'
                        for k, lbl in (("all", "All"), ("parlays", "Parlays"), ("singles", "Single bets")))
        note = ("Everyone's open bets as they're placed. This list updates by itself. Like someone's bet? "
                "<b>Tail</b> copies its picks (the ones still open) at today's odds: a parlay goes into your "
                "parlay slip, a single bet opens its bet slip. You pick your own stake."
                if view == "live" else "Bets that have been settled, newest first, with how they did.")
        body = (self._bar(request, bettor) + _flash(request) + "<h1>Everyone's bets</h1><div class='tabs'>"
                + tabs + "</div><div class='tabs small'>" + kinds + f"</div><p class='small muted'>{note}</p>"
                + f'<div id="feed">{table}</div>' + FEED_JS)
        return self._page("Everyone's bets", body)

    async def tail(self, request: web.Request) -> web.Response:
        bettor = self._require_bettor(request)
        form = await request.post()
        self._check_csrf(request, form, BETTOR_COOKIE)
        kind = request.match_info["kind"]
        try:
            legs = self.book.tail(bettor["id"], "parlay" if kind == "parlay" else "bet",
                                  _int(request.match_info["id"], 0))
        except BookError as e:
            raise _redirect("/book/feed", str(e), err=True)
        if len(legs) == 1 and kind != "parlay":
            raise web.HTTPSeeOther(f"/book/bet/{legs[0]['id']}")
        raise _redirect("/book/parlay", f"Tailed: your slip now has those {len(legs)} picks at today's odds. "
                                        f"Pick your stake.")

    def _leader_table(self, rows: list[dict]) -> str:
        if not rows:
            return "<p class='muted'>Nobody has joined yet.</p>"
        body = "".join(
            f'<tr><td class="n">{i + 1}</td><td>{_e(r["name"])}</td><td class="n"><b>{_money(r["worth"])}</b></td>'
            f'<td class="n {"won" if r["worth"] >= START_BALANCE else "lost"}">{_money(r["worth"] - START_BALANCE)}</td>'
            f'<td class="n">{_money(r["at_risk"])}</td><td class="n">{r["won"]}-{r["lost"]}</td>'
            f'<td class="n">{r["bets"]}</td></tr>' for i, r in enumerate(rows))
        return ('<div class="card scroll"><table><tr><th class="n">#</th><th>Bettor</th><th class="n">Total</th>'
                '<th class="n">Profit</th><th class="n">In open bets</th><th class="n">W-L</th>'
                f'<th class="n">Bets</th></tr>{body}</table></div>')

    async def version(self, request: web.Request) -> web.Response:
        return web.json_response({"v": self.book.version(), "f": self.book.feed_version()}, headers={"Cache-Control": "no-store"})

    async def leaderboard(self, request: web.Request) -> web.Response:
        body = (self._bar(request, self._bettor(request)) + "<h1>Sportsbook leaderboard</h1>"
                "<p class='muted'>Total = money left to bet plus money in open bets.</p>"
                + self._leader_table(self.book.leaderboard()))
        return self._page("Leaderboard", body, live=True)

    # ---------------------------------------------------------------- admin
    def _admin_bar(self, request: web.Request) -> str:
        return (f'<div class="card bar"><b>Sportsbook admin</b><a href="/admin">Matchups</a>'
                f'<a href="/admin/bettors">Bettors</a><a href="/book">Bettor view</a>'
                f'<form method="post" action="/admin/logout" style="margin-left:auto">'
                f'{self._hidden(request, ADMIN_COOKIE)}<button class="sec">Log out</button></form></div>')

    def _login_html(self, error: str = "") -> str:
        if not self.admin_password:
            return ("<h1>Sportsbook admin</h1><div class='card'>Admin is off: set <code>ADMIN_PASSWORD</code> in "
                    "<code>.env</code> and restart the bot.</div>")
        err = f'<div class="msg err">{_e(error)}</div>' if error else ""
        return (f"<h1>Sportsbook admin</h1>{err}<div class='card'><form class='stack' method='post' "
                f"action='/admin/login'><input name='username' placeholder='Username' autocomplete='username' "
                f"required><input name='password' type='password' placeholder='Password' "
                f"autocomplete='current-password' required><button>Log in</button></form></div>")

    async def admin_login(self, request: web.Request) -> web.Response:
        ip = request.headers.get("CF-Connecting-IP") or request.remote or "?"
        now = time.time()
        form = await request.post()
        if self._too_many(ip):
            return self._page("Admin", self._login_html("Too many wrong passwords. Wait 10 minutes."))
        ok = (self.admin_password
              and hmac.compare_digest(str(form.get("username", "")).strip().lower(), ADMIN_USER)
              and hmac.compare_digest(str(form.get("password", "")).encode(), self.admin_password.encode()))
        if not ok:
            self._too_many(ip, failed=True)
            await asyncio.sleep(1)
            return self._page("Admin", self._login_html("Wrong username or password."))
        exp = str(int(now) + ADMIN_SESSION_SECONDS)
        resp = web.HTTPSeeOther("/admin")
        resp.set_cookie(ADMIN_COOKIE, f"{exp}.{self._sign(self.admin_key, exp)}", max_age=ADMIN_SESSION_SECONDS,
                        httponly=True, samesite="Strict", secure=_secure(request), path="/")
        raise resp

    async def admin_logout(self, request: web.Request) -> web.Response:
        form = await request.post()
        self._check_csrf(request, form, ADMIN_COOKIE)
        resp = web.HTTPSeeOther("/admin")
        resp.del_cookie(ADMIN_COOKIE, path="/")
        raise resp

    async def admin(self, request: web.Request) -> web.Response:
        if not self._is_admin(request):
            return self._page("Admin", self._login_html())
        name = self._names()
        hand = ' <b class="lost">· some bets need settling by hand</b>'
        per = self.book.matchup_house()

        def pl(v: float) -> str:
            return f'<span class="{"won" if v >= 0 else "lost"}">{"+" if v > 0 else ""}{_money(v)}</span>'
        rows = "".join(
            f'<tr><td><a href="/admin/matchups/{mu["id"]}">#{mu["id"]} {_e(mu["title"])}</a><br>'
            f'<span class="small muted">{_e(mu["team1"])}: {_e(", ".join(name(k) for k in mu["players1"]))}<br>'
            f'{_e(mu["team2"])}: {_e(", ".join(name(k) for k in mu["players2"]))}</span></td>'
            f'<td>{_STATUS[mu["status"]]}{" · game #" + str(mu["game_id"]) if mu["game_id"] else ""}'
            f'{hand if mu["game_id"] and mu["status"] in ("open", "locked") else ""}</td>'
            f'<td class="n">{pl(per.get(mu["id"], 0))}</td>'
            f'<td class="small muted">{_time(mu["created_at"])}</td></tr>'
            for mu in self.book.matchups())
        h = self.book.house()
        house = (f'<div class="card"><h2 style="margin-top:0">House</h2>'
                 f'<span class="stat"><b>{pl(h["total"]["profit"])}</b><span class="muted">house profit/loss</span></span>'
                 f'<span class="stat"><b>{pl(h["singles"]["profit"])}</b><span class="muted">on single bets</span></span>'
                 f'<span class="stat"><b>{pl(h["parlays"]["profit"])}</b><span class="muted">on parlays</span></span>'
                 f'<span class="stat"><b>{_money(h["total"]["open"])}</b><span class="muted">in open bets</span></span>'
                 f'<span class="stat"><b>{_money(h["total"]["exposure"])}</b><span class="muted">owed if every open '
                 f'bet wins</span></span>'
                 f'<p class="small muted">Profit/loss is the stakes the book kept minus the winnings it paid, on '
                 f'settled bets ({h["total"]["n"]} bets, {_money(h["total"]["handle"])} wagered in all). Money '
                 f'given out on the Bettors page isn\'t counted.</p></div>')
        table = (f'<div class="card scroll"><table><tr><th>Matchup</th><th>Status</th>'
                 f'<th class="n" title="On single bets; parlays are only in the total">House P/L</th><th>Created</th></tr>'
                 f"{rows}</table></div>" if rows else "<p class='muted'>No matchups yet.</p>")
        pick = self._pick_table()
        create = (f'<div class="card"><h2 style="margin-top:0">New matchup</h2>'
                  f'<form method="post" action="/admin/matchups">{self._hidden(request, ADMIN_COOKIE)}'
                  f'<div class="inline" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">'
                  f'<input name="title" placeholder="Title (optional)"></div>'
                  f'<p class="small muted">Put each player on Team 1 or Team 2. The lists show each team in the '
                  f"order you picked, and that's the order it keeps (the first pick names the team). When the game "
                  f"comes in, the bot works out which side each team played from its players and settles the "
                  f"winner.</p>"
                  f'{pick}'
                  f'<p class="small muted">Players with no games yet (comma-separated names):</p>'
                  f'<div style="display:flex;gap:8px;flex-wrap:wrap"><input name="extra1" placeholder="Team 1 extras">'
                  f'<input name="extra2" placeholder="Team 2 extras"></div>'
                  f'<p><button>Create with auto-generated odds</button></p></form></div>')
        body = self._admin_bar(request) + _flash(request) + house + "<h1>Matchups</h1>" + table + create
        return self._page("Admin", body)

    def _pick_table(self, mu: dict | None = None) -> str:
        """Every player (and anyone already in the matchup) with a big Out / Team 1 / Team 2 switch, and
        each team's list in the order players were put on it. That order is kept: the first player
        picked names the team ("Team <name>") and the lineups are listed that way."""
        name, names = self._names(), Names(self.db)
        on = {k: "1" for k in mu["players1"]} | {k: "2" for k in mu["players2"]} if mu else {}
        people = {u.lower(): names.dashed(u) for u in self.db.known_usernames()}
        people.update({k: name(k) for k in on if k not in people})
        t1, t2 = (mu["team1"], mu["team2"]) if mu else ("Team 1", "Team 2")
        rows = "".join(
            f'<tr data-k="{_e(k)}" data-n="{_e(name(k))}"><td style="vertical-align:middle">{_e(label)}</td>'
            f'<td><div class="seg">'
            + "".join(f'<label><input type="radio" name="p:{_e(k)}" value="{v}"'
                      f'{" checked" if on.get(k, "") == v else ""}><span>{_e(lbl)}</span></label>'
                      for v, lbl in (("", "Out"), ("1", t1), ("2", t2)))
            + "</div></td></tr>" for k, label in sorted(people.items(), key=lambda kv: name(kv[0]).lower()))
        # Each team's card also has a dropdown to add a player straight to it, and a ✕ on each name
        order = (mu["players1"] + mu["players2"]) if mu else []
        everyone = sorted(people.items(), key=lambda kv: name(kv[0]).lower())
        options = "".join(f'<option value="{_e(k)}">{_e(label)}</option>' for k, label in everyone)
        lists = "".join(f'<div class="card t{i}" style="margin:0"><b>{_e(t)}</b>'
                        f'<select class="add" data-t="{i}" aria-label="Add a player to {_e(t)}">'
                        f'<option value="">＋ Add a player…</option>{options}</select><ol id="pk{i}">'
                        + "".join(f"<li>{_e(name(k))}</li>" for k in (mu[f"players{i}"] if mu else []))
                        + "</ol></div>" for i, t in ((1, t1), (2, t2)))
        return (f'<div class="pickbox"><input type="hidden" name="order" value="{_e(",".join(order))}">'
                f'<div class="teams">{lists}</div>'
                f'<input class="find" type="search" placeholder="Find a player" style="margin:4px 0 8px">'
                f'<div class="scroll" style="max-height:60vh;overflow:auto"><table class="pick">{rows}</table></div>'
                "</div><script>(function(){var box=document.currentScript.previousElementSibling,"
                "ord=box.querySelector('input[name=order]'),o=ord.value?ord.value.split(','):[];"
                "function draw(){[1,2].forEach(function(t){var ol=box.querySelector('#pk'+t);ol.innerHTML='';"
                "o.forEach(function(k){var tr=box.querySelector('tr[data-k=\"'+CSS.escape(k)+'\"]'),"
                "c=tr&&tr.querySelector('input:checked');if(c&&c.value==t){var li=document.createElement('li');"
                "li.textContent=tr.dataset.n+' ';var x=document.createElement('a');x.href='#';x.textContent='\\u2715';"
                "x.title='Take off the team';x.className='small';x.onclick=function(e){e.preventDefault();set(k,'')};"
                "li.appendChild(x);ol.appendChild(li);}});});ord.value=o.join(',');"
                "box.querySelectorAll('select.add option[value]').forEach(function(op){if(op.value)"
                "op.disabled=o.indexOf(op.value)>=0;});}"
                "function set(k,t){var r=box.querySelector('tr[data-k=\"'+CSS.escape(k)+'\"] input[value=\"'+t+'\"]');"
                "if(r){r.checked=true;r.dispatchEvent(new Event('change'));}}"
                "box.querySelectorAll('select.add').forEach(function(sl){sl.addEventListener('change',function(){"
                "if(sl.value)set(sl.value,sl.dataset.t);sl.value='';});});"
                "box.querySelectorAll('.seg input').forEach(function(i){i.addEventListener('change',function(){"
                "var k=i.closest('tr').dataset.k;o=o.filter(function(x){return x!==k});if(i.value)o.push(k);draw();});});"
                "box.querySelector('.find').addEventListener('input',function(e){var q=e.target.value.toLowerCase();"
                "box.querySelectorAll('tr[data-k]').forEach(function(tr){tr.style.display="
                "tr.textContent.toLowerCase().indexOf(q)>=0?'':'none';});});draw();})();</script>")

    def _sides(self, form) -> dict[str, list[str]]:
        """Team 1 and Team 2 keys from a pick chart, in the order they were picked (the hidden "order"
        field the page keeps), then any extras typed in."""
        sides: dict[str, list[str]] = {"1": [], "2": []}
        for k, v in form.items():
            if k.startswith("p:") and v in sides:
                sides[v].append(k[2:])
        order = {k: i for i, k in enumerate(str(form.get("order", "")).split(",")) if k}
        for team in sides:
            sides[team].sort(key=lambda k: order.get(k, len(order)))  # stable: unlisted keep table order
        for team in ("1", "2"):
            for raw in str(form.get(f"extra{team}", "")).split(","):
                if raw.strip():
                    key = self.db.resolve_player(raw) or raw.strip().lower()
                    if key not in sides["1"] + sides["2"]:
                        sides[team].append(key)
        return sides

    async def admin_create(self, request: web.Request) -> web.Response:
        self._need_admin(request)
        form = await request.post()
        self._check_csrf(request, form, ADMIN_COOKIE)
        sides = self._sides(form)
        try:
            mid = self.book.create_matchup(str(form.get("title", "")), sides["1"], sides["2"], self._names())
        except BookError as e:
            raise _redirect("/admin", str(e), err=True)
        raise _redirect(f"/admin/matchups/{mid}", "Matchup created and open for betting. Check the lines below.")

    def _admin_market(self, request: web.Request, m: dict, name) -> str:
        done = m["status"] in ("settled", "void")
        dis = " disabled" if done else ""
        line = (f'<label>Line <input class="n" name="line" value="{m["line"]:g}"{dis}></label>'
                if m["kind"] in ("total", "prop", "spread") else "")
        odds = " ".join(
            f'<label>{_e(s["label"])} <input class="n" name="odds_{s["id"]}" value="{fmt_odds(s["odds"])}"{dis}>'
            f'</label><span class="small muted">{s["bets"]} bets · {_money(s["handle"])}'
            + (f' · model {s["fair_p"] * 100:.0f}%' if s["fair_p"] is not None else "")
            + (f' · <b>{_e(s["outcome"])}</b>' if s["outcome"] else "") + "</span>"
            for s in m["selections"])
        if done:
            buttons = '<button class="sec" name="action" value="unsettle">Undo settlement</button>'
        else:
            winner = "".join(f'<option value="{s["id"]}">{_e(s["label"])} wins</option>' for s in m["selections"])
            auto = ('<button class="sec" name="action" value="reprice">Auto-price</button>'
                    if m["kind"] != "custom" else "")
            toggle = ('<button class="sec" name="action" value="lock">Close betting</button>' if m["status"] == "open"
                      else '<button class="sec" name="action" value="open">Reopen betting</button>')
            by_value = ("" if m["kind"] not in ("prop", "total") else
                        f'<input class="n" name="value" placeholder="Result" title="What the stat came to">'
                        f'<button class="sec" name="action" value="settle_value" title="Settles this line'
                        f'{"" if m["parent_id"] else " and every alt line of it"} from the number">'
                        f'Settle at number</button>')
            buttons = (f'<button name="action" value="save">Save</button>{auto}{toggle}'
                       f'<select name="winner">{winner}<option value="push">Push (refund all)</option></select>'
                       f'<button class="sec" name="action" value="settle">Settle</button>{by_value}'
                       f'<button class="sec" name="action" value="void">Void</button>'
                       f'<button class="bad" name="action" value="delete">Delete</button>')
        result = f'<div class="small muted">{_e(m["result"])}</div>' if m["result"] else ""
        return (f'<form class="mk" method="post" action="/admin/markets/{m["id"]}">{self._hidden(request, ADMIN_COOKIE)}'
                f'<div class="t">{"↳ <span class=small>Alt line</span> " if m["parent_id"] else ""}'
                f'<b>{_e(self._title(m, name))}</b> <span class="chip">{_STATUS[m["status"]]}</span>{result}</div>'
                f'<div style="display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center">{line}{odds}</div>'
                f'<div style="display:flex;flex-wrap:wrap;gap:6px">{buttons}</div></form>')

    async def admin_matchup(self, request: web.Request) -> web.Response:
        self._need_admin(request)
        mu = self.book.matchup(_int(request.match_info["id"], 0))
        if not mu:
            raise web.HTTPNotFound(text="No such matchup")
        name = self._names()
        csrf = self._hidden(request, ADMIN_COOKIE)
        act = f'/admin/matchups/{mu["id"]}'
        everything = self.book.markets(mu["id"])
        markets = []
        for m in (m for m in everything if not m["parent_id"]):  # each main market, then its alt lines
            markets += [m] + sorted((a for a in everything if a["parent_id"] == m["id"]), key=lambda a: a["line"])
        live = mu["status"] in ("open", "locked")
        controls = []
        if inside := self.book.insider_bets(mu["id"]):
            rows = "".join(
                f'<tr><td>{_e(b["name"])}</td><td class="small">{_e(b["label"])}'
                f'{" (in a parlay)" if b["kind"] == "parlay" else ""}<br><span class="muted">{_e(b["why"])}</span></td>'
                f'<td class="n">{_money(b["stake"])}</td><td><form class="inline" style="margin:0" method="post" '
                f'action="{act}">{csrf}<input type="hidden" name="bet" value="{b["kind"]}:{b["id"]}">'
                f'<button class="bad" name="action" value="void_bet">Void (refund)</button></form></td></tr>'
                for b in inside)
            controls.append(f'<div class="card"><h2 style="margin-top:0">⚠️ Inside bets</h2><p class="small muted">'
                            f'Open bets by players in this game on things they control, placed before the rules '
                            f'stopped them or before the player was put in the lineup. Void one to cancel it and '
                            f'give the stake back.</p><div class="scroll"><table>{rows}</table></div></div>')
        if live:
            controls.append(
                f'<form class="inline" method="post" action="{act}">{csrf}'
                + (f'<button name="action" value="lock">Close all betting</button>' if mu["status"] == "open"
                   else f'<button name="action" value="open">Reopen betting</button>')
                + f'<button class="sec" name="action" value="auto">'
                  f'{"Turn off" if mu["auto_settle"] else "Turn on"} auto-settle</button>'
                  f'<button class="sec" name="action" value="void">Void everything (refund)</button></form>')
            games = "".join(f'<option value="{g["id"]}">#{g["id"]} {_e(g["home_team"])} {g["home_score"]} – '
                            f'{g["away_score"]} {_e(g["away_team"])}</option>' for g in self.db.recent_games(25))
            controls.append(
                f'<form class="inline" method="post" action="{act}">{csrf}<select name="game">{games}</select>'
                f'<button name="action" value="settle_game">Settle from this game</button></form>'
                if games else "")
            players = "".join(f'<option value="{_e(k)}">{_e(name(k))}</option>'
                              for k in mu["players1"] + mu["players2"])
            stats = "".join(f'<option value="{k}">{_e(lbl)}</option>' for k, lbl in bettable_stats())
            controls.append(
                f'<div class="card"><h2 style="margin-top:0">Add a player prop</h2>'
                f'<form class="inline" method="post" action="{act}">{csrf}<select name="player">{players}</select>'
                f'<select name="stat">{stats}</select><input class="n" name="line" placeholder="Line (auto)">'
                f'<button name="action" value="prop">Add over/under</button></form>'
                f'<h2>Add a custom bet</h2><form class="stack" style="max-width:none" method="post" action="{act}">'
                f'{csrf}<input name="title" placeholder="Question, e.g. First score is a pick-six" required>'
                f'<textarea name="options" rows="3" required placeholder="One option per line: label | odds&#10;'
                f'Yes | +400&#10;No | -600"></textarea><button name="action" value="custom">Add custom bet'
                f"</button></form></div>")
        controls.append(
            f'<form class="inline" method="post" action="{act}" onsubmit="return confirm(\'Delete matchup #{mu["id"]}? '
            f'Open bets on it are refunded, then it and its bets are removed for good, and the matchups after it '
            f'move up a number.\')">{csrf}<button class="bad" name="action" value="delete">Delete matchup</button></form>')
        auto = ("Auto-settle is <b>on</b>: the next game with at least half of these players settles it."
                if mu["auto_settle"] else "Auto-settle is <b>off</b>.")
        lineups = "<br>".join(f'<b>{_e(mu[t])}</b>: ' + ", ".join(_e(name(k)) for k in mu[p])
                              for t, p in (("team1", "players1"), ("team2", "players2")))
        if live:  # each player with a button to switch teams or take them out
            cols = []
            for t, p, other in (("team1", "players1", "team2"), ("team2", "players2", "team1")):
                rows = "".join(
                    f'<tr><td style="vertical-align:middle">{_e(name(k))}</td><td><form style="display:flex;gap:6px;margin:0;white-space:nowrap" '
                    f'method="post" '
                    f'action="{act}">{csrf}<input type="hidden" name="player" value="{_e(k)}">'
                    f'<button class="sec" name="action" value="move">Move to {_e(mu[other])} →</button>'
                    f'<button class="sec" name="action" value="remove" title="Take out of the matchup">✕</button>'
                    f"</form></td></tr>" for k in mu[p])
                cols.append(f'<div><h2 style="margin-top:0">{_e(mu[t])}</h2><table>{rows}</table></div>')
            lineups = (f'<div class="grid">{"".join(cols)}</div><p class="small muted">Moving a player re-prices '
                       f"the winner odds; their props stay. Taking one out voids their props (stakes returned). "
                       f"Add players or rename teams under Edit matchup below.</p>")
        edit = ("" if not live else
                f'<details class="card"><summary><b>Edit matchup</b> (title, team names, players)</summary>'
                f'<form method="post" action="{act}">{csrf}'
                f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0">'
                f'<input name="title" value="{_e(mu["title"])}" placeholder="Title">'
                f'<input name="team1" value="{_e(mu["team1"])}" placeholder="Team 1 name">'
                f'<input name="team2" value="{_e(mu["team2"])}" placeholder="Team 2 name"></div>'
                f'{self._pick_table(mu)}'
                f'<p class="small muted">Players with no games yet (comma-separated names):</p>'
                f'<div style="display:flex;gap:8px;flex-wrap:wrap"><input name="extra1" placeholder="Team 1 extras">'
                f'<input name="extra2" placeholder="Team 2 extras"></div>'
                f'<p class="small muted">Taking a player out voids their props (stakes returned). Added players get '
                f'the usual props. Changing the lineups re-prices the winner odds.</p>'
                f'<button name="action" value="edit">Save matchup</button></form></details>')
        body = (self._admin_bar(request) + _flash(request)
                + f'<h1>#{mu["id"]} {_e(mu["title"])} <span class="chip">{_STATUS[mu["status"]]}</span></h1>'
                  f'<div class="card">{lineups}<span class="small">{auto if live else ""}'
                  f'{" Settled from game #" + str(mu["game_id"]) + "." if mu["game_id"] else ""} '
                  f'<a href="/book/matchups/{mu["id"]}">Bettor view →</a></span></div>'
                + self._leftover(mu, markets, name) + "".join(controls) + edit
                + '<div class="card"><h2 style="margin-top:0">Markets</h2>'
                  '<p class="small muted">Edit a line and press Auto-price to re-price it from the stats, or type '
                  'your own odds and Save. Alt lines show up under their bet once someone picks one on a slider; '
                  'closing, voiding or settling a bet at a number does its alt lines too. Changes go live right away: bettors\' open pages update within a few '
                  'seconds. They apply to new bets only; bets already placed keep their odds.</p>'
                + "".join(self._admin_market(request, m, name) for m in markets) + "</div>")
        return self._page(f'Admin: {mu["title"]}', body)

    def _leftover(self, mu: dict, markets: list[dict], name) -> str:
        """Warning for bets the game couldn't settle on its own (who won when the bot can't tell which
        side each lineup played on, custom bets): nobody is paid on them until they're settled here."""
        left = [m for m in markets if m["status"] in ("open", "locked")]
        if not mu["game_id"] or not left:
            return ""
        return (f'<div class="card" style="border-color:#c0392b"><b class="lost">Game #{mu["game_id"]} settled '
                f'the rest, but these still need settling by hand</b> (pick the result and press Settle below; '
                f'nobody is paid on them until you do): '
                + ", ".join(_e(self._title(m, name)) for m in left) + "</div>")

    async def admin_matchup_action(self, request: web.Request) -> web.Response:
        self._need_admin(request)
        form = await request.post()
        self._check_csrf(request, form, ADMIN_COOKIE)
        mid = _int(request.match_info["id"], 0)
        mu = self.book.matchup(mid)
        if not mu:
            raise web.HTTPNotFound(text="No such matchup")
        back = f"/admin/matchups/{mid}"
        action = form.get("action")
        try:
            if action in ("lock", "open"):
                self.book.set_matchup_status(mid, "locked" if action == "lock" else "open")
                msg = "Betting closed." if action == "lock" else "Betting reopened."
            elif action in ("move", "remove"):
                key = str(form.get("player", ""))
                p1, p2 = list(mu["players1"]), list(mu["players2"])
                if key not in p1 + p2:
                    raise BookError("That player isn't in this matchup.")
                src, dst = (p1, p2) if key in p1 else (p2, p1)
                src.remove(key)
                if action == "move":
                    dst.append(key)
                self.book.edit_matchup(mid, mu["title"], mu["team1"], mu["team2"], p1, p2, self._names())
                to = mu["team1"] if dst is p1 else mu["team2"]
                msg = (f"Moved {self._names()(key)} to {to}; winner odds re-priced." if action == "move" else
                       f"Took {self._names()(key)} out; their props are voided and stakes returned.")
            elif action == "edit":
                sides = self._sides(form)
                msg = self.book.edit_matchup(mid, str(form.get("title", "")), str(form.get("team1", "")),
                                             str(form.get("team2", "")), sides["1"], sides["2"], self._names())
            elif action == "delete":
                self.book.delete_matchup(mid)
                raise _redirect("/admin", f"Deleted matchup #{mid} ({mu['title']}); open bets on it were refunded "
                                          f"and the matchups after it moved up a number.")
            elif action == "void_bet":
                kind, _, bid = str(form.get("bet", "")).partition(":")
                self.book.void_bet(kind, _int(bid, 0))
                msg = "Bet voided; the stake was returned."
            elif action == "auto":
                self.book.set_auto_settle(mid, not mu["auto_settle"])
                msg = "Auto-settle turned " + ("off." if mu["auto_settle"] else "on.")
            elif action == "void":
                self.book.void_matchup(mid)
                msg = "Matchup voided; every stake was returned."
            elif action == "settle_game":
                res = self.book.settle_from_game(mid, _int(form.get("game"), 0))
                msg = f"Settled {res['settled']} markets."
                if res["left"]:
                    msg += " Settle these by hand: " + ", ".join(res["left"])
            elif action == "prop":
                line = _float(form.get("line")) if str(form.get("line", "")).strip() else None
                key = str(form.get("player", ""))
                if key not in mu["players1"] + mu["players2"]:
                    raise BookError("Pick a player from this matchup.")
                self.book.add_prop(mid, key, str(form.get("stat", "")), line, names=self._names())
                msg = f"Added {self._names()(key)}: {stat_label(str(form.get('stat', '')))}."
            elif action == "custom":
                options = []
                for row in str(form.get("options", "")).splitlines():
                    if not row.strip():
                        continue
                    lbl, _, odds = row.rpartition("|")
                    o = _int(odds)
                    if not lbl.strip() or o is None or -100 < o < 100:
                        raise BookError(f"Couldn't read “{row.strip()}”. Write each option as: label | odds")
                    options.append((lbl.strip(), o))
                self.book.add_custom(mid, str(form.get("title", "")), options)
                msg = "Custom bet added."
            else:
                raise BookError("Unknown action.")
        except BookError as e:
            raise _redirect(back, str(e), err=True)
        raise _redirect(back, msg)

    async def admin_market_action(self, request: web.Request) -> web.Response:
        self._need_admin(request)
        form = await request.post()
        self._check_csrf(request, form, ADMIN_COOKIE)
        m = self.book.market(_int(request.match_info["id"], 0))
        if not m:
            raise web.HTTPNotFound(text="No such market")
        back = f"/admin/matchups/{m['matchup_id']}"
        m["title"] = self._title(m)
        action = form.get("action")
        try:
            if action in ("save", "reprice"):
                line = _float(form.get("line")) if "line" in form else None
                if "line" in form and line is None:
                    raise BookError("The line must be a number.")
                odds = {s["id"]: _int(form.get(f"odds_{s['id']}")) for s in m["selections"]}
                self.book.update_market(m["id"], line, odds, reprice=action == "reprice")
                msg = f"{m['title']}: saved."
            elif action in ("lock", "open"):
                self.book.set_market_status(m["id"], "locked" if action == "lock" else "open")
                msg = f"{m['title']}: betting {'closed' if action == 'lock' else 'reopened'}."
            elif action == "settle":
                w = form.get("winner")
                self.book.settle_market(m["id"], None if w == "push" else _int(w, -1))
                msg = f"{m['title']}: settled and paid out."
            elif action == "settle_value":
                value = _float(form.get("value"))
                if value is None:
                    raise BookError("Type the number the stat came to, then press Settle at number.")
                n = self.book.settle_value(m["id"], value)
                msg = f"{m['title']}: settled at {value:g} ({n} line{'s' * (n != 1)}) and paid out."
            elif action == "void":
                self.book.void_market(m["id"])
                msg = f"{m['title']}: voided, stakes returned."
            elif action == "unsettle":
                self.book.unsettle_market(m["id"])
                msg = f"{m['title']}: settlement undone (payouts taken back, bets pending again)."
            elif action == "delete":
                self.book.delete_market(m["id"])
                msg = f"{m['title']}: deleted, stakes returned."
            else:
                raise BookError("Unknown action.")
        except BookError as e:
            raise _redirect(back, str(e), err=True)
        raise _redirect(back, msg)

    async def admin_bettors(self, request: web.Request) -> web.Response:
        self._need_admin(request)
        csrf = self._hidden(request, ADMIN_COOKIE)
        name, names = self._names(), Names(self.db)
        everyone = sorted(((u.lower(), names.dashed(u)) for u in self.db.known_usernames()),
                          key=lambda kv: name(kv[0]).lower())

        def plays_as(r) -> str:
            auto = self.book.player_key(r["id"]) if r["player_key"] is None else None
            opts = [("auto", f"Auto: {name(auto)}" if auto else "Auto: not a player"), ("none", "Not a player")]
            cur = "auto" if r["player_key"] is None else "none" if not r["player_key"] else r["player_key"]
            opts += everyone
            return (f'<form class="inline" style="margin:0" method="post" action="/admin/bettors/{r["id"]}">{csrf}'
                    f'<select name="player" aria-label="League player">'
                    + "".join(f'<option value="{_e(k)}"{" selected" if k == cur else ""}>{_e(lbl)}</option>'
                              for k, lbl in opts)
                    + '</select><button class="sec" name="action" value="player">Save</button></form>')
        rows = "".join(
            f'<tr><td>{_e(r["name"])}</td><td>{plays_as(r)}</td><td class="n">{_money(r["balance"])}</td>'
            f'<td class="n">{_money(r["at_risk"])}</td><td class="n">{r["bets"]}</td>'
            f'<td><form class="inline" style="margin:0" method="post" action="/admin/bettors/{r["id"]}">{csrf}'
            f'<input class="n" name="amount" placeholder="+500"><button name="action" value="adjust">Add</button>'
            f'<input class="n" name="password" placeholder="New password">'
            f'<button class="sec" name="action" value="password">Set password</button>'
            f'<button class="bad" name="action" value="delete">Delete</button></form></td></tr>'
            for r in self.book.leaderboard())
        body = (self._admin_bar(request) + _flash(request) + "<h1>Bettors</h1>"
                "<p class='muted small'>Add money with a positive amount, take it away with a negative one. "
                "Set password resets a bettor's password if they forgot it. "
                "Deleting a bettor removes their bets too.</p>"
                "<p class='muted small'><b>Plays as</b> is the league player an account belongs to. That player "
                "can't bet on their own stats in a game they play in. "
                "Auto matches the account's name to a league player (PLAYER_NAMES or /admin setname).</p>"
                + (f'<div class="card scroll"><table><tr><th>Name</th><th>Plays as</th><th class="n">Balance</th>'
                   f'<th class="n">In open bets</th><th class="n">Bets</th><th></th></tr>{rows}</table></div>'
                   if rows else "<p class='muted'>Nobody has joined yet.</p>"))
        return self._page("Bettors", body)

    async def admin_bettor_action(self, request: web.Request) -> web.Response:
        self._need_admin(request)
        form = await request.post()
        self._check_csrf(request, form, ADMIN_COOKIE)
        b = self.book.bettor(_int(request.match_info["id"], 0))
        if not b:
            raise web.HTTPNotFound(text="No such bettor")
        if form.get("action") == "delete":
            self.book.delete_bettor(b["id"])
            raise _redirect("/admin/bettors", f"Deleted {b['name']}.")
        if form.get("action") == "player":
            v = str(form.get("player", "auto"))
            self.book.set_player_key(b["id"], None if v == "auto" else "" if v == "none" else v.lower())
            who = self.book.player_key(b["id"])
            raise _redirect("/admin/bettors", f"{b['name']} plays as {self._names()(who)}." if who
                            else f"{b['name']} isn't a league player.")
        if form.get("action") == "password":
            try:
                self.book.set_password(b["id"], str(form.get("password", "")))
            except BookError as e:
                raise _redirect("/admin/bettors", str(e), err=True)
            raise _redirect("/admin/bettors", f"{b['name']}'s password is set. Tell them to log in with it.")
        amount = _float(form.get("amount"))
        if not amount:
            raise _redirect("/admin/bettors", "Enter an amount, like 500 or -500.", err=True)
        self.book.adjust_balance(b["id"], amount)
        raise _redirect("/admin/bettors", f"{b['name']}: {'+' if amount > 0 else ''}{_money(amount)}.")
