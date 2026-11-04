(function(){
'use strict';
const $=id=>document.getElementById(id);
const BASE=new URL('./',location.href).href;           // http://127.0.0.1:PORT/<secret>/
const api=(p,opt)=>fetch(BASE+'api/'+p,Object.assign({cache:'no-store',headers:{'X-Debrief':'1'}},opt||{})).then(async r=>{
  let j=null;try{j=await r.json()}catch(e){}
  if(!r.ok&&!(j&&j.error))throw new Error('Debrief returned an error ('+r.status+').');
  return j||{};
});
const post=(p,body)=>api(p,{method:'POST',headers:{'X-Debrief':'1','Content-Type':'application/json'},body:JSON.stringify(body||{})});
function h(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
const ok=v=>typeof v==='number'&&isFinite(v);
const fmtT=s=>{if(!ok(s))return '–';const m=Math.floor(s/60);return m+':'+(s-m*60).toFixed(3).padStart(6,'0')};
const fmtD=v=>ok(v)?(v<0?'−':'+')+Math.abs(v).toFixed(3)+' s':'–';
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmtDate=s=>{const m=/^(\d{4})-(\d\d)-(\d\d)/.exec(s||'');return m?MON[+m[2]-1]+' '+(+m[3])+', '+m[1]:(s||'')};
const fmtWhen=sec=>{if(!ok(sec))return '';const d=new Date(sec*1000);return MON[d.getMonth()]+' '+d.getDate()+', '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')};
// "1:23.456", "83.456" or "1:23" -> seconds
function parseLap(s){s=String(s||'').trim().replace(',','.');let m=/^(\d{1,2}):(\d{1,2}(?:\.\d{1,3})?)$/.exec(s);
  if(m){const sec=+m[2];if(sec>=60)return null;return +m[1]*60+sec}
  m=/^\d{1,4}(?:\.\d{1,3})?$/.exec(s);return m?+s:null}

/* ---------- connection lost */
let lost=false;
function connectionLost(){if(lost)return;lost=true;banner('Debrief has stopped. Close this window and open Debrief again.',true,0)}
/* ---------- 2.18: plain words in the window. Debrief's job and check lines are written for its log too, so a few carry
   debug text: an exception's name, a request count, a file path, an event id. The window says the same thing plainly,
   or leaves the line out, and keeps the lines as they were under "Details for a bug report". */
const DEBUG_ONLY=[/^\s*(?:Garage 61 )?requests used:?\s*\d+\.?\s*$/i,/^\s*Saved (?:one|\d+) sample lap record/i,/^\s*Library folder:\s/i,/^\s*Traceback \(most recent call last\)/];
function plainText(t){let x=String(t==null?'':t);if(!x.trim())return '';
  if(DEBUG_ONLY.some(r=>r.test(x)))return '';
  x=x.replace(/\s*\((?:[A-Za-z_][\w.]*\.)?[A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning|Interrupt|Exit)\)/g,'')   // "(IbtError)"
     .replace(/\s*(?:Garage 61 )?requests used:\s*\d+\.?/gi,'')
     .replace(/\bLoading event [A-Za-z0-9_-]+(\.\.\.|…)?/g,'Loading your race$1')
     .replace(/\s+\.(?=\s|$)/g,'.').replace(/\s{2,}/g,' ').trim();
  return x}
// lines as the window shows them, plus a fold with the originals when anything was changed or left out
function plainLines(lines){const raw=(lines||[]).map(t=>String(t==null?'':t)).filter(t=>t.trim());
  return {shown:raw.map(plainText).filter(Boolean),changed:raw.some(t=>plainText(t)!==t.trim())?raw:null}}
function detailsFold(raw,label){const d=h('details','dbg');d.append(h('summary',null,label||'Details for a bug report'));
  const pre=h('pre','mono dbgtext',raw.join('\n'));d.append(pre);return d}
// "broken.ibt: The telemetry file couldn't be read" notes become one line with a fold that names the files
function fileNotes(notes){const out=[],files=[];(notes||[]).forEach(n=>{const t=String(n||'');
  if(/^[^:\n]{1,200}\.(?:ibt|json|csv|gz)\s*:/i.test(t))files.push(t);else if(plainText(t))out.push(plainText(t))});
  return {notes:out,files}}
function fileNotesEl(files){if(!files.length)return null;const d=h('details','sub dbg');
  d.append(h('summary',null,plural(files.length,'file')+' couldn\'t be read. Show which'));const u=h('ul','plain mono');
  files.slice(0,50).forEach(f=>u.append(h('li',null,plainText(f))));d.append(u);return d}
try{window.debriefUi=Object.freeze({plainText,plainLines,fileNotes,keyList:()=>keyList(),pageKeys:()=>PAGE_KEYS.slice(),sure:k=>SURE[k]&&SURE[k][0]})}catch(e){}
// 2.6: error banners stay until the close button is pressed; other banners still fade after a few seconds
function banner(msg,bad,ms){msg=plainText(msg)||msg;const b=$('banner');const ba=$('bannerAct');if(ba)ba.remove();const so=$('shareOpen');if(so)so.remove();$('bannerText').textContent=msg;b.className='banner'+(bad?' bad':'');b.hidden=false;clearTimeout(banner.t);
  if(ms!==0&&!bad)banner.t=setTimeout(()=>b.hidden=true,ms||5000)}
$('bannerClose').addEventListener('click',()=>{clearTimeout(banner.t);$('banner').hidden=true});
// 2.18: a banner with one action button (e.g. Rebuild), gone with the next banner
function bannerAction(msg,label,fn){banner(msg,false,12000);const b=h('button','btn ghost',label);b.type='button';b.id='bannerAct';b.addEventListener('click',()=>{$('banner').hidden=true;fn()});$('bannerClose').before(b)}

/* ---------- heartbeat (a worker keeps ticking while the window is minimised) */
const PING=BASE+'api/ping';
function ping(){fetch(PING,{method:'POST',headers:{'X-Debrief':'1'}}).catch(()=>{})}
// 2.6: the worker is the only 5 s ping (it keeps ticking while the window is minimised); the main thread
// takes over only if the worker can't be made
ping();
try{const src='setInterval(function(){fetch('+JSON.stringify(PING)+',{method:"POST",headers:{"X-Debrief":"1"}}).catch(function(){})},5000)';
  new Worker(URL.createObjectURL(new Blob([src],{type:'text/javascript'})))}catch(e){setInterval(ping,5000)}
document.addEventListener('visibilitychange',ping);
// 2.8: the window's size, so it opens the same size next time (saved a moment after you stop resizing)
let winSent=null,winPending=false;
function saveWindowSize(keep){clearTimeout(saveWindowSize.t);winPending=false;const w=window.outerWidth,he=window.outerHeight;
  if(!(w>0&&he>0)||winSent===w+'x'+he||document.fullscreenElement)return;winSent=w+'x'+he;
  fetch(BASE+'api/window',{method:'POST',keepalive:!!keep,headers:{'X-Debrief':'1','Content-Type':'application/json'},body:JSON.stringify({width:w,height:he})}).catch(()=>{})}
addEventListener('resize',()=>{winPending=true;clearTimeout(saveWindowSize.t);saveWindowSize.t=setTimeout(saveWindowSize,800)});
addEventListener('pagehide',()=>{if(winPending)saveWindowSize(true)});
addEventListener('pagehide',()=>{try{fetch(BASE+'api/bye',{method:'POST',headers:{'X-Debrief':'1'},keepalive:true})}catch(e){}});

/* ---------- brand: the team name and join copy come from /api/brand (debrief/brand.py). 2.18: the app is
   "Debrief · by Black Magik Studios"; the team name only appears in team and league features */
const BRAND0={app_name:'Debrief',maker:'Black Magik Studios',team_slug:'midfield-menace',team_name:'Midfield Menace',team_short:'MM',team_url:'https://garage61.net',
  join_title:'Join Midfield Menace on Garage 61',join_text:'Team comparisons need you on the Midfield Menace team on Garage 61. Ask a teammate to invite you, then press Refresh. Your own reviews, practice and prep work without it.'};
let brand=Object.assign({},BRAND0);
const appTitle=()=>(brand.app_name||'Debrief')+' · by '+(brand.maker||BRAND0.maker);
function applyBrand(){
  document.querySelectorAll('.brandName').forEach(e=>{e.textContent=brand.team_name});
  document.querySelectorAll('.joinTitle').forEach(e=>{e.textContent=brand.join_title});
  document.querySelectorAll('.joinText').forEach(e=>{e.textContent=brand.join_text});
  document.querySelectorAll('a.joinLink').forEach(e=>{if(/^https:\/\//.test(brand.team_url||''))e.href=brand.team_url});
  $('brandMaker').textContent='by '+(brand.maker||BRAND0.maker);$('teamTitle').textContent=teamName||brand.team_name;
  if(!FRAME_VIEWS.includes(current.view))document.title=appTitle()}
async function loadBrand(){try{const j=await api('brand');
    if(j&&typeof j==='object')Object.keys(BRAND0).forEach(k=>{if(typeof j[k]==='string'&&j[k].trim())brand[k]=j[k].trim()});
    if(j&&typeof j==='object'&&typeof creditsHelp==='function')creditsHelp(j);
    if(j&&typeof j.copyright==='string'&&j.copyright&&$('aboutCopy'))$('aboutCopy').textContent=brand.app_name+' \u00b7 by '+brand.maker+'. '+j.copyright+'.'}catch(e){}
  if(!teamSlug)teamSlug=brand.team_slug;applyBrand();return brand}
// Team, Prepare and the weekly leaderboards show a join panel when Garage 61 says you aren't on the team
const notMember=j=>!!j&&typeof j==='object'&&(j.membership==='not_member'||(j.weekly&&typeof j.weekly==='object'&&j.weekly.membership==='not_member'));

/* ---------- theme */
const THEMES=['auto','light','dark'];
function getTheme(){try{const t=localStorage.getItem('debrief-theme');return THEMES.includes(t)?t:'auto'}catch(e){return 'auto'}}
function applyTheme(doc,t){if(!doc)return;const r=doc.documentElement;if(t==='auto')r.removeAttribute('data-theme');else r.setAttribute('data-theme',t)}
// library pages run sandboxed (no same-origin): the window can't reach into them, so the theme goes over
// postMessage to the small bridge script the server adds to each page ('*': a sandboxed page's origin is "null")
function themeFrame(t){try{const w=$('frame').contentWindow;if(w)w.postMessage({type:'debrief-theme',theme:t},'*')}catch(e){}}
function setTheme(t){try{localStorage.setItem('debrief-theme',t)}catch(e){}
  applyTheme(document,t);themeFrame(t);
  document.querySelectorAll('[data-theme-set]').forEach(b=>{const on=b.dataset.themeSet===t;b.setAttribute('aria-checked',String(on));b.tabIndex=on?0:-1})}
document.querySelectorAll('[data-theme-set]').forEach(b=>b.addEventListener('click',()=>setTheme(b.dataset.themeSet)));
// radio groups: one tab stop (the checked radio); arrow keys move the choice, as the ARIA radio pattern promises
function radioKeys(group){group.addEventListener('keydown',e=>{const rs=[...group.querySelectorAll('[role=radio]')];const i=rs.indexOf(document.activeElement);if(i<0)return;
  let j=null;if(e.key==='ArrowRight'||e.key==='ArrowDown')j=(i+1)%rs.length;else if(e.key==='ArrowLeft'||e.key==='ArrowUp')j=(i-1+rs.length)%rs.length;else if(e.key==='Home')j=0;else if(e.key==='End')j=rs.length-1;
  if(j==null)return;e.preventDefault();rs[j].click();rs[j].focus()})}
document.querySelectorAll('[role=radiogroup]').forEach(radioKeys);
setTheme(getTheme());

/* ---------- narrow window: the sidebar slides in from a Menu button */
$('btnMenu').addEventListener('click',()=>{const on=!document.body.classList.contains('navopen');document.body.classList.toggle('navopen',on);$('btnMenu').setAttribute('aria-expanded',String(on));if(on){const f=document.querySelector('.side .navbtn');f&&f.focus()}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('navopen')){document.body.classList.remove('navopen');$('btnMenu').setAttribute('aria-expanded','false');$('btnMenu').focus()}});
new MutationObserver(()=>$('btnMenu').setAttribute('aria-expanded',String(document.body.classList.contains('navopen')))).observe(document.body,{attributes:true,attributeFilter:['class']});

/* ---------- keyboard (2.18): Ctrl+1 to Ctrl+9 and Ctrl+0 open the sidebar's pages in its order (the sub-pages under
   Progress have no key), Ctrl+Enter = Review last race, Ctrl+, = Settings, F1 or ? = the key list, J and K step through
   your reviews. Ctrl+R no longer starts a review (a slow job on a key you might press to reload); it doesn't reload
   either, since a reload drops the window's place and can close Debrief, so it says where Review last race went. */
const PAGE_KEYS=[...document.querySelectorAll('.nav .navbtn[data-nav]:not(.sub)')].map(b=>b.dataset.nav).slice(0,10);
const keyOfPage=n=>{const i=PAGE_KEYS.indexOf(n);return i<0?null:'Ctrl+'+(i===9?0:i+1)};
const KEY_MSG=/^(review|reviewgo|settings|help|prev|next|rebuild|page[0-9])$/;
let oldKeyShown=false;
function shortcut(k){if(k==='reviewgo'){startReview();return true}if(k==='rebuild'){if(current.view==='review'&&current.dir)rebuildReview(current.dir);return true}if(k==='settings'){openSettings();return true}
  if(k==='review'){if(!oldKeyShown){oldKeyShown=true;banner('Review last race is Ctrl+Enter now (or the button at the top of the sidebar). Ctrl+R does nothing in Debrief.',false,9000)}return true}
  if(k==='help'){if(!$('dlgHelp').open)openHelp('keys');else showHelp('keys');return true}
  if(document.querySelector('dialog[open]'))return false;
  if(k==='prev'||k==='next'){stepReview(k);return true}
  const m=/^page([0-9])$/.exec(k);if(m){const n=PAGE_KEYS[m[1]==='0'?9:+m[1]-1];if(n)goNav(n);return true}
  return false}
const typing=t=>!!(t&&t.closest&&t.closest('input,select,textarea,[contenteditable]'));
document.addEventListener('keydown',e=>{if(!(e.ctrlKey||e.metaKey)||e.altKey)return;const k=e.key.toLowerCase();
  if(k==='r'&&!e.shiftKey){e.preventDefault();shortcut('review')}
  else if(k==='enter'&&!e.shiftKey){if(typing(e.target))return;e.preventDefault();shortcut('reviewgo')}
  else if(k===','||e.code==='Comma'){e.preventDefault();shortcut('settings')}
  else if(!e.shiftKey&&/^[0-9]$/.test(k)){e.preventDefault();shortcut('page'+k)}
  else if(e.shiftKey&&k==='m'){e.preventDefault();coachMuteKey()}});
document.addEventListener('keydown',e=>{if(e.key==='F1'&&!e.ctrlKey&&!e.metaKey&&!e.altKey){e.preventDefault();shortcut('help');return}
  if(e.ctrlKey||e.metaKey||e.altKey)return;if(typing(e.target))return;
  if(e.key==='?'){e.preventDefault();shortcut('help');return}
  if(e.shiftKey)return;const k=e.key.toLowerCase();if(k!=='j'&&k!=='k')return;
  if(shortcut(k==='j'?'prev':'next'))e.preventDefault()});
// the key list (Help > Keyboard keys, F1 or ?), made from the sidebar so it can't drift from it
function keyList(){const nm=n=>{const b=document.querySelector('.nav .navbtn[data-nav="'+n+'"]');return b?b.textContent.trim():n};
  return [['Ctrl+Enter','Review last race'],['Ctrl+,','Settings']].concat(PAGE_KEYS.map(n=>[keyOfPage(n),nm(n)]),
    [['F1 or ?','This list of keys'],['J','The review before this one'],['K','The review after this one'],['Tab','Move to the next button or chart'],['Arrow keys','Move along a chart that has focus'],['Esc','Close a window like this one']])}

/* ---------- views */
const VIEWS=['vFrame','vToday','vWelcome','vJob','vSettings','vPrep','vGoals','vTeam','vSessions','vSetupLab','vCoach','vPractice','vRacing','vCars'];
const FRAME_VIEWS=['review','library','season','skills','goalCharts','prepPage','teamPage','sample'];
const NAV_OF={today:'today',weekly:'team',sync:'sessions',sessions:'sessions',library:'library',season:'season',skills:'skills',goals:'goals',goalCharts:'goals',prep:'prep',prepPage:'prep',schedule:'prep',team:'team',teamPage:'team',settings:'settings',setuplab:'setuplab',coach:'coach',practice:'practice',racing:'racing',cars:'cars'};
let current={view:null,dir:null};
function show(view){VIEWS.forEach(v=>$(v).hidden=(v!==view));if(view!=='vFrame')document.title=appTitle()}
function markNav(){
  const nav=NAV_OF[current.view]||null;
  document.querySelectorAll('.navbtn').forEach(b=>{const on=b.dataset.nav===nav;b.classList.toggle('active',on);if(on)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current')});
  document.querySelectorAll('.rev').forEach(b=>{const on=current.view==='review'&&b.dataset.dir===current.dir;b.classList.toggle('active',on);if(on)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current')});
  document.body.classList.remove('navopen');
  frameBar();lastRaceCard();renderFirstRun();
}
// where the frame really is: links inside it (library -> review) don't change f.src, and the sandboxed page
// can't be looked into, so its bridge script tells us (see the 'message' listener below)
let frameHref=null;
const noQuery=u=>{const s=String(u||'');const i=s.indexOf('#');const a=i<0?s:s.slice(0,i),hs=i<0?'':s.slice(i);return a.split('?')[0]+hs};
// a URL the frame isn't on yet, so setting src always loads the page again (the server ignores the query)
// 2.8: a thin bar across the top while a page in the frame loads (a big review can take a few seconds)
function loading(on,text){clearTimeout(loading.t);$('loadBar').hidden=!on;if(!on)return;
  $('loadText').textContent=text||(current.view==='review'?'Opening this review...':current.view==='sample'?'Making the sample review...':'Opening...');
  loading.t=setTimeout(()=>{$('loadBar').hidden=true},30000)}
$('frame').addEventListener('load',()=>loading(false));
function fresh(u){const i=u.indexOf('#');const a=i<0?u:u.slice(0,i),hs=i<0?'':u.slice(i);return a.split('?')[0]+'?r='+Date.now().toString(36)+hs}
function openFrame(rel,reload){const f=$('frame');const want=BASE+rel;const wantDoc=want.split('#')[0];
  const at=noQuery(frameHref||f.src);const atDoc=at.split('#')[0];
  if(at===want&&!reload){show('vFrame');return}
  // same page, other #section: a hash-only change jumps there (the library page follows hashchange);
  // the same address again (or a page the frame left) loads fresh
  if(!reload&&atDoc===wantDoc&&want.includes('#'))f.src=f.src.split('#')[0].split('?')[0]===wantDoc?f.src.split('#')[0]+want.slice(wantDoc.length):want;
  else{loading(true);f.src=fresh(want)}
  frameHref=want;
  // keep the current screen until a big review page has loaded, so there's no blank flash
  if($('vFrame').hidden){clearTimeout(openFrame.t);openFrame.t=setTimeout(()=>show('vFrame'),4000);
    f.addEventListener('load',()=>{clearTimeout(openFrame.t);if(FRAME_VIEWS.includes(current.view))show('vFrame')},{once:true})}}
function openReview(dir){current={view:'review',dir};openFrame('lib/reviews/'+encodeURIComponent(dir)+'/index.html');markNav();loadResult(dir)}
function openLibrary(){
  if(!libFolder){current={view:'welcome'};welcome();return}
  current={view:'library',dir:null};openFrame('lib/index.html#overview');markNav()}
// 2.6: the newest review, pinned above the Library page so the last race is always one click away
function reviewsByDate(){const all=[];groupsCache.forEach(g=>(g.reviews||[]).forEach(r=>all.push(Object.assign({track:g.track,car:g.car},r))));
  all.sort((a,b)=>String(b.started_at||b.date||'').localeCompare(String(a.started_at||a.date||''))||String(b.dir).localeCompare(String(a.dir)));return all}
function newestReview(){return reviewsByDate()[0]||null}
// J = the race before this one, K = the race after it; from any other page either key opens the newest
function stepReview(k){const all=reviewsByDate();if(!all.length)return;
  const i=current.view==='review'?all.findIndex(r=>r.dir===current.dir):-1;
  const to=i<0?0:(k==='prev'?i+1:i-1);
  if(to<0||to>=all.length){banner(k==='prev'?'That\'s your oldest review.':'That\'s your newest review.');return}
  openReview(all[to].dir);const b=document.querySelector('.rev.active');if(b&&b.scrollIntoView)b.scrollIntoView({block:'nearest'})}
function lastRaceCard(){const box=$('lastRace');const r=current.view==='library'?newestReview():null;box.hidden=!r;if(!r)return;
  $('lrWhat').textContent=r.track+' · '+r.car;$('lrWhen').textContent=[r.session,fmtDate(r.date),r.best?'best '+r.best:''].filter(Boolean).join(' · ');
  $('lrOpen').onclick=()=>openReview(r.dir)}
function openSeason(){
  if(!libFolder){current={view:'welcome'};welcome();return}
  current={view:'season',dir:null};openFrame('lib/index.html#season');markNav()}
// Skills = the driver profile on the library page (#skills); the bar above it says how many races have scores
function openSkills(){
  if(!libFolder){current={view:'welcome'};welcome();return}
  current={view:'skills',dir:null};openFrame('lib/index.html#skills');markNav();loadProfile()}
// the charts behind the Goals page live on the library page; the sidebar keeps Goals lit
function openGoalCharts(){
  if(!libFolder){current={view:'welcome'};welcome();return}
  current={view:'goalCharts',dir:null};openFrame('lib/index.html#goals',true);markNav()}   // always fresh: goals may have just changed
// a made-up race to look at while the token is on its way; never saved in the library
function openSample(){current={view:'sample',dir:null};openFrame('sample/index.html');markNav()}
function openPage(rel,view){current={view,dir:null,page:rel};openFrame('lib/'+rel.split('/').map(encodeURIComponent).join('/'));markNav();if(view==='teamPage')loadWeekly()}
function openSettings(){current={view:'settings',dir:null};show('vSettings');markNav();loadSettings();loadTeams(false)}
function openJob(){current={view:'job',dir:null};show('vJob');markNav()}
/* ---------- Today (2.18): the start page. One card, "Your #1 thing right now", picked by debrief/today.py from what the
   other pages already worked out (the practice plan, a review's Fix first, a Racing habit, the setup steps), then the
   last race and the last practice, then links to the rest. Before any data it shows the setup steps. */
// one sureness scale for every page's badges (today.SURE_WORDS): Sure, Likely, A hint, A guess
const SURE={sure:['Sure','ok'],likely:['Likely',''],hint:['A hint','no'],guess:['A guess','no']};
function sureBadge(level,why){const x=SURE[level];if(!x)return null;const b=h('span','pill sure'+(x[1]?' '+x[1]:''),x[0]);b.title=why||'How sure this is';return b}
let td={j:null,seq:0};
function openToday(){current={view:'today',dir:null};show('vToday');markNav();loadToday()}
async function loadToday(){const seq=++td.seq;if(!td.j)$('tdTitle').textContent='Loading...';
  let j;try{j=await api('today')}catch(e){connectionLost();return}
  if(seq!==td.seq)return;td.j=j;renderToday();renderFirstRun()}
function tdGo(a){if(!a)return;
  if(a.go==='coach')openCoach(a.car_path?{car_path:a.car_path,track_code:a.track_code}:undefined);
  else if(a.go==='review'&&a.dir)openReview(a.dir);
  else if(a.go==='setup'){const f=FR_ACT[a.step];if(f)f[1]();else openSettings()}
  else if(a.go==='session'&&a.id)openPractice(a.id);
  else goNav(a.go)}
function tdBtn(a,primary){const b=h('button','btn'+(primary?' primary':''),a.label);b.type='button';b.addEventListener('click',()=>tdGo(a));return b}
function renderToday(){const j=td.j;if(!j)return;
  $('tdEyebrow').textContent=firstName?'Welcome back, '+firstName:'Debrief';
  if(!j.ok){$('tdTitle').textContent=plainText(j.error)||'Today didn\'t load.';$('tdLines').replaceChildren();$('tdActs').replaceChildren();return}
  const O=j.one||{};
  $('tdWhere').textContent=O.where||'';$('tdWhere').hidden=!O.where;
  $('tdTitle').textContent=O.title||'';
  $('tdLines').replaceChildren(...(O.lines||[]).map(([k,v])=>{const q=h('p');if(k)q.append(h('b',null,k+': '));q.append(document.createTextNode(v||''));return q}));
  $('tdWhy').textContent=O.why||'';$('tdWhy').hidden=!O.why;
  const pill=$('tdSure'),x=SURE[O.sureness];pill.hidden=!x;if(x){pill.className='pill sure'+(x[1]?' '+x[1]:'');pill.textContent=x[0];pill.title='How sure this is: '+x[0]}
  const acts=[];if(O.action)acts.push(tdBtn(O.action,true));if(O.more)acts.push(tdBtn(O.more,false));$('tdActs').replaceChildren(...acts);
  // last race
  const R=j.last_race,rb=$('tdRaceBody');$('tdRaceWhen').textContent=R?[R.session,fmtDate(R.date)].filter(Boolean).join(' · '):'';
  if(R){const top=h('p','tdwhat');top.append(h('b',null,[R.track,R.car].filter(Boolean).join(' · ')));if(R.best)top.append(h('span','num tdbest',' best '+R.best));
    const kids=[top];if(R.verdict)kids.push(h('p','note',R.verdict));const row=h('div','row');row.append(tdBtn({label:'Open the review',go:'review',dir:R.dir},false));kids.push(row);rb.replaceChildren(...kids)}
  else{const kids=[h('p','note',tokenSaved?'No race reviews yet. When your race has uploaded to Garage 61, press Review last race.':'Race reviews come from Garage 61. Connect it in Settings; Practice, Coach and Setup Lab work without it.')];
    const row=h('div','row');if(tokenSaved){const b=h('button','btn','Review last race');b.type='button';b.dataset.act='review';row.append(b)}else row.append(tdBtn({label:'Open Settings',go:'settings'},false));kids.push(row);rb.replaceChildren(...kids)}
  // last practice
  const P=j.last_practice,pb=$('tdPracBody');$('tdPracWhen').textContent=P?slWhen(P.date):'';
  if(P){const top=h('p','tdwhat');top.append(h('b',null,[P.track,P.car].filter(Boolean).join(' · ')));
    const facts=[ok(P.best)?'Best '+fmtT(P.best):'',ok(P.clean_laps)?plural(P.clean_laps,'clean lap'):'',ok(P.laps)&&P.laps!==P.clean_laps?plural(P.laps,'lap')+' in all':''].filter(Boolean).join(' · ');
    const row=h('div','row');row.append(tdBtn({label:'Open this session',go:'session',id:P.id},false));
    pb.replaceChildren(top,h('p','note',facts||'No timed laps'),row)}
  else pb.replaceChildren(h('p','note','No practice from your telemetry yet. In iRacing, press Alt+L in the car to save telemetry, then drive a few laps.'),
    (()=>{const r=h('div','row');r.append(tdBtn({label:'Open Practice',go:'practice'},false));return r})())}
// Practice and Coach show the same plan; when Today's pick is something else they say where it is
function tdPointer(hostId){const host=$(hostId);if(!host)return;const old=host.querySelector('.tdptr');if(old)old.remove();
  const O=td.j&&td.j.one;if(!O||O.source==='plan'||O.source==='none'||O.source==='setup')return;
  const p=h('p','note tdptr');p.append(document.createTextNode('Today\'s #1 thing is from '+(O.source==='review'?'your last race':'the Racing page')+'. '));
  const b=h('button','linkbtn','Open Today');b.type='button';b.dataset.nav='today';p.append(b);host.append(p)}
/* ---------- welcome + first-run guide (shown until a token is saved, then for the rest of that session) */
let guideOn=false,checkOk=false,checkFail=false,tokenRejected=false,firstName=null,installed=false;
// 2.18: Garage 61's pages, Review last race and the guide's steps wait for a token Garage 61 accepted: a check that
// failed (or a token it rejected, remembered by the server) turns nothing green and unlocks nothing
const g61Ready=()=>tokenSaved&&!tokenRejected&&!checkFail;
function greet(){$('welcomeTitle').textContent=firstName?'Welcome, '+firstName+'.':'Welcome.'}
function updateGuide(){
  const st=(id,cls)=>{$(id).classList.toggle('now',cls==='now');$(id).classList.toggle('done',cls==='done')};
  const good=g61Ready();
  st('gs1',good?'done':'');st('gs2',good?'done':'');
  st('gs3',good&&checkOk?'done':'now');st('gs4',good&&checkOk?'now':'');
  $('gReview').disabled=!good;
}
function welcome(){
  greet();
  if(!g61Ready())guideOn=true;
  const t=$('welcomeText');
  $('guide').hidden=!guideOn;$('guideHead').hidden=!guideOn;$('welcomeActions').hidden=guideOn;
  if(guideOn){t.textContent='Practice, Setup Lab, the Coach and Racing work now, from the telemetry iRacing saves on this PC. Race reviews also need Garage 61: four quick steps below.';updateGuide()}
  else t.textContent='No reviews yet. When your race has uploaded to Garage 61, click Review last race.';
  show('vWelcome');markNav()}

/* ---------- the bar above pages in the frame */
let pages={team:null,prep:[]},resultFor={dir:null,result:null},racecapOn=false;
let weeklyBlk=null;   // 2.18: why Refresh this week can't be pressed (weeklyBlock), or null
function frameBar(){
  const v=current.view,bar=$('frameBar');
  const want=v==='review'||(v==='season'&&discordSaved)||v==='prepPage'||v==='teamPage'||v==='skills'||v==='sample'||v==='goalCharts';
  bar.hidden=!want;if(!want)return;
  $('fbResult').hidden=v!=='review';$('fbPrep').hidden=v!=='prepPage';$('fbTeam').hidden=v!=='teamPage';$('fbWeekly').hidden=v!=='teamPage';
  $('fbWeeklyWhy').hidden=v!=='teamPage'||!weeklyBlk;
  // 2.18: Post to Discord is always there on a review; without a webhook it explains how to set one up
  $('fbDiscord').hidden=v!=='review'&&!(v==='season'&&discordSaved);
  $('fbDiscord').title=discordSaved?'Preview this race\'s share card, then post it to your team\'s Discord channel':'Set up once: paste your team\'s Discord webhook link in Settings';
  $('fbTeamLap').hidden=v!=='review';
  $('fbMarkers').hidden=v!=='review';$('fbRaceCap').hidden=v!=='review'||!racecapOn;$('fbScan').hidden=v!=='review';$('fbRival').hidden=v!=='review';
  $('fbSampleBack').hidden=v!=='sample';$('fbGoals').hidden=v!=='goalCharts';
  const pill=$('fbPill');pill.hidden=true;$('fbBar').hidden=true;
  if(v==='sample'){$('fbText').textContent='This is a made-up race, not your laps. Your own reviews will look like this.';
    pill.hidden=false;pill.className='pill no';pill.textContent='Sample';return}
  if(v==='goalCharts'){$('fbText').textContent='How close your reviewed races get to your goals';return}
  if(v==='skills'){const p=profileInfo,rf=p&&p.refreshing;
    $('fbText').textContent=!p?'Driver profile':
      (p.n_races?'Driver profile · '+p.n_races+' race'+(p.n_races===1?'':'s')+' with skill scores'+(p.weakest&&PHASE[p.weakest.phase]?' · most time to find lately: '+PHASE[p.weakest.phase]:''):
       p.n_rows?'Driver profile · none of your '+p.n_rows+' race review'+(p.n_rows===1?'':'s')+' has skill scores yet; the page below says why':'Driver profile · skill scores appear after a race with at least 6 clean laps with telemetry');
    if(rf&&rf.total){pill.hidden=false;pill.className='pill';pill.textContent='Adding scores to older reviews: '+rf.done+' of '+rf.total;
      $('fbBar').hidden=false;$('fbBar').firstElementChild.style.width=Math.round(100*rf.done/rf.total)+'%'}
    return}
  if(v==='review'){const r=resultFor.dir===current.dir?resultFor.result:null;
    const sc=resultFor.dir===current.dir?resultFor.scan:null;const place=sc?(ok(sc.class_finish)?'P'+sc.class_finish+' in class, P'+sc.finish+' overall':'P'+sc.finish+' overall'):'';
    $('fbText').textContent=r?'Race result saved':sc?'From your replay scan: '+place+', on the road. Load the iRacing results for the official result, incidents and iRating.':'Scan this race for your finish and race log, or add the iRacing results.';
    $('fbResult').textContent=r?'Edit race result':sc?'Add official result':'Add race result';
    if(r&&ok(r.finish_pos)){pill.hidden=false;pill.className='pill ok';pill.textContent='P'+r.finish_pos+(ok(r.field_size)?' of '+r.field_size:'')}}
  if(v==='season')$('fbText').textContent='Post this week\'s digest after checking the preview. Nothing posts until you press Post.';
  if(v==='prepPage')$('fbText').textContent='Track prep page';
  if(v==='teamPage')$('fbText').textContent='Team hub'+(pages.team_updated?' · updated '+fmtWhen(pages.team_updated):'');
}
// driver profile summary (api/profile): races with scores, the weakest phase, the saved-review refresh's progress
const PHASE={braking:'braking',entry:'turn-in to apex',mid:'apex to throttle',exit:'corner exits',overall:'overall'};
let profileInfo=null;
async function loadProfile(){let j;try{j=await api('profile')}catch(e){return null}
  if(!j||!j.ok)return null;const was=profileInfo&&profileInfo.refreshing;profileInfo=j;
  if(current.view==='skills'){frameBar();
    // the refresh finished while the page was open: reload it so the new scores show
    if(was&&!j.refreshing){const u=BASE+'lib/index.html#skills';$('frame').src=fresh(u);frameHref=u}
    if(j.refreshing){clearTimeout(loadProfile.t);loadProfile.t=setTimeout(()=>{if(current.view==='skills')loadProfile()},2500)}}
  return j}
async function loadPages(){try{const j=await api('pages');if(j.ok)pages=j}catch(e){}return pages}

// the page in the frame says where it is (and its title) through the bridge; trust only our own frame
// (check e.source, not e.origin: a sandboxed page's origin is "null")
addEventListener('message',e=>{const f=$('frame');if(!f||e.source!==f.contentWindow)return;const m=e.data;
  if(m&&typeof m==='object'&&m.type==='debrief-key'&&KEY_MSG.test(String(m.key))){shortcut(m.key);return}
  if(!m||typeof m!=='object'||m.type!=='debrief-frame'||typeof m.href!=='string')return;
  frameHref=m.href;themeFrame(getTheme());framePlace(noQuery(m.href),typeof m.title==='string'?m.title:'')});
$('frame').addEventListener('load',()=>themeFrame(getTheme()));
function framePlace(path,title){
  if(!path.startsWith(BASE))return;
  // a page finishing its load behind another page (Goals, Settings...) doesn't pull the window back to it
  if(current.view!=null&&!FRAME_VIEWS.includes(current.view))return;
  const rest=path.slice(BASE.length);const rel=rest.split('#')[0].split('?')[0];const hash=(rest.split('#')[1]||'').split('&')[0];
  const m=rel.match(/^lib\/reviews\/([^/]+)\/index\.html$/);
  if(m){const dir=decodeURIComponent(m[1]);const changed=current.dir!==dir;current={view:'review',dir};if(changed)loadResult(dir)}
  else if(rel==='lib/index.html'||rel==='lib/'){const was=current.view;current={view:hash==='season'?'season':hash==='skills'?'skills':hash==='goals'?'goalCharts':'library',dir:null};
    if(current.view==='skills'&&was!=='skills')loadProfile()}
  else if(/^lib\/prep\/[^/]+\.html$/.test(rel))current={view:'prepPage',dir:null,page:decodeURIComponent(rel.slice(4))};
  else if(rel==='sample/index.html'){current={view:'sample',dir:null};title='Sample review'}
  else if(rel==='lib/team/index.html'){if(current.view!=='teamPage')loadWeekly();current={view:'teamPage',dir:null,page:'team/index.html'}}
  if(title)document.title=/(^Debrief Library$|· Library$)/.test(title)?appTitle():'Debrief · '+title;
  // (links that leave the app are opened in the normal browser by the bridge itself)
  markNav();
}

// 2.8: while setting up (no token and no saved reviews) Garage 61's pages have nothing to show, so the sidebar
// keeps you on the steps for those. 2.18: only those: Practice, Setup Lab, the Coach, Racing, Car vs car and Prepare
// read files on this PC and open straight away. Settings and Help stay open, and Review last race has its own gate.
const G61_NAV=new Set(['library','season','skills','goals','sessions','team']);
const onboarding=()=>!g61Ready()&&!groupsCache.some(g=>(g.reviews||[]).length);
function navGate(){const off=onboarding();
  document.querySelectorAll('.navbtn[data-nav]').forEach(b=>{if(!G61_NAV.has(b.dataset.nav))return;
    if(b.dataset.t==null)b.dataset.t=b.title||'';
    b.setAttribute('aria-disabled',String(off));b.title=off?'Needs Garage 61: finish the setup steps first.':b.dataset.t})}
function goNav(name){
  if(G61_NAV.has(name)&&onboarding()){current={view:'welcome'};welcome();banner('Finish the setup steps first: this page needs Garage 61. Practice, Setup Lab, the Coach and Racing work now, from your telemetry.');return}
  ({today:openToday,sessions:openSessions,library:openLibrary,season:openSeason,skills:openSkills,goals:openGoals,prep:openPrep,team:openTeam,settings:openSettings,
    setuplab:()=>openSetupLab(),coach:()=>openCoach(),practice:()=>openPractice(),racing:()=>openRacing(),cars:()=>openCars()})[name]()}
$('btnSample').addEventListener('click',openSample);
$('fbSampleBack').addEventListener('click',()=>{current={view:'welcome'};welcome()});
$('btnGoalCharts').addEventListener('click',openGoalCharts);
$('fbGoals').addEventListener('click',openGoals);
document.addEventListener('click',e=>{
  const n=e.target.closest('[data-nav]');
  if(n){goNav(n.dataset.nav);return}
  const a=e.target.closest('[data-act]');if(!a)return;
  if(a.dataset.act==='review')startReview();
  else if(a.dataset.act==='sync')startSync(false);
  else if(a.dataset.act==='team')startJob('team');
  else if(a.dataset.act==='retry'&&lastStart)startJob(lastStart.kind,lastStart.body);
});

/* ---------- past reviews */
let libFolder=null,tokenSaved=true,groupsCache=[];
async function loadReviews(){
  let j;try{j=await api('reviews')}catch(e){connectionLost();return []}
  libFolder=j.folder;groupsCache=j.groups||[];
  const host=$('revlist');host.replaceChildren();let n=0;
  // one heading and one <ul> per track and car; each review is a plain button inside an <li>
  groupsCache.forEach((g,gi)=>{const box=h('div','grp');
    const gh=h('div','gh');gh.id='revg'+gi;const gt=h('div','gt',g.track);gt.title=g.track;const gc=h('div','gc',g.car);gc.title=g.car;gh.append(gt,gc);box.append(gh);
    const ul=h('ul','revul');ul.setAttribute('aria-labelledby',gh.id);
    (g.reviews||[]).forEach(r=>{n++;const li=h('li');const b=h('button','rev');b.type='button';b.dataset.dir=r.dir;
      b.title=(r.session||'Review')+' · '+r.laps+' laps';b.setAttribute('aria-label',(r.session||'Review')+', '+fmtDate(r.date)+', best '+(r.best||'–'));
      b.append(h('span','d',fmtDate(r.date)),h('span','b',r.best));b.addEventListener('click',()=>openReview(r.dir));li.append(b);ul.append(li)});
    box.append(ul);host.append(box)});
  if(!n)host.append(h('p','revempty',libFolder?'Your reviews will appear here.':'Finish setup on the welcome screen first.'));
  $('revcount').textContent=n?String(n):'';
  navGate();markNav();return groupsCache;
}

/* ---------- jobs: review / prep / team share the progress screen; check stays in Settings */
const TITLES0={setuplab:'Reading your telemetry files',scan:'Scanning the replay',rival:'Reading a rival\'s lap',weekly:'Updating this week\'s team leaderboards',update:'Updating Debrief',sync:'Syncing your Garage 61 history',review:'Reviewing your last race',prep:'Building your track prep page',team:'Updating the team hub',check:'Checking the connection'};
const STOPPED={scan:'The scan stopped',rival:'Reading the lap stopped',weekly:'The leaderboard refresh stopped',update:'The update stopped',review:'The review stopped',prep:'The prep page stopped',team:'The team refresh stopped'};
const TITLES=Object.assign({},TITLES0);   // the review title can change per job; TITLES0 keeps the defaults
let handled=new Set(),polling=false,lastJob=null,lastStart=null;
async function startJob(kind,body){
  if(lastJob&&lastJob.state==='running'&&lastJob.kind!=='sync'){
    if(lastJob.kind===kind&&kind!=='check')openJob();
    else banner('Debrief is busy: '+(lastJob.title||TITLES[lastJob.kind]||'a job').toLowerCase()+'. Wait for it to finish.',true);
    return false}
  let r;try{r=await post(kind==='scan'?'replay-scan':kind==='rival'?'rival-read':kind,body)}catch(e){connectionLost();return false}
  if(!r.ok){banner(r.error||'Could not start.',true);return false}
  if(lastJob&&lastJob.kind==='sync'&&lastJob.state==='running')banner('History sync paused for this. It carries on next time.');
  lastStart={kind,body};lastJob={state:'running',kind,id:r.id};
  resetJobView(kind);openJob();poll();return true;
}
async function startCheck(){
  if(lastJob&&lastJob.state==='running'&&lastJob.kind!=='sync'){banner('Debrief is busy: '+(lastJob.title||'a job').toLowerCase()+'. Wait for it to finish.',true);return}
  setMsg('chkMsg','Checking...');setMsg('tokMsg','');$('btnCheck').disabled=true;checkFail=false;
  let r;try{r=await post('check')}catch(e){$('btnCheck').disabled=false;setMsg('chkMsg','');connectionLost();return}
  if(!r.ok){$('btnCheck').disabled=false;setMsg('chkMsg',r.error,'bad');return}
  $('chkOut').hidden=true;$('chkOut').replaceChildren();$('chkProg').hidden=false;$('chkBar').firstElementChild.style.width='4%';
  $('gChkOut').hidden=true;$('gChkOut').replaceChildren();$('gChkProg').hidden=false;$('gChkBar').firstElementChild.style.width='4%';
  $('btnCheck').disabled=true;setMsg('chkMsg','Checking...');poll();
}
// 2.6: Review last race waits for a saved token (aria-disabled, so the tooltip still shows why)
function reviewGate(){const b=$('btnReview');const off=!g61Ready();b.setAttribute('aria-disabled',String(off));
  b.title=off?'Finish step 3 first.':'Review last race (Ctrl+Enter)';if(off)$('syncStrip').hidden=true;navGate()}
function startReview(){if(!g61Ready()){current={view:'welcome'};welcome();return}startJob('review')}
// 2.18: make a saved review again from Garage 61 (Review last race opens the saved one without downloading)
function rebuildReview(dir){if(!g61Ready()){current={view:'welcome'};welcome();return}startJob('review',{review:dir,rebuild:true})}
$('btnReview').addEventListener('click',startReview);

// 2.8: request counts live in Settings > About. The job's own lines are for the log too, so they keep
// theirs and the window takes them out here.
const noCounts=t=>String(t==null?'':t).replace(/\s*\((?:no |about |up to )?\d*\s*(?:Garage 61 )?requests?\)/gi,'');
const countLine=t=>/^\s*Garage 61 requests\b/i.test(String(t));
function resetJobView(kind){
  if(kind==='review'&&!(lastJob&&lastJob.title))TITLES.review=TITLES0.review;   // a plain "Review last race" never keeps an earlier job's title
  $('jobStop').hidden=kind!=='scan'&&kind!=='rival';$('jobStop').disabled=false;
  $('jobTitle').textContent=TITLES[kind]||'Working';$('jobErrTitle').textContent=STOPPED[kind]||'It stopped';
  $('jobEyebrow').textContent='Working';$('jobErr').hidden=true;$('jobSteps').replaceChildren();delete $('jobSteps').dataset.n;{const d=$('jobDbg');if(d)d.remove()}
  $('jobNow').textContent='Starting...';setBar(0);$('jobBar').parentElement.className='progress';
}
function setBar(f){const p=Math.round(f*100);$('jobBar').firstElementChild.style.width=p+'%';$('jobBar').setAttribute('aria-valuenow',p);$('jobPct').textContent=p+'%'}

async function poll(){
  if(polling)return;polling=true;
  try{
    for(;;){
      let s;try{s=await api('status')}catch(e){connectionLost();break}
      lastJob=s;render(s);
      if(s.state!=='running')break;
      await new Promise(r=>setTimeout(r,600));
    }
  }finally{polling=false}
}
function render(s){
  const btn=$('btnReview');const busy=s.state==='running'&&s.kind==='review';
  btn.classList.toggle('busy',busy);
  btn.querySelector('.lbl').textContent=busy?'Reviewing... '+Math.round((s.frac||0)*100)+'%':'Review last race';
  btn.querySelector('.minibar i').style.width=Math.round((s.frac||0)*100)+'%';
  document.querySelectorAll('.navbtn').forEach(b=>b.classList.toggle('busy',s.state==='running'&&NAV_OF[s.kind]===b.dataset.nav));
  if(s.kind==='sync')renderSync(s);else if(sessData)renderStrip(sessData.summary,s);
  if(s.kind==='update'&&s.state==='done'&&!handled.has(s.id)){handled.add(s.id);
    setBar(1);$('jobEyebrow').textContent='Installing';$('jobNow').textContent='Debrief is closing to install the update. It reopens on its own in about a minute; you can close this window.';
    lost=true;return}
  if(s.kind==='review'||s.kind==='scan'||s.kind==='rival'||s.kind==='prep'||s.kind==='team'||s.kind==='weekly'||s.kind==='update'){
    if(s.kind==='review'&&s.state==='running')TITLES.review=s.title||TITLES0.review;
    setBar(s.frac||0);$('jobNow').textContent=s.state==='error'?'Stopped.':plainText(noCounts(s.message))||'Working...';
    if($('jobTitle').textContent!==TITLES[s.kind]&&s.state==='running')resetJobView(s.kind);
    const ol=$('jobSteps');const PL=plainLines((s.steps||[]).filter(t=>!countLine(t)).map(noCounts));
    const nk=(s.steps||[]).length+'|'+s.state;if(ol.dataset.n!==nk){ol.dataset.n=nk;ol.replaceChildren(...PL.shown.map(t=>h('li',null,t)));ol.scrollTop=ol.scrollHeight;
      const old=$('jobDbg');if(old)old.remove();if(PL.changed||s.state==='error'){const d=detailsFold((s.steps||[]).map(String).concat(s.error?[String(s.error)]:[]));d.id='jobDbg';ol.after(d)}}
    if(s.state==='running')$('jobEyebrow').textContent='Working';
    if(s.state!=='running')$('jobStop').hidden=true;
    if(s.state==='error'&&!handled.has(s.id)){handled.add(s.id);
      $('jobEyebrow').textContent='Stopped';$('jobErrMsg').textContent=plainText(s.error)||s.error;$('jobErr').hidden=false;
      $('jobBar').parentElement.className='progress fail';
      if(current.view!=='job')banner((STOPPED[s.kind]||'It stopped')+': '+s.error,true,9000)}
    if(s.state==='done'&&!handled.has(s.id)){handled.add(s.id);
      $('jobEyebrow').textContent='Done';$('jobBar').parentElement.className='progress done';
      const stay=current.view==='job';  // only jump to the result if the person is still watching
      if(s.kind==='scan'){loadReviews().then(()=>{if(s.result&&s.result.review){if(stay)openReview(s.result.review);reloadFrame();banner('Race log added from the replay'+(s.result.confirmed?'.':'. Garage 61 couldn\'t confirm it\'s the same race, so check the names look right.'),false,9000)}})}
      else if(s.kind==='rival'){if(s.result&&s.result.review){if(stay)openReview(s.result.review);reloadFrame();banner((s.result.name||'The rival')+'\'s lap '+s.result.lap+' is in Compare, as a rival with no pedal data.',false,9000)}}
      else if(s.kind==='review')loadReviews().then(()=>{const R=s.result||{};if(!R.review)return;
        if(R.saved){openReview(R.review);bannerAction('You\'ve already reviewed this race, so here is the saved review. Nothing was downloaded.','Rebuild',()=>rebuildReview(R.review))}
        else if(Array.isArray(R.changes)){if(stay||current.dir===R.review)openReview(R.review);if(current.dir===R.review)reloadFrame();banner('Review rebuilt. '+R.changes.filter(x=>typeof x==='string').join(' '),false,12000)}
        else{if(stay)openReview(R.review);banner('Review ready.')}});
      else loadPages().then(()=>{const team=s.kind==='team'||s.kind==='weekly';
        if(s.result&&s.result.page){if(stay)openPage(s.result.page,team?'teamPage':'prepPage');
        banner(s.kind==='weekly'?'This week\'s leaderboards updated.':
          s.kind==='team'?'Team hub updated.':'Prep page ready.')}if(current.view==='prep')renderPrepPages();if(s.kind==='weekly')loadWeekly()});
    }
  }
  if(s.kind==='setuplab'){renderSlJob(s);prJob(s)}
  if(s.kind==='schedule'){
    if(s.state==='running')setMsg('schMsg',s.message||'Loading...');
    if((s.state==='done'||s.state==='error')&&!handled.has(s.id)){handled.add(s.id);schBusy(false);
      if(s.state==='done'){const r=(s.result&&s.result.schedule)||{};setMsg('schMsg','Loaded '+(r.season||'the schedule')+': '+(r.series||0)+' series.','ok');
        banner('Season schedule loaded.');loadPrepare(true)}
      else{setMsg('schMsg',s.error,'bad');if(current.view!=='prep')banner('The schedule didn\'t load: '+s.error,true,9000)}}
  }
  if(s.kind==='check'){
    $('chkBar').firstElementChild.style.width=Math.round((s.frac||0)*100)+'%';
    $('gChkBar').firstElementChild.style.width=Math.round((s.frac||0)*100)+'%';
    if(s.state==='running'){setMsg('chkMsg',s.message||'Checking...');if(guideOn)setMsg('gTokMsg',s.message||'Checking...')}
    if((s.state==='done'||s.state==='error')&&!handled.has(s.id)){handled.add(s.id);
      $('btnCheck').disabled=false;$('chkProg').hidden=true;
      const out=$('chkOut');out.replaceChildren();
      const lines=s.state==='done'?(s.result&&s.result.lines)||[]:(s.steps||[]);
      // a line can be a string, or {text, level} (level "info" = neutral, e.g. telemetry that needs Pro)
      const raw=[];
      lines.forEach(t=>{const o=t&&typeof t==='object';const tx0=o?String(t.text||t.line||''):String(t);const lv=o?t.level:null;raw.push(tx0);
        const tx=plainText(tx0);if(!tx)return;   // 2.18: request counts, the sample-lap file and paths stay in the details
        out.append(h('li',lv==='info'?'info':(lv==='error'||lv==='bad'||(!lv&&/NOT available/.test(tx)))?'bad':null,tx))});
      if(s.state==='error'){raw.push(String(s.error||''));out.append(h('li','bad',plainText(s.error)||s.error))}
      ['chkDbg','gChkDbg'].forEach(id=>{const e=$(id);if(e)e.remove()});
      if(raw.some(t=>plainText(t)!==t.trim())){const d=detailsFold(raw);d.id='chkDbg';out.after(d);const g=detailsFold(raw);g.id='gChkDbg';$('gChkOut').after(g)}
      out.hidden=!out.children.length;
      setMsg('chkMsg',s.state==='done'?'Connection works.':'Connection check failed.',s.state==='done'?'ok':'bad');
      // the same result in the first-run guide (2.18: its error is said once, in the line under the button)
      const gout=$('gChkOut');gout.replaceChildren(...Array.from(out.children).slice(0,s.state==='error'?-1:undefined).map(li=>li.cloneNode(true)));
      gout.hidden=!gout.children.length;$('gChkProg').hidden=true;
      checkOk=s.state==='done';checkFail=s.state==='error';
      const e401=/\b401\b|rejected the token/i.test(String(s.error||''));
      setMsg('gTokMsg',checkOk?'Connection works. Debrief is downloading your history in the background. You can review your last race right now.':
        (e401?'Garage 61 didn\'t accept that token. Check the token and paste it again.':(s.error||'The connection check failed.')),checkOk?'ok':'bad');
      updateGuide();reviewGate();loadFirstRun();
      if(s.state==='done'){loadSettings();loadTeams(false);loadSessions().then(()=>startSync(true))}
      else loadSettings();   // a rejected token is remembered by the server (token_rejected)
    }
  }
}

/* ---------- warnings from the data layer (sync summary, season schedule): optional lists of plain strings */
const warnList=v=>Array.isArray(v)?v.filter(x=>typeof x==='string'&&x.trim()).map(x=>x.trim()):[];
function showWarnings(id,list,lead){const ul=$(id);if(!ul)return;ul.replaceChildren();const w=warnList(list);ul.hidden=!w.length;if(!w.length)return;
  if(lead)ul.append(h('li','wlead',lead));w.slice(0,8).forEach(t=>ul.append(h('li',null,t)));if(w.length>8)ul.append(h('li',null,'and '+(w.length-8)+' more.'))}

/* ---------- Sessions + history sync */
let sessData=null,sessFilter='all';
const SESS_PAGE=60;   // 2.6: the server pages the list (offset/limit, total); Show more asks for the next page
const KIND={1:'practice',2:'qualifying',3:'race'};
function ago(iso){const t=Date.parse(iso||'');if(!isFinite(t))return '';const m=Math.round((Date.now()-t)/60000);
  if(m<1)return 'just now';if(m<60)return m+' min ago';const h=Math.round(m/60);if(h<24)return h+' hour'+(h===1?'':'s')+' ago';
  return fmtDate(new Date(t).toISOString())}
function localWhen(iso){const t=Date.parse(iso||'');if(!isFinite(t))return fmtDate(iso);const d=new Date(t);
  return MON[d.getMonth()]+' '+d.getDate()+', '+d.getFullYear()+' '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')}
function syncLine(sum){if(!sum)return 'Not synced yet';if(!sum.synced)return sum.stopped?'History sync paused':'Not synced yet';
  return 'Synced '+ago(sum.last_sync)}
function renderStrip(sum,job){const el=$('syncStrip');if(!tokenSaved){el.hidden=true;return}el.hidden=false;
  const busy=job&&job.state==='running'&&job.kind==='sync';
  el.classList.toggle('busy',!!busy);
  const w=warnList(sum&&sum.warnings);el.classList.toggle('warn',!busy&&(!!w.length||(!!sum&&(!sum.synced||sum.stopped))));
  el.title=w.length?'Last sync: '+w.length+' warning'+(w.length===1?'':'s')+'\n'+w.slice(0,5).join('\n')+(w.length>5?'\n...':''):'Open Sessions';
  $('syncText').textContent=busy?'Syncing... '+Math.round((job.frac||0)*100)+'%':syncLine(sum)+(w.length?' · '+w.length+' warning'+(w.length===1?'':'s'):'');
  $('syncBar').style.width=busy?Math.round((job.frac||0)*100)+'%':'0'}
const sessTotal=j=>j&&ok(j.total)?j.total:((j&&j.sessions)||[]).length;
// first page again (after a sync, or opening the page); keeps as many rows as were already shown
function aboutSync(){const n=sessData&&sessData.summary&&sessData.summary.last_requests;
  $('aboutLastSync').textContent=ok(n)?'Your last history sync used '+n+' request'+(n===1?'':'s')+'.':''}
async function loadSessions(){const n=Math.max(SESS_PAGE,sessData&&sessData.sessions?sessData.sessions.length:0);
  let j;try{j=await api('sessions?offset=0&limit='+n)}catch(e){connectionLost();return null}
  if(j&&j.ok){if(!Array.isArray(j.sessions))j.sessions=[];sessData=j}renderStrip(sessData&&sessData.summary,lastJob);return sessData}
async function moreSessions(){if(!sessData)return;const have=sessData.sessions.length;$('sessMore').disabled=true;
  let j;try{j=await api('sessions?offset='+have+'&limit='+SESS_PAGE)}catch(e){$('sessMore').disabled=false;connectionLost();return}
  $('sessMore').disabled=false;if(!j||!j.ok)return;
  const seen=new Set(sessData.sessions.map(x=>x.key));
  sessData.sessions=sessData.sessions.concat((j.sessions||[]).filter(x=>!seen.has(x.key)));
  if(ok(j.total))sessData.total=j.total;if(j.summary)sessData.summary=j.summary;
  // an old server answers every session at once (no total): nothing more to ask for
  if(!ok(j.total))sessData.total=sessData.sessions.length;
  renderSessions()}
function renderSessions(){
  const j=sessData||{},sum=j.summary,busy=lastJob&&lastJob.state==='running'&&lastJob.kind==='sync';
  if(j.token===false){$('sessSyncNote').textContent='Add your Garage 61 token in Settings first.';$('btnSync').disabled=true}
  else{$('btnSync').disabled=!!busy;
    const n=sum?sum.sessions:0;
    $('sessSyncTitle').textContent=busy?'Syncing...':(sum&&sum.synced?'Up to date':'Not synced yet');
    const pill=$('sessSyncPill');pill.textContent=syncLine(sum);pill.className='pill'+(sum&&sum.synced&&!sum.stopped?' ok':' no');
    aboutSync();
    let note=sum&&sum.synced?n+' session'+(n===1?'':'s')+' and '+sum.laps+' laps from '+sum.combos+' track and car combination'+(sum.combos===1?'':'s')+'.':
      'The first sync downloads your whole Garage 61 history at a gentle pace. You can keep using Debrief while it runs.';
    if(sum&&sum.telemetry_left)note+=' Telemetry still to download for '+sum.telemetry_left+' session'+(sum.telemetry_left===1?'':'s')+'.';
    if(j.sync_on_open)note+=' Debrief syncs each time it opens.';
    $('sessSyncNote').textContent=note}
  showWarnings('sessWarn',sum&&sum.warnings,'The last sync noted:');
  $('btnSyncStop').hidden=!busy;$('sessProg').hidden=!busy;$('sessNow').hidden=!busy;
  const host=$('sessList');host.replaceChildren();
  const all=(j.sessions||[]).filter(s=>sessFilter==='all'||String(s.type)===sessFilter);
  if(!all.length)host.append(h('p','note',(j.sessions||[]).length?(sessTotal(j)>(j.sessions||[]).length?'None of this kind in the sessions loaded so far. Press Show more to look further back.':'No sessions of this kind yet.'):(busy?'Your sessions will appear here when the sync finishes.':'No sessions yet. Click Sync now.')));
  all.forEach(s=>{const row=h('div','item sess');const tx=h('div','itx');
    const top=h('span','row');top.append(h('span','when',localWhen(s.start)),h('span','kind '+(KIND[s.type]||''),s.session||'Session'));
    tx.append(top,h('b',null,s.track),h('span','sub2',s.car));
    const line=h('span','sub2');line.append(document.createTextNode(s.laps+' lap'+(s.laps===1?'':'s')+' · best '),h('b','num',fmtT(s.best)));
    if(s.best_clean==null&&s.best!=null)line.append(document.createTextNode(' (no clean laps)'));tx.append(line);
    const acts=h('span','row');
    if(s.review){const b=h('button','btn','Open review');b.type='button';b.addEventListener('click',()=>openReview(s.review));acts.append(b)}
    if(s.event&&[1,2,3].includes(s.type)&&s.track_id!=null&&s.car_id!=null){
      // practice and qualifying get their own review (runs, targets, the qualifying lap strip)
      const what={1:'practice',2:'qualifying'}[s.type];
      const b=h('button','btn'+(s.review?'':' primary'),s.review?'Review again':(what?'Review '+what:'Review'));b.type='button';
      if(what&&s.review)b.setAttribute('aria-label','Review '+what+' again');
      b.addEventListener('click',()=>startJob('review',{event:s.event,session_type:s.type,track_id:s.track_id,car_id:s.car_id}));acts.append(b)}
    row.append(tx,acts);host.append(row)});
  const loaded=(j.sessions||[]).length,total=sessTotal(j);
  $('sessMoreRow').hidden=loaded>=total;
  $('sessCount').textContent=total?'Showing '+loaded+' of '+total:'';
}
async function openSessions(){current={view:'sessions',dir:null};show('vSessions');markNav();
  await loadSessions();if(current.view==='sessions')renderSessions()}
function renderSync(s){
  renderStrip(sessData&&sessData.summary,s);
  if(current.view==='sessions'){$('btnSyncStop').hidden=s.state!=='running';$('sessProg').hidden=s.state!=='running';$('sessNow').hidden=s.state!=='running';
    $('sessBar').firstElementChild.style.width=Math.round((s.frac||0)*100)+'%';$('sessNow').textContent=noCounts(s.message);$('btnSync').disabled=s.state==='running';
    if(s.state==='running')$('sessSyncTitle').textContent='Syncing...'}
  if((s.state==='done'||s.state==='error')&&!handled.has(s.id)){handled.add(s.id);
    if(s.state==='error'){setMsg('sessMsg',s.error,'bad');if(current.view!=='sessions'&&s.error)banner('History sync stopped: '+s.error,true,9000)}
    else{const r=(s.result&&s.result.sync)||{};
      if(r.stopped)setMsg('sessMsg','Paused. The next sync carries on from here.');
      else{setMsg('sessMsg',r.first?'Your history is downloaded.':(r.new_laps?r.new_laps+' new lap'+(r.new_laps===1?'':'s')+'.':'Nothing new.'),'ok');
        if(r.first)banner('Your Garage 61 history is synced: '+(r.sessions||0)+' sessions.');
        else if(r.new_laps)banner('Synced '+r.new_laps+' new lap'+(r.new_laps===1?'':'s')+' from Garage 61.')}}
    loadSessions().then(()=>{if(current.view==='sessions')renderSessions()})}
}
async function startSync(auto){
  if(lastJob&&lastJob.state==='running'){if(!auto&&lastJob.kind!=='sync')banner('Debrief is busy: '+(lastJob.title||'a job').toLowerCase()+'. Sync when it\'s done.',true);return}
  let r;try{r=await post('sync',{auto:!!auto})}catch(e){if(!auto)connectionLost();return}
  if(!r.ok){if(!auto)setMsg('sessMsg',r.error||'Could not start the sync.','bad');return}
  if(r.skipped)return;
  setMsg('sessMsg','');lastJob={state:'running',kind:'sync',id:r.id,frac:0};renderSync(lastJob);poll()}
$('btnSync').addEventListener('click',()=>startSync(false));
$('btnSyncStop').addEventListener('click',async()=>{try{await post('sync-stop')}catch(e){}setMsg('sessMsg','Pausing...')});
$('sessMore').addEventListener('click',moreSessions);
document.querySelectorAll('#sessFilter [data-f]').forEach(b=>b.addEventListener('click',()=>{sessFilter=b.dataset.f;
  document.querySelectorAll('#sessFilter [data-f]').forEach(x=>{x.setAttribute('aria-checked',String(x===b));x.tabIndex=x===b?0:-1});renderSessions()}));
document.querySelectorAll('#sessFilter [data-f]').forEach(x=>{const on=x.dataset.f===sessFilter;x.setAttribute('aria-checked',String(on));x.tabIndex=on?0:-1});
setInterval(()=>{if(!(lastJob&&lastJob.state==='running'))renderStrip(sessData&&sessData.summary,lastJob)},60000);

/* ---------- Prep */
let combosCache=null;
async function loadCombos(fetchLists){
  let j;try{j=await api('combos'+(fetchLists?'?fetch=1':''))}catch(e){connectionLost();return null}
  if(!j.ok){if(fetchLists)setMsg('prepLoadMsg',j.error,'bad');return combosCache}
  combosCache=j;return j}
function comboLabel(c){return c.track+' · '+c.car}
async function openPrep(){current={view:'prep',dir:null};show('vPrep');markNav();
  const [j]=await Promise.all([loadCombos(false),loadPages(),loadPrepare(false)]);renderPrep(j);renderPrepPages();renderPrepare()}

/* ---------- Prepare (2.2): schedule, your series, this/next week, track cards */
let prepData=null;
const WEEKDAY=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
function dayRange(start){const m=/^(\d{4})-(\d\d)-(\d\d)/.exec(start||'');if(!m)return '';const a=new Date(Date.UTC(+m[1],+m[2]-1,+m[3]));const b=new Date(a.getTime()+6*864e5);
  const f=d=>MON[d.getUTCMonth()]+' '+d.getUTCDate();return f(a)+' – '+f(b)}
function raceWhen(rt){const m=/^(\d{4})-(\d\d)-(\d\d) (\d{1,2}):(\d\d)/.exec(rt||'');if(!m)return '';
  const d=new Date(Date.UTC(+m[1],+m[2]-1,+m[3],+m[4],+m[5]));return WEEKDAY[d.getDay()]+' '+MON[d.getMonth()]+' '+d.getDate()+', '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')}
async function loadPrepare(render){let j;try{j=await api('prepare')}catch(e){connectionLost();return null}
  if(!j.ok){setMsg('schMsg',j.error,'bad');return null}
  prepData=j;if(render!==false)renderPrepare();return j}
function schBusy(b){$('btnSchDl').disabled=b;$('btnSchLoad').disabled=b}
function renderPrepare(){const j=prepData;if(!j)return;$('prepJoin').hidden=!notMember(j);const sc=j.schedule;
  const pill=$('schPill');
  if(!sc){pill.textContent='No schedule loaded';pill.className='schlabel no';
    $('schNote').textContent='Load iRacing\'s official season schedule to see what your series are racing. Debrief reads the PDF on this PC and never shares it.'}
  else{pill.textContent=sc.season+(sc.current_week?' · Week '+sc.current_week:'');pill.className='schlabel'+(sc.out_of_date?' no':'');
    pill.title=sc.out_of_date?'This season has ended. Load the new schedule under More.':'';
    $('schNote').textContent=sc.out_of_date?'This season has ended. Load the new season\'s schedule when iRacing publishes it.':
      sc.series+' series, '+sc.weeks+' weeks ('+fmtDate(sc.first_day)+' to '+fmtDate(sc.last_day)+'). Load it again when the next season comes out.'}
  $('btnSchDl').textContent=sc?'Download latest schedule':'Download official schedule';
  showWarnings('schWarn',sc&&sc.warnings,'Check the schedule:');
  $('prepBar').hidden=!sc;if(!pkReady&&(!sc||sc.out_of_date||(sc.warnings&&sc.warnings.length)))$('prepMore').open=true;
  if(!sc){renderFocusNone('Load the season schedule to get started','Open More below and download iRacing\'s schedule. It takes a few seconds and only happens once a season.');$('planPanel').hidden=true;return}
  if(!pkReady){pkReady=true;initPick()}else checkPick();
  renderPick();renderMissing();renderAllSeries();refreshFocus(true)}
function renderMissing(){const host=$('prepMissing'),gone=((prepData&&prepData.mine)||[]).filter(s=>s.missing);host.hidden=!gone.length;host.replaceChildren();
  gone.forEach(s=>{const li=h('li',null,s.name+' is starred, but it isn\'t in the loaded schedule. It may come back next season.');
    const b=h('button','btn','Remove star');b.type='button';b.addEventListener('click',async()=>{let r;
      try{r=await post('series',{ids:prepData.mine.map(x=>x.id).filter(i=>i!==s.id)})}catch(e){connectionLost();return}
      if(!r.ok){setMsg('pkMsg',r.error,'bad');return}stripFor=null;loadPrepare(true)});li.append(b);host.append(li)})}
function pickSeriesWeek(id,no){const s=serOf(id);if(!s)return;pk.series=id;pk.track=null;seriesAll=false;const w=s.weeks.find(x=>x.no===no);
  if(!pk.car||!serUses(s,pk.car)||(w&&!carRuns(s,w,pk.car)))pk.car=(w&&cat().driven.map(x=>x.name).find(n=>weekCars(s,w).includes(n)))||(w&&weekCars(s,w)[0])||bestCarFor(s);
  pk.week=no;pickChanged();$('prepBar').scrollIntoView({block:'nearest'})}
function renderAllSeries(){const j=prepData,mine=(j.mine||[]).filter(s=>!s.missing),host=$('prepAll');host.hidden=!mine.length;if(!mine.length)return;
  const rows=$('prepAllRows');rows.replaceChildren();const now=j.current_week;
  mine.forEach(s=>{const it=h('div','item'),tx=h('div','itx');tx.append(h('b',null,s.name));const meta=serMeta(s);if(meta)tx.append(h('span','sub2',meta));it.append(tx);
    const row=h('div','pkbtns');[['This week',s.this_week,now],['Next week',s.next_week,now+1]].forEach(([lab,w,no])=>{if(w)row.append(pkBtn(lab+': '+w.track,false,s.id+':'+no,()=>pickSeriesWeek(s.id,no),dayRange(w.start)))});
    if(!row.children.length)row.append(h('span','sub2','No race this week or next.'));it.append(row);rows.append(it)});
  const P=j.plan,host2=$('allPlanWeeks'),note=$('allPlanNote'),nn=$('allPlanNotes');host2.replaceChildren();nn.replaceChildren();if(!P){note.textContent='';nn.hidden=true;return}
  const said=(P.series||[]).map(s=>s.note).filter(Boolean);if(P.note)said.push(P.note);nn.hidden=!said.length;said.forEach(t=>nn.append(h('li',null,t)));
  if(P.state==='error'){note.textContent='The season plan couldn\'t be worked out. The rest of this page is fine.';return}
  if(P.state==='season_over'){note.textContent='No race weeks left in this season for your series.';return}
  const fl=P.flagged||[];note.textContent='Week by week for your starred series, from your saved reviews and synced sessions. It only knows laps those hold. '+
    (fl.length?'Most prep: '+fl.map(f=>'week '+f.season_week+', '+f.track).join('; ')+'.':'No week stands out as needing more prep than the others.');
  (P.weeks||[]).forEach(w=>{const box=h('div','wk'+(w.now?' now':''));if(w.flag&&!w.now)box.style.borderLeft='4px solid var(--warn)';
    box.append(h('span','wlab',(w.now?'This week · ':'')+'Week '+w.season_week+' · '+dayRange(w.start)));w.races.forEach(r=>box.append(planRace(r)));host2.append(box)})}
function serMeta(s){const bits=[];if(s.category)bits.push(s.category);if(s.license)bits.push(s.license.replace(/\s*-->\s*/,' to '));
  if(s.setup)bits.push(s.setup==='fixed'?'Fixed setup':'Open setup');return bits.join(' · ')}

/* ---------- the picker (2.11): car, series, track, week. One choice, kept in pk; everything below follows it. */
let pk={car:null,series:null,week:null,track:null},pkReady=false,focusData=null,focusSeq=0,stripData=null,stripFor=null,seriesAll=false;
const cat=()=>(prepData&&prepData.catalog)||{cars:[],driven:[],series:[],tracks:[],current_week:null};
const serOf=id=>cat().series.find(s=>s.id===id)||null;
const weekCars=(s,w)=>w.cars||s.cars||[];
const carRuns=(s,w,car)=>!car||!weekCars(s,w).length||weekCars(s,w).includes(car);
const weeksLeft=s=>(s.weeks||[]).filter(w=>w.left);
const serUses=(s,car)=>!car||(s.cars||[]).includes(car);
const carHasWeek=(s,car)=>weeksLeft(s).some(w=>carRuns(s,w,car));
// the car's next running week; a car with none left keeps the week you were on, so the page can offer a car that runs
function defaultWeek(s,car,keep){const now=cat().current_week,all=weeksLeft(s),left=all.filter(w=>carRuns(s,w,car));
  const hit=left.find(w=>w.no===now)||left[0]||all.find(w=>w.no===keep)||all.find(w=>w.no===now)||all[0];return hit?hit.no:null}
function defaultSeries(car){const all=cat().series.filter(s=>serUses(s,car));
  const live=all.filter(s=>weeksLeft(s).some(w=>carRuns(s,w,car)));const pool=live.length?live:all;
  const most=pool.slice().sort((a,b)=>weeksLeft(b).length-weeksLeft(a).length)[0];
  return (pool.find(s=>s.starred)||pool.find(s=>s.suggested)||most||{}).id||null}
function bestCarFor(s){const names=cat().driven.map(x=>x.name).filter(n=>(s.cars||[]).includes(n));
  const left=weeksLeft(s),now=left.find(w=>w.no===cat().current_week)||left[0];
  return names.find(n=>carHasWeek(s,n))||(now&&weekCars(s,now)[0])||names[0]||(s.cars||[])[0]||null}
function initPick(){const c=cat(),m=(prepData&&prepData.choice)||{},season=prepData.schedule&&prepData.schedule.season;
  const carOk=n=>n&&(c.cars.some(x=>x.name===n)||c.driven.some(x=>x.name===n));
  pk.car=carOk(m.car)?m.car:null;
  const s=serOf(m.series);pk.series=s?s.id:null;
  if(s&&!serUses(s,pk.car))pk.car=null;
  if(!pk.car&&!s&&c.driven.length)pk.car=c.driven[0].name;
  if(!pk.series){const star=c.series.find(x=>x.starred&&serUses(x,pk.car));
    pk.series=star?star.id:(pk.car?defaultSeries(pk.car):null)}
  const ser=serOf(pk.series);if(ser&&!pk.car)pk.car=bestCarFor(ser);
  pk.week=null;pk.track=null;if(!ser)pk.track=m.track||null;
  if(ser){const left=weeksLeft(ser);
    const same=m.season===season&&!m.follow_now;
    if(same&&m.week&&left.some(w=>w.no===m.week))pk.week=m.week;
    else if(same&&m.track&&!m.week)pk.track=m.track;
    else pk.week=defaultWeek(ser,pk.car)}}
function checkPick(){const s=serOf(pk.series);if(pk.series&&!s){pk.series=null;pk.week=null}
  if(s&&pk.week!=null&&!weeksLeft(s).some(w=>w.no===pk.week))pk.week=defaultWeek(s,pk.car)}
let choiceT=null;
function rememberPick(){clearTimeout(choiceT);const c=cat(),season=prepData&&prepData.schedule&&prepData.schedule.season;
  const body={car:pk.car,series:pk.series,week:pk.week,track:pk.track,follow_now:pk.week!=null&&pk.week===c.current_week,season};
  choiceT=setTimeout(()=>{post('prepare-choice',body).catch(()=>{})},250)}
function pickChanged(){rememberPick();renderPick();refreshFocus(stripFor!==pk.series)}
function setCar(name){pk.car=name;seriesAll=false;let s=serOf(pk.series);
  if(!s||!serUses(s,name)){pk.series=defaultSeries(name);s=serOf(pk.series);pk.week=s?defaultWeek(s,name):null;pk.track=null}
  else if(pk.week==null&&!pk.track)pk.week=defaultWeek(s,name);
  else if(pk.week!=null&&!weeksLeft(s).some(w=>w.no===pk.week&&carRuns(s,w,name)))pk.week=defaultWeek(s,name,pk.week);
  pickChanged()}
function setSeries(id){const s=serOf(id);if(!s)return;pk.series=id;pk.track=null;
  if(!pk.car||!serUses(s,pk.car)||!carHasWeek(s,pk.car))pk.car=bestCarFor(s);
  pk.week=defaultWeek(s,pk.car);pickChanged()}
function setWeek(no){pk.week=no;pk.track=null;pickChanged()}
function setTrack(name){const s=serOf(pk.series);const w=s&&weeksLeft(s).find(x=>x.track===name);
  if(w){pk.week=w.no;pk.track=null}else{pk.week=null;pk.track=name}pickChanged()}
function pickTrackName(){if(pk.track)return pk.track;const s=serOf(pk.series),w=s&&pk.week!=null&&s.weeks.find(x=>x.no===pk.week);return w?w.track:''}

/* a searchable dropdown: type to narrow, arrows to move, Enter to pick, Esc to back out */
function makeCombo(id,getOpts,onPick){const inp=$(id),list=$(id+'List');let isOpen=false,items=[],active=-1,val='';
  function close(){isOpen=false;list.hidden=true;inp.setAttribute('aria-expanded','false');inp.removeAttribute('aria-activedescendant');active=-1}
  function mark(i){active=i;[...list.querySelectorAll('[role=option]')].forEach((e,n)=>{const on=n===i;e.classList.toggle('on',on);if(on){inp.setAttribute('aria-activedescendant',e.id);e.scrollIntoView({block:'nearest'})}});if(i<0)inp.removeAttribute('aria-activedescendant')}
  function draw(q){q=(q||'').trim().toLowerCase();const opts=getOpts();items=[];list.replaceChildren();let grp=null,more=0;
    opts.forEach(o=>{if(q&&!o.action&&!(o.label+' '+(o.find||'')+' '+(o.sub||'')).toLowerCase().includes(q))return;
      if(items.length>=200&&!o.action){more++;return}
      if(o.group&&o.group!==grp){grp=o.group;const g=h('li','combogrp',grp);g.setAttribute('role','presentation');list.append(g)}
      if(!o.group)grp=null;
      const n=items.length,li=h('li','comboopt'+(o.action?' act':''));li.id=id+'-o'+n;li.setAttribute('role','option');li.setAttribute('aria-selected',String(!o.action&&o.label===val));
      li.append(h('span','combot',o.label));if(o.sub)li.append(h('span','combos',o.sub));
      li.addEventListener('mousedown',e=>e.preventDefault());li.addEventListener('click',()=>choose(n));
      items.push(o);list.append(li)});
    if(!items.length){const e=h('li','combogrp','Nothing matches that.');e.setAttribute('role','presentation');list.append(e)}
    if(more){const e=h('li','combogrp','And '+more+' more. Type to narrow it down.');e.setAttribute('role','presentation');list.append(e)}
    return items}
  function open(q){isOpen=true;list.hidden=false;inp.setAttribute('aria-expanded','true');draw(q);
    const cur=q?(items.length?0:-1):items.findIndex(o=>o.label===val);mark(cur)}
  function choose(n){const o=items[n];if(!o)return;
    if(o.action){const q=inp.value===val?'':inp.value;o.action();inp.value=q;open(q);inp.focus();return}
    close();val=o.label;inp.value=o.label;if(document.activeElement===inp)inp.select();onPick(o.value)}
  inp.addEventListener('focus',()=>{inp.select()});
  inp.addEventListener('click',()=>{if(!isOpen)open('');if(inp.value===val)setTimeout(()=>{if(document.activeElement===inp&&inp.value===val)inp.select()},0)});
  inp.addEventListener('input',()=>open(inp.value));
  inp.addEventListener('keydown',e=>{
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();if(!isOpen){open('');return}
      if(items.length)mark((active+(e.key==='ArrowDown'?1:-1)+items.length)%items.length)}
    else if(e.key==='Enter'){if(isOpen&&active>=0){e.preventDefault();choose(active)}else if(!isOpen){e.preventDefault();open('')}}
    else if(e.key==='Escape'){if(isOpen){e.preventDefault();e.stopPropagation();close();inp.value=val;inp.select()}}
    else if(e.key==='Tab'){if(isOpen){close();inp.value=val}}});
  inp.addEventListener('blur',()=>{if(isOpen)close();inp.value=val});
  return {set(text){val=text||'';if(document.activeElement!==inp||!isOpen)inp.value=val}}}
function carOpts(){const c=cat(),d=new Set(c.driven.map(x=>x.name)),out=[];
  c.driven.forEach(x=>out.push({label:x.name,value:x.name,group:'Cars you\'ve driven'}));
  c.cars.forEach(x=>{if(!d.has(x.name))out.push({label:x.name,value:x.name,group:x.class,find:x.class})});return out}
function serWeekText(s){const left=weeksLeft(s),w=left.find(x=>x.no===cat().current_week);return w?'This week: '+w.short:left.length?'':'No race weeks left'}
function seriesOpts(){const c=cat(),out=[],opt=(s,g)=>({label:s.name,value:s.id,group:g,sub:serWeekText(s),find:(s.category||'')+' '+(s.cars||[]).join(' ')});
  const limit=!!pk.car&&!seriesAll,use=c.series.filter(s=>!limit||serUses(s,pk.car));
  use.filter(s=>s.starred).forEach(s=>out.push(opt(s,'Starred')));
  use.filter(s=>!s.starred&&s.suggested).forEach(s=>out.push(opt(s,'From your races')));
  use.filter(s=>!s.starred&&!s.suggested).forEach(s=>out.push(opt(s,limit?'Using the '+pk.car:(s.category||'Other'))));
  if(limit)out.push({label:'Browse all series',action:()=>{seriesAll=true}});
  return out}
function trackOpts(){const c=cat(),s=serOf(pk.series),out=[],seen=new Set();
  if(s){const now=c.current_week;weeksLeft(s).forEach(w=>{seen.add(w.track);
    out.push({label:w.track,value:w.track,group:'On this series\' schedule',sub:w.no===now?'This week':w.no===now+1?'Next week':'Week '+w.week})})}
  c.tracks.forEach(t=>{if(!seen.has(t))out.push({label:t,value:t,group:'Other tracks'})});return out}
const cbCar=makeCombo('pkCar',carOpts,setCar),cbSeries=makeCombo('pkSeries',seriesOpts,setSeries),cbTrack=makeCombo('pkTrack',trackOpts,setTrack);
function keepFocus(host,fill){const a=document.activeElement,k=a&&host.contains(a)?a.dataset.k:null;fill();
  if(k!=null){const b=[...host.querySelectorAll('button')].find(x=>x.dataset.k===k);if(b)b.focus()}}
function pkBtn(label,on,k,fn,sub){const b=h('button','pkbtn'+(on?' on':''));b.type='button';b.dataset.k=k;b.setAttribute('aria-pressed',String(!!on));
  b.append(h('span',null,label));if(sub)b.title=sub;b.addEventListener('click',fn);return b}
function renderPick(){const c=cat(),s=serOf(pk.series);
  $('pkCarsRow').hidden=!c.driven.length;
  keepFocus($('pkCars'),()=>$('pkCars').replaceChildren(...c.driven.slice(0,6).map(d=>pkBtn(d.name,d.name===pk.car,d.name,()=>setCar(d.name),d.uses+' session'+(d.uses===1?'':'s')+' on record'))));
  const stars=c.series.filter(x=>x.starred);$('pkStarsRow').hidden=!stars.length;
  keepFocus($('pkStars'),()=>$('pkStars').replaceChildren(...stars.map(x=>pkBtn(x.name,x.id===pk.series,x.id,()=>setSeries(x.id)))));
  cbCar.set(pk.car);cbSeries.set(s?s.name:'');cbTrack.set(pickTrackName());
  const star=$('pkStar'),on=!!(s&&s.starred);star.disabled=!s;star.textContent=on?'★':'☆';star.setAttribute('aria-pressed',String(on));
  const lab=on?'Starred. Press to take this series off your list.':'Star this series';star.title=lab;star.setAttribute('aria-label',lab);
  const left=s?weeksLeft(s):[];$('pkWeeksRow').hidden=!left.length;
  keepFocus($('pkWeeks'),()=>$('pkWeeks').replaceChildren(...left.map(w=>{const now=c.current_week;
    const b=pkBtn(w.no===now?'This week':w.no===now+1?'Next week':'Week '+w.week,w.no===pk.week,String(w.no),()=>setWeek(w.no),w.track);
    if(!carRuns(s,w,pk.car)){b.classList.add('off');b.title='Other cars run this week: '+weekCars(s,w).join(', ')}return b})))}
$('pkStar').addEventListener('click',async()=>{const s=serOf(pk.series);if(!s)return;
  const ids=(prepData.mine||[]).map(x=>x.id).filter(i=>i!==s.id);if(!s.starred)ids.push(s.id);
  setMsg('pkMsg','');let r;try{r=await post('series',{ids})}catch(e){connectionLost();return}
  if(!r.ok){setMsg('pkMsg',r.error,'bad');return}
  stripFor=null;await loadPrepare(true);$('pkStar').focus()});

/* ---------- the one prep view: follows the picker, swaps in one go so the page never blanks */
let focusKeep=null;
async function refreshFocus(withPlan){const host=$('mySeries');
  if(!pk.car&&!pk.series&&!pk.track){focusSeq++;renderFocusNone('Pick a car to get started','Debrief shows the race, your laps at the track and the corners that keep costing you.');$('planPanel').hidden=true;return}
  // until the new choice is in, nothing here may act on the old track
  const was=document.activeElement;if(was&&host.contains(was)&&was.id)focusKeep=was.id;
  host.querySelectorAll('button').forEach(b=>{b.disabled=true});
  const seq=++focusSeq,q=new URLSearchParams();if(pk.series)q.set('series',pk.series);if(pk.week!=null)q.set('week',String(pk.week));
  if(pk.car)q.set('car',pk.car);if(pk.track)q.set('track',pk.track);if(!withPlan&&stripFor===pk.series)q.set('plan','0');
  const slow=setTimeout(()=>{if(seq===focusSeq)host.classList.add('busy')},220);let d;
  try{d=await api('prepfocus?'+q)}catch(e){clearTimeout(slow);if(seq===focusSeq)connectionLost();return}
  clearTimeout(slow);if(seq!==focusSeq)return;host.classList.remove('busy');
  if(!d.ok){renderFocusNone('That didn\'t load',d.error||'Try again.');return}
  focusData=d;if(d.plan){stripData=d.plan;stripFor=pk.series}else if(!pk.series){stripData=null;stripFor=null}
  renderFocus(d);renderPlan()}
function renderFocusNone(title,text){const e=h('div','pvnone');e.append(h('h2','pvtrack',title),h('p','lede',text));$('mySeries').replaceChildren(e)}
function pvSection(title,cls){const d=h('section','pvsec'+(cls?' '+cls:''));d.append(h('h3',null,title));return d}
function weekWhen(d){const w=d.week;if(!w)return '';return (w.now?'This week':w.next?'Next week':'Week '+w.week)+', '+dayRange(w.start)}
function racePart(d){const box=h('div','pvfacts');const R=d.race,S=d.series;
  if(!d.week){box.classList.add('none');box.append(h('p','note',S?S.name+' doesn\'t race here for the rest of this season. Your history and card still work.':'No series picked, so there are no race details. Your history and card still work.'));return box}
  const dl=h('dl','factband'),add=(k,v)=>{if(!v)return;const c=h('div');c.append(h('dt',null,k),h('dd',null,v));dl.append(c)};
  add('Length',R.length);add('Start',R.start_type&&R.start_type.replace(/ start$/,''));add('Rain',R.rain);add('Cautions',R.cautions);
  add('Setup',S&&S.setup?(S.setup==='fixed'?'Fixed':'Open'):null);
  if(dl.children.length)box.append(dl);else box.append(h('p','note','The schedule doesn\'t give the race details for this week.'));
  if(S&&S.frequency)box.append(h('p','pvfreq',S.frequency.replace(/\.?$/,'.')));
  if(S&&S.multiclass)box.append(h('p','note','Multi-class'+(S.classes&&S.classes.length?': '+S.classes.join(', '):'')+'.'));
  if(!d.week.car_runs){const dr=cat().driven.map(x=>x.name),cs=d.week.cars.slice().sort((x,y)=>(dr.includes(y)?1:0)-(dr.includes(x)?1:0));
    box.append(h('p','note','The '+d.car+' doesn\'t run this week. '+(cs.length>1?'These cars do:':'This car does:')));
    const row=h('div','pkbtns');cs.slice(0,4).forEach(c=>row.append(pkBtn('Use the '+c,false,'use:'+c,()=>setCar(c))));
    if(cs.length>4)row.append(h('span','sub2','and '+(cs.length-4)+' more in the Car list'));box.append(row)}
  return box}
function detailsPart(d){const S=d.series,R=d.race||{};if(!S)return null;const dl=h('dl'),add=(k,v)=>{if(v)dl.append(h('dt',null,k),h('dd',null,v))};
  add('In-game start time',R.in_game_time);add('Temperature',R.temperature);add('Conditions',R.conditions);
  add('License',S.license&&S.license.replace(/\s*-->\s*/,' to '));add('Category',S.category);
  const list=(d.week&&d.week.cars&&d.week.cars.length)?d.week.cars:((serOf(S.id)||{}).cars||[]);add(list.length>1?'Cars':'Car',list.join(', '));
  if(!dl.children.length)return null;const x=h('details','sub pvdetails');x.append(h('summary',null,'Race details'),dl);x.open=!!renderFocus.detailsOpen;
  x.addEventListener('toggle',()=>{renderFocus.detailsOpen=x.open});return x}
function pacePart(d){const sec=pvSection('Your laps here'),P=d.pace;
  if(!P){sec.append(h('p','note',d.status?d.status.detail:'No laps on record here.'));return sec}
  if(P.other_car)sec.append(h('p','note','Not in this car yet. These are from the '+P.other_car+'.'));
  const t=h('table','pacetbl'),hd=h('thead'),hr=h('tr');['','Best','Typical','Races'].forEach(x=>hr.append(h('th',null,x)));hd.append(hr);
  hr.firstChild.setAttribute('aria-label','Conditions');const tb=h('tbody');
  [['Dry',P.dry],['Wet',P.wet]].forEach(([lab,v])=>{const tr=h('tr');const th=h('th',null,lab);th.scope='row';
    tr.append(th,h('td','num',v&&ok(v.best)?fmtT(v.best):'–'),h('td','num',v&&ok(v.typical)?fmtT(v.typical):'–'),h('td','num',v?String(v.races):'0'));tb.append(tr)});
  t.append(hd,tb);sec.append(t);
  const st=d.status;if(st&&st.last)sec.append(h('p','note',st.detail+' Last there '+fmtDate(st.last)+'.'));
  return sec}
function cornerPart(d){const sec=pvSection('Corners that keep costing you');
  if(!d.corners.length){sec.append(h('p','note',d.reviewed_races>=2?'No corner keeps repeating. That\'s a good sign.':'This shows after two or more saved race reviews here in this car.'));return sec}
  const ul=h('ul','pvlist');d.corners.forEach(c=>{const li=h('li');li.append(h('b',null,c.id),
    h('span','sub2','Top three to find in '+c.top3+' of '+c.races+' races'+(ok(c.last_avail)?', last cost about '+c.last_avail.toFixed(2)+' s a lap on '+fmtDate(c.last_date)+(c.last_wet?' (wet)':''):'')+'.'));ul.append(li)});
  sec.append(ul);return sec}
function warmPart(d){if(!d.warmups.length)return null;const sec=pvSection('Extra laps first');const ul=h('ul','pvlist');
  d.warmups.forEach(x=>{const li=h('li');li.append(h('b',null,x.series),h('span','sub2',(x.week?'Week '+x.week+', from ':'From ')+fmtDate(x.start).replace(/, \d{4}$/,'')+(x.same_cars?', same cars':', different cars')));ul.append(li)});
  sec.append(ul);return sec}
function cardPart(d){const sec=pvSection('Track card');const ctx={cars:d.car?[d.car]:[]};
  if(d.has_notes){const cv=h('div','cardview');const c=d.card.fields||{};
    [['lap1','Lap 1'],['hotspots','Hotspots'],['passing','Passing'],['limits','Limits'],['focus','Focus']].forEach(([k,lab])=>{if(c[k]){const p=h('p');p.append(h('b',null,lab+': '),document.createTextNode(c[k]));cv.append(p)}});
    const cn=d.card.car_notes||{};Object.keys(cn).forEach(car=>{if(car===d.car&&cn[car]){const p=h('p');p.append(h('b',null,car+': '),document.createTextNode(cn[car]));cv.append(p)}});
    sec.append(cv)}
  else sec.append(h('p','note','Nothing saved for this track yet. Short notes for what telemetry can\'t see.'));
  const row=h('div','row');const bc=h('button','btn',d.has_notes?'Edit track card':'Add track card');bc.type='button';bc.id='pvCard';
  bc.addEventListener('click',()=>openCard(d.track,ctx));row.append(bc);
  if(ok(d.shared_notes)&&d.shared_notes>0){const bt=h('button','btn','Team notes ('+d.shared_notes+')');bt.type='button';bt.dataset.teamNotes='1';
    bt.addEventListener('click',()=>openCard(d.track,ctx,true));row.append(bt)}
  sec.append(row);return sec}
function craftPart(d){const RC=d.racecraft,RV=d.rivals||[];
  if(!RC&&!RV.length)return d.racecraft_wait?h('p','note pvwait',d.racecraft_wait):null;
  const sec=pvSection('Racecraft here','wide');
  if(RC){const L=RC.lap1,S=RC.passes;if(L&&ok(L.avg))sec.append(h('p','sub2','Lap 1: '+(L.avg>0?'+':'')+L.avg+' places on average over '+L.races+' race'+(L.races===1?'':'s')+'.'));
    if(S&&ok(S.made))sec.append(h('p','sub2','Passes: you made '+S.made+', they made '+(S.lost||0)+' on you, over '+RC.races+' scanned races.'))}
  if(RV.length){const ul=h('ul','pvlist');RV.forEach(a=>{const li=h('li');li.append(h('b',null,a.name),h('span','sub2',a.races+' race'+(a.races===1?'':'s')+', '+Math.round(a.seconds)+' s racing close, you passed them '+a.made+', they passed you '+a.lost));ul.append(li)});sec.append(h('h3','pvsub','Rivals'),ul)}
  if(d.racecraft_note)sec.append(h('p','note',d.racecraft_note));
  return sec}
async function buildPrep(btn){const d=focusData;if(!d)return;setMsg('pvMsg','');
  if(d.track_id!=null&&d.car_id!=null){startJob('prep',{track_id:d.track_id,car_id:d.car_id});return}
  btn.disabled=true;setMsg('pvMsg','Loading track and car lists from Garage 61...');
  const j=await loadCombos(true);btn.disabled=false;
  if(!j||!Array.isArray(j.tracks)){setMsg('pvMsg',$('prepLoadMsg').textContent||'The lists didn\'t load. Try again.','bad');return}
  renderPrep(j);const seq=focusSeq;await refreshFocus(false);if(seq+1!==focusSeq)return;const n=focusData;
  if(n&&n.track_id!=null&&n.car_id!=null)startJob('prep',{track_id:n.track_id,car_id:n.car_id});
  else setMsg('pvMsg','Debrief couldn\'t match this track and car to Garage 61\'s lists. Try Any track and car under More.','bad')}
function renderFocus(d){const wrap=document.createDocumentFragment();const a=document.activeElement,keep=focusKeep||(a&&$('mySeries').contains(a)&&a.id?a.id:null);focusKeep=null;
  const head=h('div','pvhead');const tt=h('div','pvtt');tt.append(h('h2','pvtrack',d.track||'Pick a track'));
  const by=[d.car,d.series&&d.week?d.series.name:null].filter(Boolean).join(' in ');if(by)tt.append(h('p','pvby',by));
  const when=weekWhen(d);if(when)tt.append(h('p','pvwhen',when));head.append(tt);
  const acts=h('div','pvacts');const token=!!(prepData&&prepData.token);
  if(d.track&&d.car){const b=h('button','btn primary','Build prep page');b.type='button';b.id='pvBuild';
    if(!token){b.disabled=true;b.title='Add your Garage 61 token in Settings to build a prep page.'}
    b.addEventListener('click',()=>buildPrep(b));acts.append(b);
    if(d.page){const o=h('button','btn','Open your last page');o.type='button';o.id='pvOpen';o.addEventListener('click',()=>openPage(d.page,'prepPage'));acts.append(o)}
    const f=h('button','btn','Plan fuel');f.type='button';f.id='pvFuel';f.dataset.fuel='1';
    f.addEventListener('click',()=>openFuel({track:d.track,series:d.series?d.series.id:undefined,week:d.week?d.week.week:undefined,cars:[d.car],length:d.race?d.race.length:null}));acts.append(f)}
  head.append(acts);wrap.append(head);wrap.append(toolLinks(d));
  const msg=h('p','msg');msg.id='pvMsg';msg.setAttribute('role','status');wrap.append(msg);
  if(d.track&&d.car&&!token)msg.textContent='Add your Garage 61 token in Settings to build a prep page.';
  const flag=stripData&&d.week&&(stripData.weeks||[]).find(w=>w.no===d.week.no&&w.flag);
  if(flag&&flag.flag_reason){const f=h('p','pvflag');f.append(h('b',null,'Needs prep. '),document.createTextNode(flag.flag_reason));wrap.append(f)}
  if(!d.track){const det=detailsPart(d);if(det)wrap.append(det);wrap.append(h('p','note',(d.series?d.series.name+' has no race weeks left this season. ':'')+'Pick a track above and your history there shows here.'));$('mySeries').replaceChildren(wrap);return}
  wrap.append(racePart(d));const det=detailsPart(d);if(det)wrap.append(det);
  const grid=h('div','pvgrid'),L=h('div','pvcol'),R=h('div','pvcol');
  L.append(pacePart(d));if(d.pace)L.append(cornerPart(d));R.append(cardPart(d));const wm=warmPart(d);if(wm)R.append(wm);grid.append(L,R);wrap.append(grid);
  const cr=craftPart(d);if(cr)wrap.append(cr);
  $('mySeries').replaceChildren(wrap);if(keep&&$(keep)&&!$('dlgCard').open&&!$('dlgFuel').open)$(keep).focus()}
/* season plan: the rest of the season. Facts only, worked out in prepare.plan. */
function planRace(r){const d=h('div','gridf');d.style.gap='4px';d.style.marginTop='8px';
  const top=h('div','phead');const nm=h('span',null);nm.append(h('b',null,r.track));nm.append(h('span','sub2',' · '+r.series+' week '+r.week));
  const pl=h('span','pill '+(r.status==='raced'?'ok':r.flag?'no':''),r.status_text);top.append(nm,pl);d.append(top);
  const facts=[r.length,r.rain,r.start_type].filter(Boolean);if(facts.length)d.append(h('span','sub2',facts.join(' · ')));
  d.append(h('span','sub2',r.detail+(r.last?' Last there '+fmtDate(r.last)+'.':'')));
  const P=r.pace;if(P){const bits=['Best '+fmtT(P.best)+(P.best_wet?' (wet)':'')];if(ok(P.typical))bits.push('typical '+fmtT(P.typical)+' over '+P.typical_races+(P.wet_races?' dry race':' race')+(P.typical_races===1?'':'s'));
    if(P.wet_races&&ok(P.wet_best))bits.push('wet best '+fmtT(P.wet_best));d.append(h('span','sub2',bits.join(' · ')+(P.car?' in the '+P.car:'')))}
  (r.warmups||[]).forEach(x=>d.append(h('span','sub2','Extra laps first: '+x.series+(x.week?' week '+x.week:'')+', from '+fmtDate(x.start).replace(/, \d{4}$/,'')+(x.same_cars?', same cars':', different cars'))));
  if(r.flag_reason){const f=h('span','sub2');f.append(h('b',null,'Most prep: '),document.createTextNode(r.flag_reason));d.append(f)}
  const row=h('div','row');const go=h('button','btn','See this week');go.type='button';go.style.padding='6px 11px';go.addEventListener('click',()=>pickSeriesWeek(r.series_id,r.season_week));row.append(go);
  if(r.track_id!=null&&r.car_id!=null){const bp=h('button','btn','Build prep page');bp.type='button';bp.style.padding='6px 11px';
    bp.addEventListener('click',()=>startJob('prep',{track_id:r.track_id,car_id:r.car_id}));row.append(bp)}
  d.append(row);return d}
function renderSeasonList(P){const host=$('planSeasonList'),rows=P.season||[];$('planSeason').hidden=!rows.length;host.replaceChildren();
  rows.forEach(w=>{const r=h('div',(w.no===cat().current_week?'now':'')+(w.left?'':' past'));
    r.append(h('span','mono','Wk '+w.week),h('span','mono',fmtDate(w.start).replace(/, \d{4}$/,'')),h('span',null,w.track),h('span','sub2',w.has_notes?'Card saved':(w.length||'')));host.append(r);
    if(w.warmups&&w.warmups.length){const u=h('div','sub2 warmups');u.style.gridColumn='1 / -1';u.textContent='Extra laps first: '+w.warmups.map(x=>String(x.series)+' ('+fmtDate(x.start).replace(/, \d{4}$/,'')+')').join(', ');host.append(u)}})}
function renderPlan(){const panel=$('planPanel'),P=stripData,s=serOf(pk.series);panel.hidden=!P||!s;if(!P||!s)return;
  const host=$('planWeeks'),pill=$('planPill'),note=$('planNote'),notes=$('planNotes');notes.replaceChildren();
  (P.notes||[]).forEach(t=>notes.append(h('li',null,t)));notes.hidden=!(P.notes||[]).length;renderSeasonList(P);
  const wks=P.weeks||[];pill.textContent=wks.length?wks.length+' week'+(wks.length===1?'':'s')+' left':'';
  if(P.state==='error'||!wks.length){host.replaceChildren();note.textContent=P.state==='season_over'||P.state==='ok'?'No race weeks left in this season for this series.':'The season plan couldn\'t be worked out. The rest of this page is fine.';return}
  note.textContent=wks.some(w=>w.flag)?'"Needs prep" marks the weeks with the least history behind them, from your saved reviews and synced sessions.':'No week stands out as needing more prep than the others.';
  keepFocus(host,()=>host.replaceChildren(...wks.map(w=>{const b=h('button','stripwk'+(w.no===pk.week?' on':'')+(w.flag?' flag':''));b.type='button';b.dataset.k=String(w.no);
    b.setAttribute('aria-pressed',String(w.no===pk.week));const top=h('span','stripno',(w.now?'This week':'Week '+w.week));
    b.append(top,h('span','striptr',w.short));if(w.flag)b.append(h('span','stripflag','Needs prep'));
    b.title=w.track+(w.flag_reason?'. '+w.flag_reason:'');const row=s.weeks.find(x=>x.no===w.no);
    if(row&&!carRuns(s,row,pk.car)){b.classList.add('off');b.append(h('span','stripoff','Other cars'));b.title='Other cars run this week: '+weekCars(s,row).join(', ')}
    b.addEventListener('click',()=>setWeek(w.no));return b})))}
$('btnSchDl').addEventListener('click',async()=>{if(lastJob&&lastJob.state==='running'&&lastJob.kind!=='sync'){banner('Debrief is busy. Wait for it to finish.',true);return}
  setMsg('schMsg','Starting...');schBusy(true);let r;try{r=await post('schedule',{action:'download'})}catch(e){connectionLost();return}
  if(!r.ok){schBusy(false);setMsg('schMsg',r.error,'bad');return}lastJob={state:'running',kind:'schedule',id:r.id};poll()});
$('btnSchLoad').addEventListener('click',()=>$('schFile').click());
$('schFile').addEventListener('change',async()=>{const f=$('schFile').files[0];$('schFile').value='';if(!f)return;
  if(f.size>40*1024*1024){setMsg('schMsg','That file is too big to be the season schedule.','bad');return}
  setMsg('schMsg','Sending '+f.name+'...');schBusy(true);let r;
  try{r=await fetch(BASE+'api/schedule-upload',{method:'POST',headers:{'X-Debrief':'1','Content-Type':'application/pdf'},body:f}).then(x=>x.json())}catch(e){schBusy(false);setMsg('schMsg','Couldn\'t send the file.','bad');return}
  if(!r.ok){schBusy(false);setMsg('schMsg',r.error,'bad');return}lastJob={state:'running',kind:'schedule',id:r.id};poll()});
/* track card */
let cardFor=null;
function openCard(track,s,toTeam){cardFor={track,cars:(s&&s.cars)||[],season:prepData&&prepData.schedule&&prepData.schedule.season,loaded:false,toTeam:!!toTeam};
  $('cardTeam').hidden=true;$('cardTeamList').replaceChildren();$('cardShareNote').hidden=true;
  $('cardTitle').textContent='Track card: '+track;setMsg('cardMsg','');$('cardUpdated').textContent='';$('cardReload').hidden=true;
  ['lap1','hotspots','passing','limits','focus'].forEach(k=>$('c_'+k).value='');$('c_car').value='';
  const car=cardFor.cars.length===1?cardFor.cars[0]:null;cardFor.car=car;$('cCarWrap').hidden=!car;if(car)$('cCarLab').textContent='Note for the '+car;
  const d=$('dlgCard');if(d.showModal)d.showModal();else d.setAttribute('open','');
  loadCard(track)}
// (re)load the saved card into the open dialog; remembers its "updated" time so a save can't overwrite a newer one
function loadCard(track){$('cardSave').disabled=true;
  api('notes?track='+encodeURIComponent(track)).then(j=>{if(!cardFor||cardFor.track!==track)return;if(!j.ok){setMsg('cardMsg',j.error,'bad');return}const c=j.card||{};cardFor.card=c;cardFor.loaded=true;
    cardFor.base=typeof c.updated==='string'&&c.updated?c.updated:null;const car=cardFor.car;
    ['lap1','hotspots','passing','limits','focus'].forEach(k=>$('c_'+k).value=(c.fields||{})[k]||'');
    if(car)$('c_car').value=(c.car_notes||{})[car]||'';
    $('cardUpdated').textContent=c.updated?'Last saved '+fmtDate(c.updated)+(c.season?' ('+c.season+')':''):'';
    if(c.read_only){$('cardSave').disabled=true;setMsg('cardMsg','Saved by a newer Debrief: update to edit.','bad')}else $('cardSave').disabled=false;
    renderTeamNotes(j.shared,j.sharing)})
  .catch(()=>connectionLost())}
// teammates' cards from the shared folder: untrusted text, shown with textContent only; "Copy into mine" fills
// your own field (you still press Save, which saves only your card)
const CARD_LABELS={lap1:'Lap 1 / start',hotspots:'Crash hotspots',passing:'Passing spots',limits:'Track limits / kerbs',focus:'Focus this week'};
function renderTeamNotes(shared,sharing){const list=Array.isArray(shared)?shared.filter(x=>x&&typeof x==='object'):[];
  const host=$('cardTeamList');host.replaceChildren();$('cardTeam').hidden=!list.length;
  $('cardTeamNote').textContent=list.length?list.length+' teammate'+(list.length===1?'':'s')+' · read-only':'';
  list.slice(0,50).forEach(t=>{const a=t.author||{};const box=h('div','tauthor');
    const who=h('span','who');who.append(h('b',null,String(a.name||a.slug||'A teammate').slice(0,80)));
    if(typeof t.updated==='string'&&t.updated)who.append(document.createTextNode(' · '+fmtDate(t.updated)));box.append(who);
    const f=t.fields||{};let any=false;
    Object.keys(CARD_LABELS).forEach(k=>{const v=f[k];if(typeof v!=='string'||!v.trim())return;any=true;
      const r=h('div','tfield');const p=h('p');p.append(h('b',null,CARD_LABELS[k]+': '),document.createTextNode(v));
      const b=h('button','btn','Copy into mine');b.type='button';b.dataset.copy=k;
      b.addEventListener('click',()=>{const el=$('c_'+k);const cur=el.value.trim();
        if(cur.split('\n').some(x=>x.trim()===v.trim())){setMsg('cardMsg','That\'s already in your card.');return}
        el.value=(cur?cur+'\n'+v:v).slice(0,300);setMsg('cardMsg','Copied into your '+CARD_LABELS[k].toLowerCase()+'. Press Save card to keep it.')});
      r.append(p,b);box.append(r)});
    const cn=t.car_notes&&typeof t.car_notes==='object'?t.car_notes:{};
    Object.keys(cn).slice(0,20).forEach(car=>{if(typeof cn[car]!=='string'||!cn[car].trim())return;any=true;
      const r=h('div','tfield');const p=h('p');p.append(h('b',null,car+': '),document.createTextNode(cn[car]));r.append(p);
      if(cardFor&&cardFor.car===car){const b=h('button','btn','Copy into mine');b.type='button';
        b.addEventListener('click',()=>{const el=$('c_car');el.value=(el.value.trim()?el.value.trim()+' · ':'')+cn[car];el.value=el.value.slice(0,200)});r.append(b)}
      box.append(r)});
    if(any)host.append(box)});
  if(!host.children.length)$('cardTeam').hidden=true;
  const sh=sharing||{};const n=$('cardShareNote');n.hidden=true;
  if(sh.on&&sh.folder_ok&&!sh.author){n.hidden=false;n.textContent='Check your Garage 61 connection once so Debrief knows your name.'}
  else if(sh.on&&sh.folder_ok&&sh.author){n.hidden=false;n.textContent='Saving also shares this card with your team as '+sh.author.name+'.'}
  else if(sh.on&&!sh.folder_ok){n.hidden=false;n.textContent='The team cards folder isn\'t there right now, so this card is saved on this PC only.'}
  if(cardFor&&cardFor.toTeam&&!$('cardTeam').hidden)$('cardTeam').scrollIntoView({block:'nearest'})}
function closeCard(){const d=$('dlgCard');if(d.close)d.close();else d.removeAttribute('open')}
$('cardClose').addEventListener('click',closeCard);
$('fCard').addEventListener('submit',async e=>{e.preventDefault();if(!cardFor)return;
  const fields={};['lap1','hotspots','passing','limits','focus'].forEach(k=>fields[k]=$('c_'+k).value);
  const car_notes=Object.assign({},(cardFor.card&&cardFor.card.car_notes)||{});if(cardFor.car)car_notes[cardFor.car]=$('c_car').value;
  if(!cardFor.loaded){setMsg('cardMsg','The card is still loading. Try again in a moment.','bad');return}
  setMsg('cardMsg','Saving...');$('cardReload').hidden=true;let r;
  try{r=await post('notes',{card:{track:cardFor.track,fields,car_notes,season:cardFor.season,base_updated:cardFor.base}})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('cardMsg',r.error||'The card wasn\'t saved.','bad');
    // saved somewhere else since this dialog opened: offer to load the newer card
    if(r.conflict||/changed somewhere else|open it again|newer notes/i.test(r.error||''))$('cardReload').hidden=false;return}
  closeCard();banner(r.card&&typeof r.card.share_error==='string'&&r.card.share_error?(/^Saved on this PC/.test(r.card.share_error)?'':'Track card saved on this PC. ')+r.card.share_error:'Track card saved.',
    !!(r.card&&r.card.share_error),8000);loadPrepare(true)});
$('cardReload').addEventListener('click',()=>{if(!cardFor)return;$('cardReload').hidden=true;setMsg('cardMsg','Loaded the newer card. Your unsaved text was replaced; add your change again.');loadCard(cardFor.track)});
/* ---------- fuel planner (2.4): prefill from your own laps (no Garage 61 requests), plan on the server */
let fuelFor=null;
const GAL=3.785;
const fmtL=v=>ok(v)?v.toFixed(1)+' L ('+(v/GAL).toFixed(2)+' gal)':'–';
const numOr=(id)=>{const v=$(id).value.trim();return v===''?null:Number(v)};
function fuelSrc(id,o,unitless){const el=$('s_'+id);el.textContent='';el.className='src';
  if(!o||typeof o!=='object')return;const bits=[];if(typeof o.source==='string'&&o.source)bits.push(o.source.startsWith('from')||o.source.startsWith('estimated')?o.source:'from '+o.source);
  if(o.estimated){bits.push('check it in the garage');el.className='src est'}
  el.textContent=bits.join(' · ')}
function openFuel(ctx){fuelFor={ctx,car:(ctx.cars||[])[0]||null,pre:null};
  $('fuelTitle').textContent='Plan fuel: '+ctx.track;$('fuelSub').textContent=ctx.length?'Race length from the schedule: '+ctx.length+'.':'Type in the race length.';
  ['f_laps','f_minutes','f_lap_t','f_burn','f_tank','f_pit'].forEach(id=>$(id).value='');$('f_reserve').value='1';
  ['laps','minutes','lap_t','burn','tank','pit'].forEach(k=>{$('s_'+k).textContent=''});
  $('fuelRes').hidden=true;$('fuelNoBurn').hidden=true;$('fP90Wrap').hidden=true;setMsg('fuelMsg','Loading your numbers...');
  const sel=$('f_car');sel.replaceChildren();(ctx.cars||[]).forEach(c=>{const o=h('option',null,c);o.value=c;sel.append(o)});
  $('fCarWrap').hidden=(ctx.cars||[]).length<2;
  const d=$('dlgFuel');if(d.showModal)d.showModal();else d.setAttribute('open','');
  loadFuel()}
async function loadFuel(){const f=fuelFor;if(!f)return;const c=f.ctx;
  const q='strategy?track='+encodeURIComponent(c.track)+(f.car?'&car='+encodeURIComponent(f.car):'')+(c.series?'&series='+encodeURIComponent(c.series)+'&week='+encodeURIComponent(c.week||''):'');
  let j;try{j=await api(q)}catch(e){connectionLost();return}
  if(fuelFor!==f)return;
  if(!j.ok){setMsg('fuelMsg',j.error,'bad');return}
  const p=j.prefill||{};f.pre=p;setMsg('fuelMsg','');
  const r=p.race||{};if(ok(r.laps))$('f_laps').value=r.laps;if(ok(r.minutes))$('f_minutes').value=r.minutes;
  fuelSrc(ok(r.minutes)?'minutes':'laps',r.source?{source:r.source==='schedule'?'from the schedule'+(r.text?' ('+r.text+')':''):r.source}:null);
  const lt=p.lap_t||{};if(ok(lt.value))$('f_lap_t').value=fmtT(lt.value);fuelSrc('lap_t',lt);
  const b=p.burn||{};if(ok(b.value))$('f_burn').value=b.value.toFixed(2);fuelSrc('burn',b);$('fuelNoBurn').hidden=ok(b.value);
  $('fP90Wrap').hidden=!ok(b.p90);if(ok(b.p90))$('fP90Lab').textContent='Plan with your thirstier laps ('+b.p90.toFixed(2)+' L a lap)';
  const t=p.tank||{};if(ok(t.value))$('f_tank').value=t.value;fuelSrc('tank',t);
  const pl=p.pit_loss||{};if(ok(pl.value))$('f_pit').value=pl.value.toFixed(1);fuelSrc('pit',pl);
  if(ok(p.reserve_laps))$('f_reserve').value=p.reserve_laps}
$('f_car').addEventListener('change',()=>{if(!fuelFor)return;fuelFor.car=$('f_car').value;$('fuelRes').hidden=true;loadFuel()});
function closeFuel(){const d=$('dlgFuel');if(d.close)d.close();else d.removeAttribute('open');fuelFor=null}
$('fuelClose').addEventListener('click',closeFuel);
function fuelInputs(){const inp={};const laps=numOr('f_laps'),mins=numOr('f_minutes');
  if(laps!=null)inp.laps=laps;else if(mins!=null)inp.minutes=mins;
  const lt=$('f_lap_t').value.trim();if(lt){const v=parseLap(lt);if(v==null)throw new Error('Type your lap time like 1:25.900.');inp.lap_t=v}
  const b=numOr('f_burn');if(b!=null)inp.burn_l=b;
  const t=numOr('f_tank');if(t!=null)inp.tank_l=t;
  const pl=numOr('f_pit');if(pl!=null)inp.pit_loss_s=pl;
  const rv=numOr('f_reserve');if(rv!=null)inp.reserve_laps=rv;
  const pre=(fuelFor&&fuelFor.pre)||{};const pb=pre.burn||{};
  // the thirstier-laps figure only applies to the prefilled fuel use; a typed-in number is used as it is
  if(ok(pb.p90)&&!$('fP90Wrap').hidden&&ok(pb.value)&&b!=null&&Math.abs(b-pb.value)<0.005){inp.use_p90=$('f_p90').checked;inp.p90=pb.p90}else inp.use_p90=false;
  if(ok(pb.n)&&ok(pb.value)&&b!=null&&Math.abs(b-pb.value)<0.005)inp.burn_n=pb.n;
  const pt=pre.tank||{};if(pt.estimated&&ok(pt.value)&&t!=null&&Math.abs(t-pt.value)<0.01)inp.tank_estimated=true;
  return inp}
function kpi(k,v,sub){const d=h('div','kpi');d.append(h('span','k',k),h('span','v',v));if(sub)d.append(h('span','s',sub));return d}
function renderFuelPlan(p){$('fuelRes').hidden=false;const k=$('fuelKpis');k.replaceChildren();
  k.append(kpi('Stops',ok(p.stops)?String(p.stops):'–',p.stops===0?'No stop needed':null),
           kpi('Start with',ok(p.start_fuel_l)?p.start_fuel_l.toFixed(1)+' L':'–',ok(p.start_fuel_l)?(p.start_fuel_l/GAL).toFixed(2)+' gal':null),
           kpi('Fuel for the race',ok(p.fuel_needed_l)?p.fuel_needed_l.toFixed(1)+' L':'–',ok(p.laps)?p.laps+' laps':null));
  if(ok(p.laps_per_tank))k.append(kpi('Laps a tank',String(p.laps_per_tank)));
  if(ok(p.pit_time_s)&&p.stops>0)k.append(kpi('Time in the pits',p.pit_time_s.toFixed(0)+' s'));
  const ul=$('fuelWindows');ul.replaceChildren();
  (Array.isArray(p.windows)?p.windows:[]).forEach((w,i)=>{const li=h('li');const n=ok(w.stop)?w.stop:i+1;
    // "Stop after lap 14 (window 11–17): add 25.5 L" — the add is right for any lap in the window
    const at=ok(w.at_lap)?w.at_lap:null,win=ok(w.open)&&ok(w.close)?(w.open===w.close?'lap '+w.open:w.open+'–'+w.close):null;
    li.append(h('b',null,'Stop '+n),document.createTextNode(at!=null?' after lap '+at+(win?' (window '+win+')':''):(win?' in laps '+win:'')));
    const add=ok(w.fuel_add_l)?w.fuel_add_l:ok(w.add_l)?w.add_l:null;if(add!=null)li.append(document.createTextNode(': add '+fmtL(add)));ul.append(li)});
  if((Array.isArray(p.windows)?p.windows:[]).length){const li=h('li','sub2');
    li.textContent='Pit on any lap in its window and add the same amount. Keep to the windows for every stop.';ul.append(li)}
  (Array.isArray(p.stints)?p.stints:[]).forEach((st,i)=>{if(!ok(st.from_lap))return;const li=h('li','sub2');
    li.textContent='Stint '+(i+1)+': laps '+st.from_lap+'–'+st.to_lap+(ok(st.fuel_l)?' · '+fmtL(st.fuel_l):'');ul.append(li)});
  showWarnings('fuelWarn',p.warnings,'Worth knowing:')}
$('fFuel').addEventListener('submit',async e=>{e.preventDefault();if(!fuelFor)return;let inputs;
  const badEl=['f_laps','f_minutes','f_burn','f_tank','f_pit','f_reserve'].map($).find(x=>!x.checkValidity());
  if(badEl){setMsg('fuelMsg',badEl.closest('label').querySelector('span').textContent+': '+badEl.validationMessage,'bad');badEl.focus();return}
  try{inputs=fuelInputs()}catch(err){setMsg('fuelMsg',err.message,'bad');return}
  if(!('laps' in inputs)&&!('minutes' in inputs)){setMsg('fuelMsg','Type in the race length (laps or minutes).','bad');$('f_laps').focus();return}
  if(!('burn_l' in inputs)){setMsg('fuelMsg','Type in your fuel use a lap.','bad');$('f_burn').focus();return}
  if(!('tank_l' in inputs)){setMsg('fuelMsg','Type in the tank size.','bad');$('f_tank').focus();return}
  setMsg('fuelMsg','Working it out...');$('fuelGo').disabled=true;let r;
  try{r=await post('strategy',{action:'plan',inputs})}catch(err){connectionLost();return}finally{$('fuelGo').disabled=false}
  if(!r.ok){setMsg('fuelMsg',r.error,'bad');$('fuelRes').hidden=true;return}
  setMsg('fuelMsg','');renderFuelPlan(r.plan||{})});
$('fuelSaveCar').addEventListener('click',async()=>{if(!fuelFor)return;const car=fuelFor.car;
  if(!car){setMsg('fuelMsg','Pick a car first.','bad');return}
  const tank=numOr('f_tank'),pit=numOr('f_pit');const body={action:'save_car',car};
  if(tank!=null)body.tank_l=tank;if(pit!=null){body.pit_loss_s=pit;body.track=fuelFor.ctx.track}
  if(tank==null&&pit==null){setMsg('fuelMsg','Type in the tank size first.','bad');return}
  let r;try{r=await post('strategy',body)}catch(e){connectionLost();return}
  if(!r.ok){setMsg('fuelMsg',r.error,'bad');return}
  setMsg('fuelMsg','Saved for the '+car+'.','ok');$('s_tank').textContent='saved for this car';$('s_tank').className='src'});

/* ---------- Track Intel import (2.4): upload -> map columns -> preview -> import */
let intel=null;
const INTEL_FIELDS=[['ignore','(ignore)'],['track','Track'],['car','Car'],['lap1','Lap 1 / start'],['hotspots','Crash hotspots'],
  ['passing','Passing spots'],['limits','Track limits / kerbs'],['focus','Focus'],['car_note','Car note']];
$('btnIntel').addEventListener('click',()=>$('intelFile').click());
$('intelFile').addEventListener('change',async()=>{const f=$('intelFile').files[0];$('intelFile').value='';if(!f)return;
  const ext=(/\.([a-z0-9]+)$/i.exec(f.name)||[])[1];
  if(!ext||!['xlsx','csv'].includes(ext.toLowerCase())){setMsg('intelMsg','Pick an .xlsx or .csv file.','bad');return}
  if(f.size>5*1024*1024){setMsg('intelMsg','That file is too big. A Track Intel sheet is well under 5 MB.','bad');return}
  setMsg('intelMsg','Reading '+f.name+'...');$('intelMap').hidden=true;$('btnIntel').disabled=true;let r;
  try{r=await fetch(BASE+'api/intel-upload?name='+encodeURIComponent(f.name),{method:'POST',headers:{'X-Debrief':'1','Content-Type':'application/octet-stream'},body:f}).then(x=>x.json())}
  catch(e){setMsg('intelMsg','Couldn\'t send the file.','bad');return}finally{$('btnIntel').disabled=false}
  if(!r||!r.ok){setMsg('intelMsg',(r&&r.error)||'Couldn\'t read that file.','bad');return}
  intel={id:r.upload_id,sheets:Array.isArray(r.sheets)?r.sheets:[],sheet:0,name:f.name,previewed:false};
  setMsg('intelMsg','Loaded '+f.name+'.');renderIntelSheets()});
function renderIntelSheets(){const sel=$('intelSheet');sel.replaceChildren();
  intel.sheets.forEach((sh,i)=>{const o=h('option',null,sh.name+(ok(sh.n_rows)?' ('+sh.n_rows+' rows)':''));o.value=String(i);sel.append(o)});
  $('intelSheetWrap').hidden=intel.sheets.length<2;sel.value=String(intel.sheet);$('intelMap').hidden=false;renderIntelCols()}
$('intelSheet').addEventListener('change',()=>{if(!intel)return;intel.sheet=+$('intelSheet').value;renderIntelCols()});
function intelDirty(){if(!intel)return;intel.previewed=false;$('btnIntelImport').disabled=true;$('intelPreview').replaceChildren()}
function renderIntelCols(){intelDirty();const sh=intel.sheets[intel.sheet]||{};const tb=$('intelCols');tb.replaceChildren();
  const hdr=Array.isArray(sh.header)?sh.header:[];const sug=sh.suggest||{};
  hdr.forEach((name,i)=>{const tr=h('tr');tr.append(h('td',null,String(name||'Column '+(i+1))));
    const samp=(sh.rows||[]).slice(0,3).map(r=>String((r||[])[i]||'')).filter(Boolean).join(' · ');tr.append(h('td','samp',samp));
    const td=h('td');const s=document.createElement('select');s.dataset.col=String(i);s.setAttribute('aria-label','What column '+(name||i+1)+' holds');
    INTEL_FIELDS.forEach(([v,lab])=>{const o=h('option',null,lab);o.value=v;s.append(o)});
    s.value=INTEL_FIELDS.some(f=>f[0]===sug[String(i)])?sug[String(i)]:'ignore';s.addEventListener('change',intelDirty);td.append(s);tr.append(td);tb.append(tr)});
  setMsg('intelMapMsg','')}
function intelBody(){const mapping={};document.querySelectorAll('#intelCols select').forEach(s=>{if(s.value!=='ignore')mapping[s.dataset.col]=s.value});
  const m=document.querySelector('input[name=intelMode]:checked');return {upload_id:intel.id,sheet:intel.sheet,mapping,mode:m?m.value:'fill_empty'}}
document.querySelectorAll('input[name=intelMode]').forEach(x=>x.addEventListener('change',intelDirty));
function intelRows(res){return Array.isArray(res)?res:(res&&Array.isArray(res.tracks)?res.tracks:[])}
$('btnIntelPreview').addEventListener('click',async()=>{if(!intel)return;const body=intelBody();
  if(Object.values(body.mapping).filter(v=>v==='track').length!==1){setMsg('intelMapMsg','Pick the one column that holds the track name.','bad');return}
  setMsg('intelMapMsg','Checking...');let r;try{r=await post('intel-preview',body)}catch(e){connectionLost();return}
  if(!r.ok){setMsg('intelMapMsg',r.error,'bad');if(/expired/i.test(r.error||''))intel=null;return}
  const rows=intelRows(r.preview);const host=$('intelPreview');host.replaceChildren();
  const changed=rows.filter(t=>Array.isArray(t.changes)&&t.changes.length).length;
  rows.forEach(t=>{const it=h('div','item');const tx=h('div','itx ptrack');
    tx.append(h('b',null,String(t.track||'')));
    tx.append(h('span','sub2',(t.matched?(typeof t.matched==='string'&&t.matched!==t.track?'Matches '+t.matched:'Matches a track you know'):'New: kept as typed')));
    const ch=Array.isArray(t.changes)?t.changes:[];
    tx.append(h('span','sub2',ch.length?'Changes: '+ch.map(k=>(CARD_LABELS[k]||k).toLowerCase()).join(', '):'No change'));
    warnList(t.warnings).slice(0,4).forEach(w=>tx.append(h('span','sub2 badc',w)));
    it.append(tx);host.append(it)});
  if(!rows.length)host.append(h('p','note','No rows with a track name in that sheet.'));
  intel.previewed=true;$('btnIntelImport').disabled=!changed;
  setMsg('intelMapMsg',rows.length+' track'+(rows.length===1?'':'s')+' · '+changed+' would change.')});
$('btnIntelImport').addEventListener('click',async()=>{if(!intel||!intel.previewed)return;const body=intelBody();body.confirm=true;
  setMsg('intelMapMsg','Importing...');$('btnIntelImport').disabled=true;let r;try{r=await post('intel-import',body)}catch(e){connectionLost();return}
  if(!r.ok){setMsg('intelMapMsg',r.error,'bad');return}
  const n=ok(r.changed)?r.changed:0,tot=ok(r.tracks)?r.tracks:n;intel=null;$('intelMap').hidden=true;
  const er=warnList(r.errors);
  setMsg('intelMsg','Updated '+n+' track card'+(n===1?'':'s')+' ('+Math.max(0,tot-n)+' unchanged).'+(er.length?' Not saved: '+er.slice(0,3).join(' '):''),er.length?'bad':'ok');loadPrepare(true)});
$('btnIntelCancel').addEventListener('click',()=>{intel=null;$('intelMap').hidden=true;setMsg('intelMsg','')});

function renderPrep(j){
  const host=$('prepCombos');host.replaceChildren();
  if(!j){host.append(h('p','note','Couldn\'t load your tracks.'));return}
  if(!j.prep_available)host.append(h('p','msg bad','Track prep isn\'t available right now. Details were saved to debrief.log.'));
  const combos=j.combos||[];
  if(!combos.length)host.append(h('p','note','No reviewed races yet. Review a race first, or search any track and car below.'));
  combos.forEach(c=>{const row=h('div','item');const tx=h('div','itx');tx.append(h('b',null,c.track),h('span','sub2',c.car+' · '+(c.races||0)+' race'+(c.races===1?'':'s')+(c.last_date?' · last '+fmtDate(c.last_date):'')));
    const b=h('button','btn','Build prep page');b.type='button';
    if(c.track_id==null||c.car_id==null){b.disabled=true;b.title='Load the track and car lists below so Debrief can match this one.';
      tx.append(h('span','sub2','Load the track and car lists below so Debrief can match this one.'))}
    b.addEventListener('click',()=>startJob('prep',{track_id:c.track_id,car_id:c.car_id}));
    row.append(tx,b);host.append(row)});
  const have=Array.isArray(j.tracks)&&Array.isArray(j.cars);
  $('prepSearch').hidden=!have;$('prepGoRow').hidden=!have;$('prepLoadRow').hidden=have||!j.token;
  $('prepListNote').textContent=!j.token?'Add your Garage 61 token in Settings to search every track and car.':
    have?'Search Garage 61\'s '+j.tracks.length+' tracks and '+j.cars.length+' cars (saved on this PC).':
    'Garage 61\'s track and car lists aren\'t saved on this PC yet. Load them once and Debrief reuses them for a week.';
  if(have){fillList($('dlTracks'),j.tracks);fillList($('dlCars'),j.cars)}
}
function fillList(dl,items){if(dl.children.length===items.length)return;dl.replaceChildren(...items.map(x=>{const o=document.createElement('option');o.value=x.name;return o}))}
function renderPrepPages(){const list=(pages&&pages.prep)||[];$('prepPagesPanel').hidden=!list.length;const host=$('prepPages');host.replaceChildren();
  list.slice(0,12).forEach(p=>{const row=h('div','item');const tx=h('div','itx');tx.append(h('b',null,p.name),h('span','sub2','Built '+fmtWhen(p.updated)));
    const b=h('button','btn','Open');b.type='button';b.addEventListener('click',()=>openPage(p.page,'prepPage'));row.append(tx,b);host.append(row)})}
$('btnPrepLists').addEventListener('click',async()=>{setMsg('prepLoadMsg','Loading from Garage 61...');$('btnPrepLists').disabled=true;
  const j=await loadCombos(true);$('btnPrepLists').disabled=false;
  if(j&&Array.isArray(j.tracks)){setMsg('prepLoadMsg','');renderPrep(j);banner('Track and car lists saved.')}});
$('fPrepAny').addEventListener('submit',e=>{e.preventDefault();const j=combosCache;if(!j||!j.tracks)return;
  const find=(items,v)=>{v=v.trim().toLowerCase();return items.find(x=>x.name.toLowerCase()===v)};
  const t=find(j.tracks,$('prepTrack').value),c=find(j.cars,$('prepCar').value);
  if(!t){setMsg('prepMsg','Pick a track from the list.','bad');$('prepTrack').focus();return}
  if(!c){setMsg('prepMsg','Pick a car from the list.','bad');$('prepCar').focus();return}
  setMsg('prepMsg','');startJob('prep',{track_id:t.id,car_id:c.id})});
$('fbPrep').addEventListener('click',openPrep);

/* ---------- Goals */
let editing=null,goalCombos=[];
async function openGoals(){current={view:'goals',dir:null};show('vGoals');markNav();resetGoalForm();
  const j=await loadCombos(false);goalCombos=(j&&j.combos)||[];fillGoalCombos();await loadGoals()}
function fillGoalCombos(extra){const sel=$('goalCombo');sel.replaceChildren();const seen=new Set();
  const all=goalCombos.map(c=>({track:c.track,car:c.car})).concat(extra?[extra]:[]);
  all.forEach(c=>{const k=c.track+'|'+c.car;if(seen.has(k))return;seen.add(k);const o=h('option',null,comboLabel(c));o.value=k;sel.append(o)});
  const none=!sel.children.length;$('goalSave').disabled=none;if(none){const o=h('option',null,'Review a race first');o.value='';sel.append(o)}}
async function loadGoals(){let j;try{j=await api('goals')}catch(e){connectionLost();return}
  const host=$('goalList');host.replaceChildren();
  if(!j.ok){host.append(h('p','msg bad',j.error||'Couldn\'t load goals.'));return}
  const prog=new Map((j.progress||[]).map(p=>[p.goal&&p.goal.id,p]));
  const goals=j.goals||[];
  if(!goals.length)host.append(h('p','note','No goals yet. Add one below.'));
  goals.forEach(g=>{const p=prog.get(g.id)||{};const row=h('div','item goal');const tx=h('div','itx');
    tx.append(h('b',null,comboLabel(g)));
    const line=h('span','sub2');line.append(document.createTextNode('Target '),h('b','num',fmtT(g.target_s)),document.createTextNode(' · your best '),h('b','num',fmtT(p.best)));
    if(ok(p.gap))line.append(document.createTextNode(' · '),h('b','num '+(p.gap<=0?'okc':'badc'),p.gap<=0?'beaten by '+Math.abs(p.gap).toFixed(3)+' s':fmtD(p.gap)+' to go'));
    tx.append(line);if(g.note)tx.append(h('span','sub2',g.note));
    const acts=h('span','row');if(p.achieved)acts.append(h('span','pill ok','Achieved'));
    const eb=h('button','btn','Edit');eb.type='button';eb.addEventListener('click',()=>editGoal(g));
    const db=h('button','btn','Delete');db.type='button';db.addEventListener('click',()=>deleteGoal(g,db));acts.append(eb,db);
    row.append(tx,acts);host.append(row)});
}
function resetGoalForm(){editing=null;$('goalFormTitle').textContent='Add a goal';$('goalTime').value='';$('goalNote').value='';$('goalCancel').hidden=true;setMsg('goalMsg','')}
function editGoal(g){editing=g;fillGoalCombos({track:g.track,car:g.car});$('goalCombo').value=g.track+'|'+g.car;$('goalTime').value=fmtT(g.target_s);$('goalNote').value=g.note||'';
  $('goalFormTitle').textContent='Edit goal';$('goalCancel').hidden=false;$('goalSave').disabled=false;setMsg('goalMsg','');$('goalTime').focus()}
async function deleteGoal(g,btn){
  if(btn.dataset.armed!=='1'){btn.dataset.armed='1';btn.textContent='Confirm delete';btn.classList.add('danger');
    setTimeout(()=>{btn.dataset.armed='';btn.textContent='Delete';btn.classList.remove('danger')},4000);return}
  let r;try{r=await post('goals',{action:'delete',id:g.id})}catch(e){connectionLost();return}
  if(!r.ok){setMsg('goalListMsg',r.error,'bad');return}
  if(editing&&editing.id===g.id)resetGoalForm();
  setMsg('goalListMsg','Goal deleted.','ok');loadGoals()}
$('goalCancel').addEventListener('click',()=>{resetGoalForm();fillGoalCombos()});
$('fGoal').addEventListener('submit',async e=>{e.preventDefault();
  const k=$('goalCombo').value;if(!k){setMsg('goalMsg','Review a race first, then set a goal for it.','bad');return}
  const t=parseLap($('goalTime').value);if(t==null||t<10){setMsg('goalMsg','Enter the target like 1:23.500.','bad');$('goalTime').focus();return}
  const [track,car]=k.split('|');const goal={track,car,target_s:t,note:$('goalNote').value.trim()};if(editing)goal.id=editing.id;
  const ids=goalCombos.find(c=>c.track===track&&c.car===car)||(editing&&editing.track===track&&editing.car===car?editing:{});  // Garage 61 ids when known
  if(Number.isInteger(ids.track_id)&&Number.isInteger(ids.car_id)){goal.track_id=ids.track_id;goal.car_id=ids.car_id}
  setMsg('goalMsg','Saving...');let r;try{r=await post('goals',{action:'save',goal})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('goalMsg',r.error,'bad');return}
  const was=!!editing;resetGoalForm();fillGoalCombos();setMsg('goalMsg',was?'Goal updated.':'Goal added.','ok');loadGoals()});

/* ---------- Team */
async function openTeam(){current={view:'team',dir:null};markNav();
  if(lastJob&&lastJob.state==='running'&&(lastJob.kind==='team'||lastJob.kind==='weekly')){openJob();return}
  loadWeekly();
  await loadPages();
  if(current.view!=='team')return;  // the person moved on while we looked
  if(pages.team)openPage(pages.team,'teamPage');else{show('vTeam');markNav()}}
$('fbTeam').addEventListener('click',()=>startJob('team'));

/* ---------- this week's team leaderboards (2.5): the estimate is worked out without any request */
let weeklyInfo=null;
async function loadWeekly(){let j;try{j=await api('weekly?estimate=1')}catch(e){return null}
  weeklyInfo=j&&j.ok?j:{ok:false,error:j&&j.error};renderWeekly();return weeklyInfo}
function weeklyRows(j){const p=j&&j.plan;return p&&Array.isArray(p.series)?p.series.filter(x=>x&&typeof x==='object'):[]}
function weeklyText(j){
  if(!j)return 'Checking...';
  if(!j.ok)return j.error||'Couldn\'t work out this week\'s leaderboards right now.';
  if(!j.available)return 'Weekly leaderboards aren\'t available right now. Details were saved to debrief.log.';
  if(!j.schedule)return 'Load the season schedule on the Prepare page and pick your series to see this week\'s team leaderboards.';
  if(j.token===false)return 'Add your Garage 61 token in Settings first.';
  if(!weeklyRows(j).length)return 'Pick your series on the Prepare page to see this week\'s team leaderboards.';
  return 'This week\'s leaderboards for your series.'}
function renderWeekly(){const j=weeklyInfo;teamJoinPanel();const t=weeklyText(j);$('weeklyNote').textContent=t;$('fbWeekly').title=t;
  const rows=weeklyRows(j);const can=!!(j&&j.ok&&j.available&&j.schedule&&j.token!==false&&rows.length);
  $('btnWeekly').disabled=!can;$('fbWeekly').disabled=!can;
  weeklyWhy(can?null:weeklyBlock(j));
  const pill=$('weeklyPill');pill.hidden=!rows.length;pill.textContent=rows.length+' series';pill.className='pill'+(can?' ok':'');
  const p=(j&&j.plan)||{};const sk=(Array.isArray(p.skipped)?p.skipped:[]).concat(Array.isArray(p.notes)?p.notes:[]);
  showWarnings('weeklySkipped',[...new Set(sk.map(x=>typeof x==='string'?x:(x&&typeof x==='object'?[x.name,x.note||x.reason].filter(v=>typeof v==='string'&&v).join(': '):'')))],'Not included:')}
// 2.18: why Refresh this week can't be pressed, said beside it (not only in its tooltip), with a link to the fix
function weeklyBlock(j){
  if(!j)return null;
  if(!j.ok)return {text:j.error||'Couldn\'t work out this week\'s leaderboards right now.'};
  if(!j.available)return {text:'Weekly leaderboards aren\'t available right now. Details were saved to debrief.log.'};
  if(!j.schedule||!weeklyRows(j).length)return {text:'Pick your series on Prepare first.',go:'Open Prepare',to:'prep'};
  if(j.token===false)return {text:'Add your Garage 61 token in Settings first.',go:'Open Settings',to:'settings'};
  return null}
function weeklyWhy(b){weeklyBlk=b;[['weeklyWhy','weeklyWhyText','weeklyWhyGo'],['fbWeeklyWhy','fbWeeklyWhyText','fbWeeklyWhyGo']].forEach(([w,t,g])=>{
  $(w).hidden=!b||(w==='fbWeeklyWhy'&&current.view!=='teamPage');if(!b)return;$(t).textContent=b.text;const go=$(g);go.hidden=!b.go;go.textContent=b.go||'';go.dataset.to=b.to||''})}
['weeklyWhyGo','fbWeeklyWhyGo'].forEach(id=>$(id).addEventListener('click',e=>{const to=e.currentTarget.dataset.to;if(to==='prep')openPrep();else if(to==='settings')openSettings()}));
function startWeekly(){if(lastJob&&lastJob.state==='running'&&lastJob.kind==='weekly'){openJob();return}startJob('weekly',{})}
$('btnWeekly').addEventListener('click',startWeekly);$('fbWeekly').addEventListener('click',startWeekly);

/* ---------- Post to Discord (2.5). The review page draws its share card when asked (postMessage); the
   window stages it on the server (no network), shows it, and posts only when you press Post in the dialog. */
let discordSaved=false,discordHint=null,share=null,staged=null;
function shareId(){const a=new Uint8Array(12);crypto.getRandomValues(a);return Array.from(a,b=>b.toString(16).padStart(2,'0')).join('')}
function discordHow(){const d=$('dlgDiscordHow');if(d.showModal)d.showModal();else d.setAttribute('open','')}
function closeDiscordHow(){const d=$('dlgDiscordHow');if(d.open){if(d.close)d.close();else d.removeAttribute('open')}}
$('discordHowClose').addEventListener('click',closeDiscordHow);$('discordHowCancel').addEventListener('click',closeDiscordHow);
$('discordHowGo').addEventListener('click',()=>{closeDiscordHow();openSettings();const f=$('fDiscord');
  for(let e=f&&f.parentNode;e;e=e.parentNode)if(e.tagName==='DETAILS')e.open=true;
  if(f){f.scrollIntoView({block:'start'});$('discordHook').focus()}});
function requestShare(){if(current.view==='review'&&!discordSaved){discordHow();return}
  if((current.view!=='review'&&current.view!=='season')||!discordSaved||share)return;
  const w=$('frame').contentWindow;if(!w)return;
  const id=shareId();share={id,dir:current.dir,view:current.view};$('fbDiscord').disabled=true;
  share.t=setTimeout(()=>{if(share&&share.id===id){share=null;$('fbDiscord').disabled=false;
    banner('This page didn\'t send its picture. Reload it and try again.',true)}},10000);
  // '*': the review page is sandboxed (origin "null"); the request carries nothing but a random id
  try{w.postMessage({type:'debrief:share-request',id},'*')}catch(e){clearTimeout(share.t);share=null;$('fbDiscord').disabled=false}}
$('fbDiscord').addEventListener('click',requestShare);
// the reply counts only from our own frame (e.source, never the origin string) and for the id we sent
addEventListener('message',e=>{const f=$('frame');if(!f||e.source!==f.contentWindow)return;const m=e.data;
  if(!m||typeof m!=='object'||m.type!=='debrief:share-card'||!share||m.id!==share.id)return;
  const s=share;share=null;clearTimeout(s.t);$('fbDiscord').disabled=false;
  if(current.view!==s.view||current.dir!==s.dir)return;
  if(m.error!=null&&m.png==null){banner(s.view==='season'&&typeof m.error==='string'?m.error.slice(0,200):'Couldn\'t make the share card.',true);return}   // the page couldn't draw it: stage nothing
  if(typeof m.png!=='string'||!m.png.startsWith('data:image/png;base64,')||m.png.length>6e6){banner('The share card couldn\'t be made. Reload the review and try again.',true);return}
  stageCard(m.png,typeof m.text==='string'?m.text:'')});
// 2.18 Share card: the review page hands its picture to the window, which saves it in the Downloads folder and
// says where. Only from our own frame, one at a time, a PNG only (the server checks it again).
let shareSaving=false;
addEventListener('message',async e=>{const f=$('frame');if(!f||e.source!==f.contentWindow)return;const m=e.data;
  if(!m||typeof m!=='object'||m.type!=='debrief:share-save'||shareSaving)return;
  if(typeof m.png!=='string'||!m.png.startsWith('data:image/png;base64,')||m.png.length>6e6){banner('The share card couldn\'t be made. Reload the review and try again.',true);return}
  let blob;try{blob=pngBlob(m.png)}catch(err){banner('The share card couldn\'t be read. Reload the review and try again.',true);return}
  shareSaving=true;let r;
  try{r=await fetch(BASE+'api/share-save?'+new URLSearchParams({name:typeof m.name==='string'?m.name.slice(0,200):''}),{method:'POST',headers:{'X-Debrief':'1','Content-Type':'image/png'},body:blob}).then(x=>x.json())}
  catch(err){shareSaving=false;banner('Couldn\'t save the share card. Try again.',true);return}
  shareSaving=false;
  if(!r||!r.ok){banner((r&&r.error)||'Couldn\'t save the share card.',true);return}
  shareSaved(r)});
function shareSaved(r){banner('Share card saved: '+r.path,false,15000);const b=$('banner');
  const o=h('button','btn ghost bannerbtn','Open the folder');o.type='button';o.id='shareOpen';
  o.addEventListener('click',async()=>{let x;try{x=await post('share-open')}catch(err){connectionLost();return}if(!x.ok&&x.error)banner(x.error,false,15000)});
  const old=$('shareOpen');if(old)old.remove();$('bannerText').after(o)}
function pngBlob(url){const b=atob(url.slice(url.indexOf(',')+1));const a=new Uint8Array(b.length);for(let i=0;i<b.length;i++)a[i]=b.charCodeAt(i);return new Blob([a],{type:'image/png'})}
async function stageCard(url,text){let blob;try{blob=pngBlob(url)}catch(e){banner('The share card couldn\'t be read. Reload the review and try again.',true);return}
  let r;try{r=await fetch(BASE+'api/discord-stage',{method:'POST',headers:{'X-Debrief':'1','Content-Type':'image/png'},body:blob}).then(x=>x.json())}
  catch(e){banner('Couldn\'t prepare the preview. Try again.',true);return}
  if(!r||!r.ok){banner((r&&r.error)||'Couldn\'t prepare the preview. Try again.',true);return}
  openDiscord(r,url,text)}
function discordCount(){const n=$('discordText').value.length;$('discordCount').textContent=n+' / 2000';$('discordCount').classList.toggle('over',n>2000)}
const POST_ARM_MS=600;
function openDiscord(r,url,text){staged={nonce:r.nonce};
  $('discordText').value=String(text||'').slice(0,2000);discordCount();
  const t=r.target||{};
  $('discordTarget').textContent='Posts to '+(t.name?'the webhook \u201c'+t.name+'\u201d':'your team\'s webhook')+' ('+(t.hint||discordHint||'…')+')';
  setMsg('discordDlgMsg','');
  // 2.5 fixes L7: Post stays off until the preview picture has loaded and for POST_ARM_MS after the dialog
  // opens, so a click (or double-click) meant for something else can't land on it as the dialog appears
  const post=$('discordPost'),arm={img:false,time:false,nonce:r.nonce};post.disabled=true;
  const ready=()=>{if(staged&&staged.nonce===arm.nonce&&arm.img&&arm.time&&$('dlgDiscord').open)post.disabled=false};
  const img=$('discordImg');
  const loaded=()=>{arm.img=true;ready()};
  img.onload=loaded;img.onerror=()=>{if(staged&&staged.nonce===arm.nonce)setMsg('discordDlgMsg','The preview couldn\'t be shown, so it can\'t be posted. Close this and press Post to Discord again.','bad')};img.src=url;
  if(img.complete&&img.naturalWidth>0)loaded();
  setTimeout(()=>{arm.time=true;ready()},POST_ARM_MS);
  const d=$('dlgDiscord');if(d.showModal)d.showModal();else d.setAttribute('open','');$('discordText').focus()}
$('discordText').addEventListener('input',discordCount);
// Cancel, Close or Escape: nothing is sent; the staged card is forgotten on the server within 10 minutes
function closeDiscord(){staged=null;const d=$('dlgDiscord');if(d.open){if(d.close)d.close();else d.removeAttribute('open')}}
$('discordCancel').addEventListener('click',closeDiscord);$('discordClose').addEventListener('click',closeDiscord);
$('dlgDiscord').addEventListener('close',()=>{staged=null});
// THE ONLY PLACE THAT SENDS api/discord-post: a real click on Post in the open dialog, once per preview
$('discordPost').addEventListener('click',async e=>{if(!e.isTrusted||!staged||!$('dlgDiscord').open||$('discordPost').disabled)return;
  const content=$('discordText').value;
  if(content.length>2000){setMsg('discordDlgMsg','The message can be up to 2000 characters.','bad');return}
  const nonce=staged.nonce;staged=null;$('discordPost').disabled=true;setMsg('discordDlgMsg','Posting...');
  let r;try{r=await post('discord-post',{nonce,content})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('discordDlgMsg',(r.error||'Couldn\'t post to Discord.')+(/preview again/i.test(r.error||'')?'':' Close this and press Post to Discord again to retry.'),'bad');return}
  closeDiscord();banner('Posted to Discord.')});

/* ---------- race result (on an open review) */
const RES_FIELDS=['finish_pos','start_pos','field_size','incidents','laps_led','sof','irating_before','irating_after','sr_before','sr_after','notes',
  'pos_after_lap1','pit_stops','penalties','finish_status'];
const RES_TEXT=['notes','finish_status'];
const INC_KINDS=[['off','Off track'],['spin','Spin'],['contact','Contact'],['other','Other']];
function incRow(e){e=e||{};const r=h('div','incrow');
  const lap=document.createElement('input');lap.type='number';lap.min='1';lap.max='2000';lap.step='1';lap.placeholder='Lap';lap.className='inc-lap';lap.setAttribute('aria-label','Lap');if(ok(e.lap))lap.value=e.lap;
  const kind=document.createElement('select');kind.className='inc-kind';kind.setAttribute('aria-label','What happened');INC_KINDS.forEach(([v,l])=>{const o=h('option',null,l);o.value=v;kind.append(o)});if(e.kind)kind.value=e.kind;
  const pts=document.createElement('select');pts.className='inc-pts';pts.setAttribute('aria-label','Incident points');[0,1,2,4].forEach(v=>{const o=h('option',null,v+'x');o.value=String(v);pts.append(o)});pts.value=String(ok(e.points)?e.points:0);
  const del=h('button','btn ghost','Remove');del.type='button';del.addEventListener('click',()=>{r.remove();incState()});
  r.append(lap,kind,pts,del);return r}
function incState(){const n=$('incRows').children.length;$('incAdd').disabled=n>=30;$('incNote').textContent=n>=30?'Up to 30 incidents.':'Optional. The review\'s race craft timeline marks these laps.'}
$('incAdd').addEventListener('click',()=>{if($('incRows').children.length>=30)return;const r=incRow();$('incRows').append(r);incState();r.querySelector('input').focus()});
function incValues(){const out=[];for(const r of $('incRows').children){const lap=r.querySelector('.inc-lap').value.trim();if(lap==='')continue;
    const n=Number(lap);if(!Number.isInteger(n)||n<1||n>2000)throw new Error('Incidents by lap: the lap must be a whole number from 1 to 2000.');
    out.push({lap:n,kind:r.querySelector('.inc-kind').value,points:Number(r.querySelector('.inc-pts').value)})}return out}
async function loadResult(dir){resultFor={dir,result:null};frameBar();
  let j;try{j=await api('result?review='+encodeURIComponent(dir))}catch(e){return}
  if(resultFor.dir!==dir)return;
  resultFor={dir,result:j.ok?j.result||null:null,scan:j.ok?j.scan||null:null};frameBar()}
function openResult(){const dir=current.dir;if(!dir)return;const r=(resultFor.dir===dir&&resultFor.result)||{};
  RES_FIELDS.forEach(k=>{$('r_'+k).value=r[k]!=null?String(r[k]):''});
  const sc0=resultFor.dir===dir&&!resultFor.result?resultFor.scan:null;
  if(sc0){$('r_finish_pos').value=String(ok(sc0.class_finish)?sc0.class_finish:sc0.finish);const size=ok(sc0.class_size)?sc0.class_size:sc0.cars;if(ok(size))$('r_field_size').value=String(size)}
  $('incRows').replaceChildren(...(Array.isArray(r.incident_laps)?r.incident_laps.slice(0,30):[]).map(incRow));incState();
  resCsvMe=null;$('resTitle').textContent=resultFor.result?'Edit race result':'Add race result';$('resDelete').hidden=!resultFor.result;
  $('resDelete').dataset.armed='';$('resDelete').textContent='Delete result';setMsg('resMsg','');setMsg('resCsvMsg',resultFor.dir===dir&&!resultFor.result&&resultFor.scan?'Finish filled in from your replay scan, on the road. The results file replaces it with the official one.':'');$('resCsvWho').hidden=true;
  const d=$('dlgResult');if(d.showModal)d.showModal();else d.setAttribute('open','');$('r_finish_pos').focus()}
/* ---------- 2.7.5: fill the result form from iRacing's results file; nothing is saved until Save */
let resCsvMe=null;   /* 2.18: a results file loaded in this form: who he is in it ('' until he picks himself) */
function fillFromCsv(d){resCsvMe=d.name;RES_FIELDS.filter(k=>k!=='notes').forEach(k=>{if(d.fields[k]!=null)$('r_'+k).value=String(d.fields[k])});
  setMsg('resCsvMsg','Filled in for '+d.name+(d.class?' ('+d.class+')':'')+'. Check it, then press Save result.','ok')}
$('resCsv').addEventListener('change',async()=>{const f=$('resCsv').files[0];$('resCsv').value='';const dir=current.dir;if(!f||!dir)return;
  if(f.size>2*1024*1024){setMsg('resCsvMsg','That file is too big for a results CSV.','bad');return}
  setMsg('resCsvMsg','Reading '+f.name+'...');let r;
  try{r=await fetch(BASE+'api/result-csv?review='+encodeURIComponent(dir),{method:'POST',headers:{'X-Debrief':'1','Content-Type':'application/octet-stream'},body:f}).then(x=>x.json())}
  catch(e){setMsg('resCsvMsg','Couldn\'t send the file.','bad');return}
  if(!r||!r.ok){setMsg('resCsvMsg',(r&&r.error)||'Couldn\'t read that file.','bad');return}
  const who=$('resCsvWho');if(r.matched){who.hidden=true;fillFromCsv(r.matched);return}
  resCsvMe='';
  const first=h('option',null,'Which one is you?');first.value='';who.replaceChildren(first,...r.drivers.map((d,i)=>{const o=h('option',null,d.name+(d.car?' #'+d.car:''));o.value=String(i);return o}));
  who._drivers=r.drivers;who.hidden=false;setMsg('resCsvMsg','Your name wasn\'t found exactly. Pick yourself.')});
$('resCsvWho').addEventListener('change',()=>{const w=$('resCsvWho');if(w.value!=='')fillFromCsv(w._drivers[+w.value])});
/* ---------- 2.7.5: RaceCap link, Resolve markers, help */
$('fRaceCap').addEventListener('submit',async e=>{e.preventDefault();setMsg('rcapMsg','Saving...');let r;
  try{r=await post('settings',{racecap_library:$('rcapFolder').value.trim()})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('rcapMsg',r.error,'bad');return}fillSettings(r.settings);frameBar();setMsg('rcapMsg','Saved.','ok')});
$('fTelemetry').addEventListener('submit',async e=>{e.preventDefault();setMsg('telMsg','Saving...');let r;
  try{r=await post('settings',{iracing_folder:$('telFolder').value.trim()})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('telMsg',r.error,'bad');return}fillSettings(r.settings);setMsg('telMsg','Saved.','ok')});
function openDlg(d){if(d.showModal)d.showModal();else d.setAttribute('open','')}
function closeDlg(d){if(d.close)d.close();else d.removeAttribute('open')}
$('fbScan').addEventListener('click',()=>{setMsg('scanMsg','');openDlg($('dlgScan'))});
$('scanClose').addEventListener('click',()=>closeDlg($('dlgScan')));
$('fScan').addEventListener('submit',async e=>{e.preventDefault();const dir=current.dir;if(!dir)return;closeDlg($('dlgScan'));startJob('scan',{review:dir})});
$('fbRival').addEventListener('click',async()=>{const dir=current.dir;if(!dir)return;const sel=$('rivalCar');sel.replaceChildren();$('rivalGo').disabled=true;setMsg('rivalMsg','Looking in the saved scan...');openDlg($('dlgRival'));
  let r;try{r=await post('rival-choices',{review:dir})}catch(e){connectionLost();return}
  if(!r.ok){setMsg('rivalMsg',r.error||'Could not read the saved scan.','bad');return}
  const cars=r.cars||[];cars.forEach(c=>{const m=Math.floor(c.lap_time/60),sec=(c.lap_time-m*60).toFixed(3).padStart(6,'0');const why=c.seconds>=60?', fought '+Math.round(c.seconds/60)+' min':c.seconds>0?', fought '+c.seconds+' s':'';
    const o=h('option',null,'#'+c.car+' '+c.name+why+', lap '+c.lap+' in '+m+':'+sec+(c.read?' (already read)':''));o.value=c.car;sel.append(o)});
  $('rivalGo').disabled=!cars.length;setMsg('rivalMsg',cars.length?'':'No nearby driver has a clean lap to read in this race.')});
$('rivalClose').addEventListener('click',()=>closeDlg($('dlgRival')));
$('fRival').addEventListener('submit',e=>{e.preventDefault();const dir=current.dir,car=$('rivalCar').value;if(!dir||!car)return;closeDlg($('dlgRival'));startJob('rival',{review:dir,car})});
$('jobStop').addEventListener('click',async()=>{$('jobStop').disabled=true;$('jobNow').textContent='Stopping and putting the replay back...';try{await post('replay-scan-stop')}catch(e){}});
$('fbRaceCap').addEventListener('click',()=>{$('linkSub').value='';setMsg('linkMsg','');openDlg($('dlgLink'));$('linkSub').focus()});
$('linkClose').addEventListener('click',()=>closeDlg($('dlgLink')));
$('fLink').addEventListener('submit',async e=>{e.preventDefault();const dir=current.dir;if(!dir)return;setMsg('linkMsg','Looking...');$('linkGo').disabled=true;let r;
  try{r=await post('racecap-link',{review:dir,subsession:$('linkSub').value.trim()})}catch(err){connectionLost();return}finally{$('linkGo').disabled=false}
  if(!r.ok){setMsg('linkMsg',r.error,'bad');return}closeDlg($('dlgLink'));reloadFrame();banner('Linked to RaceCap race '+r.subsession+' as '+r.name+'. If that isn\'t you, the race ID is wrong.',false,9000)});
$('fbMarkers').addEventListener('click',async()=>{const dir=current.dir;if(!dir)return;let j;try{j=await api('markers?review='+encodeURIComponent(dir))}catch(e){banner('Couldn\'t make the marker file.',true);return}
  if(!j.ok){banner(j.error||'Couldn\'t make the marker file.',true);return}
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([j.edl],{type:'text/plain'}));a.download=j.name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),5000);
  banner(j.exact?'Markers saved to Downloads, for a 60 fps timeline. In Resolve, put the green flag at the timeline\'s start.':'Markers saved to Downloads, for a 60 fps timeline. They come from lap times, so they\'re close, not exact. Link RaceCap for exact moments.',false,9000)});
const HELP={tour:[['Review your race','Press Review last race. Debrief pulls your newest race from Garage 61 and writes the review.'],
  ['Start at the top','What to work on comes first: the corners worth the most time. Every number behind it is further down.'],
  ['Compare two laps','Pick any two laps, or a comparison lap, and see where the time went on the track map and traces.'],
  ['Add the result','On a review, Add race result takes the iRacing results file, so there is little or nothing to type.'],
  ['Prepare and look back','Prepare gets you ready for the next track. Progress, Season and Skills show how you are trending.'],
  ['Setup Lab','Reads the telemetry files iRacing saves on this PC: what changed between your setups, which one was faster (only when the laps clearly say so) and one change to try next.'],
  ['Coach','Press Start coaching, then drive. Short spoken cues from your own best lap: after corners where you lost or gained time, sector times, your lap time, and brake markers before your focus corners. Pick Quiet, Normal or Busy for how much it says. Keep Debrief open while you drive.'],
  ['Practice','Every time you drove, from your telemetry files: your best and typical lap, the best possible lap, your laps, fuel and setups, and the Corner coach: one thing to try in each corner. Coach these corners hands those corners to the live coach.'],
  ['One thing this session','The top of Practice picks one thing to work on next time at that track, with a number you can check. Coach this has the live coach watch it. After you drive, open the session: Debrief counts the laps where you did it and says whether to keep it or move on.'],
  ['Getting set up','The setup checklist on the start page shows what is done and the next step. Hide it when you like; Help > Setup checklist brings it back.']],
 words:[['Clean lap','A full lap Garage 61 didn\'t flag: no incident and not a pit lap.'],['Typical lap','The middle of your clean laps. Half were quicker, half slower.'],
  ['Comparison lap','A lap by a teammate, a followed driver or a rival, that yours are measured against.'],['Best sectors added up','Your best time through each part of the track, added together. A guide, not a lap you must be able to drive.'],
  ['Brake point','Where on the track you first brake.'],['Apex','The slowest point of a corner, near the inside.'],['Coasting','Time with both pedals under 5%: after the brake is fully off and before you pick up the throttle, or a lift with no brake. It can sit beside trail braking, which happens earlier, while you turn in.'],
  ['Trail braking','Keeping a little brake on as you turn in, then easing off towards the apex. It keeps weight on the front tyres so the car turns.'],
  ['Median','The middle value: half your laps were quicker, half slower. Debrief calls the median lap your typical lap.'],
  ['Pace drift','How much your clean laps slowed (+) or sped up (\u2212) from the start of the race to the end, from a line drawn through them.'],
  ['Consistency score','Out of 100: how close together your clean lap times were. 100 means every lap the same; under 45 means they were spread out. 60% comes from the spread, 40% from how many laps were close to your best.'],['Stint','A run of laps between pit stops.'],
  ['Possibly traffic','A lap lost time in one short stretch your other laps didn\'t. It could be another car or a mistake.'],['Race log','Passes, fights, incidents and pit stops from a replay scan. Open one of your race reviews and press Scan this race.'],
  ['On the road','The order the cars crossed the line in the replay. The iRacing results are the official order.'],['Rolling or standing start','Whether the field was moving at the green flag.'],
  ['Resolve markers','For making a video of your race: a file the DaVinci Resolve video editor imports to mark the race\'s moments on its timeline.'],
  ['Setup','The car settings you drive with (pressures, camber, springs, wing...). Setup Lab reads them from your telemetry files, because iRacing\'s .sto setup files are locked.'],
  ['Open or fixed setup','In an open setup series you can change the setup; in a fixed one it is locked, so Setup Lab has nothing to suggest.'],
  ['Laps counted','The clean laps Setup Lab compares setups on: not out laps, first flying laps, laps in traffic or laps far off the pace.'],
  ['Reference lap','Your best clean lap for this car and track, from your telemetry files. With none there, the coach says "Building your reference", and your first clean lap becomes it ("Reference set, one thirty-eight nine"). The coach measures you against it.'],
  ['Focus corner','One of the corners your race reviews or the Corner coach say keep costing you time. With none of those, the coach learns them from this session: the corners where your last clean laps lost the most against your reference. The coach talks about one to three at a time.'],
  ['Coaching level','How much the coach says. Quiet: your focus corners and your lap time only, at most 2 tips a lap. Normal (the default): up to 4 cues a lap, 3 seconds apart. Busy: up to 6, 2.5 seconds apart. Only when I ask: nothing until you ask for your pace. Every level stays quiet under braking, in corners, with a car alongside and on lap 1 of a race.'],
  ['How\'s my pace?','Press Ctrl+Shift+P, the button on the Coach page or a wheel button you chose: the coach says your last clean lap against your reference and the corner that cost the most there ("Down three tenths, most in T4"). It answers as soon as it is safe to talk, never under braking or in a corner.'],
  ['Hard zones','From your reference lap, the stretch from hard braking until you are back on full throttle. Sector and lap times and other cues that are not about a corner wait until you are out of one, and only start when they finish before the next one.'],
  ['Checked before it plays','Every cue is checked again against the car just before it plays. A brake marker that could no longer finish a second before the brake point, a word about a corner once you are braking for the next one, or a sector time once a newer one is done is dropped, never said late. The coach log says "no longer valid" for those.'],
  ['Say lap times','How the coach reads a lap time. Short, the default, as race engineers do: "thirty-eight nine". Full: "one thirty-eight nine".'],
  ['Sector time','The lap split in three by your reference lap\'s time. On Normal and Busy the coach says each of the first two sectors against your reference ("Sector one, up two tenths"); the lap time covers the rest.'],
  ['Brake marker','The cue the coach says on the straight before a focus corner, so you think about that braking point.'],
  ['Session (Practice)','The telemetry files of one go in the car: the same car and track, with no more than 3 hours between files.'],
  ['Best possible lap','Your best time through each part of the lap, added up. Counted from clean laps once there are 5 of them. A guide, not a lap you can count on.'],
  ['Corner coach','Your best laps through a corner against your typical ones: what you do differently on the good ones, how to try it and what it changes. A tip is only given when it passes a fair test: Debrief shuffles which laps count as your best 400 times, and if shuffled laps show a pattern as clear in 1 shuffle in 20 or more, it says "no clear pattern yet" instead. Race laps and practice or qualifying laps are compared separately, and so are setups.'],
  ['Corner time','"Your best laps were about 0.12 s quicker here": each corner\'s share of the gap between your quickest third of laps and the rest, so the corners never add up to more than that gap. It describes your own laps; it is not a promise of lap time, because part of any best lap is luck.'],
  ['One thing this session','One change for your next session at a track, picked from the Corner coach: the corner, what to do, and the check (for example, peak brake at or below 88%). It is never surer than the Corner coach tip it comes from.'],
  ['A guess (an experiment)','"Try this and see": a plan for a corner where your best laps are quicker but the Corner coach found no clear reason. Drive it the same way every lap and see whether it helps. Not a known fix.'],
  ['Hit it','A lap where your number for the plan\'s corner met the check. 7 laps out of 10 means it is a habit, and the next session moves on.'],
  ['Quicker, No clear difference','Whether your laps that hit the plan were quicker through that corner than the ones that missed from the same part of the session (runs of 5 or 6 laps in a row, so a session that simply gets quicker doesn\'t count). Debrief shuffles which laps count as hits 400 times; it says Quicker only when fewer than 1 shuffle in 20 shows a gap as big as the real one (and at least 0.03 s), with at least 3 laps of each. It describes those laps; it is not a promise of lap time.'],
  ['Wheel button mute','Coach page > Settings > Mute from a wheel button: press Learn, then the button on your wheel. That button then mutes and unmutes the coach while it is on.'],
  ['Sure, Likely, A hint, A guess','How sure something is, the same four words on every page. Sure: a direct reading, or a Corner coach tip that passed the shuffle test in fewer than 1 in 100 shuffles from 12 or more laps (and still passes when every corner is counted at once). Likely: passed in fewer than 1 in 20. A hint: only a few laps show it, or it could still be chance. A guess: no clear pattern, so try it and see.']],
 keys:[['Ctrl+Shift+M','Mute or unmute the coach']]};   // 2.18: the page keys come first, from keyList()
function showHelp(k){const b=$('helpBody');const L=k==='keys'?keyList().concat(HELP.keys):HELP[k];b.classList.toggle('keygrid',k==='keys');
  b.replaceChildren(...(k==='tour'?[h('p','note','A quick look at how Debrief works.')]:k==='keys'?[h('p','note','Ctrl+1 to Ctrl+9 and Ctrl+0 open the pages in the sidebar\'s order.')]:[]),...L.map(([t,d])=>{const p=h('p');p.append(h('b',null,t+': '),document.createTextNode(d));return p}));
  document.querySelectorAll('[data-help]').forEach(x=>x.setAttribute('aria-pressed',x.dataset.help===k?'true':'false'))}
function openHelp(k){showHelp(k);openDlg($('dlgHelp'))}
$('btnHelp').addEventListener('click',()=>openHelp('tour'));$('btnCredits').addEventListener('click',()=>openHelp('credits'));$('helpClose').addEventListener('click',()=>closeDlg($('dlgHelp')));
document.querySelectorAll('[data-help]').forEach(x=>x.addEventListener('click',()=>showHelp(x.dataset.help)));
function closeResult(){const d=$('dlgResult');if(d.close)d.close();else d.removeAttribute('open')}
$('fbResult').addEventListener('click',openResult);
$('resClose').addEventListener('click',closeResult);
function reloadFrame(){const f=$('frame');const at=frameHref||f.src;if(at&&at.startsWith(BASE)){loading(true);f.src=fresh(noQuery(at))}}
$('fResult').addEventListener('submit',async e=>{e.preventDefault();const dir=current.dir;if(!dir)return;
  const fields={};RES_FIELDS.forEach(k=>{const v=$('r_'+k).value.trim();if(v!=='')fields[k]=RES_TEXT.includes(k)?v:Number(v)});
  try{const inc=incValues();if(inc.length)fields.incident_laps=inc}catch(err){setMsg('resMsg',err.message,'bad');return}
  if(!('finish_pos' in fields)){setMsg('resMsg','Add your finishing position.','bad');$('r_finish_pos').focus();return}
  const bad=RES_FIELDS.find(k=>!RES_TEXT.includes(k)&&!$('r_'+k).checkValidity());
  if(bad){setMsg('resMsg',$('r_'+bad).closest('label').querySelector('span').textContent+': '+$('r_'+bad).validationMessage,'bad');$('r_'+bad).focus();return}
  if(ok(fields.field_size)&&(fields.finish_pos>fields.field_size||(ok(fields.start_pos)&&fields.start_pos>fields.field_size)||(ok(fields.pos_after_lap1)&&fields.pos_after_lap1>fields.field_size))){
    setMsg('resMsg','A position can\'t be more than the number of cars.','bad');return}
  setMsg('resMsg','Saving...');$('resSave').disabled=true;let r;
  const body={review:dir,action:'save',fields};if(resCsvMe!==null)body.field_me=resCsvMe;
  try{r=await post('result',body)}catch(err){connectionLost();return}finally{$('resSave').disabled=false}
  if(!r.ok){setMsg('resMsg',r.error,'bad');return}
  resultFor={dir,result:r.result||fields};resCsvMe=null;closeResult();frameBar();reloadFrame();
  banner('Race result saved.'+(r.field&&r.field.note?' '+r.field.note:''),!!(r.field&&!r.field.kept),r.field?9000:undefined)});
$('resDelete').addEventListener('click',async()=>{const b=$('resDelete');const dir=current.dir;
  if(b.dataset.armed!=='1'){b.dataset.armed='1';b.textContent='Confirm delete';return}
  let r;try{r=await post('result',{review:dir,action:'delete'})}catch(e){connectionLost();return}
  if(!r.ok){setMsg('resMsg',r.error,'bad');return}
  resultFor={dir,result:null};closeResult();frameBar();reloadFrame();banner('Race result deleted.')});

/* ---------- Prepare: small links to Setup Lab (only when its index has this car at this track) and the Coach */
function toolLinks(d){const p=h('p','pvlinks');const co=h('button','linkbtn','Coach');co.type='button';co.id='pvCoach';
  co.title='Spoken cues while you drive';co.addEventListener('click',()=>openCoach());
  if(!d.track||!d.car){p.append(co);return p}
  const sb=h('button','linkbtn','Setup Lab for this car');sb.type='button';sb.id='pvSetupLab';sb.hidden=true;p.append(sb,co);
  const seq=focusSeq,q=new URLSearchParams({car:d.car,track:d.track});
  api('setuplab/combos?'+q).then(j=>{if(seq!==focusSeq||!j||!j.ok||!j.match)return;const m=j.match;sb.hidden=false;
    sb.title=m.files+' telemetry file'+(m.files===1?'':'s')+' for the '+m.car+' at '+m.track;
    sb.addEventListener('click',()=>openSetupLab({car_path:m.car_path,track_code:m.track_code,series:pk.series}))}).catch(()=>{});
  return p}

/* ---------- Setup Lab (2.12): his setups, read from the telemetry files iRacing saves (read only) */
let sl={data:null,sel:null,series:null,seq:0};
// 2.18: one sureness scale (SURE): a reading is Sure, low to medium is A hint, a guess is A guess
const SL_CONF={reading:['Sure','ok','From a reading in your telemetry'],guess:['A guess','no','A guess: not read from your laps'],'low-medium':['A hint','no','Low to medium: a hint from a few readings']};
const slWhen=s=>{const m=/^(\d{4})-(\d\d)-(\d\d)(?:[ T](\d\d):(\d\d))?/.exec(s||'');if(!m)return s||'';return MON[+m[2]-1]+' '+(+m[3])+(m[4]?', '+m[4]+':'+m[5]:'')};
const slSame=(c,s)=>!!c&&!!s&&String(c.car_path||'').toLowerCase()===String(s.car).toLowerCase()&&String(c.track_code||'').toLowerCase()===String(s.track).toLowerCase();
const plural=(n,w)=>n+' '+w+(n===1?'':'s');
const carTracks=n=>n===1?'1 car and track':n+' cars and tracks';
async function openSetupLab(pick){current={view:'setuplab',dir:null};show('vSetupLab');markNav();
  if(pick&&pick.car_path){sl.sel={car:pick.car_path,track:pick.track_code};sl.series=pick.series||null}
  await loadSetupLab(true)}
async function loadSetupLab(withReport){let j;try{j=await api('setuplab/combos')}catch(e){connectionLost();return}
  if(!j.ok){setMsg('slMsg',j.error||'Setup Lab didn\'t load.','bad');return}
  sl.data=j;const combos=j.combos||[];
  if(!sl.sel&&combos.length)sl.sel={car:combos[0].car_path,track:combos[0].track_code};
  renderSlTel();renderSlPick();
  if(j.running&&!polling)poll();
  if(!withReport)return;
  if(!j.folder_ok)slEmpty('Debrief can\'t find your telemetry folder','iRacing saves telemetry in Documents\\iRacing\\telemetry. If yours is somewhere else, set the folder in Settings > Setup Lab and Coach. '+TEL_HOW);
  else if(!j.indexed&&j.unread===0&&!j.running)slEmpty('No telemetry files yet',TEL_HOW+' Then press Read my telemetry.');
  else if(!j.indexed)slEmpty(j.running?'Reading your telemetry...':'Not read yet','Press Read my telemetry. The first time reads every file, so a few hundred files take a few minutes; after that only new ones.');
  else if(!combos.length)slEmpty('No telemetry files yet',TEL_HOW+' Then press Read my telemetry.');
  else loadSlReport()}
// 2.18: iRacing records no telemetry until it is turned on; every telemetry page's empty state says so the same way
const TEL_HOW='iRacing doesn\'t save telemetry until you turn it on: in the car, press Alt+L (or turn on telemetry recording in iRacing\'s settings), then drive a few laps.';
function slEmpty(title,text){const e=h('div','panel pvnone');e.append(h('h2','pvtrack',title),h('p','lede',text));$('slReport').replaceChildren(e)}
function renderSlTel(){const j=sl.data;if(!j)return;const pill=$('slPill'),run=!!j.running||(lastJob&&lastJob.state==='running'&&lastJob.kind==='setuplab');
  $('slPath').textContent=j.folder||'';$('slFolderSet').hidden=!!j.folder_ok;$('slRead').disabled=!j.folder_ok||run;$('slStop').hidden=!run;
  const n=j.combos?j.combos.length:0;
  if(run){pill.className='pill';pill.textContent='Reading'}
  else if(!j.folder_ok){pill.className='pill no';pill.textContent='Folder not found'}
  else if(!j.indexed){pill.className='pill no';pill.textContent='Not read yet'}
  else{pill.className='pill ok';pill.textContent=plural(j.files,'file')}
  $('slTelNote').textContent=!j.folder_ok?'Debrief can\'t find this folder. If iRacing saves telemetry somewhere else, set the folder in Settings.':
    !j.indexed?(ok(j.unread)?plural(j.unread,'telemetry file')+' to read.':'Not read yet.'):
    plural(j.files,'telemetry file')+' for '+carTracks(n)+'.'+(ok(j.unread)&&j.unread>0?' '+plural(j.unread,'new file')+' since the last read.':'')+slBadText(j);
  $('slProg').hidden=!run;$('slNow').hidden=!run}
/* 2.18: an unreadable file is named, with the reason, so he can find it (Debrief never deletes or changes it) */
function slBadText(j){const B=j.bad_files||[];if(!j.bad)return '';
  if(!B.length)return ' '+plural(j.bad,'file')+' couldn\'t be read.';
  const one=b=>b.file+' ('+b.why+')';
  return ' '+plural(j.bad,'file')+' couldn\'t be read: '+B.slice(0,3).map(one).join('; ')+(j.bad>3?'; and '+(j.bad-3)+' more':'')+'. Debrief skips '+(j.bad===1?'it':'them')+' and leaves '+(j.bad===1?'it':'them')+' as '+(j.bad===1?'it is':'they are')+'.'}
function renderSlPick(){const j=sl.data,combos=(j&&j.combos)||[];$('slPick').hidden=!combos.length;if(!combos.length)return;
  const top=combos.slice(0,6),rest=combos.slice(6),label=c=>(c.track||c.track_code)+' · '+(c.car||c.car_path);
  keepFocus($('slCombos'),()=>$('slCombos').replaceChildren(...top.map(c=>pkBtn(label(c),slSame(c,sl.sel),c.car_path+'|'+c.track_code,()=>slPick(c),plural(c.files,'file')+', last '+slWhen(c.last)))));
  $('slPickNote').textContent='Newest first';const sel=$('slSelect');$('slSelectWrap').hidden=!rest.length;sel.replaceChildren();
  if(rest.length){const o0=h('option',null,'Pick one...');o0.value='';sel.append(o0);
    rest.forEach((c,i)=>{const o=h('option',null,label(c)+' ('+plural(c.files,'file')+')');o.value=String(i+6);if(slSame(c,sl.sel))o.selected=true;sel.append(o)})}}
$('slSelect').addEventListener('change',()=>{const c=((sl.data&&sl.data.combos)||[])[+$('slSelect').value];if(c)slPick(c)});
function slPick(c){sl.sel={car:c.car_path,track:c.track_code};sl.series=null;renderSlPick();loadSlReport()}
async function loadSlReport(){if(!sl.sel)return;const seq=++sl.seq,host=$('slReport');
  const q=new URLSearchParams({car:sl.sel.car,track:sl.sel.track});if(sl.series)q.set('series',sl.series);
  const slow=setTimeout(()=>{if(seq===sl.seq)host.classList.add('busy')},220);let j;
  try{j=await api('setuplab/report?'+q)}catch(e){clearTimeout(slow);connectionLost();return}
  clearTimeout(slow);if(seq!==sl.seq)return;host.classList.remove('busy');
  if(!j.ok){slEmpty('That didn\'t load',j.error||'Try again.');$('slStatus').textContent='The report didn\'t load.';return}
  renderSlReport(j.report);csSetupLab(j.report);const R=j.report||{};$('slStatus').textContent='Showing '+(R.track||R.track_code||'')+', '+(R.car||R.car_path||'')+'.'}
function slSec(title,id){const d=h('section','panel slsec');if(id)d.id=id;d.append(h('h3',null,title));return d}
function slSourceText(R){if(R.series_override)return 'You set this.';
  const src=String(R.series_setup_source||'');if(!src)return 'Debrief can\'t tell yet. Set it here if you know.';
  return (R.series_setup==='unknown'?'Not known yet: ':'From ')+src+'.'}
function confBadge(c){const x=SL_CONF[c]||[String(c||''),''];const b=h('span','pill sure'+(x[1]?' '+x[1]:''),x[0]);b.title='How sure this is'+(x[2]?': '+x[2]:'');return b}
function renderSlReport(R){const out=document.createDocumentFragment();
  const head=h('div','panel slhead');const tt=h('div','pvtt');tt.append(h('h2','pvtrack',R.track||R.track_code),h('p','pvby',R.car||R.car_path));head.append(tt);
  const kind=R.series_setup||'unknown',ser=h('div','slser');
  const pill=h('span','pill '+(kind==='open'?'ok':kind==='fixed'?'no':''),kind==='open'?'Open setup':kind==='fixed'?'Fixed setup':'Setup rules unknown');
  ser.append(pill,h('span','note',slSourceText(R)));
  const lab=h('label','field slover');lab.htmlFor='slSeries';lab.append(h('span',null,'This series is'));const sel=document.createElement('select');sel.id='slSeries';
  [['','Not sure (let Debrief tell)'],['open','Open setup'],['fixed','Fixed setup']].forEach(([v,t])=>{const o=h('option',null,t);o.value=v;sel.append(o)});
  sel.value=R.series_override||'';sel.addEventListener('change',()=>slSaveSeries(sel.value));lab.append(sel);
  // known from the schedule, the telemetry or the car itself: nothing to ask, but he can still correct it
  if(kind!=='unknown'&&!R.series_override){const d=h('details','sub slserfix');d.id='slSeriesFix';d.append(h('summary',null,'Not right? Change it'),lab);ser.append(d)}
  else ser.append(lab);
  head.append(ser);out.append(head);
  if(!R.files){const e=h('div','panel pvnone');e.append(h('h2','pvtrack','No telemetry for this car here yet'),h('p','lede','Drive it with telemetry recording on (Alt+L in the car), then press Read my telemetry.'));out.append(e);$('slReport').replaceChildren(out);return}
  const S=R.suggestions||{};
  if(kind==='fixed'){const sec=slSec('Fixed setup series','slFixed');const it=(S.items||[]).find(i=>i.rule==='fixed_series');
    sec.append(h('p',null,it?it.text:'Fixed setup series: the setup is locked, so there are no garage changes to try.'));
    const fuel=(S.items||[]).filter(i=>i.rule==='fuel');
    if(fuel.length){sec.append(h('h4',null,'What the telemetry shows'));const u=h('ul','pvlist slideas');fuel.forEach(i=>u.append(slIdeaLi(i)));sec.append(u)}
    sec.append(h('p','note','How the car handles, below, points to the in-car dials where this car has them, otherwise to the driving.'));
    out.append(sec);$('slReport').replaceChildren(out);return}
  // your setups
  const ss=slSec('Your setups','slSetups'),ul=h('ul','pvlist');
  (R.setups||[]).forEach(s=>{const li=h('li');li.append(h('b',null,s.label));
    const fl=s.files||[];li.append(h('span','sub2',plural(fl.length,'file')+', '+(s.first===s.last?slWhen(s.first):slWhen(s.first)+' to '+slWhen(s.last))));
    if(fl.length){const d=h('details','sub slfiles');d.append(h('summary',null,'Files'));const fu=h('ul','plain mono');fl.forEach(f=>fu.append(h('li',null,f)));d.append(fu);li.append(d)}
    ul.append(li)});
  ss.append(ul);out.append(ss);
  // what changed
  const dc=slSec('What changed','slDiffs');
  if(!(R.diffs||[]).length)dc.append(h('p','note',(R.setups||[]).filter(s=>s.fingerprint).length>1?'Nothing to compare in the order you drove them.':'Only one setup so far. Change one thing, drive it, and what changed shows here.'));
  (R.diffs||[]).forEach(df=>{const lbl=id=>((R.setups||[]).find(s=>s.id===id)||{}).label||('Setup '+id);
    const box=h('div','sldiff');box.append(h('h4',null,lbl(df.from)+' → '+lbl(df.to)));const ch=df.changes||[];
    const set=ch.filter(c=>c.kind==='setting'),fuel=ch.filter(c=>c.kind==='fuel'),res=ch.filter(c=>c.kind==='result');
    if(!set.length)box.append(h('p','note','Nothing you set changed.'));
    const add=(list,cls)=>{const u=h('ul','slchg'+(cls?' '+cls:''));list.forEach(c=>{const li=h('li');li.append(h('span',null,c.text));if(c.explain)li.append(h('span','sub2',c.explain));u.append(li)});return u};
    if(set.length)box.append(add(set));if(fuel.length)box.append(add(fuel));
    if(res.length){box.append(h('p','note','Garage results (these follow from the settings and the fuel):'));box.append(add(res,'muted'))}
    dc.append(box)});
  out.append(dc);
  // which was faster
  const C=R.compare||{},fc=slSec('Which was faster','slCompare');
  const head1=C.headline||C.verdict;if(head1)fc.append(h('p','slverdict',head1));
  const rows=C.setups||[];
  if(rows.length){const wrap=h('div','tblwrap'),t=h('table','pacetbl sltbl'),hr=h('tr');['Setup','Typical lap','Best','Laps counted','Left out','Track'].forEach(x=>hr.append(h('th',null,x)));
    const th=h('thead');th.append(hr);const tb=h('tbody');
    rows.forEach(r=>{const p=r.pace||{},lo=p.left_out||{},tr=h('tr'),c0=h('th',null,r.label);c0.scope='row';
      const left=[lo.warmup?lo.warmup+' warm-up':'',lo.traffic?lo.traffic+' in traffic':'',lo.slow?lo.slow+' off pace':''].filter(Boolean).join(', ')||'none';
      tr.append(c0,h('td','num',ok(p.median)?fmtT(p.median):'–'),h('td','num',ok(p.best)?fmtT(p.best):'–'),h('td','num',String(p.clean_laps||0)),h('td',null,left),
        h('td','num',(ok(p.surface_temp)?Math.round(p.surface_temp)+' °C':'–')+(p.track_state&&p.track_state!=='dry'?' '+p.track_state:'')));tb.append(tr)});
    t.append(th,tb);wrap.append(t);fc.append(wrap)}
  (C.pairs||[]).forEach(pr=>{const box=h('div','slpair');if(pr.text&&!String(head1||'').includes(String(pr.text).trim()))box.append(h('p',null,pr.text));
    (pr.confounders||[]).forEach(x=>box.append(h('p','note warn','Not a fair test: '+x+'.')));
    if(box.children.length)fc.append(box)});
  const miss=rows.flatMap(r=>r.missing||[]);if(miss.length)fc.append(h('p','note','Still missing: '+miss.join('; ')+'.'));
  (C.not_compared||[]).forEach(x=>fc.append(h('p','note',x)));
  fc.append(h('p','note','A lap counts when it is clean, not an out lap or the first flying lap, not spent alongside another car, and not far off that run\'s pace. Debrief only names a faster setup when the laps clearly say so.'));
  out.append(fc);
  // try next
  const tn=slSec('Try next','slNext'),items=S.items||[];
  items.filter(i=>i.kind==='note').forEach(i=>tn.append(h('p','note warn',i.text)));
  if(S.next){const n=S.next,card=h('div','slnextcard');const top=h('div','phead');top.append(h('span','eyebrow','One change to try next'),confBadge(n.confidence));card.append(top);
    card.append(h('p','slaction',n.action||n.text));if(n.why)card.append(h('p',null,n.why));
    if(n.check)card.append(h('p','note','Then check: '+n.check));if(n.data)card.append(h('p','note','From: '+n.data));tn.append(card)}
  else tn.append(h('p','note',R.suggestions?'No tyre pressure, ride height or fuel change to try from the telemetry so far. Ideas for how the car handles are under How the car handles, below.':'Nothing to suggest until a setup is recorded.'));
  const others=items.filter(i=>i!==S.next&&!i.next&&i.kind!=='note');
  const slList=(title,list)=>{if(!list.length)return;tn.append(h('h4',null,title));const u=h('ul','pvlist slideas');
    list.forEach(i=>u.append(slIdeaLi(i)));tn.append(u)};
  slList(S.next?'Other ideas, one at a time':'Also worth a look',others.filter(i=>i.kind==='change'));
  slList('What the telemetry shows',others.filter(i=>i.kind!=='change'));
  out.append(tn);
  // notes
  const notes=(R.notes||[]).filter(x=>typeof x==='string'&&x.trim()),bad=R.bad_files||[];
  if(notes.length||bad.length){const ns=slSec('Notes','slNotes');const u=h('ul','pvlist');notes.forEach(x=>u.append(h('li','note',x)));ns.append(u);
    if(bad.length){const d=h('details','sub');d.append(h('summary',null,plural(bad.length,'file')+' Debrief couldn\'t read'));const bu=h('ul','plain mono');
      bad.slice(0,50).forEach(b=>bu.append(h('li',null,b.file+': '+(plainText(b.error)||b.error))));d.append(bu);ns.append(d)}
    out.append(ns)}
  $('slReport').replaceChildren(out)}
function slIdeaLi(i){const li=h('li'),top=h('div','slidea');top.append(h('b',null,i.setting||'Note'),confBadge(i.confidence));li.append(top);
  const lines=Array.isArray(i.lines)&&i.lines.length?i.lines:[(i.kind==='change'&&i.action?i.action+' ':'')+(i.why||i.text||'')];
  lines.forEach(x=>li.append(h('span','sub2',x)));if(i.check)li.append(h('span','sub2','Check: '+i.check));return li}
async function slSaveSeries(v){if(!sl.sel)return;let r;try{r=await post('setuplab/series',{car:sl.sel.car,track:sl.sel.track,setup:v})}catch(e){connectionLost();return}
  if(!r.ok){banner(r.error||'That wasn\'t saved.',true);return}loadSlReport()}
$('slRead').addEventListener('click',async()=>{
  if(lastJob&&lastJob.state==='running'&&lastJob.kind!=='sync'){banner('Debrief is busy: '+(lastJob.title||TITLES[lastJob.kind]||'a job').toLowerCase()+'. Wait for it to finish.',true);return}
  setMsg('slMsg','');$('slRead').disabled=true;let r;try{r=await post('setuplab/read')}catch(e){connectionLost();return}
  if(!r.ok){$('slRead').disabled=false;setMsg('slMsg',r.error||'Could not start.','bad');return}
  lastJob={state:'running',kind:'setuplab',id:r.id};$('slBar').firstElementChild.style.width='2%';$('slNow').textContent='Starting...';
  if(sl.data)sl.data.running=true;renderSlTel();poll()});
$('slStop').addEventListener('click',async()=>{$('slStop').disabled=true;$('slNow').textContent='Stopping. What has been read is kept.';try{await post('setuplab/stop')}catch(e){}});
function renderSlJob(s){const pct=Math.round((s.frac||0)*100);$('slBar').firstElementChild.style.width=pct+'%';
  if(s.state==='running'){$('slNow').textContent=s.message||'Reading...';if(current.view==='setuplab'&&sl.data&&!sl.data.running){sl.data.running=true;renderSlTel()}return}
  if(handled.has(s.id))return;handled.add(s.id);$('slStop').disabled=false;
  if(s.state==='error'){setMsg('slMsg',s.error,'bad');if(current.view!=='setuplab')banner('Reading your telemetry stopped: '+s.error,true,9000)}
  else{const r=s.result||{};setMsg('slMsg',r.stopped?'Stopped. What was read is kept; the rest is read next time.':'Done: '+plural(r.files||0,'file')+' for '+carTracks(r.combos||0)+'.','ok');
    if(current.view!=='setuplab'&&current.view!=='practice')banner('Your telemetry has been read. Open Setup Lab to see your setups.')}
  if(sl.data)sl.data.running=false;
  if(current.view==='setuplab')loadSetupLab(true);else renderSlTel()}
$('slCheckBtn').addEventListener('click',()=>$('slCheckFile').click());
$('slCheckFile').addEventListener('change',async()=>{const f=$('slCheckFile').files[0];$('slCheckFile').value='';if(!f)return;
  const out=$('slCheckOut');out.hidden=true;out.replaceChildren();
  if(f.size>50*1024*1024){setMsg('slCheckMsg','That file is far too big to be a setup. Don\'t use it.','bad');return}
  setMsg('slCheckMsg','Checking '+f.name+'...');$('slCheckBtn').disabled=true;let r;
  try{r=await fetch(BASE+'api/setuplab/check-file?name='+encodeURIComponent(f.name),{method:'POST',headers:{'X-Debrief':'1','Content-Type':'application/octet-stream'},body:f}).then(x=>x.json())}
  catch(e){setMsg('slCheckMsg','Couldn\'t send the file.','bad');return}finally{$('slCheckBtn').disabled=false}
  if(!r||!r.ok){setMsg('slCheckMsg',(r&&r.error)||'Couldn\'t check that file.','bad');return}
  const c=r.check||{},probs=Array.isArray(c.problems)?c.problems:[];
  if(c.ok){setMsg('slCheckMsg',f.name+': no programs or scripts found. It looks like '+(c.kind==='zip'?'a setup pack ('+plural((c.members||[]).length,'file')+')':'a setup file')+'.','ok');
    (c.members||[]).slice(0,30).forEach(m=>out.append(h('li',null,m)))}
  else{setMsg('slCheckMsg',f.name+': don\'t use this file.','bad');probs.slice(0,20).forEach(p=>out.append(h('li','bad',p.charAt(0).toUpperCase()+p.slice(1)+'.')))}
  out.hidden=!out.children.length});

/* ---------- Coach (2.12): spoken cues while you drive. One runner on the server; polled once a second, only
   while this page is open or the coach is running */
let co={st:null,timer:null,seq:0,filled:false,dirty:false,busy:false,testing:null};
const CO_SHOW={coaching:['Coaching','ok'],muted:['Muted','no'],rehearsing:['Rehearsing','ok'],waiting:['Waiting for iRacing',''],starting:['Starting',''],off:['Off','']};
// 2.18: the mute in words, as the page shows it on reopening: since when, by what, and what still comes through
function coMuteWords(st){const M=st.mute||{};if(!st.muted)return '';
  if(ok(M.quiet_left))return 'Muted: quiet for '+Math.ceil(M.quiet_left)+' s more, then it talks again.';
  const t=ok(M.since)?new Date(M.since*1000):null,hm=t?String(t.getHours()).padStart(2,'0')+':'+String(t.getMinutes()).padStart(2,'0'):'';
  return 'Muted'+(hm?' since '+hm:'')+(M.by?' ('+M.by+')':'')+'. Nothing is said until you unmute, except the answer when you ask for your pace.'}
const CO_STATE={coaching:['Coaching','ok'],waiting:['Waiting for iRacing',''],no_reference:['No reference lap yet','no'],starting:['Starting',''],
  finished:['Finished',''],error:['Stopped','no'],stopped:['Off','']};
const fmtClock=t=>{if(!ok(t))return '';t=Math.max(0,Math.floor(t));const hh=Math.floor(t/3600),m=Math.floor(t%3600/60),s=t%60;return (hh?hh+':'+String(m).padStart(2,'0'):String(m))+':'+String(s).padStart(2,'0')};
function openCoach(plan){current={view:'coach',dir:null};show('vCoach');markNav();co.filled=false;loadCoPlan(plan);coachPoll();co2Open()}
async function coachPoll(){const seq=++co.seq;clearTimeout(co.timer);let j;try{j=await api('coach/status')}catch(e){connectionLost();return}
  if(seq!==co.seq)return;if(j&&j.ok)renderCoach(j);
  if(current.view==='coach'||(j&&j.running))co.timer=setTimeout(coachPoll,1000)}
function renderCoach(st){co.st=st;const run=!!st.running,state=CO_STATE[st.state]||[st.state||'',''];
  const nb=document.querySelector('.navbtn[data-nav=coach]');if(nb)nb.classList.toggle('live',run);
  if(current.view!=='coach')return;
  // 2.18: the pill says what the driving view says (Coaching, Muted, Rehearsing, Off); never "Coaching" in a rehearsal
  const show=CO_SHOW[st.show]&&!(st.show==='coaching'&&st.state==='no_reference')?CO_SHOW[st.show]:state;
  const pill=$('coPill');pill.className='pill'+(show[1]?' '+show[1]:'');pill.textContent=run||st.state==='error'?show[0]:'Off';
  const mb=$('coMuted');mb.hidden=!(run&&st.muted);mb.replaceChildren(...(run&&st.muted?[h('b',null,'Muted'),h('span',null,coMuteWords(st))]:[]));
  $('coText').textContent=st.text||'';
  const b=$('coStart');b.textContent=run?'Stop coaching':'Start coaching';b.classList.toggle('primary',!run);b.disabled=!!co.busy;
  const m=$('coMute'),hk=st.hotkey||{};m.disabled=!run;m.setAttribute('aria-pressed',String(!!st.muted));
  m.replaceChildren(document.createTextNode(st.muted?'Unmute ':'Mute '),h('kbd',null,hk.key||'Ctrl+Shift+M'));
  m.title=run&&!hk.on?'This key works while the Debrief window is in front.':'Works from the car too, while Debrief is open.';
  const q=$('coQuiet');q.hidden=!(run&&st.quiet_reason);q.textContent=st.quiet_reason?'Quiet now: '+st.quiet_reason+'.':'';
  if(st.state==='error'&&st.error)setMsg('coMsg',st.error,'bad');
  const R=st.reference;$('coRef').textContent=R?R.text||R.lap_text:'None yet';
  $('coRefNote').textContent=R?(R.source&&R.source.kind==='live'?(st.reference_why?cap(st.reference_why)+'.':'A lap you drove just now.'):'Your best clean lap in your telemetry files'+(st.car?' for the '+st.car+(st.track?' at '+st.track:''):'')+'.'):
    'Debrief uses your best clean lap for the car and track iRacing shows, from your telemetry files. No lap yet? '+TEL_HOW+' Your best clean lap becomes the reference.';
  const L=st.last_lap,ll=$('coLast');ll.hidden=!(L&&ok(L.time));
  if(L&&ok(L.time))ll.textContent='Last lap '+fmtT(L.time)+(ok(L.delta)?', '+Math.abs(L.delta).toFixed(2)+' s '+(L.delta>0?'down on':'up on')+' your reference':'')+(L.clean===false?' (not clean)':'')+'.';
  const W=st.weak||[],wu=$('coWeak');wu.replaceChildren(...W.map(w=>{const li=h('li');li.append(h('b',null,coCornerLabel(w)),h('span','sub2',ok(w.lost_s)?(w.source==='session'?'about '+w.lost_s.toFixed(2)+' s a lap lost to your reference this session':w.source==='corner coach'?'your best laps were about '+w.lost_s.toFixed(2)+' s quicker here':'about '+w.lost_s.toFixed(2)+' s a lap to find'):'a corner that keeps costing you'));
    if(w.plan_owns)li.append(h('span','sub2','This session the coach watches your one thing here instead.'));
    else if(w.phrase)li.append(h('span','sub2','After it, the coach says: "'+w.phrase+'"'));return li}));
  $('coWeakNote').textContent=st.weak_note||(W.length?'From your race reviews of this car at this track. One focus at a time.':R?'From your race reviews of this car at this track.':'Shows once the coach knows the car and track.');
  const sp=(st.spoken||[]).slice().reverse(),su=$('coSpoken');
  su.replaceChildren(...(sp.length?sp.map(c=>{const li=h('li');li.append(h('span','num cot',fmtClock(c.t)),h('span',null,c.text));return li}):[h('li','note','Nothing said yet.')]));
  $('coSpokenNote').textContent=sp.length?'Newest first, session time':'';
  coPlanShow(!(run&&W.length));
  renderCoFocus(st);renderWheel(st.wheel);
  if(!co.filled||!co.dirty){fillCoachSettings(st.settings||{});co.filled=true}
  coLevelShow(st);coTestResult(st.test_voice);csCoach(st);co2Render(st);cdRender(st)}
// 2.17.2 Coaching level (Quiet / Normal / Busy / Only when I ask): saved as soon as it is picked; the coach uses it at once
function coLevelShow(st){const lv=(st.settings||{}).level||st.level||'normal';
  if(!co.levelBusy)document.querySelectorAll('input[name=coLevel]').forEach(r=>{r.checked=r.value===lv});
  const fi=st.focus_info,fw=$('coFocusWhy');fw.hidden=!(fi&&fi.text&&st.running);fw.textContent=fi&&fi.text?fi.text:''}
document.querySelectorAll('input[name=coLevel]').forEach(r=>r.addEventListener('change',async()=>{if(!r.checked)return;
  co.levelBusy=true;setMsg('coLevelMsg','Saving...');let j;
  try{j=await post('coach/settings',{settings:{level:r.value}})}catch(e){co.levelBusy=false;connectionLost();return}
  co.levelBusy=false;if(!j.ok){setMsg('coLevelMsg',j.error,'bad');return}
  setMsg('coLevelMsg',co.st&&co.st.running?'Saved. The coach uses it now.':'Saved.','ok');coachPoll()}));
function fillCoachSettings(s){$('coLapTimes').value=s.lap_times==='full'?'full':'short';$('coInRaces').checked=!!s.in_races;$('coMarkers').checked=s.markers!==false;$('coFeedback').checked=s.feedback!==false;
  $('coLapDelta').checked=s.lap_delta!==false;$('coBeep').checked=!!s.brake_beep;$('coTopN').value=String(Math.max(1,Math.min(3,s.top_n||2)));
  $('coVol').value=String(ok(s.volume)?s.volume:100);$('coRate').value=String(ok(s.rate)?s.rate:0);coRanges()}
function coRanges(){$('coVolV').textContent=$('coVol').value+'%';const r=+$('coRate').value;$('coRateV').textContent=r<0?'slower':r>0?'faster':'normal'}
// "Turn 2 (C2 in your race review)": the spoken name as words, and the race review's own name for the corner
function coCornerLabel(w){const m=/^[A-Za-z](\d+)$/.exec(String(w.name||''));const n=m?'Turn '+m[1]:String(w.name||'');
  return w.source==='session'?n+' (from this session)':w.source==='corner coach'?n+' (from the Corner coach)':w.review_id?n+' ('+w.review_id+' in your race review)':n}
['coVol','coRate'].forEach(id=>$(id).addEventListener('input',()=>{coRanges();co.dirty=true}));
$('fCoach').addEventListener('change',()=>{co.dirty=true});
$('fCoach').addEventListener('submit',async e=>{e.preventDefault();setMsg('coSetMsg','Saving...');
  const settings={in_races:$('coInRaces').checked,markers:$('coMarkers').checked,feedback:$('coFeedback').checked,lap_delta:$('coLapDelta').checked,
    brake_beep:$('coBeep').checked,top_n:+$('coTopN').value,volume:+$('coVol').value,rate:+$('coRate').value,lap_times:$('coLapTimes').value};
  let r;try{r=await post('coach/settings',{settings})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('coSetMsg',r.error,'bad');return}co.dirty=false;fillCoachSettings(r.settings||settings);
  setMsg('coSetMsg',co.st&&co.st.running?'Saved. The coach uses them now.':'Saved.','ok')});
const CO_HEAR='You should hear: "Brake marker, T3." Save first to test a new volume.';
$('coTest').addEventListener('click',async()=>{setMsg('coSetMsg','Speaking test phrase...');$('coTest').disabled=true;let r;
  try{r=await post('coach/test-voice')}catch(e){$('coTest').disabled=false;connectionLost();return}
  if(!r.ok){$('coTest').disabled=false;setMsg('coSetMsg',r.error||'The voice didn\'t work.','bad');return}
  if(r.pending){co.testing=r.id;coachPoll();return}
  $('coTest').disabled=false;const v=r.voice||{};setMsg('coSetMsg',r.spoken?CO_HEAR:'No sound: '+(v.reason||'speech isn\'t available on this computer')+'.',r.spoken?'ok':'bad')});
function coTestResult(t){if(co.testing==null)return;if(t&&t.id!==co.testing)return;
  if(t&&t.state==='starting'){setMsg('coSetMsg','Speaking test phrase...');return}
  co.testing=null;$('coTest').disabled=false;
  if(!t){setMsg('coSetMsg','');return}
  if(t.state==='spoken')setMsg('coSetMsg',CO_HEAR,'ok');
  else if(t.state==='silent')setMsg('coSetMsg','No sound: '+(t.reason||'speech isn\'t available on this computer')+'.','bad');
  else setMsg('coSetMsg',t.text||'The voice didn\'t work.','bad')}
$('coStart').addEventListener('click',async()=>{if(co.busy)return;const run=!!(co.st&&co.st.running);setMsg('coMsg',run?'Stopping...':'Starting...');co.busy=true;$('coStart').disabled=true;let r;
  try{r=await post(run?'coach/stop':'coach/start')}catch(e){connectionLost();return}finally{co.busy=false}
  $('coStart').disabled=false;if(!r.ok){setMsg('coMsg',r.error||'The coach didn\'t start.','bad');return}
  setMsg('coMsg',run?'Stopped.':'Started. It talks once you are on track with a reference lap.','ok');if(r.status)renderCoach(r.status);coachPoll()});
// 2.18: Mute sends the state the button shows (never a toggle), so its first press always mutes
async function coachMute(){const want=!(co.st&&co.st.muted);if(co.st){co.st.muted=want}
  let r;try{r=await post('coach/mute-set',{muted:want})}catch(e){connectionLost();return}
  if(!r.ok){setMsg('coMsg',r.error,'bad');return}setMsg('coMsg',r.muted?'Muted.':'Unmuted: the coach talks again.','ok');coachPoll()}
$('coMute').addEventListener('click',coachMute);
// Ctrl+Shift+M inside the window: only when Windows' own key isn't on (that one already toggled the coach)
function coachMuteKey(){const st=co.st;if(st&&st.running&&!(st.hotkey&&st.hotkey.on))coachMute()}

/* ---------- 2.18 the driving view: one glance from the car (state, one thing and its count, last cues, delta,
   reference). The sidebar folds away while it is on; the choice is remembered on this PC. */
const CD_WORD={coaching:'Coaching',muted:'Muted',rehearsing:'Rehearsing',off:'Off',waiting:'Waiting',starting:'Starting'};
function coDrivePref(){try{return localStorage.getItem('debrief-coach-drive')==='1'}catch(e){return false}}
function coDriveSet(on){try{localStorage.setItem('debrief-coach-drive',on?'1':'0')}catch(e){}coDriveApply();if(co.st)renderCoach(co.st)}
function coDriveApply(){const on=coDrivePref()&&!$('vCoach').hidden;document.body.classList.toggle('codriving',on);
  $('coDrive').hidden=!on;$('coFull').hidden=on;$('coDriveBtn').setAttribute('aria-pressed',String(on))}
new MutationObserver(coDriveApply).observe($('vCoach'),{attributes:true,attributeFilter:['hidden']});
$('coDriveBtn').addEventListener('click',()=>coDriveSet(true));
$('cdExit').addEventListener('click',()=>coDriveSet(false));
$('cdStart').addEventListener('click',()=>$('coStart').click());
$('cdMute').addEventListener('click',coachMute);
$('cdRepeat').addEventListener('click',()=>$('coRepeat').click());
$('cdPace').addEventListener('click',()=>$('coPace').click());
new MutationObserver(()=>{$('cdMsg').textContent=$('coMsg').textContent;$('cdMsg').className='dmsg '+$('coMsg').className}).observe($('coMsg'),{childList:true,characterData:true,subtree:true,attributes:true});
function cdSigned(d){return (d>0?'+':d<0?'\u2212':'')+Math.abs(d).toFixed(2)}
function cdRender(st){if(current.view!=='coach'||$('coDrive').hidden)return;const run=!!st.running,reh=st.mode==='rehearsal';
  const err=st.state==='error',show=err?'off':(st.show||'off');
  const box=$('cdState');box.className='dstate '+show;$('cdWord').textContent=err?'Stopped':(CD_WORD[show]||show);
  let sub='';
  if(err)sub=st.error||st.text||'';
  else if(show==='muted')sub=coMuteWords(st)+(reh?' Rehearsal: '+coRhWhere(st)+'.':'');
  else if(show==='rehearsing')sub=(st.skipping?'Skipping to the next '+st.skipping+'. ':'')+((st.rehearsal||{}).title||'')+' · '+coRhWhere(st)+(st.skip_note?'. '+st.skip_note:'');
  else if(show==='coaching')sub=(st.car?st.car+(st.track?' at '+st.track:''):'')+(st.state==='no_reference'?'. Building your reference: drive one clean lap.':'');
  else if(show==='waiting')sub='Waiting for iRacing to start.';
  else if(show==='starting')sub='Starting the coach.';
  else sub=st.state==='finished'?'Rehearsal finished.':'Press Start coaching, then drive.';
  $('cdSub').textContent=sub;
  // the one thing and its count (finished laps only)
  const F=st.focus,P=co.focusPlan,T=F&&(run||F.measured)?F:P;
  $('cdThing').textContent=T?mphText(turnWords(T.text||'')):'No one thing for this car and track yet. The coach uses your focus corners.';
  $('cdCount').textContent=F&&run?(F.count_text||F.progress||''):F&&F.count_of?'Last drive: '+F.count_text:'';
  $('cdDone').textContent=T?(F&&F.done?'Done: you hit it on 7 of your last 10 laps.':(F&&F.done_text)||'Done when you hit it on 7 of 10 laps')+'.':'';
  // against the reference: this lap so far, else the last lap
  const D=$('cdDelta'),L=st.last_lap;let dt='--',dc='dbig num',dn='';
  if(run&&ok(st.live_delta)){dt=cdSigned(st.live_delta)+' s';dc+=st.live_delta>0.005?' down':st.live_delta<-0.005?' up':'';dn='This lap so far'+(st.live_delta>0.005?', down on':st.live_delta<-0.005?', up on':', level with')+' your reference.'}
  else if(L&&ok(L.delta)){dt=cdSigned(L.delta)+' s';dc+=L.delta>0.005?' down':L.delta<-0.005?' up':'';dn='Last lap '+fmtT(L.time)+(L.clean===false?' (not clean)':'')+'.'}
  D.textContent=dt;D.className=dc;
  const R=st.reference;$('cdRef').textContent=(dn?dn+' ':'')+(R?'Reference '+(R.text||R.lap_text)+(st.reference_why?': '+st.reference_why:'')+'.':'No reference lap yet: your first clean lap becomes it.');
  // the last 3 cues, newest first
  const sp=(st.spoken||[]).slice(-3).reverse();
  $('cdCues').replaceChildren(...(sp.length?sp.map(c=>{const li=h('li');li.append(h('span','cot',fmtClock(c.t)),h('span',null,c.text));return li}):[h('li','dnone','Nothing said yet.')]));
  // buttons
  const b=$('cdStart');b.textContent=run?(reh?'Stop rehearsal':'Stop coaching'):'Start coaching';b.classList.toggle('primary',!run);b.disabled=!!co.busy;
  const m=$('cdMute');m.disabled=!run;m.setAttribute('aria-pressed',String(!!st.muted));m.textContent=st.muted?'Unmute':'Mute';
  $('cdRepeat').disabled=$('cdPace').disabled=!run;
  $('cdRh').hidden=!(run&&reh);$('cdSkipLap').disabled=$('cdSkipCue').disabled=!!st.skipping}

/* ---------- Coach 2.0: Repeat that, Mute for 1 minute, rehearsal, voice and sound, coach log, credits */
let co2={voices:null,sessions:null,vdirty:false,previewing:false,measuring:false,logName:null};
const CO2_EV={spoken:'Said',made:'Made',dropped:'Dropped',cancelled:'Cut off',not_made:'Not made',corner_fixed:'Fixed',
  corner_regressed:'Slipped back',focus_learned:'Focus learned',lap_end:'Lap',repeated:'Repeated',asked:'Asked',muted:'Muted'};
function co2Open(){co2LoadVoices();co2LoadSessions();co2LoadLogs()}
function co2Render(st){if(current.view!=='coach')return;const run=!!st.running,K=st.keys||{},reh=st.mode==='rehearsal';
  const rp=$('coRepeat');rp.disabled=!run;rp.title=run&&!(K.repeat&&K.repeat.on)?'This key works while the Debrief window is in front.':'Says the last cue again. Works from the car too, while Debrief is open.';
  const q1=$('coQuiet1');q1.disabled=!run;const ql=st.muted&&ok(st.quiet_left)?Math.ceil(st.quiet_left):null;
  q1.replaceChildren(document.createTextNode(ql!=null?'Quiet for '+ql+' s ':'Mute for 1 minute '),h('kbd',null,'Ctrl+Shift+N'));
  q1.title=run&&!(K.quiet&&K.quiet.on)?'This key works while the Debrief window is in front.':'Quiet for a minute, then it talks again by itself.';
  const pc=$('coPace');pc.disabled=!run;pc.title=run&&!(K.pace&&K.pace.on)?'This key works while the Debrief window is in front.':'Your last lap against your reference and the corner that cost the most. Works from the car too, while Debrief is open.';
  if(run&&reh)$('coStart').textContent='Stop rehearsal';
  const P=st.progress,pr=$('coRhProg');pr.hidden=!(reh&&P&&(run||st.state==='finished'));
  if(!pr.hidden){const pc=Math.round(100*(P.fraction||0)),b=$('coRhBar');b.firstElementChild.style.width=pc+'%';b.setAttribute('aria-valuenow',String(pc));
    $('coRhPct').textContent=coRhWhere(st)}
  $('coRhGo').disabled=run||!$('coRhPick').value;
  $('coRhSkipLap').disabled=$('coRhSkipCue').disabled=!(run&&reh)||!!st.skipping;
  if(!co2.speedBusy){const sp=String(reh&&run?st.speed||1:st.rehearse_speed||1);$('coRhSpeed').value=sp;$('cdSpeed').value=sp}
  const R=st.rehearsal;if(R&&reh)setMsg('coRhMsg',run?(st.skipping?'Skipping to the next '+st.skipping+'...':'Rehearsing: '+R.title+'.'+(st.skip_note?' '+st.skip_note:'')+' Cues show under Last cues as they are said.'):st.state==='finished'?'Rehearsal finished.':'',run||st.state==='finished'?'ok':'');
  const RN=st.repeat_note;if(RN&&RN.text&&st.muted&&RN.wall!==co2.repeatWall){co2.repeatWall=RN.wall;setMsg('coMsg',RN.text,'bad')}
  const v=st.voice||{},pk=v.pack,dev=v.device,S=st.settings||{};
  const VN=st.voice_note||{};       // 2.18: the chosen voice can't play: why, and what plays instead
  $('coVoiceLine').textContent=run?((VN.text?VN.text+' ':'Voice: '+(pk?pk.name:'Windows speech')+'. ')+(dev?'Through '+dev.device+'. ':'')+(dev&&dev.note?dev.note+' ':'')+
    (v.said_fallback?'Windows speech said '+plural(v.said_fallback,'phrase')+' the voice pack can\'t. ':'')+'Audio delay allowed for: '+Math.round(1000*(S.audio_latency_s||0))+' ms.'):(VN.text||'');
  $('coVoiceLine').className='note'+(VN.text?' warn':'');
  const M=st.measure;if(M&&co2.measuring){if(M.state!=='measuring'){co2.measuring=false;$('coMeasure').disabled=false;if(M.state==='done')$('coLat').textContent=Math.round(M.value*1000)+' ms'}
    setMsg('coMeasureMsg',M.state==='measuring'?'Measuring...':M.text||'',M.state==='done'?'ok':M.state==='error'?'bad':'')}
  const PV=st.preview;if(PV&&co2.previewing){if(PV.state!=='starting'&&PV.state!=='playing'){co2.previewing=false;$('coPreview').disabled=false}
    setMsg('coVoiceMsg',PV.state==='starting'?'Getting the voice ready...':PV.text||'',PV.state==='error'?'bad':'ok');if(PV.state==='done')setMsg('coVoiceMsg','')}
  co2Wheel(st.wheel_repeat);co2Wheel(st.wheel_pace,'P')}
async function co2Post(p,body,msgId){let r;try{r=await post(p,body)}catch(e){connectionLost();return null}
  if(!r.ok&&msgId)setMsg(msgId,r.error||'That didn\'t work.','bad');return r}
$('coRepeat').addEventListener('click',async()=>{const r=await co2Post('coach/repeat',{},'coMsg');if(r&&r.ok){if(r.muted)co2.repeatWall=null;
  setMsg('coMsg',r.said?'Said again: "'+r.text+'"':r.muted?r.note:'Nothing said yet to repeat.',r.said?'ok':r.muted?'bad':'')}});
$('coPace').addEventListener('click',async()=>{const r=await co2Post('coach/pace',{},'coMsg');if(r&&r.ok)setMsg('coMsg','Asked. The coach answers as soon as it is safe to talk.','ok')});
$('coQuiet1').addEventListener('click',async()=>{const r=await co2Post('coach/quiet',{},'coMsg');if(r&&r.ok){setMsg('coMsg','Quiet for 1 minute. Unmute ends it early.','ok');coachPoll()}});
document.addEventListener('keydown',e=>{if(!(e.ctrlKey||e.metaKey)||!e.shiftKey||e.altKey)return;const k=e.key.toLowerCase();if(k!=='r'&&k!=='n'&&k!=='p')return;
  if(!(co.st&&co.st.running))return;e.preventDefault();const K=co.st.keys||{},role=k==='r'?'repeat':k==='p'?'pace':'quiet';
  if(K[role]&&K[role].on)return;        // 2.18: Windows' own key already did it; a second press would double it
  $(k==='r'?'coRepeat':k==='p'?'coPace':'coQuiet1').click()});
// rehearsal: one of his Practice sessions, played through the coach at real speed
async function co2LoadSessions(){const sel=$('coRhPick');let j;try{j=await api('sessions-ibt')}catch(e){return}
  const S=(j&&j.ok&&j.sessions)||[];co2.sessions=S;const keep=sel.value;
  if(!S.length){sel.replaceChildren(h('option',null,j&&j.ok&&!j.indexed?'Read your telemetry on the Practice page first':'No practice sessions yet'));sel.options[0].value='';$('coRhGo').disabled=true;return}
  sel.replaceChildren(...S.slice(0,60).map(x=>{const o=h('option',null,(x.date||'')+'  '+(x.car||x.car_path)+' at '+(x.track||x.track_code)+(ok(x.laps)?', '+plural(x.laps,'lap'):''));o.value=x.id;return o}));
  if(keep&&S.some(x=>x.id===keep))sel.value=keep;$('coRhGo').disabled=!!(co.st&&co.st.running)}
$('coRhPick').addEventListener('change',()=>{$('coRhGo').disabled=!!(co.st&&co.st.running)||!$('coRhPick').value});
$('coRhGo').addEventListener('click',async()=>{const id=$('coRhPick').value;if(!id)return;setMsg('coRhMsg','Starting...');$('coRhGo').disabled=true;
  const r=await co2Post('coach/rehearse',{id},'coRhMsg');if(!r){return}if(!r.ok){$('coRhGo').disabled=false;return}
  setMsg('coRhMsg','Rehearsing. Cues show under Last cues as they are said.','ok');if(r.status)renderCoach(r.status);coachPoll()});
// 2.18 rehearsal speed and skipping ahead
function coRhWhere(st){const P=st.progress||{};return fmtClock(P.at_s)+' of '+fmtClock(P.total_s)+(P.files>1?', file '+P.file+' of '+P.files:'')+(st.speed&&st.speed!==1?', at '+st.speed+'x':'')}
async function coRhSpeed(v){co2.speedBusy=true;$('coRhSpeed').value=$('cdSpeed').value=v;const r=await co2Post('coach/rehearse-speed',{speed:+v},'coRhMsg');co2.speedBusy=false;
  if(r&&r.ok){setMsg('coRhMsg',co.st&&co.st.running&&co.st.mode==='rehearsal'?'Now at '+v+'x.':'The rehearsal plays at '+v+'x.','ok');coachPoll()}}
['coRhSpeed','cdSpeed'].forEach(id=>$(id).addEventListener('change',()=>coRhSpeed($(id).value)));
async function coRhSkip(what){const r=await co2Post('coach/rehearse-skip',{what},'coRhMsg');if(r&&r.ok){setMsg('coRhMsg','Skipping to the next '+what+'...','ok');coachPoll()}}
$('coRhSkipLap').addEventListener('click',()=>coRhSkip('lap'));$('coRhSkipCue').addEventListener('click',()=>coRhSkip('cue'));
$('cdSkipLap').addEventListener('click',()=>coRhSkip('lap'));$('cdSkipCue').addEventListener('click',()=>coRhSkip('cue'));
// voice and sound
async function co2LoadVoices(){let j;try{j=await api('coach/voices')}catch(e){return}if(!j||!j.ok)return;co2.voices=j;if(!co2.vdirty)co2FillVoices(j)}
function co2FillVoices(j){const S=j.settings||{},ps=$('coPack'),ds=$('coDev');
  ps.replaceChildren(...(j.packs||[]).filter(p=>p.ok).map(p=>{const o=h('option',null,p.name+(p.bundled?'':' (yours)'));o.value=p.id;return o}));
  ps.value=S.voice_pack&&[...ps.options].some(o=>o.value===S.voice_pack)?S.voice_pack:(ps.options[0]?ps.options[0].value:'');
  const devs=(j.devices||[]).slice();if(S.device&&!devs.includes(S.device))devs.push(S.device);
  ds.replaceChildren(h('option',null,'Windows default'),...devs.map(n=>{const o=h('option',null,n+(n===S.device&&j.device_missing?' (not connected)':''));o.value=n;return o}));
  ds.options[0].value='';ds.value=S.device||'';ds.disabled=!j.can_choose_device;
  $('coDevNote').textContent=!j.can_choose_device?'Sound devices can only be chosen on Windows.':j.device_missing?S.device+' isn\'t connected now, so the coach plays through Windows default until it is.':'Pick the headset you use in iRacing. Windows default follows whatever Windows plays through.';
  $('coLat').textContent=ok(S.audio_latency_s)?Math.round(S.audio_latency_s*1000)+' ms':'not yet';co2PackNote()}
function co2PackNote(){const j=co2.voices;if(!j)return;const p=(j.packs||[]).find(x=>x.id===$('coPack').value);
  const cn=j.chosen_note||{},saved=(j.settings||{}).voice_pack;
  $('coPackNote').textContent=(!cn.ok&&cn.text&&(!p||p.id!==saved||!co2.vdirty)?cn.text+' ':'')+(p?[p.voice_credit,p.license].filter(Boolean).join('. ')+(p.bundled?'':'. Added by you.'):'')}
['coPack','coDev'].forEach(id=>{$(id).addEventListener('input',()=>{co2.vdirty=true;co2PackNote()});$(id).addEventListener('change',()=>{co2.vdirty=true;co2PackNote()})});
$('coVoiceSave').addEventListener('click',async()=>{setMsg('coVoiceMsg','Saving...');
  const r=await co2Post('coach/voice-settings',{settings:{voice_pack:$('coPack').value,device:$('coDev').value}},'coVoiceMsg');
  if(!r||!r.ok)return;co2.vdirty=false;setMsg('coVoiceMsg',r.note||'Saved.','ok');co2LoadVoices()});
$('coPreview').addEventListener('click',async()=>{if(co2.vdirty){setMsg('coVoiceMsg','Save first to hear the new voice or device.','bad');return}
  co2.previewing=true;$('coPreview').disabled=true;setMsg('coVoiceMsg','Getting the voice ready...');
  const r=await co2Post('coach/preview',{},'coVoiceMsg');if(!r||!r.ok){co2.previewing=false;$('coPreview').disabled=false;return}coachPoll()});
$('coMeasure').addEventListener('click',async()=>{if(co2.vdirty){setMsg('coMeasureMsg','Save the device first.','bad');return}
  co2.measuring=true;$('coMeasure').disabled=true;setMsg('coMeasureMsg','Measuring...');
  const r=await co2Post('coach/measure',{},'coMeasureMsg');if(!r||!r.ok){co2.measuring=false;$('coMeasure').disabled=false;return}coachPoll()});
function co2PackResult(r){const ul=$('coPackProbs');ul.hidden=true;ul.replaceChildren();if(!r)return;
  if(r.ok){setMsg('coPackMsg',r.note||'Added.','ok');co2LoadVoices()}
  else{setMsg('coPackMsg',r.error||'That voice pack can\'t be used.','bad');const P=r.problems||[];ul.replaceChildren(...P.map(p=>h('li',null,p.charAt(0).toUpperCase()+p.slice(1)+'.')));ul.hidden=!P.length}}
$('coPackZipBtn').addEventListener('click',()=>$('coPackZip').click());
$('coPackZip').addEventListener('change',async()=>{const f=$('coPackZip').files[0];$('coPackZip').value='';if(!f)return;
  if(f.size>60*1024*1024){setMsg('coPackMsg','That file is far bigger than a voice pack.','bad');return}
  setMsg('coPackMsg','Checking '+f.name+'...');let r;
  try{r=await fetch(BASE+'api/coach/import-pack-zip?'+new URLSearchParams({name:f.name}),{method:'POST',headers:{'X-Debrief':'1','Content-Type':'application/octet-stream'},body:f}).then(x=>x.json())}
  catch(e){setMsg('coPackMsg','Couldn\'t send the file.','bad');return}co2PackResult(r)});
$('coPackDirBtn').addEventListener('click',async()=>{const v=$('coPackDir').value.trim();if(!v){setMsg('coPackMsg','Type the folder that holds the voice pack.','bad');return}
  setMsg('coPackMsg','Checking the folder...');let r;try{r=await post('coach/import-pack',{folder:v})}catch(e){connectionLost();return}co2PackResult(r)});
// wheel buttons: R repeats the last cue, P asks for the pace (2.17.2)
function co2Wheel(w,x){if(!w)return;x=x||'R';const learning=w.state==='learning';$('coWheel'+x+'Text').textContent=w.text||'';
  $('coWheel'+x+'Learn').disabled=learning;$('coWheel'+x+'Clear').disabled=learning||!w.label;if(w.note)setMsg('coWheel'+x+'Msg',w.note,/^Saved|cleared/.test(w.note)?'ok':'bad')}
[['R','repeat'],['P','pace']].forEach(([x,role])=>{
  $('coWheel'+x+'Learn').addEventListener('click',async()=>{setMsg('coWheel'+x+'Msg','Press the button on your wheel now...');const r=await co2Post('coach/wheel-'+role+'-learn',{},'coWheel'+x+'Msg');if(r&&r.wheel)co2Wheel(r.wheel,x);coachPoll()});
  $('coWheel'+x+'Clear').addEventListener('click',async()=>{const r=await co2Post('coach/wheel-'+role+'-clear',{},'coWheel'+x+'Msg');if(r&&r.wheel)co2Wheel(r.wheel,x)})});
// the coach log
async function co2LoadLogs(){let j;try{j=await api('coach/logs')}catch(e){return}if(!j||!j.ok)return;const tb=$('coLogList'),L=j.logs||[];
  tb.replaceChildren(...(L.length?L.map(x=>{const tr=h('tr'),b=h('button','btn chipbtn','View');b.type='button';b.addEventListener('click',()=>co2ShowLog(x.name,x.title+(x.mode==='rehearsal'?' (rehearsal)':'')+', '+x.date));
    const td=h('td');td.append(b);tr.append(h('td','num',x.date),h('td',null,x.title+(x.mode==='rehearsal'?' (rehearsal)':'')),h('td','num',String(x.laps)),h('td','num',String(x.spoken)),h('td','num',String(x.dropped)),td);return tr}):
    [(()=>{const tr=h('tr'),td=h('td','note','No coach sessions yet. A log starts when the coach knows the car and track.');td.colSpan=6;tr.append(td);return tr})()]))}
$('coLogs').addEventListener('toggle',()=>{if($('coLogs').open)co2LoadLogs()});
function co2Why(r){if(r.dropped)return r.dropped;const m=r.measured||{},b=[];if(r.why)b.push(mphText(r.why));
  if(ok(m.min_kmh))b.push('min '+spdOf(m.min_kmh,'kmh')+' vs '+spdOf(m.ref_min_kmh,'kmh')+' '+spdLabel());if(ok(m.delta_s))b.push((m.delta_s>0?'+':'')+m.delta_s.toFixed(2)+' s');
  if(r.ev==='spoken'&&ok(r.next_brake_s))b.push(r.next_brake_s.toFixed(1)+' s to the next brake point');if(r.ev==='lap_end')b.push(r.clean?'clean':'not clean');return b.join('; ')}
async function co2ShowLog(name,title){co2.logName=name;let j;try{j=await api('coach/log?'+new URLSearchParams({name}))}catch(e){return}
  if(!j||!j.ok){setMsg('coLogMsg',(j&&j.error)||'That log couldn\'t be read.','bad');return}
  $('coLogView').hidden=false;$('coLogTitle').textContent=title;$('coLogSpd').textContent=spdLabel();setMsg('coLogMsg','');const C=j.counts||{};
  $('coLogRows').replaceChildren(...(j.rows||[]).filter(r=>CO2_EV[r.ev]).map(r=>{const tr=h('tr','ev-'+r.ev);
    tr.append(h('td','num',r.lap!=null?String(r.lap):''),h('td','num',ok(r.dist_m)?r.dist_m+' m':''),h('td','num',ok(r.kmh)?String(spdOf(r.kmh,'kmh')):''),
      h('td',null,CO2_EV[r.ev]),h('td',null,r.text||(r.ev==='lap_end'&&ok(r.lap_time)?fmtT(r.lap_time):r.corner||'')),h('td',null,co2Why(r)));return tr}));
  if(!$('coLogRows').children.length){const tr=h('tr'),td=h('td','note','Nothing was said or dropped in this session.');td.colSpan=6;tr.append(td);$('coLogRows').append(tr)}
  setMsg('coLogMsg',plural(C.spoken||0,'cue')+' said, '+(C.dropped||0)+' dropped, '+plural(C.laps||0,'lap')+'.')}
$('coLogCopy').addEventListener('click',async()=>{if(!co2.logName)return;let j;try{j=await api('coach/log?'+new URLSearchParams({name:co2.logName,summary:'1'}))}catch(e){return}
  if(!j||!j.ok){setMsg('coLogMsg',(j&&j.error)||'Couldn\'t make the summary.','bad');return}
  const done=await copyText(j.text);setMsg('coLogMsg',done?'Copied. Paste it wherever you\'re asking for help.':'Couldn\'t copy to the clipboard.',done?'ok':'bad')});
// help: Coach 2.0 and credits
HELP.tour.push(['Coach voice','The coach speaks with a natural voice, through the headset you pick on the Coach page (Voice and sound). Rehearse plays one of your sessions through it first.']);
HELP.words.push(['Voice pack','The recorded phrases the coach says. Six come with Debrief; you can add your own.'],
  ['Audio delay','How long Windows takes to start a sound. The coach starts each cue that much earlier, so it ends before your brake point.'],
  ['Coach log','Every cue the coach made, said or dropped and why. Copy summary puts a short summary on the clipboard.'],
  ['Fixed corner','A focus corner where you made a mistake and then drove it well twice in a row. The coach stops talking about it until it slips twice in three laps.'],
  ['Driving view','Coach page > Driving view: a big view to glance at from the car. COACHING, MUTED, REHEARSING or OFF in large type, your one thing and its count (finished laps only), the last 3 cues, this lap against your reference and where the reference lap is from. The sidebar folds away; Debrief remembers it on this PC.'],
  ['Mute','Mute stays on until you unmute or stop the coach; every start begins unmuted. While muted nothing is said, Repeat that included, except the answer when you ask for your pace.']);
HELP.keys.push(['Ctrl+Shift+R','Say the coach\'s last cue again'],['Ctrl+Shift+N','Mute the coach for 1 minute'],['Ctrl+Shift+P','Ask the coach how your pace is']);
// 2.18: Help > Credits is the list in debrief/credits.py (sent with /api/brand), the same as CREDITS.md. This short
// version shows only if that didn't load.
HELP.credits=[['Debrief','Made by Black Magik Studios. \u00a9 2026 Black Magik Studios.'],
  ['Coach voices','Made with Kokoro-82M by hexgrad, Apache-2.0 licence. The voices were generated once and come with Debrief as sound clips; Kokoro itself is not part of Debrief.'],
  ['Everything else','CREDITS.md and THIRD_PARTY_LICENSES.txt, in Debrief\'s program folder, list every part Debrief uses, who made it and its licence.']];
function creditsHelp(j){const L=Array.isArray(j&&j.credits)?j.credits.filter(c=>c&&typeof c.name==='string'):[];if(!L.length)return;
  const kinds=(j.kinds&&typeof j.kinds==='object')?j.kinds:{};const by=String(j.maker||BRAND0.maker);
  const out=[['Debrief','Made by '+by+'. '+(typeof j.copyright==='string'&&j.copyright?j.copyright+'.':'')]];
  L.forEach(c=>out.push([c.name+(kinds[c.kind]&&c.kind!=='bundled'?' ('+kinds[c.kind].toLowerCase()+')':''),
    [c.author,c.licence?'Licence: '+c.licence:'',c.use,c.link].filter(Boolean).map(x=>String(x).replace(/[.]$/,'')).join('. ')+'.']));
  out.push(['Licence texts','THIRD_PARTY_LICENSES.txt, in Debrief\'s program folder, holds the licence texts and notices these parts ask to be passed on.']);
  if(typeof j.trademarks==='string'&&j.trademarks)out.push(['Trademarks',j.trademarks]);
  HELP.credits=out}

/* ---------- Practice (2.13): every time you drove, from the telemetry files iRacing saves (read only). A session
   page: how it went, the best possible lap, the corner coach, the laps, fuel, conditions and setups */
let pr={data:null,sel:null,seq:0,more:false,doneId:null};
// 2.18: the shared scale (SURE) for the Corner coach's levels: clear = Sure, likely = Likely, thin = A hint
const CC_CONF={clear:['Sure','ok','Sure: passed the shuffle test in fewer than 1 in 100 shuffles, from 12 or more laps'],likely:['Likely','','Likely: passed the shuffle test in fewer than 1 in 20 shuffles'],thin:['A hint','no','A hint: only a few laps show it']};
const cap=t=>{t=String(t||'');return t.charAt(0).toUpperCase()+t.slice(1)};
const setupLetter=t=>{const m=/^Setup (\S+)/.exec(String(t||''));return m?m[1]:(t||'–')};
async function openPractice(id){current={view:'practice',dir:null};show('vPractice');markNav();pr.sel=id||null;showPrList(!pr.sel);await loadPractice()}
function showPrList(on){$('prList').hidden=!on;$('prOne').hidden=on}
async function loadPractice(){let j;try{j=await api('sessions-ibt')}catch(e){connectionLost();return}
  if(!j.ok){setMsg('prMsg',j.error||'Practice didn\'t load.','bad');return}
  pr.data=j;renderPrTel();if(j.running&&!polling)poll();
  if(pr.sel){openPrSession(pr.sel);return}
  renderPrList();ptLoad()}
function renderPrTel(){const j=pr.data;if(!j)return;const run=!!j.running||(lastJob&&lastJob.state==='running'&&lastJob.kind==='setuplab'),pill=$('prPill'),n=(j.sessions||[]).length;
  $('prFolderSet').hidden=!!j.folder_ok;$('prRead').disabled=!j.folder_ok||run;
  if(run){pill.className='pill';pill.textContent='Reading'}
  else if(!j.folder_ok){pill.className='pill no';pill.textContent='Folder not found'}
  else if(!j.indexed){pill.className='pill no';pill.textContent='Not read yet'}
  else{pill.className='pill ok';pill.textContent=plural(n,'session')}
  $('prTelNote').textContent=!j.folder_ok?'Debrief can\'t find your telemetry folder ('+(j.folder||'')+'). If iRacing saves telemetry somewhere else, set the folder in Settings.':
    !j.indexed?'Not read yet. The first read goes through every file, then only new ones. Debrief only reads them: it never changes iRacing\'s files.':
    plural(n,'session')+' from your telemetry files.'+(ok(j.unread)&&j.unread>0?' '+plural(j.unread,'new file')+' since the last read: press Read my telemetry to add them.':'');
  $('prNow').hidden=!run}
function prEmpty(host,title,text){const e=h('div','panel pvnone');e.append(h('h2','pvtrack',title),h('p','lede',text));host.replaceChildren(e)}
function renderPrList(){const j=pr.data,host=$('prSessions'),ss=(j&&j.sessions)||[];if(!j)return;
  if(!j.folder_ok){prEmpty(host,'Debrief can\'t find your telemetry folder','iRacing saves telemetry in Documents\\iRacing\\telemetry. If yours is somewhere else, set the folder in Settings > Setup Lab and Coach. '+TEL_HOW);return}
  if(!j.indexed&&j.unread===0&&!j.running){prEmpty(host,'No telemetry files yet',TEL_HOW+' Then press Read my telemetry.');return}
  if(!j.indexed){prEmpty(host,j.running?'Reading your telemetry...':'Not read yet','Press Read my telemetry. A few hundred files take a few minutes the first time; after that only new ones are read.');return}
  if(!ss.length){prEmpty(host,'No telemetry files yet',TEL_HOW+' Then press Read my telemetry.');return}
  const box=h('div','panel'),top=h('div','phead');top.append(h('h3',null,'Your sessions'),h('span','note','Newest first'));box.append(top);
  const ul=h('ul','prlist');ul.setAttribute('aria-label','Your sessions, newest first');
  (pr.more?ss:ss.slice(0,40)).forEach(x=>{const li=h('li'),b=h('button','prrow');b.type='button';b.dataset.id=x.id;
    const what=h('span','prwhat');what.append(h('b',null,x.track||x.track_code),h('span','sub2',x.car||x.car_path));
    const types=(x.session_types||[]).join(', ')||'Session';
    b.append(h('span','prwhen num',slWhen(x.date)),what,h('span','prtypes',types),h('span','prlaps num',(x.clean_laps||0)+' clean of '+plural(x.laps||0,'lap')),
      h('span','prbest num',ok(x.best)?fmtT(x.best):'No valid lap'));
    b.setAttribute('aria-label',types+' at '+(x.track||'')+' in the '+(x.car||'')+', '+slWhen(x.date)+', best '+(ok(x.best)?fmtT(x.best):'none'));
    b.addEventListener('click',()=>openPrSession(x.id));li.append(b);ul.append(li)});
  box.append(ul);
  if(ss.length>40&&!pr.more){const m=h('button','btn','Show all '+ss.length);m.type='button';m.addEventListener('click',()=>{pr.more=true;renderPrList()});box.append(m)}
  host.replaceChildren(box)}
$('prRead').addEventListener('click',async()=>{
  if(lastJob&&lastJob.state==='running'&&lastJob.kind!=='sync'){banner('Debrief is busy: '+(lastJob.title||TITLES[lastJob.kind]||'a job').toLowerCase()+'. Wait for it to finish.',true);return}
  setMsg('prMsg','');$('prRead').disabled=true;let r;try{r=await post('setuplab/read')}catch(e){connectionLost();return}
  if(!r.ok){$('prRead').disabled=false;setMsg('prMsg',r.error||'Could not start.','bad');return}
  lastJob={state:'running',kind:'setuplab',id:r.id};$('prNow').textContent='Starting...';if(pr.data)pr.data.running=true;renderPrTel();poll()});
$('prBack').addEventListener('click',()=>{pr.sel=null;pr.seq++;showPrList(true);renderPrList();$('prList').querySelector('h1').scrollIntoView({block:'start'})});
function prJob(s){if(s.state==='running'){$('prNow').textContent=s.message||'Reading...';if(current.view==='practice'&&pr.data&&!pr.data.running){pr.data.running=true;renderPrTel()}return}
  if(pr.doneId===s.id)return;pr.doneId=s.id;
  if(s.state==='error')setMsg('prMsg',s.error,'bad');else{const r=s.result||{};setMsg('prMsg',r.stopped?'Stopped. What was read is kept.':'Done: '+plural(r.files||0,'file')+' read.','ok')}
  if(pr.data)pr.data.running=false;
  if(current.view==='practice'&&!pr.sel)loadPractice();else renderPrTel()}
async function openPrSession(id){pr.sel=id;const seq=++pr.seq,host=$('prReport');showPrList(false);
  if(current.view!=='practice'){current={view:'practice',dir:null};show('vPractice');markNav()}
  prEmpty(host,'Reading this session...','A big session takes a second or two the first time.');host.classList.add('busy');
  let j;try{j=await api('session-review?'+new URLSearchParams({id}))}catch(e){connectionLost();return}
  if(seq!==pr.seq)return;host.classList.remove('busy');
  if(!j.ok){prEmpty(host,'That didn\'t load',j.error||'Try again.');return}
  renderPrSession(j);$('prOne').scrollIntoView({block:'start'})}
function prFacts(items){const dl=h('dl','factband');items.forEach(([k,v])=>{const d=h('div');d.append(h('dt',null,k),h('dd','num',v));dl.append(d)});return dl}
function ccBadge(c){const x=CC_CONF[c]||[String(c||''),'',''];const b=h('span','pill sure'+(x[1]?' '+x[1]:''),x[0]);b.title=x[2]||'How sure this is';return b}
/* 2.13 review: a corner reads "Turn 3". 2.18: everywhere (reviews too), so the "(C3)" beside it is gone; the
   hover says the numbering is Debrief's own */
function turnNum(x){const m=/^[CT](\d+)$/.exec(String(x||''));return m?+m[1]:null}
function turnName(x){const n=turnNum(x);return n!==null?'Turn '+n:String(x||'')}
function turnEl(tag,cls,name,id){const e=h(tag,cls,turnName(name));const n=turnNum(id||name);
  if(n!==null&&turnNum(name)!==null)e.title=TURN_NOTE;return e}
const TURN_NOTE='Debrief numbers the corners itself, from the speed trace, so Turn 3 here may not be the track map\u2019s Turn 3.';
function ccCard(c){const card=h('article','cccard'),top=h('div','cchead');
  // 2.18: a teammate's card says the gap between the two drivers (their lap against your typical laps)
  const ref=!!c.reference,ls=h('span','cclost num',ok(c.lost_s)?(ref&&c.gap_text?c.gap_text:'best laps '+c.lost_s.toFixed(2)+' s quicker'):'');
  if(ok(c.lost_s))ls.title=ref?cap(String(c.reference))+' was about '+c.lost_s.toFixed(2)+' s quicker through this corner than your typical laps. One lap of theirs, so treat it as a hint.':'Your best laps were about '+c.lost_s.toFixed(2)+' s quicker here than your typical ones (this corner\'s share of the gap between your quickest laps and the rest). Not a promise of lap time.';
  top.append(turnEl('h4',null,c.corner,c.id),ls,ccBadge(c.confidence));card.append(top);
  const row=(k,v,cls)=>{const p=h('p',cls||null);p.append(h('b',null,k+': '),document.createTextNode(cap(v)));return p};
  card.append(row('What\'s different',c.different),row('How to try it',c.how));if(c.gets)card.append(row('What it gets you',c.gets));
  if(c.why)card.append(row('Why',c.why,'note'));if(c.evidence)card.append(row('Evidence',c.evidence,'note'));
  return card}
function renderPrSession(j){const R=j.review||{},S=R.summary||{},C=j.corners||{},out=document.createDocumentFragment();
  const head=h('div','panel slhead'),tt=h('div','pvtt');
  tt.append(h('h2','pvtrack',R.track||R.track_code||''),h('p','pvby',(R.car||R.car_path||'')+' · '+slWhen(R.date)+' · '+((R.session_types||[]).join(', ')||'Session')));head.append(tt);
  const files=R.files||[];head.append(h('span','pill',plural(files.length,'file')));out.append(head);
  // 1. how it went
  const hw=slSec('How it went','prHead');const bl=S.best_lap||{};
  hw.append(prFacts([['Best lap',ok(S.best)?fmtT(S.best):'–'],['Typical lap',ok(S.median)?fmtT(S.median):'–'],['Clean laps',(S.clean_laps||0)+' of '+(S.laps||0)]]));
  if(ok(S.best))hw.append(h('p','note','Best on lap '+bl.lap+(bl.session?' ('+bl.session+')':'')+'. Typical lap: the middle of your clean laps.'));
  const cons=R.consistency||{};if(cons.text)hw.append(h('p',null,cons.text));
  if(R.history&&R.history.text)hw.append(h('p','prvs',R.history.text));
  out.append(hw);
  // 2.14: the practice plan scored on this session
  out.append(oneThingSec(j.one_thing,j.session));
  // 2. best possible lap
  const T=R.theoretical||{},bp=slSec('Best possible lap','prTheo');
  if(T.enough&&ok(T.possible)){bp.append(prFacts([['Best possible',fmtT(T.possible)],['Under your best',ok(T.gap_s)?T.gap_s.toFixed(2)+' s':'–']]));
    bp.append(h('p','note',T.text));
    const G=(T.gains||[]).filter(g=>ok(g.gain_s));if(G.length){bp.append(h('h4',null,'Where the time is'));const mx=Math.max(...G.map(g=>g.gain_s));
      const ul=h('ul','prbars');G.forEach(g=>{const li=h('li'),bar=h('span','prbar'),i=h('i');i.style.width=Math.max(3,Math.round(100*g.gain_s/mx))+'%';bar.append(i);
        li.append(turnEl('span','prseg',g.segment),bar,h('span','num',g.gain_s.toFixed(2)+' s'),h('span','sub2','best on lap '+g.from_lap));
        li.setAttribute('aria-label',turnName(g.segment)+': '+g.gain_s.toFixed(2)+' seconds, best on lap '+g.from_lap);ul.append(li)});bp.append(ul)}}
  else bp.append(h('p','note',T.text||'Not enough laps yet.'));
  const SP=R.segment_spread||{};if(SP.text)bp.append(h('p','note',SP.text));
  out.append(bp);
  // 3. corner coach
  const cc=slSec('Corner coach','prCorners'),cards=C.cards||[];
  const gu=(C.groups||[]).filter(g=>g.used).map(g=>String(g.label||'').replace(/ only$/,''));const grp=gu.length>1?gu.slice(0,-1).join(', ')+' and '+gu[gu.length-1]+', each compared only with its own kind':gu.join('');
  cc.append(h('p','note',C.ok?'From your last '+plural(C.laps||0,'clean lap')+' here ('+(grp?grp+', ':'')+plural(C.files||0,'telemetry file')+' of this car at this track). Your best laps through each corner against your typical ones; a tip only when it passes the shuffle test (Help explains).':(C.message||'Not enough clean laps here yet.')));
  if(cards.length){const box=h('div','cccards');cards.forEach(c=>box.append(ccCard(c)));cc.append(box);cc.append(h('p','note',TURN_NOTE))}
  else if(C.ok)cc.append(h('p',null,C.message||'No corner shows a clear pattern yet.'));
  const U=C.unclear||[];if(U.length){cc.append(h('h4',null,'No clear pattern yet'));const ul=h('ul','pvlist');U.forEach(u=>{const li=h('li','note'),t=String(u.text||''),m=/^[CT]\d+:\s*/.exec(t);
    if(m&&turnNum(u.corner)!==null){const sp=h('span');sp.append(turnEl('b',null,u.corner,u.id),': '+t.slice(m[0].length));li.append(sp)}
    else li.textContent=t||turnName(u.corner);ul.append(li)});cc.append(ul)}
  const fine=(C.fine||[]).map(f=>turnName(f.corner)).filter(Boolean);if(fine.length)cc.append(h('p','note','Already close to your best: '+fine.join(', ')+'.'));
  const BS=(C.summary&&C.summary.lines)||[];if(C.ok&&BS.length){cc.append(h('h4',null,'Braking habits'));const ul=h('ul','pvlist');BS.forEach(x=>ul.append(h('li',null,x)));cc.append(ul)}
  if(cards.length){const row=h('div','row'),b=h('button','btn primary','Coach these corners');b.type='button';b.id='prCoach';
    b.addEventListener('click',()=>openCoach({car_path:R.car_path,track_code:R.track_code}));
    row.append(b,h('span','note','The live coach says each corner\'s short tip while you drive.'));cc.append(row)}
  out.append(cc);
  // 4. laps
  const ls=slSec('Laps','prLaps'),L=R.laps||[];
  if(L.length){const wrap=h('div','tblwrap'),t=h('table','pacetbl prlaptbl'),hr=h('tr');['Lap','Time','Session','Counted','Fuel','Setup'].forEach(x=>hr.append(h('th',null,x)));
    const thd=h('thead');thd.append(hr);const tb=h('tbody');let st=null;
    L.forEach(r=>{const tr=h('tr');if(st!==null&&r.stint!==st)tr.className='newrun';st=r.stint;
      if(ok(S.best)&&r.valid&&Math.abs(r.time-S.best)<1e-6&&bl.lap===r.lap&&bl.file===r.file)tr.classList.add('best');
      const c0=h('th','num',String(r.lap));c0.scope='row';
      tr.append(c0,h('td','num',ok(r.time)?fmtT(r.time):'–'),h('td',null,r.session||''),
        h('td','prwhy'+(r.clean?' okc':''),r.clean?'Yes':cap(r.reason||'no')),h('td','num',ok(r.fuel_used)?r.fuel_used.toFixed(2)+' L':'–'),h('td',null,setupLetter(r.setup)));tb.append(tr)});
    t.append(thd,tb);wrap.append(t);ls.append(wrap);
    ls.append(h('p','note','Counted: a clean lap that shows your pace (not an out lap or first flying lap, not off track, no incident, not far off that run\'s pace). A line marks a new run.'))}
  else ls.append(h('p','note','No laps in these files.'));
  out.append(ls);
  // 5. fuel, conditions, setups
  const fs=slSec('Fuel and strategy','prFuel');fs.append(h('p',null,(R.fuel&&R.fuel.text)||'No fuel readings.'));out.append(fs);
  const cs=slSec('Conditions','prCond');cs.append(h('p',null,(R.conditions&&R.conditions.text)||'Not recorded.'));out.append(cs);
  const su=slSec('Setups used','prSetups'),SU=R.setups||[];
  if(SU.length){const ul=h('ul','pvlist');SU.forEach(x=>{const li=h('li');li.append(h('b',null,x.label||x.name||'Setup'),h('span','sub2',plural((x.files||[]).length,'file')));ul.append(li)});su.append(ul)}
  else su.append(h('p','note','No setup read from these files.'));
  const sb=h('button','linkbtn','Open Setup Lab for this car');sb.type='button';sb.id='prSetupLab';
  sb.addEventListener('click',()=>openSetupLab({car_path:R.car_path,track_code:R.track_code}));const sp=h('p');sp.append(sb);su.append(sp);out.append(su);
  // notes
  const notes=(R.notes||[]).filter(x=>typeof x==='string'&&x.trim()),bad=R.bad_files||[];
  if(notes.length||bad.length){const ns=slSec('Notes','prNotes');const ul=h('ul','pvlist');notes.forEach(x=>ul.append(h('li','note',x)));bad.forEach(b=>ul.append(h('li','note',b.file+': '+b.error)));ns.append(ul);out.append(ns)}
  $('prReport').replaceChildren(out);csSession(R)}
/* the Coach page shows the corners the corner coach would have it focus on (from Practice's button, else the newest
   car and track in the telemetry) */
async function loadCoPlan(plan){if(plan&&plan.car_path)co.plan=plan;let p=co.plan;
  if(!p){try{const j=await api('sessions-ibt');const x=j&&j.ok&&(j.sessions||[])[0];if(x)p={car_path:x.car_path,track_code:x.track_code}}catch(e){}}
  csCoachCombo(p);
  if(!p){co.planRep=null;co.focusPlan=null;renderCoPlan();renderCoFocus(co.st);return}
  const q=new URLSearchParams({car:p.car_path,track:p.track_code});
  let j;try{j=await api('corner-coach?'+q)}catch(e){return}
  co.planRep=j&&j.ok?j:null;renderCoPlan();
  let k=null;try{k=await api('plan?'+q)}catch(e){}
  co.focusPlan=k&&k.ok&&k.plan&&FOCUS_KINDS.includes(k.plan.kind)?k.plan:null;renderCoFocus(co.st)}
function renderCoPlan(){const box=$('coPlan'),j=co.planRep;box.replaceChildren();if(!j){box.hidden=true;return}
  const F=j.focus||[],R=j.report||{},where=(j.car||j.car_path)+' at '+(j.track||j.track_code);box.append(h('h4',null,'From the Corner coach'));
  if(F.length){box.append(h('p','note','For the '+where+', from your last '+plural(R.laps||0,'clean lap')+' there. It starts with the biggest and says the tip after the corner:'));
    const ul=h('ul','pvlist');F.forEach(f=>{const li=h('li'),nm=/^([CT]\d+):/.exec(String(f.phrase||''));li.append(nm?turnEl('b',null,nm[1],f.id):h('b',null,turnName(f.id)),
      h('span','sub2','Says "'+f.phrase+'"'+(ok(f.lost_s)?', best laps '+f.lost_s.toFixed(2)+' s quicker here':'')));ul.append(li)});box.append(ul)}
  else box.append(h('p','note','For the '+where+': '+(R.message||'no corner shows a clear pattern yet.')+' So the coach uses your race reviews.'));
  box.hidden=!!co.planHide}
function coPlanShow(on){co.planHide=!on;if(co.planRep)$('coPlan').hidden=!on}

/* ---------- Practice loop (2.14): one thing this session, scored afterwards, watched by the coach */
let pt={j:null,combo:null,seq:0};
const FOCUS_KINDS=['focus','consistency'];
// the plan's pill, on the shared scale: Sure, Likely, A hint, and A guess for an experiment (no clear pattern: try it and see)
const PT_CONF=Object.assign({},CC_CONF,{unclear:['A guess','no','A guess: no clear pattern in your laps here, so try it and see whether it helps']});
// "T4" -> "Turn 4" in window text (the spoken name stays T4 in the coach's own phrases)
const turnWords=t=>String(t||'').replace(/\b[TC](\d{1,2})\b(?!\.\w)/g,'Turn $1');
const VERDICT_PILL={worth:['Quicker','ok'],slower:['Not quicker yet','no'],unclear:['No clear difference',''],too_few:['Too few laps to tell',''],never:['Not measured','no']};
function ptQuery(){const j=pt.j;return j&&j.car_path?{car:j.car_path,track:j.track_code}:pt.combo?{car:pt.combo.car_path,track:pt.combo.track_code}:null}
async function ptLoad(){const seq=++pt.seq,q=pt.combo?'?'+new URLSearchParams({car:pt.combo.car_path,track:pt.combo.track_code}):'';
  if(!pt.j)ptRenderBusy('Picking one thing for your next session...');
  let j;try{j=await api('plan'+q)}catch(e){connectionLost();return}
  if(seq!==pt.seq)return;pt.j=j;ptRender()}
function ptRenderBusy(t){const box=$('prPlan');box.hidden=false;$('ptBody').replaceChildren(h('p','note',t));$('ptActs').hidden=true;$('ptLast').replaceChildren();$('ptHist').hidden=true;$('ptConf').textContent=''}
function hitDots(rows){const d=h('span','ptdots');(rows||[]).forEach(r=>{if(r.hit==null)return;const i=h('i',r.hit?'hit':'miss');i.title='Lap '+r.lap+': '+(r.hit?'hit':'missed')+(r.value!=null?' ('+r.value+')':'');d.append(i)});d.setAttribute('aria-hidden','true');return d}
function scoreBlock(plan,next){const sc=plan&&plan.score;const box=h('div','ptscore');if(!sc)return box;
  const top=h('div','pthead');top.append(h('b','ptres',sc.text||''));const vp=VERDICT_PILL[sc.verdict];if(vp)top.append(h('span','pill'+(vp[1]?' '+vp[1]:''),vp[0]));box.append(top);
  const dots=hitDots(sc.per_lap);if(dots.children.length)box.append(dots);
  if(sc.verdict_text)box.append(h('p','note',turnWords(sc.verdict_text)));
  const act=(sc.next||{}).action;let nx='';
  if(act==='keep')nx='Next: keep this until it is a habit (7 laps out of 10).';
  else if(act==='next')nx=next&&next.corner&&next.corner!==plan.corner?'Next: move to '+turnWords(next.corner)+'.':'Next: move on to the next thing.';
  if(nx)box.append(h('p','ptnext',nx));return box}
function ptBodyEl(P){const f=document.createDocumentFragment();
  f.append(h('p','ptmain',turnWords(P.text)));
  const row=(k,v,cls)=>{if(!v)return;const q=h('p',cls||null);q.append(h('b',null,k+': '),document.createTextNode(turnWords(cap(v))));f.append(q)};
  if(FOCUS_KINDS.includes(P.kind)){row('How',P.how);row('The check',P.check);
    const meta=h('p','note ptmeta');meta.append(document.createTextNode('From '+plural(P.from_laps||0,'lap')+'.'+(P.evidence?' '+P.evidence:'')));f.append(meta);
    if(P.reason)f.append(h('p','note',turnWords(P.reason)))}
  else if(P.check)f.append(h('p','note',P.check));
  return f}
function ptRender(){const j=pt.j,box=$('prPlan');box.hidden=false;const body=$('ptBody');
  const sel=$('ptCombo'),C=(j&&j.combos)||[];$('ptComboWrap').hidden=C.length<2;
  if(C.length>1){sel.replaceChildren(...C.map((c,i)=>{const o=h('option',null,(c.track||c.track_code)+' · '+(c.car||c.car_path));o.value=String(i);
    if(j.car_path&&c.car_path===j.car_path&&c.track_code===j.track_code)o.selected=true;return o}))}
  const pill=$('ptConf');pill.className='pill';pill.textContent='';
  if(!j||!j.ok){body.replaceChildren(h('p','note',(j&&j.error)||'The plan didn\'t load.'));$('ptActs').hidden=true;$('ptLast').replaceChildren();$('ptHist').hidden=true;return}
  const P=j.plan;$('ptLast').replaceChildren();
  if(!P){body.replaceChildren(h('p','note',j.message||'Nothing to plan yet.'));$('ptActs').hidden=true;$('ptHist').hidden=true;return}
  const where=h('p','note ptwhere',(j.track||'')+' · '+(j.car||''));
  body.replaceChildren(where,ptBodyEl(P));
  const focus=FOCUS_KINDS.includes(P.kind);$('ptActs').hidden=!focus;$('ptCoach').disabled=false;$('ptAnother').disabled=false;
  if(focus){const c=PT_CONF[P.confidence]||null;if(c){pill.className='pill sure'+(c[1]?' '+c[1]:'');pill.textContent=c[0];pill.title=c[2]}}
  const L=j.last;if(L&&L.score){const lb=h('div','ptlast');lb.append(h('h4',null,'Last session'+(L.score.session_date?', '+slWhen(L.score.session_date):'')),h('p','note','The one thing: '+turnWords(L.text)),scoreBlock(L,P));$('ptLast').replaceChildren(lb)}
  const H=(j.history||[]).filter(x=>x.score);const hl=$('ptHistList');$('ptHist').hidden=!H.length;
  hl.replaceChildren(...H.map(x=>{const li=h('li');const sc=x.score;li.append(h('span','num pthd',fmtDate(String(sc.session_date||x.made||'').slice(0,10))),h('b',null,turnWords(x.corner||'')||'Laps'),
    h('span','sub2',turnWords(x.text)),h('span','num pthr',sc.measured?sc.hits+' of '+sc.measured:'not measured'));return li}));tdPointer('ptLast')}
$('ptCombo').addEventListener('change',()=>{const C=(pt.j&&pt.j.combos)||[];pt.combo=C[+$('ptCombo').value]||null;pt.j=null;ptLoad()});
$('ptAnother').addEventListener('click',async()=>{const q=ptQuery();if(!q)return;$('ptAnother').disabled=true;setMsg('ptMsg','Picking another...');let r;
  try{r=await post('plan/new',q)}catch(e){connectionLost();return}
  $('ptAnother').disabled=false;if(!r.ok){setMsg('ptMsg',r.error||'That didn\'t work.','bad');return}
  const before=pt.j&&pt.j.plan&&pt.j.plan.id;pt.j=r;ptRender();
  setMsg('ptMsg',r.plan&&r.plan.id===before?'Nothing else stands out yet, so it\'s the same corner.':'A different one from your latest laps.','ok')});
$('ptCoach').addEventListener('click',()=>{const j=pt.j;if(!j||!j.plan)return;openCoach({car_path:j.car_path,track_code:j.track_code})});
// the session page's "Your one thing": the plan scored on this session
function oneThingSec(o,S){const sec=slSec('Your one thing','prOneThing');
  const coachBox=h('div','prcoach');coachBox.id='prCoachRes';
  if(!o||!o.plan){sec.append(h('p','note','No plan was scored on this session. The Practice page picks one thing for your next session at this track.'),coachBox);prCoachResult(coachBox,S,null);return sec}
  const P=o.plan;sec.append(h('p','ptmain',turnWords(P.text)));if(P.check)sec.append(h('p','note',turnWords(P.check)));
  sec.append(scoreBlock(P,o.next_plan));
  if(o.just_scored)sec.append(h('p','note','Scored just now from this session. The Practice page shows your next one thing.'));
  sec.append(coachBox);prCoachResult(coachBox,S,P);
  return sec}
// 2.18: what the live coach counted for this one thing, what counts as done, and the lap the coach measures you
// against here (named and dated as this list names it)
async function prCoachResult(box,S,P){if(!S||!S.car_path||!S.track_code)return;let j;
  try{j=await api('coach/result?'+new URLSearchParams({car:S.car_path,track:S.track_code,plan:(P&&P.plan_id)||''}))}catch(e){return}
  if(!j||!j.ok||!box.isConnected)return;const L=[];
  if(P){const r=j.result;L.push(h('p','prcoachres',(r?r.text+' ':'')+(j.done_text||'')+'.'));
    if(!r)L.push(h('p','note','The coach counts it live while you drive with it watching this plan (Coach this). No live count was kept for this plan.'))}
  const R=j.reference;if(R)L.push(h('p','note','The coach measures you here against '+R.lap_text+', your best clean lap in your telemetry files'+(R.when?' ('+slWhen(R.when)+')':'')+'.'));
  box.replaceChildren(...L)}
// the Coach page's "This session's one thing": live progress while coaching, else the plan it will watch
// 2.17.2 speeds on the Coach page are in mph (the owner's choice): km/h numbers from the coach and older logs are shown in mph
const KM_MI=1.609344;const mphOf=k=>spdOf(k,'kmh');   // 2.18: follows Settings > Units (the name is from 2.17.2)
function mphText(s){return String(s==null?'':s).replace(/(-?\d+(?:\.\d+)?) km\/h/g,(m,n)=>spdOf(+n,'kmh')+' '+spdLabel())}
function renderCoFocus(st){const box=$('coFocus');if(!box)return;const F=st&&st.focus,P=co.focusPlan,run=!!(st&&st.running);
  const pill=$('coFocusPill');pill.className='pill';
  if(F&&(run||(F.measured&&(!P||!F.plan_id||F.plan_id===P.plan_id)))){box.hidden=false;$('coFocusText').textContent=mphText(turnWords(F.text||''));$('coFocusProg').textContent=F.progress||'';
    pill.textContent=run?'Watching':'Last drive';if(run)pill.className='pill ok';
    $('coFocusNote').textContent=run?'After each clean pass through '+turnWords(F.corner||'the corner')+' the coach checks it and now and then says how it went.':'From your last drive with the coach.';return}
  if(P){box.hidden=false;$('coFocusText').textContent=mphText(turnWords(P.text||''));$('coFocusProg').textContent=run?'Watching for this car and track.':'Not started';pill.textContent='';
    $('coFocusNote').textContent='Start coaching and drive: the coach measures '+turnWords(P.corner||'the corner')+' each clean lap and keeps count. '+mphText(turnWords(P.check||''));return}
  box.hidden=true}
// wheel button mute
function renderWheel(w){if(!w)return;const learning=w.state==='learning';
  $('coWheelText').textContent=(w.text||'')+(w.note&&!learning?' '+w.note:'');
  $('coWheelLearn').disabled=learning;$('coWheelLearn').textContent=learning?'Press a button now...':'Learn';$('coWheelClear').disabled=!w.binding&&!learning}
$('coWheelLearn').addEventListener('click',async()=>{setMsg('coWheelMsg','');let r;try{r=await post('coach/wheel-learn')}catch(e){connectionLost();return}
  if(!r.ok){setMsg('coWheelMsg',r.error||'That didn\'t work.','bad');if(r.wheel)renderWheel(r.wheel);return}
  if(r.wheel)renderWheel(r.wheel);coachPoll()});
$('coWheelClear').addEventListener('click',async()=>{let r;try{r=await post('coach/wheel-clear')}catch(e){connectionLost();return}
  if(!r.ok){setMsg('coWheelMsg',r.error||'That didn\'t work.','bad');return}setMsg('coWheelMsg','');if(r.wheel)renderWheel(r.wheel)});

/* ---------- first-run checklist (2.14): on the start page until every step is done or it is hidden */
let fr={data:null,force:false,at:0};
const FR_ACT={settings_token:['Open Settings',()=>{if(!$('vWelcome').hidden&&guideOn){const g=$('gs3');if(g&&g.scrollIntoView)g.scrollIntoView({block:'start'});$('gTok').focus();return}openSettings()}],
  settings_folder:['Open Settings',()=>{openSettings();const f=$('fTelemetry');if(f&&f.scrollIntoView)f.scrollIntoView({block:'start'})}],
  read_telemetry:['Read my telemetry',()=>{openSetupLab();const b=$('slRead');if(b&&!b.disabled)b.click()}],
  practice:['Open Practice',()=>goNav('practice')],
  // 2.18: Test voice is folded under the Coach page's Settings: open it there and press it, so you hear it at once
  test_voice:['Test voice',()=>{openCoach();const d=$('coSet');if(d)d.open=true;const b=$('coTest');if(!b)return;
    if(b.scrollIntoView)b.scrollIntoView({block:'center'});b.focus();if(!b.disabled)b.click()}]};
async function loadFirstRun(){fr.at=Date.now();let j;try{j=await api('firstrun')}catch(e){return}if(!j||!j.ok)return;fr.data=j;renderFirstRun()}
// 2.18: setup counts as done once there is something of yours to show (a Garage 61 token, or a session with 5 clean
// laps): today.setup_done. After that the box is gone and any step left is one quiet line on Today.
const setupDone=j=>!!(td.j&&td.j.setup?td.j.setup_done:j&&(j.all_done||(j.steps||[]).some(x=>x.done&&(x.id==='token'||x.id==='session'))));
function renderFirstRun(){const box=$('firstRun'),j=fr.data;if(!box)return;
  // 2.18: Today shows the steps (before any data they are the page); Welcome shows them too while Garage 61 is being
  // set up, so the telemetry steps show from the first minute
  const where=current.view==='today'?'today':!$('vWelcome').hidden?'wel':null;
  if(where&&Date.now()-fr.at>15000)loadFirstRun();
  // before any data (no review, no telemetry session) the steps are the page, whatever is done already
  const hasData=td.j&&td.j.ok?!!td.j.has_data:groupsCache.some(g=>(g.reviews||[]).length);
  const done=setupDone(j)&&hasData;
  const want=!!(j&&where&&(fr.force||(j.show&&!done)));box.hidden=!want;
  const line=$('tdSetupLine');
  if(line){const nx=j&&(j.steps||[]).find(x=>x.id===j.next);const quiet=!!(j&&where==='today'&&!want&&j.show&&done&&nx);line.hidden=!quiet;
    if(quiet){const left=(j.total||0)-(j.done||0);const a=FR_ACT[nx.action];line.replaceChildren(h('span',null,'Setup: '+plural(left,'step')+' left. Next: '+nx.title+'.'));
      if(a){const b=h('button','linkbtn',a[0]);b.type='button';b.addEventListener('click',a[1]);line.append(b)}
      const x=h('button','linkbtn','Hide');x.type='button';x.addEventListener('click',()=>$('frHide').click());line.append(x)}}
  // before any data Today is the setup steps: the #1 card and the last race and practice wait for something to show
  const noData=where==='today'&&td.j&&td.j.ok&&!td.j.has_data;$('tdOne').hidden=!!noData;document.querySelector('#vToday .tdgrid').hidden=!!noData;
  $('tdLede').textContent=noData?'A few steps and Debrief is ready. Your #1 thing to work on shows here once you have driven a few laps with telemetry on, or reviewed a race.':'Your one thing to work on, your last race and your last practice.';
  if(!want)return;
  if(where==='wel'){const host=$('vWelcome').querySelector('.col');
    // with the Garage 61 steps showing, the checklist goes above them and leaves its own Garage 61 step to them
    if(guideOn){if(box.nextElementSibling!==$('guideHead'))host.insertBefore(box,$('guideHead'))}else if(box.parentNode!==host||box.nextElementSibling)host.append(box)}
  else if(box.parentNode!==$('tdSetupHost'))$('tdSetupHost').append(box);
  const steps=(j.steps||[]).filter(st=>!(where==='wel'&&guideOn&&st.id==='token'));
  const nDone=steps.filter(st=>st.done).length;
  $('frTitle').textContent=where==='wel'&&guideOn?'Start with your telemetry':'Getting set up';
  $('frPill').textContent=nDone+' of '+steps.length+' done';$('frPill').className='pill'+(nDone===steps.length?' ok':'');
  $('frList').replaceChildren(...steps.map(st=>{const li=h('li','frstep'+(st.done?' done':'')+(st.id===j.next?' now':''));
    const mk=h('span','frtick',st.done?'\u2713':'');mk.setAttribute('aria-hidden','true');
    const tx=h('div','frtx');tx.append(h('b',null,st.title),h('span','note',st.text));li.append(mk,tx);
    li.setAttribute('aria-label',(st.done?'Done: ':'To do: ')+st.title);
    const a=FR_ACT[st.action];if(!st.done&&a){const b=h('button','btn'+(st.id===j.next?' primary':''),a[0]);b.type='button';b.dataset.fr=st.action;b.addEventListener('click',a[1]);li.append(b)}
    return li}))}
$('frHide').addEventListener('click',async()=>{let r;try{r=await post('firstrun/dismiss')}catch(e){connectionLost();return}
  if(r&&r.ok){fr.data=r;fr.force=false;renderFirstRun();banner('Hidden. Help > Setup checklist shows it again.')}});
$('helpFirstRun').addEventListener('click',async()=>{let r;try{r=await post('firstrun/dismiss',{dismissed:false})}catch(e){connectionLost();return}
  if(!r||!r.ok)return;fr.data=r;fr.at=Date.now();fr.force=true;closeDlg($('dlgHelp'));
  if(current.view!=='today'&&$('vWelcome').hidden){if(!onboarding())openToday();else{current={view:'welcome'};welcome()}}
  renderFirstRun()});

/* ---------- settings */
// "New here?" once, only for someone new: never with saved reviews or telemetry sessions (2.18)
let started=false;
function tourHint(){if(tourHint.done||!g61Ready())return;tourHint.done=true;
  if(groupsCache.some(g=>(g.reviews||[]).length)||(td.j&&td.j.has_data)||(fr.data&&(fr.data.steps||[]).some(x=>x.id==='session'&&x.done)))return;
  let seen=true;try{seen=localStorage.getItem('debrief.tourSeen')==='1';if(!seen)localStorage.setItem('debrief.tourSeen','1')}catch(e){}
  if(!seen)banner('New here? Press Help (or F1) for a quick tour and what the words mean.',false,9000)}
function setMsg(id,t,cls){const e=$(id);e.textContent=plainText(t)||t||'';e.className='msg'+(cls?' '+cls:'')}
// 2.18 Settings > Units: one speed unit everywhere (the server writes its sentences in it; numbers the window
// formats itself go through spdOf). Debrief's own numbers are km/h on the telemetry pages, mph in reviews.
let UNITS='mph';
const spdLabel=()=>UNITS==='kmh'?'km/h':'mph';
function spdOf(v,from){if(!ok(+v))return '';const x=+v;const f=from==='kmh'?'kmh':'mph';const y=f===UNITS?x:(f==='mph'?x*1.609344:x/1.609344);return Math.round(y)}
function fillSettings(v){if(v&&(v.units==='kmh'||v.units==='mph'))UNITS=v.units;
  if(v&&$('unitsSel')&&document.activeElement!==$('unitsSel'))$('unitsSel').value=UNITS;
  tokenSaved=!!v.token_saved;tokenRejected=!!v.token_rejected;reviewGate();
  firstName=typeof v.first_name==='string'&&v.first_name?v.first_name:null;installed=!!v.installed;greet();
  $('aboutVersion').textContent='Debrief '+(v.version||'');
  $('aboutText').textContent=(installed?'Installed app. ':'Running from source. ')+'Your settings, the Garage 61 data Debrief has saved, and its log (debrief.log) are in this folder:';
  $('dataPath').textContent=v.data_folder||'';
  $('secretsNote').textContent=v.secrets_at_rest==='dpapi'?'Your Garage 61 token and Discord webhook are stored there encrypted to your Windows account (DPAPI), so a copied settings.toml doesn\'t hold them in the clear.':'Your Garage 61 token and Discord webhook are stored there in settings.toml. Keep that file private.';
  if(typeof v.verification_note==='string'&&v.verification_note)$('updVerify').textContent=v.verification_note;
  if(guideOn)updateGuide();
  // 2.7.1 (M1): a token saved encrypted to another Windows account (or damaged) is not "no token saved yet"
  const unread=!v.token_saved&&!!v.token_unreadable;$('tokUnread').hidden=!unread;$('gTokUnread').hidden=!unread;
  const st=$('tokStatus');st.textContent=v.token_saved?'Token saved (ends in '+v.token_hint+')':(unread?'Saved token can\'t be opened':'No token saved yet');
  st.className='pill '+(v.token_saved?'ok':'no');
  $('refs').value=String(v.max_reference_laps);$('prac').checked=!!v.include_practice;
  if(v.token_saved&&started)tourHint();
  racecapOn=!!v.racecap_library;$('rcapPill').textContent=racecapOn?'On':'Off';$('rcapPill').className='pill'+(racecapOn?' ok':'');
  if(document.activeElement!==$('rcapFolder'))$('rcapFolder').value=v.racecap_library||'';
  if(document.activeElement!==$('telFolder'))$('telFolder').value=v.iracing_folder||'';
  $('telPath').textContent=v.telemetry_folder?'Reading: '+v.telemetry_folder:'';
  $('telPill').textContent=v.telemetry_folder_ok?'Found':'Not found';$('telPill').className='pill '+(v.telemetry_folder_ok?'ok':'no');
  if(document.activeElement!==$('salut'))$('salut').value=typeof v.salutation==='string'?v.salutation:'';
  $('syncOpen').checked=v.sync_on_open!==false;$('updAuto').checked=v.check_updates!==false;$('syncTel').checked=v.sync_telemetry!=='none';
  $('libPath').textContent=v.library_folder||'Not set yet. It will be Documents\\Debrief Reviews once settings are saved.';
  const fe=$('setFileErr');fe.hidden=!v.error;fe.textContent=v.error||'';fe.className='note msg bad';
  teamSlug=v.team_slug||teamSlug;
  const idS=!!v.iracing_client_id_saved,seS=!!v.iracing_client_secret_saved;
  $('irIdStatus').textContent='Client ID: '+(idS?'saved':'not saved');$('irIdStatus').className='pill'+(idS?' ok':'');
  $('irSecStatus').textContent='Client secret: '+(seS?'saved':'not saved');$('irSecStatus').className='pill'+(seS?' ok':'');
  $('irClear').hidden=!(idS||seS);
  if(document.activeElement!==$('cardsFolder'))$('cardsFolder').value=typeof v.team_cards_folder==='string'?v.team_cards_folder:'';
  $('cardsShare').checked=!!v.share_my_cards;cardsStatus(v);
  discordSaved=!!v.discord_saved;discordHint=typeof v.discord_hint==='string'?v.discord_hint:null;
  const dUnread=!discordSaved&&!!v.discord_unreadable;$('discordUnread').hidden=!dUnread;
  const dp=$('discordPill');dp.textContent=discordSaved?'Webhook saved ('+(discordHint||'…')+')':(dUnread?'Can\'t be opened':'Off');dp.className='pill'+(discordSaved?' ok':(dUnread?' no':''));
  $('discordCheck').hidden=!discordSaved;$('discordClear').hidden=!discordSaved;if(!discordSaved)$('discordWhere').hidden=true;
  frameBar();
}
function cardsStatus(v){const p=$('cardsPill'),st=$('cardsStatus');const f=v.team_cards_folder;
  if(!f){p.textContent='Off';p.className='pill';st.textContent='No shared folder picked.';return}
  if(!v.team_cards_folder_ok){p.textContent='Folder missing';p.className='pill no';st.textContent='Debrief can\'t find '+f+' right now. Is your sync app running?';return}
  if(!v.share_my_cards){p.textContent='Reading';p.className='pill ok';st.textContent='Showing teammates\' cards from '+f+'. Your cards stay on this PC.';return}
  p.textContent='Sharing';p.className='pill ok';
  st.textContent=v.share_as?'Sharing as '+v.share_as+' to '+f:'Check your Garage 61 connection once so Debrief knows your name. Until then your cards stay on this PC.'}
$('fCards').addEventListener('submit',async e=>{e.preventDefault();setMsg('cardsMsg','Saving...');let r;
  try{r=await post('settings',{team_cards_folder:$('cardsFolder').value.trim(),share_my_cards:$('cardsShare').checked})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('cardsMsg',r.error,'bad');return}
  fillSettings(r.settings);setMsg('cardsMsg','Saved.','ok')});
$('fDiscord').addEventListener('submit',async e=>{e.preventDefault();const inp=$('discordHook');const v=inp.value.trim();
  if(!v){setMsg('discordMsg','Paste the webhook address first.','bad');inp.focus();return}
  setMsg('discordMsg','Saving...');let r;try{r=await post('settings',{discord_webhook:v})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('discordMsg',r.error,'bad');return}
  inp.value='';$('discordWhere').hidden=true;fillSettings(r.settings);setMsg('discordMsg','Saved. Press Check to see where posts go.','ok')});
$('discordCheck').addEventListener('click',async()=>{setMsg('discordMsg','Checking with Discord...');$('discordCheck').disabled=true;let r;
  try{r=await post('discord-check')}catch(e){connectionLost();return}finally{$('discordCheck').disabled=false}
  if(!r.ok){setMsg('discordMsg',r.error,'bad');return}
  const w=$('discordWhere');w.hidden=false;
  w.textContent='Posts go to the webhook '+(r.name?'\u201c'+r.name+'\u201d':'(no name)')+(typeof r.channel_id==='string'&&r.channel_id?' in channel '+r.channel_id.slice(0,4)+'…':'')+'.';
  setMsg('discordMsg','The webhook works.','ok')});
$('discordClear').addEventListener('click',async()=>{const b=$('discordClear');
  if(b.dataset.armed!=='1'){b.dataset.armed='1';b.textContent='Confirm remove';setTimeout(()=>{b.dataset.armed='';b.textContent='Remove'},4000);return}
  let r;try{r=await post('settings',{clear_discord:true})}catch(e){connectionLost();return}
  if(!r.ok){setMsg('discordMsg',r.error,'bad');return}
  b.dataset.armed='';b.textContent='Remove';fillSettings(r.settings);setMsg('discordMsg','Removed.','ok')});
let teamSlug=null;   // 2.6: from /api/brand (Midfield Menace), or settings.toml's team_slug override
async function loadSettings(){try{fillSettings(await api('settings'))}catch(e){connectionLost()}}
// Settings > Team is a read-only status: Midfield Menace, member or not. The override is under Advanced.
let teamMember='unknown',teamName=null;
function teamJoinPanel(){const isBrand=!teamSlug||String(teamSlug).toLowerCase()===String(brand.team_slug).toLowerCase();
  $('teamJoin').hidden=!(notMember(weeklyInfo)||(teamMember==='not_member'&&isBrand))}
async function loadTeams(fetchIt){let j;try{j=await api('teams'+(fetchIt?'?fetch=1':''))}catch(e){connectionLost();return}
  if(!j.ok){setMsg('teamSetMsg',j.error,'bad');return}
  teamSlug=j.team_slug||teamSlug||brand.team_slug;
  const teams=Array.isArray(j.teams)?j.teams.filter(t=>t&&typeof t.slug==='string'):null;
  const cur=teams&&teams.find(t=>t.slug.toLowerCase()===String(teamSlug).toLowerCase());
  teamMember=typeof j.membership==='string'?j.membership:(teams?(cur?'member':'not_member'):'unknown');
  const isBrand=String(teamSlug).toLowerCase()===String(brand.team_slug).toLowerCase();
  const name=(j.team&&typeof j.team.name==='string'&&j.team.name&&j.team.name.toLowerCase()!==String(teamSlug).toLowerCase()&&j.team.name)||(cur&&cur.name)||(isBrand?brand.team_name:teamSlug);
  teamName=name;$('teamTitle').textContent=name;teamJoinPanel();
  const state=teamMember==='member'?'member':teamMember==='not_member'?'not on the team yet (join on Garage 61)':'not checked yet';
  $('teamLine').textContent='Team: '+name+' — '+state;
  const st=$('teamStatus');st.textContent=teamMember==='member'?'Member':teamMember==='not_member'?'Not on the team yet':name;
  st.className='pill'+(teamMember==='member'?' ok':teamMember==='not_member'?' no':'');
  const jn=$('teamJoinNote');jn.hidden=teamMember!=='not_member'||!isBrand;jn.textContent=brand.join_text;
  if(document.activeElement!==$('teamSlugIn'))$('teamSlugIn').value=teamSlug||'';
  const dl=$('dlTeams');dl.replaceChildren(...(teams||[]).map(t=>{const o=h('option');o.value=t.slug;o.label=t.name;return o}));
  $('teamReset').hidden=isBrand;
  $('btnTeams').hidden=!j.token;$('btnTeams').textContent=teams?'Check again':'Check my teams';
  if(!j.token)setMsg('teamSetMsg','Save your Garage 61 token first.');
  else if(fetchIt)setMsg('teamSetMsg','Teams loaded.','ok');
  else setMsg('teamSetMsg','');
}
$('btnTeams').addEventListener('click',()=>{setMsg('teamSetMsg','Loading...');loadTeams(true)});
async function saveTeam(v){setMsg('teamSetMsg','Saving...');let r;
  try{r=await post('settings',{team_slug:v})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('teamSetMsg',r.error,'bad');return}
  fillSettings(r.settings);await loadTeams(false);setMsg('teamSetMsg','Saved. Refresh the Team hub to see the new team.','ok')}
$('fTeam').addEventListener('submit',e=>{e.preventDefault();const v=$('teamSlugIn').value.trim().toLowerCase();if(!v){$('teamSlugIn').focus();return}saveTeam(v)});
$('teamReset').addEventListener('click',()=>saveTeam(brand.team_slug));
$('fIracing').addEventListener('submit',async e=>{e.preventDefault();
  const id=$('irId').value.trim(),sec=$('irSecret').value.trim();
  if(!id&&!sec){setMsg('irMsg','Paste a client ID or secret first.','bad');return}
  setMsg('irMsg','Saving...');let r;try{r=await post('settings',{iracing_client_id:id,iracing_client_secret:sec})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('irMsg',r.error,'bad');return}
  $('irId').value='';$('irSecret').value='';fillSettings(r.settings);setMsg('irMsg','Saved.','ok')});
$('irClear').addEventListener('click',async()=>{const b=$('irClear');
  if(b.dataset.armed!=='1'){b.dataset.armed='1';b.textContent='Confirm remove';setTimeout(()=>{b.dataset.armed='';b.textContent='Remove'},4000);return}
  let r;try{r=await post('settings',{clear_iracing:true})}catch(e){connectionLost();return}
  if(!r.ok){setMsg('irMsg',r.error,'bad');return}
  b.dataset.armed='';b.textContent='Remove';fillSettings(r.settings);setMsg('irMsg','Removed.','ok')});
// 2.6: one "Save and check" button: saves a pasted token, then checks the connection (an empty box just
// checks the saved token)
$('fToken').addEventListener('submit',async e=>{e.preventDefault();
  const inp=$('tok');const v=inp.value.trim();
  if(!v){if(tokenSaved){startCheck();return}setMsg('tokMsg','Paste your token in the box first.','bad');inp.focus();return}
  setMsg('tokMsg','Saving...');setMsg('chkMsg','');let r;
  try{r=await post('settings',{token:v})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('tokMsg',r.error,'bad');return}
  inp.value='';checkOk=false;fillSettings(r.settings);await loadReviews();loadTeams(false);
  setMsg('tokMsg','Saved.','ok');startCheck();
});
$('fGuideToken').addEventListener('submit',async e=>{e.preventDefault();
  const inp=$('gTok');const v=inp.value.trim();
  if(!v){setMsg('gTokMsg','Paste your token in the box first.','bad');inp.focus();return}
  setMsg('gTokMsg','Saving...');$('gTokSave').disabled=true;let r;
  try{r=await post('settings',{token:v})}catch(err){connectionLost();return}finally{$('gTokSave').disabled=false}
  if(!r.ok){setMsg('gTokMsg',r.error,'bad');inp.focus();return}
  inp.value='';checkOk=false;fillSettings(r.settings);await loadReviews();loadTeams(false);
  setMsg('gTokMsg','Token saved. Checking the connection...','ok');updateGuide();
  startCheck()});
$('fOpts').addEventListener('submit',async e=>{e.preventDefault();setMsg('optMsg','Saving...');let r;
  const sal=$('salut').value.replace(/\s+/g,' ').trim();
  if(sal.length>20){setMsg('optMsg','Keep how Debrief addresses you to 20 characters or fewer.','bad');$('salut').focus();return}
  if(/[<>]/.test(sal)){setMsg('optMsg','How Debrief addresses you can\'t contain < or >.','bad');$('salut').focus();return}
  try{r=await post('settings',{max_reference_laps:+$('refs').value,include_practice:$('prac').checked,salutation:sal})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('optMsg',r.error,'bad');return}
  fillSettings(r.settings);setMsg('optMsg','Saved.','ok')});
$('fUnits').addEventListener('submit',async e=>{e.preventDefault();setMsg('unitsMsg','Saving...');let r;
  try{r=await post('settings',{units:$('unitsSel').value==='kmh'?'kmh':'mph'})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('unitsMsg',r.error,'bad');return}
  fillSettings(r.settings);setMsg('unitsMsg','Saved. Pages and the coach use '+spdLabel()+' from now on; an open review shows it when you open it again.','ok')});
$('fSync').addEventListener('submit',async e=>{e.preventDefault();setMsg('syncOptMsg','Saving...');let r;
  try{r=await post('settings',{sync_on_open:$('syncOpen').checked,sync_telemetry:$('syncTel').checked?'best':'none'})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('syncOptMsg',r.error,'bad');return}
  fillSettings(r.settings);setMsg('syncOptMsg','Saved.','ok')});
$('btnFolder').addEventListener('click',async()=>{let r;try{r=await post('open-folder')}catch(e){connectionLost();return}
  setMsg('folderMsg',r.ok?'Opened in File Explorer.':r.error,r.ok?'ok':'')});

/* ---------- diagnostics (About > Copy diagnostics) */
function fmtBytes(n){if(!ok(n))return 'unknown';if(n<1024*1024)return Math.round(n/1024)+' KB';return (n/1048576).toFixed(1)+' MB'}
function diagText(d){const L=[];
  L.push('Debrief '+(d.version||'?')+' ('+(d.frozen?'installed':'from source')+')');
  L.push('Platform: '+(d.platform||'?')+'; Python '+(d.python||'?'));
  L.push('Updates checked by: '+(d.verification_level==='signature'?'Ed25519 signature':d.verification_level==='checksum'?'SHA-256 checksum':String(d.verification_level||'?')));
  L.push('Reviews: '+(ok(d.reviews)?d.reviews:'unknown')+'; synced sessions: '+(ok(d.sessions)?d.sessions:'unknown')+'; cache: '+fmtBytes(d.cache_bytes));
  L.push('Settings folder: '+(d.settings_folder||'?'));
  if(d.library_folder)L.push('Library folder: '+d.library_folder);
  L.push('');L.push('--- debrief.log (last '+((d.log||[]).length)+' lines; home folder, token, webhook and e-mail addresses removed) ---');
  (d.log||[]).forEach(x=>L.push(String(x)));
  return L.join('\n')+'\n'}
async function copyText(t){
  if(navigator.clipboard&&navigator.clipboard.writeText){try{await navigator.clipboard.writeText(t);return true}catch(e){}}
  const ta=document.createElement('textarea');ta.value=t;ta.setAttribute('readonly','');ta.style.position='fixed';ta.style.left='-9999px';
  document.body.appendChild(ta);ta.select();let done=false;try{done=document.execCommand('copy')}catch(e){}ta.remove();return done}
$('btnDiag').addEventListener('click',async()=>{const b=$('btnDiag');b.disabled=true;setMsg('diagMsg','Collecting...');
  let d;try{d=await api('diagnostics')}catch(e){b.disabled=false;setMsg('diagMsg','Couldn\'t read the diagnostics.','bad');return}
  if(!d||!d.ok){b.disabled=false;setMsg('diagMsg',(d&&d.error)||'Couldn\'t read the diagnostics.','bad');return}
  const okc=await copyText(diagText(d));b.disabled=false;
  setMsg('diagMsg',okc?'Copied. Paste it wherever you\'re asking for help.':'Couldn\'t copy to the clipboard.',okc?'ok':'bad')});

/* ---------- updates */
let updInfo=null;
function renderUpdate(){const j=updInfo;const strip=$('updStrip');
  if(!j||!j.ok){strip.hidden=true;$('btnUpdNow').hidden=true;$('updPill').textContent='Debrief '+(j&&j.current||'');$('updPill').className='pill';return}
  const can=j.newer&&j.installed;
  strip.hidden=!can;if(can)$('updText').textContent='Debrief '+j.latest+' is out';
  $('btnUpdNow').hidden=!can;$('updPill').textContent=j.newer?'Update: '+j.latest:'Up to date';$('updPill').className='pill '+(j.newer?'no':'ok');
  if(j.newer&&!j.installed)setMsg('updMsg','Debrief '+j.latest+' is out. This copy runs from source: update it with git.')}
// the last in-app update didn't finish: say so once (the server learns it at start, from updater.pending_result)
let lastUpdShown=false;
function lastUpdate(j){const u=j&&j.last_update;if(!u||typeof u!=='object'||u.ok!==false||lastUpdShown)return false;lastUpdShown=true;
  const m=typeof u.message==='string'&&u.message?u.message:'The last update didn\'t finish. Your reviews and settings are fine.';
  banner(m,true,15000);setMsg('updMsg',m,'bad');return true}
async function checkUpdate(mode){let j;try{j=await api('update'+(mode==='force'?'?force=1':mode==='auto'?'?auto=1':''))}catch(e){return}
  const shown=lastUpdate(j);
  if(j.skipped)return;updInfo=j;renderUpdate();if(shown&&mode!=='force')return;
  if(mode==='force')setMsg('updMsg',!j.ok?j.error:j.newer?'Debrief '+j.latest+' is available.':'You have the newest version ('+j.current+').',!j.ok?'bad':'ok')}
async function startUpdate(){if(!confirm('Update Debrief to '+(updInfo&&updInfo.latest||'the newest version')+' now? Debrief will close, install the update and reopen.'))return;
  startJob('update')}
$('updStrip').addEventListener('click',startUpdate);$('btnUpdNow').addEventListener('click',startUpdate);
$('btnUpdCheck').addEventListener('click',()=>{setMsg('updMsg','Checking...');checkUpdate('force')});
$('fUpdate').addEventListener('submit',async e=>{e.preventDefault();setMsg('updMsg','Saving...');let r;
  try{r=await post('settings',{check_updates:$('updAuto').checked})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('updMsg',r.error,'bad');return}fillSettings(r.settings);setMsg('updMsg','Saved.','ok')});

/* ---------- start */
(async function init(){
  // 2.6: everything the first screen needs, loaded side by side
  const [,,groups]=await Promise.all([loadSettings(),loadBrand(),loadReviews(),loadSessions()]);
  let s=null;try{s=await api('status')}catch(e){}
  if(s&&s.state==='running'){lastJob=s;if(s.kind==='sync'){renderSync(s);if(!onboarding())openToday();else welcome()}
    else if(s.kind!=='check'){resetJobView(s.kind);openJob()}poll();return}
  // (if the user already clicked something while the lists loaded, stay there)
  if(current.view==null){if(!onboarding())openToday();else welcome()}
  reviewGate();
  if(tokenSaved)startSync(true);   // catch up with Garage 61 every time Debrief opens
  checkUpdate('auto');
  coachPoll();                     // once: keeps polling only if the coach is already running
  loadFirstRun().then(()=>{started=true;tourHint()});   // 2.14: the setup checklist; 2.18: then "New here?" if new
})();
/* ---------- 2.15 Car and setup: how the car handles (balance map and setup ideas), tyre pressures over time,
   share what changed (copy only, never posted), and a teammate's lap to compare with and coach against. Called
   from loadSlReport (csSetupLab), renderPrSession (csSession), renderCoach (csCoach) and loadCoPlan (csCoachCombo). */
let cs={seq:0,balSetup:null,combo:null,coachCombo:null,coachTeam:null,coachSeq:0};
const BAL_WORD={push:['Pushes','push'],rotate:['Rotates','rot'],neutral:['Neutral','neu'],unclear:['Not clear yet','unc']};
const BAL_PHASE=[['entry','On the way in'],['mid','In the middle'],['exit','On the way out']];
// 2.18 shared scale: an idea comes from a balance finding and is never tested on the laps, so it is only A hint (or A guess)
const IDEA_CONF={'worth a try':['A hint','no','A hint: worth a try, from a clear finding in your laps, but the idea itself is untested'],'a guess':['A guess','no','A guess: from a likely finding']};
const csQ=(car,track,extra)=>new URLSearchParams(Object.assign({car,track},extra||{}));
const csTurn=t=>turnWords(String(t||''));

/* ---------- Setup Lab: new sections under the report */
function csSetupLab(R){
  ['slBalance','slPress'].forEach(id=>{const e=$(id);if(e)e.remove()});
  if(!R||!R.files||!R.car_path)return;
  const combo={car:R.car_path,track:R.track_code};
  if(!cs.combo||cs.combo.car!==combo.car||cs.combo.track!==combo.track)cs.balSetup=null;
  cs.combo=combo;
  const host=$('slReport'),before=$('slNotes');
  const put=el=>{if(before&&before.parentNode===host)host.insertBefore(el,before);else host.append(el)};
  csShareButton(R);
  const bal=slSec('How the car handles','slBalance');bal.classList.add('balsec');put(bal);csLoadBalance();
  if(R.series_setup!=='fixed'){const pr=slSec('Tyre pressures over time','slPress');pr.classList.add('presssec');put(pr);csLoadPressure()}}

async function csLoadBalance(){const sec=$('slBalance'),c=cs.combo;if(!sec||!c)return;const seq=++cs.seq;
  const body=h('div','balbody');sec.replaceChildren(h('h3',null,'How the car handles'),body);
  body.append(h('p','note','Working out how the car handles from your laps. This takes a few seconds the first time.'));sec.classList.add('busy');
  const extra=cs.balSetup?{setup:cs.balSetup}:{};let j;
  try{j=await api('balance?'+csQ(c.car,c.track,extra))}catch(e){connectionLost();return}
  if(seq!==cs.seq||!$('slBalance'))return;sec.classList.remove('busy');
  if(!j.ok){body.replaceChildren(h('p','note warn',j.error||'This didn\'t load.'));return}
  csRenderBalance(body,j)}

function csRenderBalance(body,j){const R=j.report;body.replaceChildren();
  const setups=j.setups||[];
  if(setups.length>1||(setups.length&&!R)){const lab=h('label','field balpick');lab.htmlFor='balSetup';lab.append(h('span',null,'Laps from'));
    const sel=document.createElement('select');sel.id='balSetup';
    setups.forEach((s,i)=>{const o=h('option',null,s.label+(i===0?' (newest)':'')+', '+plural(s.files,'file'));o.value=s.id;sel.append(o)});
    const all=h('option',null,'All setups together');all.value='all';sel.append(all);sel.value=j.setup||(setups[0]&&setups[0].id)||'all';
    sel.addEventListener('change',()=>{cs.balSetup=sel.value;csLoadBalance()});lab.append(sel);body.append(lab)}
  if(!R){body.append(h('p','note',j.message||'No telemetry with a setup for this car and track yet.'));return}
  body.append(h('p','balsum',csTurn(R.summary||R.message)));
  if(R.ok&&(R.corners||[]).length){body.append(csBalanceGrid(R));body.append(csLegend())}
  if(R.ok&&R.entry_note)body.append(h('p','note',R.entry_note));
  const CS=(R.countersteer&&R.countersteer.by_corner)||{};
  const cs3=Object.entries(CS).filter(([k])=>k!=='between corners').sort((a,b)=>b[1].events-a[1].events).slice(0,3);
  if(cs3.length)body.append(h('p','note','Catching the rear (countersteer): '+cs3.map(([k,v])=>csTurn(k)+' '+plural(v.events,'time')+' on '+v.laps+' of '+plural(v.of_laps,'lap')).join('; ')+'. Each one is a moment the rear stepped out and you caught it.'));
  else if(R.ok)body.append(h('p','note','No countersteer: you never had to catch the rear on these laps.'));
  (R.notes||[]).forEach(n=>body.append(h('p','note warn',n)));
  if(R.ok){const X=R.cross_check;const small=h('p','balsmall');
    small.textContent=(X&&X.of?'Cross-check: worked out again from sideways g instead of how fast the car turns, '+X.agree+' of '+plural(X.of,'clear place')+' read the same. ':'')+
      'From '+plural(R.laps||0,'clean lap')+' in '+plural(j.files||R.files||0,'file')+'. "Pushes" and "Rotates" are only shown when they pass a luck check: Debrief flips your laps\' readings at random thousands of times, and a result that often turns up that way is not shown.';
    body.append(small);if(j.laps_note)body.append(h('p','balsmall','Lap counts: '+j.laps_note));
    if(R.car_fit&&R.car_fit.text){const d=h('details','balhow');d.append(h('summary',null,'How this is measured'));
      d.append(h('p','note','Debrief learns how much steering this car normally needs in gentle bends, then looks at how much you used near the limit. More than normal: the car pushes (the front slides first). Less: it rotates (the rear slides first). So it is always compared with how this car steers. How much the car turns comes from its yaw rate: how fast it rotates, which iRacing records.'));
      d.append(h('p','note',R.car_fit.text));body.append(d)}}
  body.append(csIdeas(R,j))}

function csBalanceGrid(R){const wrap=h('div','tblwrap'),t=h('table','baltbl');t.setAttribute('aria-label','How the car handles in each corner');
  const thd=h('thead'),hr=h('tr');hr.append(h('th',null,'Corner'));BAL_PHASE.forEach(([,w])=>hr.append(h('th',null,w)));thd.append(hr);
  const tb=h('tbody'),cells=R.cells||[];
  (R.corners||[]).forEach(c=>{const tr=h('tr'),th=h('th',null,csTurn(c.name));th.scope='row';
    const sp=h('small','balcls',c['class']==='slow'?'slow':c['class']==='fast'?'fast':'medium');th.append(' ',sp);tr.append(th);
    BAL_PHASE.forEach(([ph,w])=>{const x=cells.find(k=>k.corner===c.name&&k.phase===ph),td=h('td');
      if(!x){const s=h('span','bal none','Not measured');td.append(s);td.title='Too few laps had a reading here';}
      else{const bw=BAL_WORD[x.label]||[x.label_words||x.label,'unc'],s=h('span','bal '+bw[1],bw[0]);
        if(x.confidence==='likely')s.append(h('small',null,' likely'));
        td.append(s);td.title=csTurn(x.text||'')}
      td.setAttribute('aria-label',csTurn(c.name)+', '+w.toLowerCase()+': '+td.textContent);tr.append(td)});
    tb.append(tr)});
  t.append(thd,tb);wrap.append(t);return wrap}

function csLegend(){const p=h('p','ballegend');
  [['push','Pushes: understeer, the front slides first'],['rotate','Rotates: oversteer, the rear slides first'],['neutral','Neutral: neither'],['unclear','Not clear yet: needs more laps']].forEach(([k,t])=>{
    const s=h('span','balkey');s.append(h('i','bal '+BAL_WORD[k][1]),document.createTextNode(t));p.append(s)});return p}

function csIdeas(R,j){const fixedSeries=R.series_setup==='fixed',box=h('div','balideas');box.append(h('h4',null,fixedSeries?'What you can still change':'Setup ideas'));
  const I=R.setup_ideas||[];
  if(!I.length){box.append(h('p','note',R.ok&&(R.findings||[]).length?'This car\'s setup has nothing Debrief knows how to change for this.':'No setup idea until the car clearly pushes or rotates somewhere.'));return box}
  const fixed=I.length===1&&!I[0].finding&&!I[0].change&&!I[0].dial&&!I[0].driving;
  if(fixed){box.append(h('p','note warn',csTurn(I[0].text)));return box}
  if(fixedSeries)box.append(h('p','note warn','This series uses a fixed setup, so the garage is locked. These use the in-car dials where this car has them, otherwise the driving.'));
  else{box.append(h('p','note','One change at a time, so you know what did it.'));
    const L=j&&j.last_change;if(L&&!L.slower)box.append(h('p','note','Debrief doesn\'t suggest simply undoing your change from '+L.from+' to '+L.to+': the laps don\'t show it made the car slower.'))}
  const ul=h('ul','pvlist');
  I.forEach(i=>{const li=h('li','balidea'+(i.first?' first':'')),top=h('div','slidea');
    const cf=IDEA_CONF[i.confidence]||[i.confidence||'',''];const pill=h('span','pill sure'+(cf[1]?' '+cf[1]:''),cf[0]);pill.title=cf[2]||'How sure this is';
    top.append(h('b',null,i.change?i.change.label:i.dial?i.dial.label:i.driving?'Driving':'No setup change'),pill);li.append(top);
    let t=String(i.text||''),then=String(i.then||''),cav='';
    if(then&&t.endsWith(then))t=t.slice(0,-then.length).trim();
    const k=t.indexOf('On most cars in iRacing');if(k>0){cav=t.slice(k);t=t.slice(0,k).trim()}
    li.append(h('span',null,csTurn(t)));if(cav)li.append(h('span','sub2',cav));if(then)li.append(h('span','sub2 balthen',then));
    ul.append(li)});
  box.append(ul);return box}

/* ---------- tyre pressures over time */
async function csLoadPressure(){const sec=$('slPress'),c=cs.combo;if(!sec||!c)return;
  const body=h('div','pressbody');sec.replaceChildren(h('h3',null,'Tyre pressures over time'),body);body.append(h('p','note','Reading your runs...'));
  let j;try{j=await api('pressure?'+csQ(c.car,c.track))}catch(e){connectionLost();return}
  if(!$('slPress')||cs.combo!==c)return;
  if(!j.ok){body.replaceChildren(h('p','note warn',j.error||'This didn\'t load.'));return}
  const R=j.report||{};body.replaceChildren();
  body.append(h('p','note','Your runs grouped by track temperature (5 °C bands): the cold pressure you set, the hot pressure it came up to, and your typical lap. A run is a stint with at least 3 counted laps. Pressures are in kPa (kilopascals), the unit iRacing\'s garage uses: 100 kPa is about 14.5 psi.'));
  const B=R.bands||[];
  if(B.length){const wrap=h('div','tblwrap'),t=h('table','pacetbl presstbl'),hr=h('tr');['Track','Cold pressure','Runs','Typical lap','Hot (front / rear)'].forEach(x=>hr.append(h('th',null,x)));
    const thd=h('thead');thd.append(hr);const tb=h('tbody');
    B.forEach(b=>(b.groups.length?b.groups:[{cold:'not recorded',runs:b.runs.length,median:null,hot:{}}]).forEach((g,i)=>{const tr=h('tr');if(i===0&&tb.children.length)tr.className='newrun';
      const th=h('th',null,i===0?b.band:'');th.scope='row';const H=g.hot||{};
      const ax=(a,b2)=>{const v=[H[a],H[b2]].filter(ok);return v.length?Math.round(v.reduce((x,y)=>x+y,0)/v.length):null};
      const fh=ax('LF','RF'),rh=ax('LR','RR');
      tr.append(th,h('td',null,g.cold),h('td','num',String(g.runs)),h('td','num',ok(g.median)?fmtT(g.median):'not timed'),h('td','num',fh!==null?fh+' / '+rh+' kPa':'not read'));tb.append(tr)}));
    t.append(thd,tb);wrap.append(t);body.append(wrap)}
  (R.text||[]).filter(x=>x&&x!==R.prediction_text).forEach(x=>body.append(h('p',B.some(b=>b.finding&&b.text===x)?'pressfind':null,x)));
  const pp=h('div','presspred');pp.append(h('h4',null,'Hot pressure from cold'));pp.append(h('p',null,R.prediction_text||''));
  if(j.can_predict)pp.append(csPredictForm());body.append(pp);
  if((R.by_car||[]).length>0&&!B.length)body.append(h('p','note','This car at other tracks: '+R.by_car.map(b=>b.band+' ('+plural(b.runs,'run')+')').join(', ')+'.'))}

function csPredictForm(){const f=document.createElement('form');f.className='pressform';f.id='pressForm';f.autocomplete='off';
  const fld=(id,label,ph)=>{const l=h('label','field');l.htmlFor=id;l.append(h('span',null,label));const i=document.createElement('input');i.id=id;i.type='number';i.step='0.5';i.inputMode='decimal';i.placeholder=ph;l.append(i);return l};
  const g=h('div','grid3');g.append(fld('prCF','Cold front (kPa)','e.g. 172'),fld('prCR','Cold rear (kPa)','e.g. 168'),fld('prTT','Track temperature (°C)','optional'));
  const row=h('div','row'),b=h('button','btn','Predict');b.type='submit';const m=h('span','msg');m.id='prPredMsg';m.setAttribute('role','status');row.append(b,m);
  const out=h('p','pressout');out.id='prPredOut';out.hidden=true;
  f.append(g,row,out);
  f.addEventListener('submit',async e=>{e.preventDefault();const c=cs.combo;if(!c)return;setMsg('prPredMsg','Working it out...');out.hidden=true;
    let r;try{r=await post('pressure/predict',{car:c.car,track:c.track,cold_front:$('prCF').value,cold_rear:$('prCR').value,track_temp:$('prTT').value})}catch(err){connectionLost();return}
    if(!r.ok){setMsg('prPredMsg',r.error||'That didn\'t work.','bad');return}
    setMsg('prPredMsg','');out.textContent=r.text;out.hidden=false});
  return f}

/* ---------- share what changed (copy only) */
function csShareButton(R){const dc=$('slDiffs');if(!dc||!(R.diffs||[]).length)return;
  const row=h('div','row'),b=h('button','btn','Share what changed');b.type='button';b.id='slShare';
  const st=h('span','msg');st.id='slShareMsg';st.setAttribute('role','status');
  row.append(b,h('span','note','Copies a short text to paste in your team\'s Discord. Debrief never posts it for you.'),st);
  const out=h('div','shareout');out.id='slShareOut';out.hidden=true;
  b.addEventListener('click',async()=>{const c=cs.combo;if(!c)return;b.disabled=true;let r;
    try{r=await api('setuplab/share?'+csQ(c.car,c.track))}catch(e){connectionLost();return}finally{b.disabled=false}
    out.replaceChildren();out.hidden=false;
    if(!r.ok){out.append(h('p','note warn',r.error||'That didn\'t work.'));return}
    out.append(csCopyBox('Plain text',r.text,'shareText'));
    if(r.ready)out.append(csCopyBox('With bold for Discord',r.markdown,'shareMd'));
    out.append(h('p','note','No names, no file names: only the car, the track, what you changed and what the laps said.'));
    // 2.18: the click copies (the Discord version when there is one), and says plainly what happened
    if(!r.ready){st.textContent='Nothing copied: '+r.text;st.className='msg';return}
    const done=await csCopy(r.markdown||r.text,$(r.markdown?'shareMd':'shareText'));
    st.textContent=done?'Copied the Discord version (with bold) to your clipboard: paste it in Discord with Ctrl+V. Both versions are below.':
      'Couldn\'t copy by itself: select the text below and press Ctrl+C.';st.className='msg'+(done?' ok':' bad')});
  dc.append(row,out)}

async function csCopy(text,ta){let done=false;try{await navigator.clipboard.writeText(text);done=true}catch(e){}
  if(!done&&ta){try{ta.focus();ta.select();done=document.execCommand('copy')}catch(e){}}return done}

function csCopyBox(title,text,id){const box=h('div','copybox'),top=h('div','phead');top.append(h('h4',null,title));
  const b=h('button','btn','Copy');b.type='button';b.id=id+'Copy';top.append(b);
  const ta=document.createElement('textarea');ta.id=id;ta.readOnly=true;ta.value=text||'';ta.rows=Math.min(8,Math.max(3,String(text||'').split('\n').length+1));ta.setAttribute('aria-label',title);
  const m=h('span','msg');m.setAttribute('role','status');
  b.addEventListener('click',async()=>{const done=await csCopy(ta.value,ta);
    m.textContent=done?'Copied.':'Select the text and press Ctrl+C.';m.className='msg'+(done?' ok':' bad')});
  top.append(m);box.append(top,ta);return box}

/* ---------- a teammate's lap: the session page and the Coach page */
const TEAM_MAX=1536*1024*1024;
function csTeamPanel(car,track,where,onChange){const sec=where==='coach'?h('div','panel teampanel'):slSec('Compare with a teammate\'s lap','prTeam');
  if(where==='coach'){sec.id='coTeam';sec.append(h('div','phead teamhead'))}
  else sec.classList.add('teampanel');
  const head=where==='coach'?sec.querySelector('.teamhead'):null;if(head)head.append(h('h3',null,'Compare with a teammate\'s lap'));
  const body=h('div','teambody');sec.append(body);
  const draw=async()=>{let j;try{j=await api('teamref?'+csQ(car,track))}catch(e){connectionLost();return}
    if(!j.ok){body.replaceChildren(h('p','note warn',j.error||'This didn\'t load.'));return}
    csTeamBody(body,j.teammate,car,track,where,draw,onChange);if(onChange)onChange(j.teammate)};
  body.append(h('p','note','Loading...'));draw();return sec}

function csTeamBody(body,T,car,track,where,redraw,onChange){body.replaceChildren();const id=where==='coach'?'co':'pr';
  if(T){const p=h('p','teamnow');p.append(document.createTextNode('Using '),h('b',null,T.label),document.createTextNode(T.kept?'. Kept for this car and track.':'. Kept until Debrief closes.'));body.append(p)}
  else body.append(h('p','note','Got a teammate\'s telemetry file (.ibt) for this car and track? Debrief checks it for anything harmful, takes their best clean lap and compares yours with it. The file itself is read once and never copied.'));
  // 2.18: the teammate laps Debrief already has here, and how to get a file
  const have=h('div','teamhave');have.id=id+'TeamHave';body.append(have);csTeamHave(have,car,track,!T);
  const row=h('div','row'),inp=document.createElement('input');inp.type='file';inp.accept='.ibt';inp.hidden=true;inp.id=id+'TeamFile';
  const b=h('button','btn'+(T?'':' primary'),T?'Pick another file...':'Pick a teammate\'s file...');b.type='button';b.id=id+'TeamPick';
  const keepL=h('label','check');keepL.htmlFor=id+'TeamKeep';const keep=document.createElement('input');keep.type='checkbox';keep.id=id+'TeamKeep';keep.checked=!!(T&&T.kept);
  keepL.append(keep,h('span',null,'Keep for this car and track'));
  const m=h('span','msg');m.id=id+'TeamMsg';m.setAttribute('role','status');
  row.append(b,inp,keepL);
  if(T){const f=h('button','btn ghost','Forget it');f.type='button';f.id=id+'TeamForget';
    f.addEventListener('click',async()=>{let r;try{r=await post('teamref/forget',{car,track})}catch(e){connectionLost();return}if(r.ok)redraw()});row.append(f)}
  row.append(m);body.append(row);
  body.append(h('p','balsmall',T?'Keep saves only the lap Debrief worked out (not the file) so it is here next time. Untick it to remove the saved lap.':'Tick Keep before you pick the file to save the lap Debrief works out from it (never the file) for next time.'));
  if(T)keep.addEventListener('change',async()=>{const want=keep.checked;keep.disabled=true;let r;
    try{r=await post('teamref/keep',{car,track,keep:want})}catch(e){connectionLost();return}
    keep.disabled=false;
    if(!r.ok){keep.checked=!want;setMsg(m.id,r.error||'That didn\'t work.','bad');return}
    await redraw();setMsg(m.id,want?'Kept for this car and track.':'Not kept: it goes when Debrief closes.','ok')});
  b.addEventListener('click',()=>inp.click());
  inp.addEventListener('change',async()=>{const f=inp.files[0];inp.value='';if(!f)return;
    if(f.size>TEAM_MAX){setMsg(m.id,'That file is far too big to be a telemetry file.','bad');return}
    setMsg(m.id,'Checking '+f.name+'...');b.disabled=true;let r;
    try{r=await fetch(BASE+'api/teamref/upload?'+new URLSearchParams({name:f.name,car,track,keep:keep.checked?'1':'0'}),{method:'POST',headers:{'X-Debrief':'1','Content-Type':'application/octet-stream'},body:f}).then(x=>x.json())}
    catch(e){setMsg(m.id,'Couldn\'t send the file.','bad');b.disabled=false;return}
    b.disabled=false;
    if(!r||!r.ok){setMsg(m.id,(r&&r.error)||'Couldn\'t read that file.','bad');return}
    if(!r.accepted){setMsg(m.id,r.reason||'This file can\'t be used.','bad');return}
    await redraw();
    if(r.still_kept)setMsg(m.id,'Loaded '+r.teammate.label+'. '+r.still_kept+' is still kept for next time: tick Keep to keep this one instead.','ok');
    else setMsg(m.id,'Loaded '+r.teammate.label+'.','ok')});
  if(where==='session'&&T)body.append(csTeamCards(car,track,T));
  if(where==='coach'&&T){const cl=h('label','check');cl.htmlFor='coTeamOn';const cb=document.createElement('input');cb.type='checkbox';cb.id='coTeamOn';cb.checked=!!T.coach_on;
    cl.append(cb,h('span',null,'Coach against '+T.short));body.append(cl);
    const n=h('p','note');n.id='coTeamNote';body.append(n);csTeamNote(T.coach_on);
    cb.addEventListener('change',async()=>{let r;try{r=await post('teamref/coach',{car,track,on:cb.checked})}catch(e){connectionLost();return}
      if(!r.ok){cb.checked=!cb.checked;setMsg(m.id,r.error||'That didn\'t work.','bad');return}
      csTeamNote(cb.checked,r.restart);coachPoll()})}}

function csTeamHave(box,car,track,open){(async()=>{let j;try{j=await api('teamref/laps?'+csQ(car,track))}catch(e){return}
  if(!j||!j.ok)return;box.replaceChildren();const L=j.laps||[];
  if(L.length){box.append(h('h4',null,'Teammate laps Debrief already has here'));const ul=h('ul','pvlist teamlaps');
    L.forEach(x=>{const li=h('li');li.append(h('b',null,String(x.driver||'A teammate')),document.createTextNode(' '+(x.lap_text||'')+(x.in_use?' (in use)':'')),h('span','sub2',' '+(x.note||'')));ul.append(li)});box.append(ul)}
  else box.append(h('p','note','Debrief has no teammate laps for this car and track yet.'+(j.team_page?'':' Refresh team data on the Team page lists your teammates\' best lap times here too.')));
  const how=h('details','teamhow');how.open=!!open;how.append(h('summary',null,'How to get a teammate\'s file'),h('p','note',j.how||''));box.append(how)})()}
function csTeamNote(on,restart){const n=$('coTeamNote');if(!n)return;
  n.textContent=on?(restart?'On. Stop and start the coach to switch to their lap.':'On. The coach measures you against their lap the next time you start it, and your own new bests don\'t replace it.'):'Off: the coach uses your own best lap.'}

// 2.18: teammate cards in mph, like the rest of the pages (the corner coach words its speeds in km/h)
function ccMph(c){const o=Object.assign({},c);['different','how','gets','why','evidence'].forEach(k=>{if(typeof o[k]==='string')o[k]=mphText(o[k])});return o}
function csTeamCards(car,track,T){const box=h('div','teamcards');box.append(h('p','note','Comparing your laps with '+T.short+'...'));
  (async()=>{let j;try{j=await api('teamref/cards?'+csQ(car,track))}catch(e){connectionLost();return}
    box.replaceChildren();
    if(!j.ok){box.append(h('p','note warn',j.error||'This didn\'t load.'));return}
    if(j.headline){const hl=h('p','teamheadline',mphText(turnWords(j.headline)));hl.id='prTeamHead';box.append(hl)}   // 2.18: who is quicker, and where
    box.append(h('p','note',j.message||''));const C=j.cards||[];
    if(!C.length){box.append(h('p','note','No corner where your typical laps clearly differ from '+T.short+' yet.'));return}
    const g=h('div','cccards');C.forEach(c=>{const card=ccCard(ccMph(c));const ey=h('span','eyebrow teamey','Against '+T.short);card.prepend(ey);g.append(card)});box.append(g);
    box.append(h('p','balsmall','A single lap of theirs: its own lap-to-lap spread can\'t be measured, so these are always "Few laps".'))})();
  return box}

/* the session page: after the Corner coach, with a one-click way there from the top (2.18) */
function csSession(R){const old=$('prTeam');if(old)old.remove();if(!R||!R.car_path||!R.track_code)return;
  const sec=csTeamPanel(R.car_path,R.track_code,'session');const after=$('prCorners'),host=$('prReport');
  if(after&&after.parentNode===host)after.after(sec);else host.append(sec);
  const head=host.querySelector('.slhead');
  if(head&&!$('prTeamJump')){const b=h('button','btn','Compare with a teammate\'s lap');b.type='button';b.id='prTeamJump';
    b.addEventListener('click',csTeamFocus);head.append(b)}
  if(cs.teamFocus){cs.teamFocus=false;setTimeout(csTeamFocus,0)}}   // after the page scrolls to its top
function csTeamFocus(){const t=$('prTeam');if(!t)return;t.scrollIntoView({block:'start'});const b=$('prTeamPick');if(b)b.focus({preventScroll:true})}
/* a race review: open the Practice session of the same car and track, at its teammate panel (2.18) */
$('fbTeamLap').addEventListener('click',async()=>{if(current.view!=='review'||!current.dir)return;const b=$('fbTeamLap');b.disabled=true;let j;
  try{j=await api('teamref/for-review?'+new URLSearchParams({dir:current.dir}))}catch(e){connectionLost();return}finally{b.disabled=false}
  if(!j.ok){banner(j.error||'That didn\'t work.',true);return}
  if(!j.session){banner(j.message||'No telemetry of yours for this car and track yet.',true);return}
  cs.teamFocus=true;openPrSession(j.session)});

/* the Coach page: under the reference lap, for the car and track the Coach page shows */
function csCoachCombo(p){cs.coachCombo=p&&p.car_path?{car:p.car_path,track:p.track_code}:null;const old=$('coTeam');if(old)old.remove();
  if(!cs.coachCombo)return;const c=cs.coachCombo,grid=document.querySelector('#vCoach .cogrid');if(!grid)return;
  grid.after(csTeamPanel(c.car,c.track,'coach',T=>{cs.coachTeam=T;csCoach(co.st)}))}

function csCoach(st){if(!st||current.view!=='coach')return;const T=st.teammate||{},R=st.reference;
  if(R&&R.source&&R.source.kind==='teammate'){$('coRef').textContent=(R.source.label?cap(R.source.label):'Teammate\'s best lap')+' ('+(R.lap_text||'')+')';
    $('coRefNote').textContent='Against '+(T.short||'your teammate\'s lap')+': their best clean lap from their telemetry file. Your own new bests don\'t replace it.'}
  else if(T.on&&!st.running){$('coRefNote').textContent='Coach against '+(T.short||'your teammate\'s lap')+' is on: it is used when you start the coach in that car at that track.'}}

/* ---------- Help */
if(typeof HELP==='object'){
  HELP.tour.push(['How the car handles','Setup Lab shows, corner by corner, where the car pushes (understeer) or rotates (oversteer), with one setup change to try at a time.'],
    ['Teammate\'s lap','On a Practice session or the Coach page, pick a teammate\'s telemetry file to compare your corners with their best lap, or to be coached against it.']);
  HELP.words.push(['Pushes, Rotates','Pushes (understeer): you needed more steering than this car normally does, so the front was sliding first. Rotates (oversteer): less steering, so the rear was sliding first. Always compared with how this car steers in gentle bends, learned from your own laps; on the way in, with your own typical corner entry (braking always turns a car in more).'],
    ['Not clear yet','The laps don\'t say for sure either way. Debrief shuffles your laps (every possible way with up to 11 laps, otherwise 4,000 ways) and only says Pushes or Rotates when the real pattern beats 95 in 100 of the shuffles and is big enough to feel.'],
    ['Countersteer','Steering the other way to catch the rear. Each one is a moment of oversteer you had to save.'],
    ['Setup ideas: A hint, A guess','How sure a setup idea is. A hint (worth a try) comes from a clear finding but the idea itself is untested; A guess from a likely finding. Change one thing, drive at least 6 clean laps, then compare again.'],
    ['Hot pressure','The tyre pressure once the tyres are warm, read from your laps. The prediction says what a cold pressure comes up to at a track temperature, with how far off it typically is.'],
    ['Share what changed','Setup Lab writes a short text of what you changed and what the laps said, with the same numbers as the page, and copies it for you to paste in Discord. Debrief never posts it.'],
    ['kPa','Kilopascals, the unit iRacing\'s garage uses for tyre pressure. 100 kPa is about 14.5 psi, so 172 kPa is about 25 psi.'],
    ['Yaw rate','How fast the car rotates, which iRacing records many times a second. Setup Lab compares it with your steering to tell a push from a rotation.'],
    ['Fixed setup','A series where the garage setup is locked, tyre pressures included. Fuel and the in-car dials (brake bias, traction control and ABS on cars that have them) are still yours, so Setup Lab points there or to the driving.'],
    ['Teammate\'s lap','Their best clean lap from their telemetry file, checked for anything harmful first. The file is read once and never copied; only the worked-out lap is kept if you tick Keep.'])}
/* ---------- 2.16 Racing: your starts (green flag to the end of lap 1) and your incidents, across your races, from
   the replay scans, telemetry files and saved reviews on this PC (api/racing, read only) */
let rc={data:null,track:'',seq:0};
const RC_KIND={own:'Your own',contact:'Contact',caught_up:'Caught up',unknown:'Not known'};
const RC_KIND_LONG={own:'Your own mistake',contact:'Contact',caught_up:'Caught up in someone else\'s',unknown:'Kind not known'};
const RC_SESS={race:'Race',qualifying:'Qualifying',practice:'Practice'};
const RC_CONTACT={contact_4x:['Contact (4x)','badc'],possibly:['Possibly',''],no_4x:['No hard contact','okc']};
const rcSigned=n=>ok(n)?(n>0?'+'+n:n<0?'−'+Math.abs(n):'0'):'';
const rcPts=n=>ok(n)?n+'x':'';
async function openRacing(){current={view:'racing',dir:null};show('vRacing');markNav();await loadRacing()}
async function loadRacing(){const seq=++rc.seq;setMsg('rcMsg','Working it out...');$('rcAgain').disabled=true;
  ['rcStarts','rcInc'].forEach(id=>$(id).classList.add('busy'));
  let j;try{j=await api('racing')}catch(e){connectionLost();return}finally{if(seq===rc.seq)$('rcAgain').disabled=false}
  if(seq!==rc.seq)return;['rcStarts','rcInc'].forEach(id=>$(id).classList.remove('busy'));
  if(!j.ok){setMsg('rcMsg',j.error||'Racing didn\'t load.','bad');return}
  const P=j.pending;setMsg('rcMsg',P?'Reading your telemetry files: '+P.left+' of '+P.total+' still to read. This fills in as they are read.':'');rc.data=j;renderRacing();
  if(P)setTimeout(()=>{if(current.view==='racing'&&seq===rc.seq)loadRacing()},2500)}
$('rcAgain').addEventListener('click',()=>loadRacing());
$('rcTrack').addEventListener('change',()=>{rc.track=$('rcTrack').value;renderRacing()});
function rcTracks(j){const out=[];const add=t=>{if(t&&!out.includes(t))out.push(t)};
  ((j.starts||{}).tracks||[]).forEach(g=>add(g.track));((j.incidents||{}).tracks||[]).forEach(t=>add(t.track));return out}
function renderRacing(){const j=rc.data;if(!j)return;const W=j.where||{},C=j.counts||{},F=j.found||{};
  const tracks=rcTracks(j);if(rc.track&&!tracks.includes(rc.track))rc.track='';
  const sel=$('rcTrack');sel.replaceChildren(new Option('All tracks',''),...tracks.map(t=>new Option(t,t)));sel.value=rc.track;$('rcTrackWrap').hidden=tracks.length<2;
  const races=((j.starts||{}).races)||0,sess=((j.incidents||{}).sessions)||0;
  const pill=$('rcPill');pill.className='pill'+(races||sess?' ok':' no');pill.textContent=races||sess?plural(races,'race')+', '+plural(sess,'session'):'Nothing yet';
  const bits=[plural(F.reviews||0,'saved review'),plural(F.scans||0,'replay scan'),plural(F.telemetry||0,'telemetry file')];
  let note='Found '+bits.join(', ')+'.';
  if(!W.library)note+=' Your library folder wasn\'t found, so saved reviews and replay scans can\'t be read.';
  if(!W.telemetry)note+=' Your telemetry folder wasn\'t found. If iRacing saves telemetry somewhere else, set the folder in Settings.';
  else note+=' Telemetry comes from the folder "'+(W.telemetry_name||'telemetry')+'".';
  if(W.racecap)note+=' RaceCap\'s replay scans are read too.';else if(W.racecap_set)note+=' RaceCap\'s folder wasn\'t found, so its scans are left out.';
  $('rcFoundNote').textContent=note;
  rcStarts(j);rcIncidents(j)}
function rcEmpty(host,title,lines,settings){const e=h('div','panel pvnone');e.append(h('h2','pvtrack',title));lines.forEach(t=>e.append(h('p','lede',t)));
  if(settings){const b=h('button','btn','Set the folder in Settings');b.type='button';b.dataset.nav='settings';const r=h('div','row');r.append(b);e.append(r)}
  host.replaceChildren(e)}
const RC_HOW_SCAN='Open a race review and press Scan this race: Debrief then sees every car near you on lap 1 and who was close when you had an incident.';
const RC_HOW_TEL='Record telemetry in iRacing (Alt+L in the car, or automatic recording in iRacing\'s options): your incident count and your position at the green come from it.';
function rcSentences(box,list,pats){(list||[]).forEach(t=>{const pat=(pats||[]).some(p=>t===p)||/looks like a habit|is a pattern/.test(t);
  const p=h('p',pat?'rcpat':'rcsay',t);if(pat){p.prepend(h('b',null,'Pattern: '))}box.append(p)})}
/* (a) your starts */
function rcStarts(j){const host=$('rcStarts'),S=j.starts||{},W=j.where||{};
  if(!S.races){const N=fileNotes(S.notes);rcEmpty(host,'Your starts',['No races to show yet.',RC_HOW_TEL,RC_HOW_SCAN].concat(N.notes.slice(0,3)),!W.telemetry);const f=fileNotesEl(N.files);if(f)host.firstChild.append(f);return}
  const out=document.createDocumentFragment();
  const sec=slSec('Your starts','rcStartSec');sec.classList.add('rcsec');
  const g=rc.track?(S.tracks||[]).find(x=>x.track===rc.track):S.overall;
  const rows=(S.list||[]).filter(r=>!rc.track||r.track===rc.track);
  if(g){const f=g.lap1||{};
    sec.append(prFacts([['Races',String(g.races)]].concat(ok(f.n)&&f.n<g.races?[['Lap 1 known in',f.n+' of '+plural(g.races,'race')]]:[]).concat([['Gained places in',plural(f.gained||0,'race')],['Lost places in',plural(f.lost||0,'race')],['Kept your place in',plural(f.same||0,'race')],
      ['Average on lap 1',ok(f.avg)?rcSigned(Math.round(f.avg*10)/10):'Not known']])));
    if(rc.track||(S.tracks||[]).length!==1)rcSentences(sec,g.sentences)}
  // per track: what happened there and the corner bars (with one track, its words stand for all races)
  const groups=rc.track?(g?[g]:[]):(S.tracks||[]);
  groups.forEach(t=>{const box=h('div','rctrackbox');if(!rc.track){box.append(h('h4',null,t.track));rcSentences(box,t.sentences)}
    box.append(rcCornerBars(rows.filter(r=>r.track===t.track)));sec.append(box)});
  sec.append(rcRaceTable(rows,!rc.track&&groups.length>1));
  const how=h('details','rchow');how.append(h('summary',null,'How this is worked out'),h('p','note',S.how||''),
    h('p','note','Where the start position comes from, best first: the official results saved with the review, RaceCap\'s results file, iRacing\'s position at the green from your telemetry or the replay, and last a guess from the replay. Each race says which one was used.'),
    h('p','note','Corner by corner on lap 1 needs a replay scan and only counts the cars the replay saw near you, so it can add up to less than your real change.'));
  sec.append(how);
  const N=fileNotes(S.notes);N.notes.slice(0,6).forEach(n=>sec.append(h('p','note',n)));{const f=fileNotesEl(N.files);if(f)sec.append(f)}
  out.append(sec);host.replaceChildren(out)}
function rcCornerBars(rows){const box=h('div','rcbars');const scanned=rows.filter(r=>(r.corners||[]).length);
  if(!scanned.length){box.append(h('p','note','Corner by corner on lap 1 needs a replay scan of the race. '+RC_HOW_SCAN));return box}
  const order=[],agg={};scanned.forEach(r=>r.corners.forEach(s=>{if(!agg[s.key]){agg[s.key]={label:s.label,g:0,l:0};order.push(s.key)}
    if(/^the start/.test(s.label))agg[s.key].label=s.label;agg[s.key].g+=s.gained||0;agg[s.key].l+=s.lost||0}));
  const max=Math.max(1,...order.map(k=>Math.max(agg[k].g,agg[k].l)));
  box.append(h('p','rcbarhead','Places seen changing on lap 1, corner by corner'));
  const ul=h('ul','rcbarlist');ul.setAttribute('aria-label','Places gained and lost on lap 1 by corner');
  order.forEach(k=>{const a=agg[k],li=h('li','rcbar');li.append(h('span','rcbarlab',cap(a.label)));
    const lost=h('span','rcl'),gain=h('span','rcg');const li1=h('i');li1.style.width=(a.l/max*100)+'%';const gi=h('i');gi.style.width=(a.g/max*100)+'%';
    lost.append(li1);gain.append(gi);
    li.append(h('span','rcbarn num lose',a.l?'−'+a.l:''),lost,gain,h('span','rcbarn num gain',a.g?'+'+a.g:''));
    li.setAttribute('aria-label',cap(a.label)+': gained '+a.g+', lost '+a.l);ul.append(li)});
  box.append(ul);
  box.append(h('p','note','Only cars the replay saw near you are counted ('+plural(scanned.length,'scanned race')+'). Left: places lost. Right: places gained.'));
  return box}
function rcWhere(r){const segs=(r.corners||[]).filter(s=>s.gained||s.lost);
  if(!(r.corners||[]).length)return h('span','note','No replay scan');
  if(!segs.length)return h('span','note','No change seen');
  const s=h('span','rcwhere');segs.forEach((x,i)=>{if(i)s.append(', ');s.append(cap(x.label)+' ');s.append(h('b','num '+(x.net>0?'gain':x.net<0?'lose':''),rcSigned(x.net)))});
  if(r.change!=null&&r.seen_net!=null&&r.seen_net!==r.change)s.append(h('span','note',' (only cars seen)'));return s}
function rcWhenCell(r){const td=h('td','rcwhen');td.append(h('span','num',fmtWhen(r.when)||'Date not known'));
  if(r.review){const b=h('button','linkbtn','Open review');b.type='button';b.title='Open the saved review of this race';b.addEventListener('click',()=>openReview(r.review));td.append(b)}return td}
function rcRaceTable(rows,withTrack){const wrap=h('div','tblwrap'),t=h('table','pacetbl rctbl'),hr=h('tr');
  ['Date'].concat(withTrack?['Track']:[]).concat(['Start \u2192 after lap 1','Change','Where places changed','Lap 1 incidents','Contact']).forEach(x=>hr.append(h('th',null,x)));
  const hd=h('thead');hd.append(hr);t.append(hd);const tb=h('tbody');const cols=withTrack?7:6;
  rows.slice().reverse().forEach(r=>{const tr=h('tr');tr.append(rcWhenCell(r));if(withTrack)tr.append(h('td',null,r.track||''));
    const pos=h('td','num');if(!r.start&&!r.lap1_pos){pos.className='note';pos.textContent='Not known'}else{pos.textContent=(r.start?'P'+r.start:'Not known')+' → '+(r.lap1_pos?'P'+r.lap1_pos:'not known');if(r.in_class)pos.append(h('small','note',' in class'))}tr.append(pos);
    tr.append(h('td','num '+(r.change>0?'gain':r.change<0?'lose':''),r.change!=null?rcSigned(r.change):'Not known'));
    const w=h('td');w.append(rcWhere(r));tr.append(w);
    const inc=r.incidents||[];tr.append(h('td','num',inc.length?String(inc.length)+(ok(r.incident_points)?' ('+rcPts(r.incident_points)+')':''):'None'));
    const c=RC_CONTACT[r.contact];const ct=h('td',c?c[1]:'note',c?c[0]:'Not known');if(r.contact_why)ct.title=r.contact_why;tr.append(ct);
    tb.append(tr);
    // 2.18: where the positions came from goes on the row's own line, so the table fits its panel
    const s=h('tr','rcsub'),td=h('td');td.colSpan=cols;if((r.sentences||[]).length)td.append(document.createTextNode(r.sentences.join(' ')+' '));
    td.append(h('span','rcsrc','Start: '+(r.start_words||'not known')+'. After lap 1: '+(r.lap1_words||'not known')+'.'));s.append(td);tb.append(s)});
  t.append(tb);wrap.append(t);const box=h('div','rcraces');box.append(h('p','rcbarhead','Your races, newest first'),wrap);return box}
/* (b) your incidents */
function rcIncidents(j){const host=$('rcInc'),M=j.incidents||{},W=j.where||{};
  const ts=(M.tracks||[]).filter(t=>!rc.track||t.track===rc.track);
  if(!M.sessions){const N=fileNotes(M.notes);rcEmpty(host,'Your incidents',['No sessions to show yet.',RC_HOW_TEL,RC_HOW_SCAN].concat(N.notes.slice(0,3)),!W.telemetry);const f=fileNotesEl(N.files);if(f)host.firstChild.append(f);return}
  const sec=slSec('Your incidents','rcIncSec');sec.classList.add('rcsec');
  sec.append(h('p','note','iRacing counts 1x off track, 2x loss of control, 4x contact. '+(M.steps_note||'')));
  ts.forEach(t=>sec.append(rcIncTrack(t)));
  const how=h('details','rchow');how.append(h('summary',null,'How this is worked out'),h('p','note',M.how||''),
    h('p','note','The replay only shows where cars were, not who touched whom: another driver is only named as the closest car and how far away along the track. Without a replay scan or the spotter, an incident\'s kind isn\'t known.'));
  sec.append(how);{const N=fileNotes(M.notes);N.notes.slice(0,6).forEach(n=>sec.append(h('p','note',n)));const f=fileNotesEl(N.files);if(f)sec.append(f)}
  host.replaceChildren(sec)}
function rcIncTrack(t){const box=h('div','rctrackbox'),top=h('div','phead');top.append(h('h4',null,t.track),h('span','pill',plural(t.events,'incident')+(t.points_known||t.points?', '+t.points+' points':'')));box.append(top);
  const L=t.lap1||{},B=t.by_session||{};
  box.append(prFacts([['Lap 1',String(L.events||0)],['Later in races',String(L.later||0)],['Race',String(B.race||0)],['Qualifying',String(B.qualifying||0)],['Practice',String(B.practice||0)]]));
  rcSentences(box,t.sentences,(t.patterns||[]).map(p=>p.text));
  (t.early_signs||[]).forEach(e=>{const d=h('div','rcearly');const p=h('p','rcsay');p.append(h('b',null,(e.label_text||'Early sign, not proven yet')+': '),String(e.said||'')+'. That isn\u2019t enough to call it a pattern, but these are safe things to try:');d.append(p);
    const ul=h('ul','pvlist');(e.tries||[]).forEach(x=>ul.append(h('li',null,cap(String(x))+'.')));d.append(ul);box.append(d)});
  if(t.corner_names==='quarters')box.append(h('p','note','This track\'s corners aren\'t known yet (no saved review or clean lap there), so the lap is cut into quarters.'));
  if(!t.points_known)box.append(h('p','note','Incidents seen only in a replay scan have no points: iRacing\'s count comes from telemetry.'));
  if(t.not_recorded)box.append(h('p','note',plural(t.not_recorded,'point')+' not placed: added while no telemetry was recording.'));
  if(t.events){box.append(rcGrid(t));box.append(rcIncList(t))}
  return box}
function rcGrid(t){const cells={};(t.cells||[]).forEach(c=>{cells[c.zone+'|'+c.type]=c});
  const max=Math.max(1,...(t.cells||[]).map(c=>c.events));
  const wrap=h('div','tblwrap'),tb=h('table','pacetbl rcgrid'),hr=h('tr');
  ['Where','Your own','Contact','Caught up','Not known','Lap 1','All'].forEach(x=>hr.append(h('th',null,x)));const hd=h('thead');hd.append(hr);tb.append(hd);
  const body=h('tbody');
  (t.by_zone||[]).forEach(b=>{const tr=h('tr',b.events?null:'rcnone');const th=h('th',null,cap(b.label));tr.append(th);
    ['own','contact','caught_up','unknown'].forEach(k=>{const c=cells[b.zone+'|'+k],td=h('td','num rccell');
      if(c&&c.events){td.textContent=c.events+(c.points?' · '+rcPts(c.points):'');td.style.background='color-mix(in srgb,var(--lose) '+Math.round(8+c.events/max*30)+'%,var(--panel))';td.classList.add('hot');
        td.title=plural(c.events,'incident')+(c.points?', '+c.points+' points':'')+' at '+b.label+': '+RC_KIND_LONG[k].toLowerCase()}
      tr.append(td)});
    tr.append(h('td','num',b.lap1?String(b.lap1):''),h('td','num',b.events?b.events+(b.points?' · '+rcPts(b.points):''):''));body.append(tr)});
  tb.append(body);wrap.append(tb);const box=h('div');box.append(h('p','rcbarhead','Where, and what kind (count · points)'),wrap);return box}
function rcIncList(t){const d=h('details','rcincs');d.append(h('summary',null,'Every incident ('+t.events+')'));
  const wrap=h('div','tblwrap'),tb=h('table','pacetbl rclist'),hr=h('tr');
  ['When','Session','Lap','Where','Points','What','Kind','How sure','Closest car','Why'].forEach(x=>hr.append(h('th',null,x)));const hd=h('thead');hd.append(hr);tb.append(hd);
  const body=h('tbody');(t.list||[]).slice().reverse().forEach(r=>{const tr=h('tr');
    tr.append(rcWhenCell(r),h('td',null,RC_SESS[r.kind_of_session]||cap(r.session_type||'')),
      h('td','num',r.before_green?'Pace lap':(r.lap===0?'Before lap 1':(r.lap!=null?String(r.lap):''))),h('td',null,cap(r.where||'')),
      h('td','num',r.points!=null?rcPts(r.points)+(r.at_once?' at once':''):'Not known'),h('td',null,cap(r.what||'')),
      h('td',null,RC_KIND_LONG[r.type]||''),h('td',null,cap(r.sure||'')),h('td',null,r.closest||''),h('td','rcwhy',r.why||''));
    body.append(tr)});
  tb.append(body);wrap.append(tb);d.append(wrap);return d}
if(typeof HELP==='object'){
  HELP.tour.push(['Racing','Your starts and your incidents across all your races: how many places you gain or lose on lap 1 and where the replay saw them change, and which corners your incidents happen in and what kind they probably were. It only calls something a pattern when at least 3 races show it and chance can\'t explain it.']);
  HELP.words.push(['Places seen changing','On lap 1, cars the replay saw pass you or be passed between two corners. Cars out of the replay\'s view can\'t be counted, so these can add up to less than your real change.'],
    ['Your own, Contact, Caught up, Not known','What kind an incident probably was. Contact: iRacing counted contact (4x), or another car was within 10 m (possibly). Caught up: another car\'s trouble started first in the same spot (possibly). Your own: the replay or spotter shows no other car near. Not known: no replay scan or spotter data. The replay shows where cars were, not who touched whom, so another driver is only named as the closest car.'],
    ['Incident points','iRacing counts 1x off track, 2x loss of control and 4x contact. Two at once (say 4 and 1) are split. Points added while no telemetry was recording can\'t be placed on the track.'],
    ['Pattern (Racing)','Said only when at least 3 races show it and a fair test says chance alone would show it less than 1 time in 20, counting every corner tested. A corner or lap 1 is compared with the rest of the field in your replay-scanned races (some corners catch out everyone), and a lap 1 habit must go against what your grid place alone gives. With fewer races the page only says what happened.'])}
if(typeof HELP==='object'){
  HELP.tour.push(['Car vs car','Which car of a class is quicker at a track for drivers of similar iRating, from the iRacing results files you add to your reviews, and where you were faster in each car from your telemetry. It only says one car is quicker when there are enough drivers and chance can\'t explain the gap, and car choice and driver skill can\'t be fully separated.']);
  HELP.words.push(['Similar drivers (Car vs car)','Each driver\'s lap time with their iRating taken into account: each race\'s own speed, the cars, the iRating band and the time drivers gain per 1000 iRating are worked out together.'],
    ['Not enough to tell','Shown instead of a verdict when a car has fewer than 15 drivers with an iRating, or when the gap could be chance (a shuffle test over every pair of cars at once).'],
    ['Likely, A hint (Car vs car)','On the shared sureness scale. Likely (unlikely to be chance): chance alone would show a gap that big less than 1 time in 20. A hint: less than 1 time in 5, so it could still be chance. Car choice and driver skill can\'t be fully separated either way.'])}
/* ---------- 2.18 Car vs car: which car of a class is quicker at a track, for drivers of similar iRating, and where
   each car is faster in his own telemetry (api/carcompare, read only, nothing fetched) */
let cc={data:null,pick:'',period:'season',seq:0};
const CC_HOW=['Add the iRacing results file to each race review (Add race result, then Load iRacing results file, then Save result): every driver\'s car, iRating and lap times come from it.',
  'Scan this race on a review: the replay gives every car\'s laps, but no iRatings, so those drivers only count in the raw numbers.',
  'For where you were faster in each car: drive the other car at the same track (iRacing saves the telemetry), or load a teammate\'s file from that car on the Coach page or a Practice session (Pick a teammate\'s file); their best lap is used and shown under their name.'];
async function openCars(){current={view:'cars',dir:null};show('vCars');markNav();await loadCars()}
async function loadCars(refresh){const seq=++cc.seq;setMsg('ccMsg','Working it out...');$('ccAgain').disabled=true;
  ['ccTable','ccWhere'].forEach(id=>$(id).classList.add('busy'));
  const q=new URLSearchParams();if(cc.pick){const [t,c]=JSON.parse(cc.pick);q.set('track',t);q.set('class',c)}if(cc.period==='all')q.set('period','all');if(refresh)q.set('refresh','1');
  let j;try{j=await api('carcompare'+(q.toString()?'?'+q:''))}catch(e){connectionLost();return}finally{if(seq===cc.seq)$('ccAgain').disabled=false}
  if(seq!==cc.seq)return;['ccTable','ccWhere'].forEach(id=>$(id).classList.remove('busy'));
  if(!j.ok){setMsg('ccMsg',j.error||'Car vs car didn\'t load.','bad');return}
  setMsg('ccMsg','');cc.data=j;renderCars()}
$('ccAgain').addEventListener('click',()=>loadCars(true));
$('ccPick').addEventListener('change',()=>{cc.pick=$('ccPick').value;loadCars()});
$('ccPeriod').addEventListener('change',()=>{cc.period=$('ccPeriod').value;loadCars()});
const ccGap=g=>!ok(g)?'':(Math.abs(g)<0.005?'0.00 s':(g<0?'−':'+')+Math.abs(g).toFixed(2)+' s');
// 2.18 shared scale: unlikely to be chance (under 1 in 20) is Likely, Car vs car's own "likely" (under 1 in 5) is A hint
function ccPillOf(C){if(!C)return['Nothing yet',' no'];if(C.enough)return['Likely',' ok'];
  if((C.pairs||[]).some(p=>p.confidence==='likely'))return['A hint',' no'];return['Not enough to tell','']}
function renderCars(){const j=cc.data;if(!j)return;const F=j.found||{},opts=j.options||[],sel=j.selected;
  // 2.18: a class with one car at that track can't be compared: said in its label and not pickable; no races, no choice
  const s=$('ccPick');s.replaceChildren(...opts.map(o=>{const one=o.comparable===false||(o.cars||[]).length<2;
    const op=new Option(o.label||(o.track+', '+o.class+(one?' (1 car, nothing to compare)':' ('+plural(o.cars.length,'car')+')')),JSON.stringify([o.track,o.class]));op.disabled=one;return op}));
  if(!opts.length)s.append(new Option(j.races?'Nothing to compare yet':'No races yet',''));
  if(sel){s.value=JSON.stringify([sel.track,sel.class]);cc.pick=s.value}
  const good=opts.filter(o=>o.comparable!==false&&(o.cars||[]).length>1),selGood=!!sel&&good.some(o=>o.track===sel.track&&o.class===sel.class);
  s.disabled=!good.length||(good.length===1&&selGood);
  const P=(j.compare||{}).period||{},ps=$('ccPeriod');
  ps.options[0].textContent=P.mode==='season'?'One season: '+String(P.label).split(' (')[0]:'One season (the newest race\'s)';
  ps.value=j.period==='all'?'all':'season';
  const pill=$('ccPill'),[pt,pc]=opts.length?ccPillOf(j.compare):['Nothing yet',' no'];pill.className='pill'+pc;pill.textContent=pt;
  let note=plural(j.races||0,'race')+' of yours name'+((j.races||0)===1?'s':'')+' every car in the field (from a results file or a replay scan); only those can compare cars. Also read: '+plural(j.telemetry_files||0,'telemetry file')+'.';
  if(P.mode==='season')note+=' Counting '+plural(P.races||0,'race')+' here from '+P.label+' (of '+(P.races_all||0)+'; iRacing can change the cars\' balance between seasons).';
  else if(P.races_all)note+=' Counting all '+plural(P.races_all,'race')+' here.';
  if(!F.library)note+=' Your library folder wasn\'t found.';
  if(!F.telemetry)note+=' Your telemetry folder wasn\'t found. If iRacing saves telemetry somewhere else, set the folder in Settings.';
  (j.notes||[]).forEach(n=>note+=' '+n);
  $('ccFoundNote').textContent=note;
  ccTable(j);ccWhere(j)}
function ccEmpty(host,title,lines){const e=h('div','panel pvnone');e.append(h('h2','pvtrack',title));lines.forEach(t=>e.append(h('p','lede',t)));host.replaceChildren(e)}
function ccTable(j){const host=$('ccTable'),C=j.compare;
  if(!C||!(C.cars||[]).length){ccEmpty(host,'No races to compare yet',CC_HOW);return}
  const sec=slSec(C.track+', '+C.class,'ccSec');sec.classList.add('rcsec');
  const n=C.counts||{};
  sec.append(prFacts([['Races',String(n.races||0)],['Drivers',String(C.drivers||0)],['With an iRating',String(C.with_irating||0)],['Not counted',String(n.left_out||0)]]));
  (C.verdicts||[]).forEach(v=>{const p=h('p','rcpat',v.text);p.prepend(h('b',null,'Likely: '));sec.append(p)});
  if((C.verdicts||[]).length)sec.append(h('p','note',C.caveat||'Car choice and driver skill can\'t be fully separated.'));
  else sec.append(h('p','rcsay',C.summary||''));
  const t=h('table','pacetbl cctbl'),th=h('thead'),tr=h('tr');
  ['Car','Drivers','Typical lap','Best lap','Gap, similar drivers','How sure'].forEach((x,i)=>{const c=h('th',i===0?null:i===5?'ccconf':'num',x);c.scope='col';tr.append(c)});
  th.append(tr);t.append(th);const tb=h('tbody');
  (C.cars||[]).forEach(r=>{const row=h('tr');row.append(h('th',null,r.car));row.lastChild.scope='row';
    row.append(h('td','num',r.drivers+(r.with_irating!==r.drivers?' ('+r.with_irating+' with iRating)':'')),h('td','num',fmtT(r.median)),h('td','num',ok(r.best)?fmtT(r.best):'Not known'),
      h('td','num',r.reference?'Reference':ok(r.gap)?ccGap(r.gap)+' a lap':'No numbers'),h('td','ccconf'+(r.confidence==='unlikely to be chance'?' gain':''),r.reference?'Reference car':({'unlikely to be chance':'Likely','likely':'A hint'})[r.confidence]||cap(r.confidence)));tb.append(row)});
  t.append(tb);const wrap=h('div','tblwrap');wrap.append(t);sec.append(wrap);
  sec.append(h('p','note','Gap: against the '+(C.reference||'reference car')+', for drivers of similar iRating; minus means quicker. The typical and best laps are raw: they include who drove each car.'));
  const you=(C.you||{}).items||[];const ys=h('div','rctrackbox');ys.append(h('h4',null,'You vs the field'));
  if(you.length)you.forEach(y=>ys.append(h('p','rcsay',y.text)));else ys.append(h('p','note','Your own races aren\'t in these results yet. Add the iRacing results file to your race reviews at this track.'));
  sec.append(ys);
  const sl=C.slope_per_1000,slope=!ok(sl)?'not known':sl<0?'−'+Math.abs(sl).toFixed(2)+' s a lap per 1000 iRating':'not reliable here (higher-rated drivers weren\'t quicker in these races)';
  const how=h('details','rchow');how.append(h('summary',null,'How this is worked out'),
    h('p','note','Pace is each driver\'s average race lap from the iRacing results (the median lap in a replay scan). Counted: drivers whose status was Running at the end and who completed at least 90% of the class winner\'s laps, with at least 3 laps and an average no slower than 107% of the class median.'),
    h('p','note','One model for every driver in every race: each race\'s own speed, each car, the iRating band and the time drivers gain per 1000 iRating (here '+slope+') are worked out together, so a race with more of one car can\'t make that car look quicker or slower.'),
    h('p','note','Likely: unlikely to be chance. A shuffle test says a gap this big would come up by chance less than 1 time in 20, counting every pair of cars at once, and each car has at least 15 drivers with an iRating in iRating bands both cars have. A hint: less than 1 time in 5. Otherwise: not enough to tell.'),
    h('p','note','By default only one season\'s races count, because iRacing can change the cars\' balance between seasons. Pick All races to pool every season.'));
  sec.append(how);host.replaceChildren(sec)}
function ccWhere(j){const host=$('ccWhere'),W=j.where,cars=j.tel_cars||[];
  const sec=slSec('Where you were faster in each car','ccWhereSec');sec.classList.add('rcsec');
  if(!W){sec.append(h('p','rcsay',cars.length===1?'Your telemetry here is only in the '+cars[0].car+'. '+CC_HOW[2]:'No telemetry of yours in two cars of this class at this track. '+CC_HOW[2]));host.replaceChildren(sec);return}
  const L=W.lap||{};
  sec.append(prFacts([[W.a+' laps',String(W.laps_a)],[W.b+' laps',String(W.laps_b)],[W.a+' best',ok(L.a_best)?fmtT(L.a_best):'Not known'],[W.b+' best',ok(L.b_best)?fmtT(L.b_best):'Not known']]));
  sec.append(h('p','note','The laps in each car come from different sessions, days and setups'+(W.teammate?' (and one of them is '+W.teammate+'\'s)':'')+', so a difference can be the day, the setup or the driver as well as the car.'));
  if(W.rough)sec.append(h('p','rcsay','Fewer than 5 clean laps in a car: a rough look. Only the biggest differences are shown (at most 3), as possible ones.'));
  else sec.append(h('p','note','Shown only when a shuffle test says the difference is unlikely to be chance, counting every corner and measure at once.'));
  const ul=h('ul','ccwlist');(W.rows||[]).forEach(r=>ul.append(h('li','rcsay',mphText(r.text))));   // 2.18: mph
  if((W.rows||[]).length)sec.append(ul);else sec.append(h('p','rcsay',mphText(W.note||'No clear difference.')));
  host.replaceChildren(sec)}
})();
