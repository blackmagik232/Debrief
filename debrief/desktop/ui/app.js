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
// 2.6: error banners stay until the close button is pressed; other banners still fade after a few seconds
function banner(msg,bad,ms){const b=$('banner');$('bannerText').textContent=msg;b.className='banner'+(bad?' bad':'');b.hidden=false;clearTimeout(banner.t);
  if(ms!==0&&!bad)banner.t=setTimeout(()=>b.hidden=true,ms||5000)}
$('bannerClose').addEventListener('click',()=>{clearTimeout(banner.t);$('banner').hidden=true});

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

/* ---------- brand: the team name and join copy come from /api/brand (debrief/brand.py) */
const BRAND0={app_name:'Debrief',team_slug:'midfield-menace',team_name:'Midfield Menace',team_short:'MM',team_url:'https://garage61.net',
  join_title:'Join Midfield Menace on Garage 61',join_text:'Team comparisons need you on the Midfield Menace team on Garage 61. Ask a teammate to invite you, then press Refresh. Your own reviews, practice and prep work without it.'};
let brand=Object.assign({},BRAND0);
const appTitle=()=>(brand.app_name||'Debrief')+' · '+(brand.team_name||BRAND0.team_name);
function applyBrand(){
  document.querySelectorAll('.brandName').forEach(e=>{e.textContent=brand.team_name});
  document.querySelectorAll('.joinTitle').forEach(e=>{e.textContent=brand.join_title});
  document.querySelectorAll('.joinText').forEach(e=>{e.textContent=brand.join_text});
  document.querySelectorAll('a.joinLink').forEach(e=>{if(/^https:\/\//.test(brand.team_url||''))e.href=brand.team_url});
  $('brandTeam').textContent=brand.team_name;$('teamTitle').textContent=teamName||brand.team_name;
  if(!FRAME_VIEWS.includes(current.view))document.title=appTitle()}
async function loadBrand(){try{const j=await api('brand');
    if(j&&typeof j==='object')Object.keys(BRAND0).forEach(k=>{if(typeof j[k]==='string'&&j[k].trim())brand[k]=j[k].trim()})}catch(e){}
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

/* ---------- keyboard: Ctrl+R = Review last race (not a reload), Ctrl+, = Settings */
// 2.8: Ctrl+1 to Ctrl+9 open the pages, in sidebar order (2.12: Setup Lab and Coach are 8 and 9); J and K step
// through your reviews, older and newer
const PAGE_KEYS=['library','season','skills','goals','sessions','prep','team','setuplab','coach'];
const KEY_MSG=/^(review|settings|prev|next|page[1-9])$/;
function shortcut(k){if(k==='review'){startReview();return true}if(k==='settings'){openSettings();return true}
  if(document.querySelector('dialog[open]'))return false;
  if(k==='prev'||k==='next'){stepReview(k);return true}
  const m=/^page([1-9])$/.exec(k);if(m){goNav(PAGE_KEYS[+m[1]-1]);return true}
  return false}
document.addEventListener('keydown',e=>{if(!(e.ctrlKey||e.metaKey)||e.altKey)return;const k=e.key.toLowerCase();
  if(k==='r'&&!e.shiftKey){e.preventDefault();shortcut('review')}
  else if(k===','||e.code==='Comma'){e.preventDefault();shortcut('settings')}
  else if(!e.shiftKey&&/^[1-9]$/.test(k)){e.preventDefault();shortcut('page'+k)}
  else if(e.shiftKey&&k==='m'){e.preventDefault();coachMuteKey()}});
document.addEventListener('keydown',e=>{if(e.ctrlKey||e.metaKey||e.altKey||e.shiftKey)return;const k=e.key.toLowerCase();if(k!=='j'&&k!=='k')return;
  if(e.target&&e.target.closest&&e.target.closest('input,select,textarea,[contenteditable]'))return;
  if(shortcut(k==='j'?'prev':'next'))e.preventDefault()});

/* ---------- views */
const VIEWS=['vFrame','vWelcome','vJob','vSettings','vPrep','vGoals','vTeam','vSessions','vSetupLab','vCoach'];
const FRAME_VIEWS=['review','library','season','skills','goalCharts','prepPage','teamPage','sample'];
const NAV_OF={weekly:'team',sync:'sessions',sessions:'sessions',library:'library',season:'season',skills:'skills',goals:'goals',goalCharts:'goals',prep:'prep',prepPage:'prep',schedule:'prep',team:'team',teamPage:'team',settings:'settings',setuplab:'setuplab',coach:'coach'};
let current={view:null,dir:null};
function show(view){VIEWS.forEach(v=>$(v).hidden=(v!==view));if(view!=='vFrame')document.title=appTitle()}
function markNav(){
  const nav=NAV_OF[current.view]||null;
  document.querySelectorAll('.navbtn').forEach(b=>{const on=b.dataset.nav===nav;b.classList.toggle('active',on);if(on)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current')});
  document.querySelectorAll('.rev').forEach(b=>{const on=current.view==='review'&&b.dataset.dir===current.dir;b.classList.toggle('active',on);if(on)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current')});
  document.body.classList.remove('navopen');
  frameBar();lastRaceCard();
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
/* ---------- welcome + first-run guide (shown until a token is saved, then for the rest of that session) */
let guideOn=false,checkOk=false,firstName=null,installed=false;
function greet(){$('welcomeTitle').textContent=firstName?'Welcome, '+firstName+'.':'Welcome.'}
function updateGuide(){
  const st=(id,cls)=>{$(id).classList.toggle('now',cls==='now');$(id).classList.toggle('done',cls==='done')};
  st('gs1',tokenSaved?'done':'');st('gs2',tokenSaved?'done':'');
  st('gs3',tokenSaved&&checkOk?'done':'now');st('gs4',tokenSaved&&checkOk?'now':'');
  $('gReview').disabled=!tokenSaved;
}
function welcome(){
  greet();
  if(!tokenSaved)guideOn=true;
  const t=$('welcomeText');
  $('guide').hidden=!guideOn;$('welcomeActions').hidden=guideOn;
  if(guideOn){t.textContent='Four quick steps and Debrief is ready to review your races.';updateGuide()}
  else t.textContent='No reviews yet. When your race has uploaded to Garage 61, click Review last race.';
  show('vWelcome');markNav()}

/* ---------- the bar above pages in the frame */
let pages={team:null,prep:[]},resultFor={dir:null,result:null},racecapOn=false;
function frameBar(){
  const v=current.view,bar=$('frameBar');
  const want=v==='review'||(v==='season'&&discordSaved)||v==='prepPage'||v==='teamPage'||v==='skills'||v==='sample'||v==='goalCharts';
  bar.hidden=!want;if(!want)return;
  $('fbResult').hidden=v!=='review';$('fbPrep').hidden=v!=='prepPage';$('fbTeam').hidden=v!=='teamPage';$('fbWeekly').hidden=v!=='teamPage';
  $('fbDiscord').hidden=(v!=='review'&&v!=='season')||!discordSaved;
  $('fbMarkers').hidden=v!=='review';$('fbRaceCap').hidden=v!=='review'||!racecapOn;$('fbScan').hidden=v!=='review';$('fbRival').hidden=v!=='review';
  $('fbSampleBack').hidden=v!=='sample';$('fbGoals').hidden=v!=='goalCharts';
  const pill=$('fbPill');pill.hidden=true;$('fbBar').hidden=true;
  if(v==='sample'){$('fbText').textContent='This is a made-up race, not your laps. Your own reviews will look like this.';
    pill.hidden=false;pill.className='pill no';pill.textContent='Sample';return}
  if(v==='goalCharts'){$('fbText').textContent='How close your reviewed races get to your goals';return}
  if(v==='skills'){const p=profileInfo,rf=p&&p.refreshing;
    $('fbText').textContent=!p?'Driver profile':
      (p.n_races?'Driver profile · '+p.n_races+' race'+(p.n_races===1?'':'s')+' with skill scores'+(p.weakest&&PHASE[p.weakest.phase]?' · most time to find lately: '+PHASE[p.weakest.phase]:''):
       'Driver profile · skill scores appear after a race with at least 6 clean laps');
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

// 2.8: while setting up (no token and no saved reviews) the pages have nothing to show, so the sidebar
// keeps you on the steps. Settings and Help stay open, and Review last race has its own gate.
const onboarding=()=>!tokenSaved&&!groupsCache.some(g=>(g.reviews||[]).length);
function navGate(){const off=onboarding();
  document.querySelectorAll('.navbtn[data-nav]').forEach(b=>{if(b.dataset.nav==='settings')return;
    if(b.dataset.t==null)b.dataset.t=b.title||'';
    b.setAttribute('aria-disabled',String(off));b.title=off?'Finish the setup steps first.':b.dataset.t})}
function goNav(name){
  if(name!=='settings'&&onboarding()){current={view:'welcome'};welcome();banner('Finish the setup steps first, then the rest of Debrief opens up.');return}
  ({sessions:openSessions,library:openLibrary,season:openSeason,skills:openSkills,goals:openGoals,prep:openPrep,team:openTeam,settings:openSettings,
    setuplab:()=>openSetupLab(),coach:openCoach})[name]()}
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
  setMsg('chkMsg','Checking...');setMsg('tokMsg','');$('btnCheck').disabled=true;
  let r;try{r=await post('check')}catch(e){$('btnCheck').disabled=false;setMsg('chkMsg','');connectionLost();return}
  if(!r.ok){$('btnCheck').disabled=false;setMsg('chkMsg',r.error,'bad');return}
  $('chkOut').hidden=true;$('chkOut').replaceChildren();$('chkProg').hidden=false;$('chkBar').firstElementChild.style.width='4%';
  $('gChkOut').hidden=true;$('gChkOut').replaceChildren();$('gChkProg').hidden=false;$('gChkBar').firstElementChild.style.width='4%';
  $('btnCheck').disabled=true;setMsg('chkMsg','Checking...');poll();
}
// 2.6: Review last race waits for a saved token (aria-disabled, so the tooltip still shows why)
function reviewGate(){const b=$('btnReview');const off=!tokenSaved;b.setAttribute('aria-disabled',String(off));
  b.title=off?'Finish step 3 first.':'Review last race (Ctrl+R)';if(off)$('syncStrip').hidden=true;navGate()}
function startReview(){if(!tokenSaved){current={view:'welcome'};welcome();return}startJob('review')}
$('btnReview').addEventListener('click',startReview);

// 2.8: request counts live in Settings > About. The job's own lines are for the log too, so they keep
// theirs and the window takes them out here.
const noCounts=t=>String(t==null?'':t).replace(/\s*\((?:no |about |up to )?\d*\s*(?:Garage 61 )?requests?\)/gi,'');
const countLine=t=>/^\s*Garage 61 requests\b/i.test(String(t));
function resetJobView(kind){
  if(kind==='review'&&!(lastJob&&lastJob.title))TITLES.review=TITLES0.review;   // a plain "Review last race" never keeps an earlier job's title
  $('jobStop').hidden=kind!=='scan'&&kind!=='rival';$('jobStop').disabled=false;
  $('jobTitle').textContent=TITLES[kind]||'Working';$('jobErrTitle').textContent=STOPPED[kind]||'It stopped';
  $('jobEyebrow').textContent='Working';$('jobErr').hidden=true;$('jobSteps').replaceChildren();
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
    setBar(s.frac||0);$('jobNow').textContent=s.state==='error'?'Stopped.':noCounts(s.message);
    if($('jobTitle').textContent!==TITLES[s.kind]&&s.state==='running')resetJobView(s.kind);
    const ol=$('jobSteps');const steps=(s.steps||[]).filter(t=>!countLine(t));
    if(ol.children.length!==steps.length){ol.replaceChildren(...steps.map(t=>h('li',null,noCounts(t))));ol.scrollTop=ol.scrollHeight}
    if(s.state==='running')$('jobEyebrow').textContent='Working';
    if(s.state!=='running')$('jobStop').hidden=true;
    if(s.state==='error'&&!handled.has(s.id)){handled.add(s.id);
      $('jobEyebrow').textContent='Stopped';$('jobErrMsg').textContent=s.error;$('jobErr').hidden=false;
      $('jobBar').parentElement.className='progress fail';
      if(current.view!=='job')banner((STOPPED[s.kind]||'It stopped')+': '+s.error,true,9000)}
    if(s.state==='done'&&!handled.has(s.id)){handled.add(s.id);
      $('jobEyebrow').textContent='Done';$('jobBar').parentElement.className='progress done';
      const stay=current.view==='job';  // only jump to the result if the person is still watching
      if(s.kind==='scan'){loadReviews().then(()=>{if(s.result&&s.result.review){if(stay)openReview(s.result.review);reloadFrame();banner('Race log added from the replay'+(s.result.confirmed?'.':'. Garage 61 couldn\'t confirm it\'s the same race, so check the names look right.'),false,9000)}})}
      else if(s.kind==='rival'){if(s.result&&s.result.review){if(stay)openReview(s.result.review);reloadFrame();banner((s.result.name||'The rival')+'\'s lap '+s.result.lap+' is in Compare, as a rival with no pedal data.',false,9000)}}
      else if(s.kind==='review')loadReviews().then(()=>{if(s.result&&s.result.review){if(stay)openReview(s.result.review);banner('Review ready.')}});
      else loadPages().then(()=>{const team=s.kind==='team'||s.kind==='weekly';
        if(s.result&&s.result.page){if(stay)openPage(s.result.page,team?'teamPage':'prepPage');
        banner(s.kind==='weekly'?'This week\'s leaderboards updated.':
          s.kind==='team'?'Team hub updated.':'Prep page ready.')}if(current.view==='prep')renderPrepPages();if(s.kind==='weekly')loadWeekly()});
    }
  }
  if(s.kind==='setuplab')renderSlJob(s);
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
      lines.forEach(t=>{const o=t&&typeof t==='object';const tx=o?String(t.text||t.line||''):String(t);const lv=o?t.level:null;
        out.append(h('li',lv==='info'?'info':(lv==='error'||lv==='bad'||(!lv&&/NOT available/.test(tx)))?'bad':null,tx))});
      if(s.state==='error')out.append(h('li','bad',s.error));
      out.hidden=!out.children.length;
      setMsg('chkMsg',s.state==='done'?'Connection works.':'Connection check failed.',s.state==='done'?'ok':'bad');
      // the same result in the first-run guide
      const gout=$('gChkOut');gout.replaceChildren(...Array.from(out.children).map(li=>li.cloneNode(true)));
      gout.hidden=!gout.children.length;$('gChkProg').hidden=true;
      checkOk=s.state==='done';
      const e401=/\b401\b|rejected the token/i.test(String(s.error||''));
      setMsg('gTokMsg',checkOk?'Connection works. Debrief is downloading your history in the background. You can review your last race right now.':
        (e401?'Garage 61 didn\'t accept that token. Check the token and paste it again.':(s.error||'The connection check failed.')),checkOk?'ok':'bad');
      updateGuide();
      if(s.state==='done'){loadSettings();loadTeams(false);loadSessions().then(()=>startSync(true))}
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
  const pill=$('weeklyPill');pill.hidden=!rows.length;pill.textContent=rows.length+' series';pill.className='pill'+(can?' ok':'');
  const p=(j&&j.plan)||{};const sk=(Array.isArray(p.skipped)?p.skipped:[]).concat(Array.isArray(p.notes)?p.notes:[]);
  showWarnings('weeklySkipped',[...new Set(sk.map(x=>typeof x==='string'?x:(x&&typeof x==='object'?[x.name,x.note||x.reason].filter(v=>typeof v==='string'&&v).join(': '):'')))],'Not included:')}
function startWeekly(){if(lastJob&&lastJob.state==='running'&&lastJob.kind==='weekly'){openJob();return}startJob('weekly',{})}
$('btnWeekly').addEventListener('click',startWeekly);$('fbWeekly').addEventListener('click',startWeekly);

/* ---------- Post to Discord (2.5). The review page draws its share card when asked (postMessage); the
   window stages it on the server (no network), shows it, and posts only when you press Post in the dialog. */
let discordSaved=false,discordHint=null,share=null,staged=null;
function shareId(){const a=new Uint8Array(12);crypto.getRandomValues(a);return Array.from(a,b=>b.toString(16).padStart(2,'0')).join('')}
function requestShare(){if((current.view!=='review'&&current.view!=='season')||!discordSaved||share)return;
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
  $('resTitle').textContent=resultFor.result?'Edit race result':'Add race result';$('resDelete').hidden=!resultFor.result;
  $('resDelete').dataset.armed='';$('resDelete').textContent='Delete result';setMsg('resMsg','');setMsg('resCsvMsg',resultFor.dir===dir&&!resultFor.result&&resultFor.scan?'Finish filled in from your replay scan, on the road. The results file replaces it with the official one.':'');$('resCsvWho').hidden=true;
  const d=$('dlgResult');if(d.showModal)d.showModal();else d.setAttribute('open','');$('r_finish_pos').focus()}
/* ---------- 2.7.5: fill the result form from iRacing's results file; nothing is saved until Save */
function fillFromCsv(d){RES_FIELDS.filter(k=>k!=='notes').forEach(k=>{if(d.fields[k]!=null)$('r_'+k).value=String(d.fields[k])});
  setMsg('resCsvMsg','Filled in for '+d.name+(d.class?' ('+d.class+')':'')+'. Check it, then press Save result.','ok')}
$('resCsv').addEventListener('change',async()=>{const f=$('resCsv').files[0];$('resCsv').value='';const dir=current.dir;if(!f||!dir)return;
  if(f.size>2*1024*1024){setMsg('resCsvMsg','That file is too big for a results CSV.','bad');return}
  setMsg('resCsvMsg','Reading '+f.name+'...');let r;
  try{r=await fetch(BASE+'api/result-csv?review='+encodeURIComponent(dir),{method:'POST',headers:{'X-Debrief':'1','Content-Type':'application/octet-stream'},body:f}).then(x=>x.json())}
  catch(e){setMsg('resCsvMsg','Couldn\'t send the file.','bad');return}
  if(!r||!r.ok){setMsg('resCsvMsg',(r&&r.error)||'Couldn\'t read that file.','bad');return}
  const who=$('resCsvWho');if(r.matched){who.hidden=true;fillFromCsv(r.matched);return}
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
  ['Coach','Press Start coaching, then drive. A few short spoken cues from your own best lap and the corners your reviews say cost you time. Keep Debrief open while you drive.']],
 words:[['Clean lap','A full lap Garage 61 didn\'t flag: no incident and not a pit lap.'],['Typical lap','The middle of your clean laps. Half were quicker, half slower.'],
  ['Comparison lap','A lap by a teammate, a followed driver or, with RaceCap, a rival, that yours are measured against.'],['Best sectors added up','Your best time through each part of the track, added together. A guide, not a lap you must be able to drive.'],
  ['Brake point','Where on the track you first brake.'],['Apex','The slowest point of a corner, near the inside.'],['Coasting','Time with neither pedal pressed.'],['Stint','A run of laps between pit stops.'],
  ['Possibly traffic','A lap lost time in one short stretch your other laps didn\'t. It could be another car or a mistake.'],['Race log','Passes, fights, incidents and pit stops from a replay scan. Press Scan this race on a review, or link a scan from RaceCap.'],
  ['On the road','The order the cars crossed the line in the replay. The iRacing results are the official order.'],['Rolling or standing start','Whether the field was moving at the green flag.'],
  ['Resolve markers','A file DaVinci Resolve imports to mark your race\'s moments on a timeline.'],
  ['Setup','The car settings you drive with (pressures, camber, springs, wing...). Setup Lab reads them from your telemetry files, because iRacing\'s .sto setup files are locked.'],
  ['Open or fixed setup','In an open setup series you can change the setup; in a fixed one it is locked, so Setup Lab has nothing to suggest.'],
  ['Laps counted','The clean laps Setup Lab compares setups on: not out laps, first flying laps, laps in traffic or laps far off the pace.'],
  ['Reference lap','Your best clean lap for this car and track. The coach measures you against it.'],
  ['Focus corner','One of the corners your race reviews say keep costing you time. The coach talks about one or two at a time.'],
  ['Brake marker','The cue the coach says on the straight before a focus corner, so you think about that braking point.']],
 keys:[['Ctrl+R','Review last race'],['Ctrl+,','Settings'],['Ctrl+1 to Ctrl+9','Progress, Season, Skills, Goals, All sessions, Prepare, Team, Setup Lab, Coach'],['Ctrl+Shift+M','Mute or unmute the coach'],['J','The review before this one'],['K','The review after this one'],['Tab','Move to the next button or chart'],['Arrow keys','Move along a chart that has focus'],['Esc','Close a window like this one']]};
function showHelp(k){const b=$('helpBody');b.replaceChildren(...(k==='tour'?[h('p','note','A quick look at how Debrief works.')]:[]),...HELP[k].map(([t,d])=>{const p=h('p');p.append(h('b',null,t+': '),document.createTextNode(d));return p}));
  document.querySelectorAll('[data-help]').forEach(x=>x.setAttribute('aria-pressed',x.dataset.help===k?'true':'false'))}
function openHelp(k){showHelp(k);openDlg($('dlgHelp'))}
$('btnHelp').addEventListener('click',()=>openHelp('tour'));$('helpClose').addEventListener('click',()=>closeDlg($('dlgHelp')));
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
  try{r=await post('result',{review:dir,action:'save',fields})}catch(err){connectionLost();return}finally{$('resSave').disabled=false}
  if(!r.ok){setMsg('resMsg',r.error,'bad');return}
  resultFor={dir,result:r.result||fields};closeResult();frameBar();reloadFrame();banner('Race result saved.')});
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
const SL_CONF={reading:['From a reading','ok'],guess:['A guess','no'],'low-medium':['Low to medium','']};
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
  if(!j.folder_ok)slEmpty('Debrief can\'t find your telemetry folder','iRacing saves telemetry in Documents\\iRacing\\telemetry. If yours is somewhere else, set the folder in Settings > Setup Lab and Coach.');
  else if(!j.indexed)slEmpty(j.running?'Reading your telemetry...':'Not read yet','Press Read my telemetry. The first time reads every file, so a few hundred files take a few minutes; after that only new ones.');
  else if(!combos.length)slEmpty('No telemetry files yet','In iRacing, press Alt+L in the car to record telemetry (or switch on automatic recording in iRacing\'s options), then press Read my telemetry.');
  else loadSlReport()}
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
    plural(j.files,'telemetry file')+' for '+carTracks(n)+'.'+(ok(j.unread)&&j.unread>0?' '+plural(j.unread,'new file')+' since the last read.':'')+(j.bad?' '+plural(j.bad,'file')+' couldn\'t be read.':'');
  $('slProg').hidden=!run;$('slNow').hidden=!run}
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
  if(!j.ok){slEmpty('That didn\'t load',j.error||'Try again.');return}
  renderSlReport(j.report)}
function slSec(title,id){const d=h('section','panel slsec');if(id)d.id=id;d.append(h('h3',null,title));return d}
function confBadge(c){const x=SL_CONF[c]||[String(c||''),''];const b=h('span','pill'+(x[1]?' '+x[1]:''),x[0]);b.title='How sure this is';return b}
function renderSlReport(R){const out=document.createDocumentFragment();
  const head=h('div','panel slhead');const tt=h('div','pvtt');tt.append(h('h2','pvtrack',R.track||R.track_code),h('p','pvby',R.car||R.car_path));head.append(tt);
  const kind=R.series_setup||'unknown',ser=h('div','slser');
  const pill=h('span','pill '+(kind==='open'?'ok':kind==='fixed'?'no':''),kind==='open'?'Open setup':kind==='fixed'?'Fixed setup':'Setup rules unknown');
  ser.append(pill,h('span','note',R.series_setup_source?'From '+R.series_setup_source+'.':'Debrief can\'t tell yet. Set it here if you know.'));
  const lab=h('label','field slover');lab.htmlFor='slSeries';lab.append(h('span',null,'This series is'));const sel=document.createElement('select');sel.id='slSeries';
  [['','Not sure (let Debrief tell)'],['open','Open setup'],['fixed','Fixed setup']].forEach(([v,t])=>{const o=h('option',null,t);o.value=v;sel.append(o)});
  sel.value=R.series_override||'';sel.addEventListener('change',()=>slSaveSeries(sel.value));lab.append(sel);ser.append(lab);head.append(ser);out.append(head);
  if(!R.files){const e=h('div','panel pvnone');e.append(h('h2','pvtrack','No telemetry for this car here yet'),h('p','lede','Drive it with telemetry recording on (Alt+L in the car), then press Read my telemetry.'));out.append(e);$('slReport').replaceChildren(out);return}
  const S=R.suggestions||{};
  if(kind==='fixed'){const sec=slSec('Fixed setup series');const it=(S.items||[]).find(i=>i.rule==='fixed_series');
    sec.append(h('p',null,it?it.text:'Fixed setup series: the setup is locked, so there are no setup changes to try.'));out.append(sec);$('slReport').replaceChildren(out);return}
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
  if(C.verdict)fc.append(h('p','slverdict',C.verdict));
  const rows=C.setups||[];
  if(rows.length){const wrap=h('div','tblwrap'),t=h('table','pacetbl sltbl'),hr=h('tr');['Setup','Typical lap','Best','Laps counted','Left out','Track'].forEach(x=>hr.append(h('th',null,x)));
    const th=h('thead');th.append(hr);const tb=h('tbody');
    rows.forEach(r=>{const p=r.pace||{},lo=p.left_out||{},tr=h('tr'),c0=h('th',null,r.label);c0.scope='row';
      const left=[lo.warmup?lo.warmup+' warm-up':'',lo.traffic?lo.traffic+' in traffic':'',lo.slow?lo.slow+' off pace':''].filter(Boolean).join(', ')||'none';
      tr.append(c0,h('td','num',ok(p.median)?fmtT(p.median):'–'),h('td','num',ok(p.best)?fmtT(p.best):'–'),h('td','num',String(p.clean_laps||0)),h('td',null,left),
        h('td','num',(ok(p.surface_temp)?Math.round(p.surface_temp)+' °C':'–')+(p.track_state&&p.track_state!=='dry'?' '+p.track_state:'')));tb.append(tr)});
    t.append(th,tb);wrap.append(t);fc.append(wrap)}
  (C.pairs||[]).forEach(pr=>{const box=h('div','slpair');if(pr.text&&String(C.verdict||'').trim()!==String(pr.text).trim())box.append(h('p',null,pr.text));
    (pr.confounders||[]).forEach(x=>box.append(h('p','note warn','Not a fair test: '+x+'.')));
    if(box.children.length)fc.append(box)});
  const miss=rows.flatMap(r=>r.missing||[]);if(miss.length)fc.append(h('p','note','Still missing: '+miss.join('; ')+'.'));
  fc.append(h('p','note','A lap counts when it is clean, not an out lap or the first flying lap, not spent alongside another car, and not far off that run\'s pace. Debrief only names a faster setup when the laps clearly say so.'));
  out.append(fc);
  // try next
  const tn=slSec('Try next','slNext'),items=S.items||[];
  items.filter(i=>i.kind==='note').forEach(i=>tn.append(h('p','note warn',i.text)));
  if(S.next){const n=S.next,card=h('div','slnextcard');const top=h('div','phead');top.append(h('span','eyebrow','One change to try next'),confBadge(n.confidence));card.append(top);
    card.append(h('p','slaction',n.action||n.text));if(n.why)card.append(h('p',null,n.why));
    if(n.check)card.append(h('p','note','Then check: '+n.check));if(n.data)card.append(h('p','note','From: '+n.data));tn.append(card)}
  else tn.append(h('p','note',R.suggestions?'Nothing to change from what the telemetry shows so far.':'Nothing to suggest until a setup is recorded.'));
  const others=items.filter(i=>i!==S.next&&!i.next&&i.kind!=='note');
  if(others.length){tn.append(h('h4',null,S.next?'Other ideas, one at a time':'Ideas'));const u=h('ul','pvlist slideas');
    others.forEach(i=>{const li=h('li'),top=h('div','slidea');top.append(h('b',null,i.setting||'Note'),confBadge(i.confidence));li.append(top);
      li.append(h('span','sub2',(i.kind==='change'&&i.action?i.action+' ':'')+(i.why||i.text||'')));if(i.check)li.append(h('span','sub2','Check: '+i.check));u.append(li)});tn.append(u)}
  out.append(tn);
  // notes
  const notes=(R.notes||[]).filter(x=>typeof x==='string'&&x.trim()),bad=R.bad_files||[];
  if(notes.length||bad.length){const ns=slSec('Notes','slNotes');const u=h('ul','pvlist');notes.forEach(x=>u.append(h('li','note',x)));ns.append(u);
    if(bad.length){const d=h('details','sub');d.append(h('summary',null,plural(bad.length,'file')+' Debrief couldn\'t read'));const bu=h('ul','plain mono');
      bad.slice(0,50).forEach(b=>bu.append(h('li',null,b.file+': '+b.error)));d.append(bu);ns.append(d)}
    out.append(ns)}
  $('slReport').replaceChildren(out)}
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
    if(current.view!=='setuplab')banner('Your telemetry has been read. Open Setup Lab to see your setups.')}
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
let co={st:null,timer:null,seq:0,filled:false,dirty:false};
const CO_STATE={coaching:['Coaching','ok'],waiting:['Waiting for iRacing',''],no_reference:['No reference lap yet','no'],starting:['Starting',''],
  finished:['Finished',''],error:['Stopped','no'],stopped:['Off','']};
const fmtClock=t=>{if(!ok(t))return '';t=Math.max(0,Math.floor(t));const hh=Math.floor(t/3600),m=Math.floor(t%3600/60),s=t%60;return (hh?hh+':'+String(m).padStart(2,'0'):String(m))+':'+String(s).padStart(2,'0')};
function openCoach(){current={view:'coach',dir:null};show('vCoach');markNav();co.filled=false;coachPoll()}
async function coachPoll(){const seq=++co.seq;clearTimeout(co.timer);let j;try{j=await api('coach/status')}catch(e){connectionLost();return}
  if(seq!==co.seq)return;if(j&&j.ok)renderCoach(j);
  if(current.view==='coach'||(j&&j.running))co.timer=setTimeout(coachPoll,1000)}
function renderCoach(st){co.st=st;const run=!!st.running,state=CO_STATE[st.state]||[st.state||'',''];
  const nb=document.querySelector('.navbtn[data-nav=coach]');if(nb)nb.classList.toggle('live',run);
  if(current.view!=='coach')return;
  $('coTitle').textContent=run?state[0]:'Coach off';const pill=$('coPill');pill.className='pill'+(state[1]?' '+state[1]:'');pill.textContent=run||st.state==='error'?state[0]:'Off';
  $('coText').textContent=st.text||'';
  const b=$('coStart');b.textContent=run?'Stop coaching':'Start coaching';b.classList.toggle('primary',!run);b.disabled=false;
  const m=$('coMute'),hk=st.hotkey||{};m.disabled=!run;m.setAttribute('aria-pressed',String(!!st.muted));
  m.replaceChildren(document.createTextNode(st.muted?'Unmute ':'Mute '),h('kbd',null,hk.key||'Ctrl+Shift+M'));
  m.title=run&&!hk.on?'This key works while the Debrief window is in front.':'Works from the car too, while Debrief is open.';
  const q=$('coQuiet');q.hidden=!(run&&st.quiet_reason);q.textContent=st.quiet_reason?'Quiet now: '+st.quiet_reason+'.':'';
  if(st.state==='error'&&st.error)setMsg('coMsg',st.error,'bad');
  const R=st.reference;$('coRef').textContent=R?R.text||R.lap_text:'None yet';
  $('coRefNote').textContent=R?(R.source&&R.source.kind==='live'?'A lap you drove just now.':'Your best clean lap in your telemetry files'+(st.car?' for the '+st.car+(st.track?' at '+st.track:''):'')+'.'):
    'Debrief uses your best clean lap for the car and track iRacing shows, from your telemetry files. No lap yet? Drive a clean one and it becomes the reference.';
  const L=st.last_lap,ll=$('coLast');ll.hidden=!(L&&ok(L.time));
  if(L&&ok(L.time))ll.textContent='Last lap '+fmtT(L.time)+(ok(L.delta)?', '+Math.abs(L.delta).toFixed(2)+' s '+(L.delta>0?'down on':'up on')+' your reference':'')+(L.clean===false?' (not clean)':'')+'.';
  const W=st.weak||[],wu=$('coWeak');wu.replaceChildren(...W.map(w=>{const li=h('li');li.append(h('b',null,w.name),h('span','sub2',(ok(w.lost_s)?'about '+w.lost_s.toFixed(2)+' s a lap to find':'a corner that keeps costing you')+(w.id?' (the review calls it '+w.id+')':'')));return li}));
  $('coWeakNote').textContent=st.weak_note||(W.length?'From your race reviews of this car at this track. One focus at a time.':R?'From your race reviews of this car at this track.':'Shows once the coach knows the car and track.');
  const sp=(st.spoken||[]).slice().reverse(),su=$('coSpoken');
  su.replaceChildren(...(sp.length?sp.map(c=>{const li=h('li');li.append(h('span','num cot',fmtClock(c.t)),h('span',null,c.text));return li}):[h('li','note','Nothing said yet.')]));
  $('coSpokenNote').textContent=sp.length?'Newest first, session time':'';
  if(!co.filled||!co.dirty){fillCoachSettings(st.settings||{});co.filled=true}}
function fillCoachSettings(s){$('coInRaces').checked=!!s.in_races;$('coMarkers').checked=s.markers!==false;$('coFeedback').checked=s.feedback!==false;
  $('coLapDelta').checked=s.lap_delta!==false;$('coBeep').checked=!!s.brake_beep;$('coTopN').value=String(Math.max(1,Math.min(3,s.top_n||2)));
  $('coVol').value=String(ok(s.volume)?s.volume:100);$('coRate').value=String(ok(s.rate)?s.rate:0);coRanges()}
function coRanges(){$('coVolV').textContent=$('coVol').value+'%';const r=+$('coRate').value;$('coRateV').textContent=r>0?'+'+r:String(r)}
['coVol','coRate'].forEach(id=>$(id).addEventListener('input',()=>{coRanges();co.dirty=true}));
$('fCoach').addEventListener('change',()=>{co.dirty=true});
$('fCoach').addEventListener('submit',async e=>{e.preventDefault();setMsg('coSetMsg','Saving...');
  const settings={in_races:$('coInRaces').checked,markers:$('coMarkers').checked,feedback:$('coFeedback').checked,lap_delta:$('coLapDelta').checked,
    brake_beep:$('coBeep').checked,top_n:+$('coTopN').value,volume:+$('coVol').value,rate:+$('coRate').value};
  let r;try{r=await post('coach/settings',{settings})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('coSetMsg',r.error,'bad');return}co.dirty=false;fillCoachSettings(r.settings||settings);
  setMsg('coSetMsg',co.st&&co.st.running?'Saved. The coach uses them now.':'Saved.','ok')});
$('coTest').addEventListener('click',async()=>{setMsg('coSetMsg','Speaking...');$('coTest').disabled=true;let r;
  try{r=await post('coach/test-voice')}catch(e){connectionLost();return}finally{$('coTest').disabled=false}
  if(!r.ok){setMsg('coSetMsg',r.error||'The voice didn\'t work.','bad');return}
  const v=r.voice||{};setMsg('coSetMsg',r.spoken?'You should hear: "Debrief coach. Brake marker, T3." Save first to test new volume or speed.':'No sound: '+(v.reason||'speech isn\'t available on this computer')+'.',r.spoken?'ok':'bad')});
$('coStart').addEventListener('click',async()=>{const run=!!(co.st&&co.st.running);setMsg('coMsg',run?'Stopping...':'Starting...');$('coStart').disabled=true;let r;
  try{r=await post(run?'coach/stop':'coach/start')}catch(e){connectionLost();return}
  $('coStart').disabled=false;if(!r.ok){setMsg('coMsg',r.error||'The coach didn\'t start.','bad');return}
  setMsg('coMsg',run?'Stopped.':'Started. It talks once you are on track with a reference lap.','ok');if(r.status)renderCoach(r.status);coachPoll()});
async function coachMute(){let r;try{r=await post('coach/mute')}catch(e){connectionLost();return}
  if(!r.ok){setMsg('coMsg',r.error,'bad');return}coachPoll()}
$('coMute').addEventListener('click',coachMute);
function coachMuteKey(){if(co.st&&co.st.running)coachMute()}

/* ---------- settings */
function setMsg(id,t,cls){const e=$(id);e.textContent=t||'';e.className='msg'+(cls?' '+cls:'')}
function fillSettings(v){
  tokenSaved=!!v.token_saved;reviewGate();
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
  if(v.token_saved&&!fillSettings.toured){fillSettings.toured=true;let seen=true;try{seen=localStorage.getItem('debrief.tourSeen')==='1';if(!seen)localStorage.setItem('debrief.tourSeen','1')}catch(e){}if(!seen)banner('New here? Press Help in the sidebar for a quick tour and what the words mean.',false,9000)}
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
  if(s&&s.state==='running'){lastJob=s;if(s.kind==='sync'){renderSync(s);if(groups.length)openLibrary();else welcome()}
    else if(s.kind!=='check'){resetJobView(s.kind);openJob()}poll();return}
  // (if the user already clicked something while the lists loaded, stay there)
  if(current.view==null){if(groups.length)openLibrary();else welcome()}
  reviewGate();
  if(tokenSaved)startSync(true);   // catch up with Garage 61 every time Debrief opens
  checkUpdate('auto');
  coachPoll();                     // once: keeps polling only if the coach is already running
})();
})();
