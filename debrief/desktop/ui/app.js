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
function shortcut(k){if(k==='review'){startReview();return true}if(k==='settings'){openSettings();return true}return false}
document.addEventListener('keydown',e=>{if(!(e.ctrlKey||e.metaKey)||e.altKey)return;const k=e.key.toLowerCase();
  if(k==='r'&&!e.shiftKey){e.preventDefault();shortcut('review')}
  else if(k===','||e.code==='Comma'){e.preventDefault();shortcut('settings')}});

/* ---------- views */
const VIEWS=['vFrame','vWelcome','vJob','vSettings','vPrep','vGoals','vTeam','vSessions'];
const FRAME_VIEWS=['review','library','season','skills','prepPage','teamPage'];
const NAV_OF={weekly:'team',sync:'sessions',sessions:'sessions',library:'library',season:'season',skills:'skills',goals:'goals',prep:'prep',prepPage:'prep',schedule:'prep',team:'team',teamPage:'team',settings:'settings'};
let current={view:null,dir:null};
function show(view){VIEWS.forEach(v=>$(v).hidden=(v!==view));if(view!=='vFrame')document.title=appTitle()}
const LIB_TABS=['library','season','skills'];
function markNav(){
  const nav=NAV_OF[current.view]||null;
  document.querySelectorAll('.navbtn').forEach(b=>{const on=b.dataset.nav===nav;b.classList.toggle('active',on);if(on)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current')});
  $('libGroup').classList.toggle('on',LIB_TABS.includes(nav));
  document.querySelectorAll('.rev').forEach(b=>{const on=current.view==='review'&&b.dataset.dir===current.dir;b.classList.toggle('active',on);if(on)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current')});
  document.body.classList.remove('navopen');
  frameBar();lastRaceCard();
}
// where the frame really is: links inside it (library -> review) don't change f.src, and the sandboxed page
// can't be looked into, so its bridge script tells us (see the 'message' listener below)
let frameHref=null;
const noQuery=u=>{const s=String(u||'');const i=s.indexOf('#');const a=i<0?s:s.slice(0,i),hs=i<0?'':s.slice(i);return a.split('?')[0]+hs};
// a URL the frame isn't on yet, so setting src always loads the page again (the server ignores the query)
function fresh(u){const i=u.indexOf('#');const a=i<0?u:u.slice(0,i),hs=i<0?'':u.slice(i);return a.split('?')[0]+'?r='+Date.now().toString(36)+hs}
function openFrame(rel){const f=$('frame');const want=BASE+rel;const wantDoc=want.split('#')[0];
  const at=noQuery(frameHref||f.src);const atDoc=at.split('#')[0];
  if(at===want){show('vFrame');return}
  // same page, other #section: a hash-only change jumps there (the library page follows hashchange);
  // the same address again (or a page the frame left) loads fresh
  if(atDoc===wantDoc&&want.includes('#'))f.src=want;
  else f.src=fresh(want);
  frameHref=want;
  // keep the current screen until a big review page has loaded, so there's no blank flash
  if($('vFrame').hidden){clearTimeout(openFrame.t);openFrame.t=setTimeout(()=>show('vFrame'),4000);
    f.addEventListener('load',()=>{clearTimeout(openFrame.t);if(FRAME_VIEWS.includes(current.view))show('vFrame')},{once:true})}}
function openReview(dir){current={view:'review',dir};openFrame('lib/reviews/'+encodeURIComponent(dir)+'/index.html');markNav();loadResult(dir)}
function openLibrary(){
  if(!libFolder){current={view:'welcome'};welcome();return}
  current={view:'library',dir:null};openFrame('lib/index.html');markNav()}
// 2.6: the newest review, pinned above the Library page so the last race is always one click away
function newestReview(){const all=[];groupsCache.forEach(g=>(g.reviews||[]).forEach(r=>all.push(Object.assign({track:g.track,car:g.car},r))));
  all.sort((a,b)=>String(b.started_at||b.date||'').localeCompare(String(a.started_at||a.date||''))||String(b.dir).localeCompare(String(a.dir)));return all[0]||null}
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
let pages={team:null,prep:[]},resultFor={dir:null,result:null};
function frameBar(){
  const v=current.view,bar=$('frameBar');
  const want=v==='review'||v==='prepPage'||v==='teamPage'||v==='skills';
  bar.hidden=!want;if(!want)return;
  $('fbResult').hidden=v!=='review';$('fbPrep').hidden=v!=='prepPage';$('fbTeam').hidden=v!=='teamPage';$('fbWeekly').hidden=v!=='teamPage';
  $('fbDiscord').hidden=v!=='review'||!discordSaved;
  const pill=$('fbPill');pill.hidden=true;$('fbBar').hidden=true;
  if(v==='skills'){const p=profileInfo,rf=p&&p.refreshing;
    $('fbText').textContent=!p?'Driver profile':
      (p.n_races?'Driver profile · '+p.n_races+' race'+(p.n_races===1?'':'s')+' with skill scores'+(p.weakest&&PHASE[p.weakest.phase]?' · most time to find lately: '+PHASE[p.weakest.phase]:''):
       'Driver profile · skill scores appear after a race with at least 6 clean laps');
    if(rf&&rf.total){pill.hidden=false;pill.className='pill';pill.textContent='Adding scores to older reviews: '+rf.done+' of '+rf.total;
      $('fbBar').hidden=false;$('fbBar').firstElementChild.style.width=Math.round(100*rf.done/rf.total)+'%'}
    return}
  if(v==='review'){const r=resultFor.dir===current.dir?resultFor.result:null;
    $('fbText').textContent=r?'Race result saved':'Add your finishing position, incidents and iRating to this race.';
    $('fbResult').textContent=r?'Edit race result':'Add race result';
    if(r&&ok(r.finish_pos)){pill.hidden=false;pill.className='pill ok';pill.textContent='P'+r.finish_pos+(ok(r.field_size)?' of '+r.field_size:'')}}
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
  if(m&&typeof m==='object'&&m.type==='debrief-key'&&(m.key==='review'||m.key==='settings')){shortcut(m.key);return}
  if(!m||typeof m!=='object'||m.type!=='debrief-frame'||typeof m.href!=='string')return;
  frameHref=m.href;themeFrame(getTheme());framePlace(noQuery(m.href),typeof m.title==='string'?m.title:'')});
$('frame').addEventListener('load',()=>themeFrame(getTheme()));
function framePlace(path,title){
  if(!path.startsWith(BASE))return;
  const rest=path.slice(BASE.length);const rel=rest.split('#')[0].split('?')[0];const hash=(rest.split('#')[1]||'');
  const m=rel.match(/^lib\/reviews\/([^/]+)\/index\.html$/);
  if(m){const dir=decodeURIComponent(m[1]);const changed=current.dir!==dir;current={view:'review',dir};if(changed)loadResult(dir)}
  else if(rel==='lib/index.html'||rel==='lib/'){const was=current.view;current={view:hash==='season'?'season':hash==='skills'?'skills':'library',dir:null};
    if(current.view==='skills'&&was!=='skills')loadProfile()}
  else if(/^lib\/prep\/[^/]+\.html$/.test(rel))current={view:'prepPage',dir:null,page:decodeURIComponent(rel.slice(4))};
  else if(rel==='lib/team/index.html'){if(current.view!=='teamPage')loadWeekly();current={view:'teamPage',dir:null,page:'team/index.html'}}
  if(title)document.title=/(^Debrief Library$|· Library$)/.test(title)?appTitle():'Debrief · '+title;
  // (links that leave the app are opened in the normal browser by the bridge itself)
  markNav();
}

document.addEventListener('click',e=>{
  const n=e.target.closest('[data-nav]');
  if(n){({sessions:openSessions,library:openLibrary,season:openSeason,skills:openSkills,goals:openGoals,prep:openPrep,team:openTeam,settings:openSettings})[n.dataset.nav]();return}
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
  markNav();return groupsCache;
}

/* ---------- jobs: review / prep / team share the progress screen; check stays in Settings */
const TITLES0={weekly:'Updating this week\'s team leaderboards',update:'Updating Debrief',sync:'Syncing your Garage 61 history',review:'Reviewing your last race',prep:'Building your track prep page',team:'Updating the team hub',check:'Checking the connection'};
const STOPPED={weekly:'The leaderboard refresh stopped',update:'The update stopped',review:'The review stopped',prep:'The prep page stopped',team:'The team refresh stopped'};
const TITLES=Object.assign({},TITLES0);   // the review title can change per job; TITLES0 keeps the defaults
let handled=new Set(),polling=false,lastJob=null,lastStart=null;
async function startJob(kind,body){
  if(lastJob&&lastJob.state==='running'&&lastJob.kind!=='sync'){
    if(lastJob.kind===kind&&kind!=='check')openJob();
    else banner('Debrief is busy: '+(lastJob.title||TITLES[lastJob.kind]||'a job').toLowerCase()+'. Wait for it to finish.',true);
    return false}
  let r;try{r=await post(kind,body)}catch(e){connectionLost();return false}
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
  b.title=off?'Finish step 3 first.':'Review last race (Ctrl+R)'}
function startReview(){if(!tokenSaved){current={view:'welcome'};welcome();return}startJob('review')}
$('btnReview').addEventListener('click',startReview);

function resetJobView(kind){
  if(kind==='review'&&!(lastJob&&lastJob.title))TITLES.review=TITLES0.review;   // a plain "Review last race" never keeps an earlier job's title
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
  if(s.kind==='review'||s.kind==='prep'||s.kind==='team'||s.kind==='weekly'||s.kind==='update'){
    if(s.kind==='review'&&s.state==='running')TITLES.review=s.title||TITLES0.review;
    setBar(s.frac||0);$('jobNow').textContent=s.state==='error'?'Stopped.':(s.message||'');
    if($('jobTitle').textContent!==TITLES[s.kind]&&s.state==='running')resetJobView(s.kind);
    const ol=$('jobSteps');const steps=s.steps||[];
    if(ol.children.length!==steps.length){ol.replaceChildren(...steps.map(t=>h('li',null,t)));ol.scrollTop=ol.scrollHeight}
    if(s.state==='running')$('jobEyebrow').textContent='Working';
    if(s.state==='error'&&!handled.has(s.id)){handled.add(s.id);
      $('jobEyebrow').textContent='Stopped';$('jobErrMsg').textContent=s.error;$('jobErr').hidden=false;
      $('jobBar').parentElement.className='progress fail';
      if(current.view!=='job')banner((STOPPED[s.kind]||'It stopped')+': '+s.error,true,9000)}
    if(s.state==='done'&&!handled.has(s.id)){handled.add(s.id);
      $('jobEyebrow').textContent='Done';$('jobBar').parentElement.className='progress done';
      const stay=current.view==='job';  // only jump to the result if the person is still watching
      if(s.kind==='review')loadReviews().then(()=>{if(s.result&&s.result.review){if(stay)openReview(s.result.review);banner('Review ready.')}});
      else loadPages().then(()=>{const team=s.kind==='team'||s.kind==='weekly';
        if(s.result&&s.result.page){if(stay)openPage(s.result.page,team?'teamPage':'prepPage');
        banner(s.kind==='weekly'?'This week\'s leaderboards updated'+(ok(s.result.requests)?' ('+s.result.requests+' Garage 61 request'+(s.result.requests===1?'':'s')+').':'.'):
          s.kind==='team'?'Team hub updated.':'Prep page ready.')}if(current.view==='prep')renderPrepPages();if(s.kind==='weekly')loadWeekly()});
    }
  }
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
    let note=sum&&sum.synced?n+' session'+(n===1?'':'s')+' and '+sum.laps+' laps from '+sum.combos+' track and car combination'+(sum.combos===1?'':'s')+'.':
      'The first sync downloads your whole Garage 61 history, paced at about one request a second. You can keep using Debrief while it runs.';
    if(sum&&sum.telemetry_left)note+=' Telemetry still to download for '+sum.telemetry_left+' session'+(sum.telemetry_left===1?'':'s')+'.';
    if(sum&&sum.synced&&ok(sum.last_requests))note+=' Last sync used '+sum.last_requests+' Garage 61 request'+(sum.last_requests===1?'':'s')+'.';
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
    $('sessBar').firstElementChild.style.width=Math.round((s.frac||0)*100)+'%';$('sessNow').textContent=s.message||'';$('btnSync').disabled=s.state==='running';
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
let prepData=null,picked=new Set(),pickerOpen=false;
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
  if(!sc){pill.textContent='Not loaded';pill.className='pill no';
    $('schNote').textContent='Load iRacing\'s official season schedule to see what your series are racing. Debrief reads the PDF on this PC and never shares it.'}
  else{pill.textContent=sc.season+(sc.current_week?' · Week '+sc.current_week:'');pill.className='pill '+(sc.out_of_date?'no':'ok');
    $('schNote').textContent=sc.out_of_date?'This season has ended. Load the new season\'s schedule when iRacing publishes it.':
      sc.series+' series, '+sc.weeks+' weeks ('+fmtDate(sc.first_day)+' to '+fmtDate(sc.last_day)+'). Load it again when the next season comes out.'}
  $('btnSchDl').textContent=sc?'Download latest schedule':'Download official schedule';
  showWarnings('schWarn',sc&&sc.warnings,'Check the schedule:');
  const showPicker=!!sc&&(pickerOpen||!j.chosen);
  $('seriesPicker').hidden=!showPicker;if(showPicker)renderPicker();
  renderMySeries()}
function serMeta(s){const bits=[];if(s.category)bits.push(s.category);if(s.license)bits.push(s.license.replace(/\s*-->\s*/,' → '));
  if(s.setup)bits.push(s.setup==='fixed'?'Fixed setup':'Open setup');return bits.join(' · ')}
function pickRow(s,sub,checked){const lab=h('label','pick');const cb=document.createElement('input');cb.type='checkbox';cb.checked=checked;
  cb.addEventListener('change',()=>{if(cb.checked)picked.add(s.id);else picked.delete(s.id);
    document.querySelectorAll('.pick input[data-id="'+CSS.escape(s.id)+'"]').forEach(x=>{if(x!==cb)x.checked=cb.checked})});
  cb.dataset.id=s.id;const tx=h('span','itx');tx.append(h('b',null,s.name),h('span','sub2',sub));lab.append(cb,tx);return lab}
function renderPicker(){const j=prepData;
  if(!renderPicker.init){renderPicker.init=true;picked=new Set((j.mine||[]).map(s=>s.id));
    if(!j.chosen)(j.suggestions||[]).forEach(s=>{if(s.sure)picked.add(s.id)})}
  const sug=$('pickSuggest');sug.replaceChildren();const list=j.suggestions||[];
  $('pickNote').textContent=list.length?'Debrief matched these from your races this season. Sure matches are ticked; check the rest.':
    'No races this season to match yet. Search for your series below.';
  list.forEach(s=>{const sub=s.races+' race'+(s.races===1?'':'s')+' this season'+(s.shared?' · shares a car with another series':'')+(s.multiclass?' · multi-class':'');
    sug.append(pickRow(s,sub,picked.has(s.id)))});
  renderPickAll()}
function renderPickAll(){const q=$('seriesSearch').value.trim().toLowerCase();const host=$('pickAll');host.replaceChildren();
  const all=(prepData&&prepData.all)||[];
  const hit=all.filter(s=>!q||[s.name,s.track,s.category].concat(s.cars||[]).some(x=>String(x||'').toLowerCase().includes(q)));
  hit.slice(0,80).forEach(s=>host.append(pickRow(s,[s.category,s.track?'This week: '+s.track:'',s.multiclass?'Multi-class':''].filter(Boolean).join(' · '),picked.has(s.id))));
  if(hit.length>80)host.append(h('p','note','Showing 80 of '+hit.length+'. Type more to narrow it down.'));
  if(!hit.length)host.append(h('p','note','No series match that.'))}
$('seriesSearch').addEventListener('input',renderPickAll);
$('btnSeriesSave').addEventListener('click',async()=>{setMsg('pickMsg','Saving...');
  let r;try{r=await post('series',{ids:Array.from(picked)})}catch(e){connectionLost();return}
  if(!r.ok){setMsg('pickMsg',r.error,'bad');return}
  setMsg('pickMsg','');pickerOpen=false;renderPicker.init=false;await loadPrepare(true)});
$('btnPickSkip').addEventListener('click',async()=>{pickerOpen=false;renderPicker.init=false;
  if(!prepData.chosen){let r;try{r=await post('series',{ids:[]})}catch(e){connectionLost();return}}
  await loadPrepare(true)});
$('btnPickClose').addEventListener('click',()=>{pickerOpen=false;renderPicker.init=false;if(prepData&&prepData.chosen)$('seriesPicker').hidden=true});
function weekBox(w,label,s,now){const box=h('div','wk'+(now?' now':''));
  box.append(h('span','wlab',label+' · Week '+w.week+' · '+dayRange(w.start)),h('span','wtrack',w.track));
  const facts=[];if(w.length)facts.push(w.length);const rt=raceWhen(w.race_time);if(rt)facts.push('Race '+rt);
  if(facts.length)box.append(h('span','sub2',facts.join(' · ')));
  if(w.conditions)box.append(h('span','sub2',w.conditions));
  const hs=w.history||{};
  box.append(h('span','sub2',hs.sessions?('You: '+hs.sessions+' session'+(hs.sessions===1?'':'s')+(hs.races?' ('+hs.races+' race'+(hs.races===1?'':'s')+')':'')+
    (hs.best?' · best '+fmtT(hs.best):'')+(hs.last?' · last '+fmtDate(hs.last):'')):(hs.other_car_sessions?'You\'ve driven here in another car, not this one yet.':'You haven\'t driven here yet.')));
  if(w.has_notes){const cv=h('div','cardview');const c=w.card.fields||{};
    [['lap1','Lap 1'],['hotspots','Hotspots'],['passing','Passing'],['limits','Limits'],['focus','Focus']].forEach(([k,lab])=>{if(c[k]){const p=h('p');p.append(h('b',null,lab+': '),document.createTextNode(c[k]));cv.append(p)}});
    const cn=(w.card.car_notes||{});Object.keys(cn).forEach(car=>{if((s.cars||[]).some(x=>x===car)){const p=h('p');p.append(h('b',null,car+': '),document.createTextNode(cn[car]));cv.append(p)}});
    box.append(cv)}
  const row=h('div','row');const bc=h('button','btn',w.has_notes?'Edit track card':'Add track card');bc.type='button';
  bc.addEventListener('click',()=>openCard(w.track,s));row.append(bc);
  if(ok(w.shared_notes)&&w.shared_notes>0){const bt=h('button','btn','Team notes ('+w.shared_notes+')');bt.type='button';bt.dataset.teamNotes='1';
    bt.addEventListener('click',()=>openCard(w.track,s,true));row.append(bt)}
  const bf=h('button','btn','Plan fuel');bf.type='button';bf.dataset.fuel='1';
  bf.addEventListener('click',()=>openFuel({track:w.track,series:s.id,week:w.week,cars:s.cars||[],length:w.length}));row.append(bf);
  const bp=h('button','btn','Build prep page');bp.type='button';
  if(w.track_id==null||w.car_id==null){bp.disabled=true;bp.title='Debrief needs Garage 61\'s ids for this track and car: drive it once, or load the track and car lists under "Prep pages for any track and car".'}
  bp.addEventListener('click',()=>startJob('prep',{track_id:w.track_id,car_id:w.car_id}));row.append(bp);
  box.append(row);return box}
function renderMySeries(){const host=$('mySeries');host.replaceChildren();const j=prepData;if(!j||!j.schedule)return;
  const mine=j.mine||[];
  const head=h('div','phead');head.append(h('h3',null,mine.length?'Your series':'No series picked yet'));
  const be=h('button','btn',mine.length?'Change series':'Pick your series');be.type='button';
  be.addEventListener('click',()=>{pickerOpen=true;renderPicker.init=false;renderPrepare();$('seriesPicker').scrollIntoView({behavior:'smooth',block:'start'})});
  head.append(be);host.append(head);
  if(!mine.length){host.append(h('p','note','Pick the series you race and Debrief shows this week\'s and next week\'s tracks for each one.'));return}
  mine.forEach(s=>{const c=h('div','sercard');const sh=h('div','shead');sh.append(h('h3',null,s.name));
    const chips=h('div','chips');if(s.missing)chips.append(h('span','chip','Not running this season'));
    if(s.multiclass)chips.append(h('span','chip mc','Multi-class: '+(s.classes||[]).join(', ')));
    if(s.setup)chips.append(h('span','chip',s.setup==='fixed'?'Fixed':'Open'));sh.append(chips);c.append(sh);
    if(s.missing){c.append(h('p','note','This series isn\'t in the loaded schedule. Change series to remove it.'));host.append(c);return}
    const meta=serMeta(s);if(meta)c.append(h('span','sub2',meta));
    if(s.cars&&s.cars.length)c.append(h('span','sub2',(s.cars.length>1?'Cars: ':'Car: ')+s.cars.slice(0,8).join(', ')+(s.cars.length>8?' and '+(s.cars.length-8)+' more':'')));
    const wk=h('div','wk2');
    if(s.this_week)wk.append(weekBox(s.this_week,'This week',s,true));
    if(s.next_week)wk.append(weekBox(s.next_week,'Next week',s,false));
    if(!s.this_week&&!s.next_week)wk.append(h('p','note','No race this week (off-week or season over).'));
    c.append(wk);
    if(s.season&&s.season.length){const d=h('details','sub');d.append(h('summary',null,'Whole season ('+s.season.length+' weeks)'));
      const sl=h('div','seasonlist');s.season.forEach(w=>{const r=h('div',w.week===j.current_week?'now':null);
        r.append(h('span','mono','Wk '+w.week),h('span','mono',fmtDate(w.start).replace(/, \d{4}$/,'')),h('span',null,w.track),h('span','sub2',w.has_notes?'Card ✓':(w.length||'')));sl.append(r)});
      d.append(sl);c.append(d)}
    host.append(c)})}
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
    have?'Search Garage 61\'s '+j.tracks.length+' tracks and '+j.cars.length+' cars (saved on this PC, no requests).':
    'Garage 61\'s track and car lists aren\'t saved on this PC yet. Loading them uses up to 2 requests, then they\'re reused for a week.';
  if(have){fillList($('dlTracks'),j.tracks);fillList($('dlCars'),j.cars)}
}
function fillList(dl,items){if(dl.children.length===items.length)return;dl.replaceChildren(...items.map(x=>{const o=document.createElement('option');o.value=x.name;return o}))}
function renderPrepPages(){const list=(pages&&pages.prep)||[];$('prepPagesPanel').hidden=!list.length;const host=$('prepPages');host.replaceChildren();
  list.slice(0,12).forEach(p=>{const row=h('div','item');const tx=h('div','itx');tx.append(h('b',null,p.name),h('span','sub2','Built '+fmtWhen(p.updated)));
    const b=h('button','btn','Open');b.type='button';b.addEventListener('click',()=>openPage(p.page,'prepPage'));row.append(tx,b);host.append(row)})}
$('btnPrepLists').addEventListener('click',async()=>{setMsg('prepLoadMsg','Loading from Garage 61...');$('btnPrepLists').disabled=true;
  const j=await loadCombos(true);$('btnPrepLists').disabled=false;
  if(j&&Array.isArray(j.tracks)){setMsg('prepLoadMsg','');renderPrep(j);banner('Track and car lists saved ('+(j.requests||0)+' Garage 61 request'+(j.requests===1?'':'s')+').')}});
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
  const p=j.plan,n=ok(p.est_total)?p.est_total:0;
  let t='This week\'s leaderboards for your series. About '+n+' Garage 61 request'+(n===1?'':'s')+' (1 per series, 2 if a lap needs a second look).';
  // 2.5 fixes L3: past-week fills and the one-off look-up of your Garage 61 name are part of the count
  const past=ok(p.est_past)?p.est_past:0,me=ok(p.est_me)?p.est_me:0,mx=ok(p.est_max)?p.est_max:0;
  const inc=[];if(past>0)inc.push(past+' for earlier weeks');if(me>0)inc.push('1 to look up your Garage 61 name');
  if(inc.length)t+=' That includes '+inc.join(' and ')+'.';
  if(mx>n)t+=' At most '+mx+'.';
  return t}
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
function requestShare(){if(current.view!=='review'||!discordSaved||share)return;
  const w=$('frame').contentWindow;if(!w)return;
  const id=shareId();share={id,dir:current.dir};$('fbDiscord').disabled=true;
  share.t=setTimeout(()=>{if(share&&share.id===id){share=null;$('fbDiscord').disabled=false;
    banner('This review didn\'t send its share card. Reload it and try again.',true)}},10000);
  // '*': the review page is sandboxed (origin "null"); the request carries nothing but a random id
  try{w.postMessage({type:'debrief:share-request',id},'*')}catch(e){clearTimeout(share.t);share=null;$('fbDiscord').disabled=false}}
$('fbDiscord').addEventListener('click',requestShare);
// the reply counts only from our own frame (e.source, never the origin string) and for the id we sent
addEventListener('message',e=>{const f=$('frame');if(!f||e.source!==f.contentWindow)return;const m=e.data;
  if(!m||typeof m!=='object'||m.type!=='debrief:share-card'||!share||m.id!==share.id)return;
  const s=share;share=null;clearTimeout(s.t);$('fbDiscord').disabled=false;
  if(current.view!=='review'||current.dir!==s.dir)return;
  if(m.error!=null&&m.png==null){banner('Couldn\'t make the share card.',true);return}   // the page couldn't draw it: stage nothing
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
  resultFor={dir,result:j.ok?j.result||null:null};frameBar()}
function openResult(){const dir=current.dir;if(!dir)return;const r=(resultFor.dir===dir&&resultFor.result)||{};
  RES_FIELDS.forEach(k=>{$('r_'+k).value=r[k]!=null?String(r[k]):''});
  $('incRows').replaceChildren(...(Array.isArray(r.incident_laps)?r.incident_laps.slice(0,30):[]).map(incRow));incState();
  $('resTitle').textContent=resultFor.result?'Edit race result':'Add race result';$('resDelete').hidden=!resultFor.result;
  $('resDelete').dataset.armed='';$('resDelete').textContent='Delete result';setMsg('resMsg','');
  const d=$('dlgResult');if(d.showModal)d.showModal();else d.setAttribute('open','');$('r_finish_pos').focus()}
function closeResult(){const d=$('dlgResult');if(d.close)d.close();else d.removeAttribute('open')}
$('fbResult').addEventListener('click',openResult);
$('resClose').addEventListener('click',closeResult);
function reloadFrame(){const f=$('frame');const at=frameHref||f.src;if(at&&at.startsWith(BASE))f.src=fresh(noQuery(at))}
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

/* ---------- settings */
function setMsg(id,t,cls){const e=$(id);e.textContent=t||'';e.className='msg'+(cls?' '+cls:'')}
function fillSettings(v){
  tokenSaved=!!v.token_saved;reviewGate();
  firstName=typeof v.first_name==='string'&&v.first_name?v.first_name:null;installed=!!v.installed;greet();
  $('aboutVersion').textContent='Debrief '+(v.version||'');
  $('aboutText').textContent=(installed?'Installed app. ':'Running from source. ')+'Your settings, the Garage 61 data Debrief has saved, and its log (debrief.log) are in this folder:';
  $('dataPath').textContent=v.data_folder||'';
  if(guideOn)updateGuide();
  const st=$('tokStatus');st.textContent=v.token_saved?'Token saved (ends in '+v.token_hint+')':'No token saved yet';
  st.className='pill '+(v.token_saved?'ok':'no');
  $('refs').value=String(v.max_reference_laps);$('prac').checked=!!v.include_practice;
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
  const dp=$('discordPill');dp.textContent=discordSaved?'Webhook saved ('+(discordHint||'…')+')':'Off';dp.className='pill'+(discordSaved?' ok':'');
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
  else if(fetchIt)setMsg('teamSetMsg','Teams loaded ('+(j.requests||0)+' Garage 61 request'+(j.requests===1?'':'s')+').','ok');
  else if(!teams)setMsg('teamSetMsg','Checking uses 1 Garage 61 request.');
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
})();
})();
