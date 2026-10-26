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
function banner(msg,bad,ms){const b=$('banner');b.textContent=msg;b.className='banner'+(bad?' bad':'');b.hidden=false;clearTimeout(banner.t);if(ms!==0)banner.t=setTimeout(()=>b.hidden=true,ms||5000)}

/* ---------- heartbeat (a worker keeps ticking while the window is minimised) */
const PING=BASE+'api/ping';
function ping(){fetch(PING,{method:'POST',headers:{'X-Debrief':'1'}}).catch(()=>{})}
ping();setInterval(ping,5000);
try{const src='setInterval(function(){fetch('+JSON.stringify(PING)+',{method:"POST",headers:{"X-Debrief":"1"}}).catch(function(){})},5000)';
  new Worker(URL.createObjectURL(new Blob([src],{type:'text/javascript'})))}catch(e){}
document.addEventListener('visibilitychange',ping);
addEventListener('pagehide',()=>{try{fetch(BASE+'api/bye',{method:'POST',headers:{'X-Debrief':'1'},keepalive:true})}catch(e){}});

/* ---------- theme */
const THEMES=['auto','light','dark'];
function getTheme(){try{const t=localStorage.getItem('debrief-theme');return THEMES.includes(t)?t:'auto'}catch(e){return 'auto'}}
function applyTheme(doc,t){if(!doc)return;const r=doc.documentElement;if(t==='auto')r.removeAttribute('data-theme');else r.setAttribute('data-theme',t)}
function setTheme(t){try{localStorage.setItem('debrief-theme',t)}catch(e){}
  applyTheme(document,t);try{applyTheme($('frame').contentDocument,t)}catch(e){}
  document.querySelectorAll('[data-theme-set]').forEach(b=>b.setAttribute('aria-checked',String(b.dataset.themeSet===t)))}
document.querySelectorAll('[data-theme-set]').forEach(b=>b.addEventListener('click',()=>setTheme(b.dataset.themeSet)));
setTheme(getTheme());

/* ---------- views */
const VIEWS=['vFrame','vWelcome','vJob','vSettings','vPrep','vGoals','vTeam'];
const FRAME_VIEWS=['review','library','season','prepPage','teamPage'];
const NAV_OF={library:'library',season:'season',goals:'goals',prep:'prep',prepPage:'prep',team:'team',teamPage:'team',settings:'settings'};
let current={view:null,dir:null};
function show(view){VIEWS.forEach(v=>$(v).hidden=(v!==view))}
function markNav(){
  const nav=NAV_OF[current.view]||null;
  document.querySelectorAll('.navbtn').forEach(b=>b.classList.toggle('active',b.dataset.nav===nav));
  document.querySelectorAll('.rev').forEach(b=>b.classList.toggle('active',current.view==='review'&&b.dataset.dir===current.dir));
  frameBar();
}
function openFrame(rel){const f=$('frame');const want=BASE+rel;const wantDoc=want.split('#')[0];
  // compare with where the frame really is: links inside it (library -> review) don't change f.src
  let at=f.src;try{at=f.contentWindow.location.href}catch(e){}
  const atDoc=String(at).split('#')[0];
  if(at===want){show('vFrame');return}
  if(atDoc===wantDoc){ // same page, different #section: jump there and reload so the page opens on it
    // (with a #section the jump is instant, so reload to let the page open on it; without one, replace() reloads)
    try{f.contentWindow.location.replace(want);if(want.includes('#'))f.contentWindow.location.reload()}catch(e){f.src=want}
  }else if(f.src===want){try{f.contentWindow.location.replace(want)}catch(e){f.src='about:blank';f.src=want}}
  else f.src=want;
  // keep the current screen until a big review page has loaded, so there's no blank flash
  if($('vFrame').hidden){clearTimeout(openFrame.t);openFrame.t=setTimeout(()=>show('vFrame'),4000);
    f.addEventListener('load',()=>{clearTimeout(openFrame.t);if(FRAME_VIEWS.includes(current.view))show('vFrame')},{once:true})}}
function openReview(dir){current={view:'review',dir};openFrame('lib/reviews/'+encodeURIComponent(dir)+'/index.html');markNav();loadResult(dir)}
function openLibrary(){
  if(!libFolder){current={view:'welcome'};welcome();return}
  current={view:'library',dir:null};openFrame('lib/index.html');markNav()}
function openSeason(){
  if(!libFolder){current={view:'welcome'};welcome();return}
  current={view:'season',dir:null};openFrame('lib/index.html#season');markNav()}
function openPage(rel,view){current={view,dir:null,page:rel};openFrame('lib/'+rel.split('/').map(encodeURIComponent).join('/'));markNav()}
function openSettings(){current={view:'settings',dir:null};show('vSettings');markNav();loadSettings();loadTeams(false)}
function openJob(){current={view:'job',dir:null};show('vJob');markNav()}
/* ---------- welcome + first-run guide (shown until a token is saved, then for the rest of that session) */
let guideOn=false,checkOk=false,firstName=null,installed=false;
function greet(){$('welcomeTitle').textContent=firstName?'Welcome, '+firstName+'.':(installed?'Welcome.':'Welcome, sir.')}
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
  const want=v==='review'||v==='prepPage'||v==='teamPage';
  bar.hidden=!want;if(!want)return;
  $('fbResult').hidden=v!=='review';$('fbPrep').hidden=v!=='prepPage';$('fbTeam').hidden=v!=='teamPage';
  const pill=$('fbPill');pill.hidden=true;
  if(v==='review'){const r=resultFor.dir===current.dir?resultFor.result:null;
    $('fbText').textContent=r?'Race result saved':'Add your finishing position, incidents and iRating to this race.';
    $('fbResult').textContent=r?'Edit race result':'Add race result';
    if(r&&ok(r.finish_pos)){pill.hidden=false;pill.className='pill ok';pill.textContent='P'+r.finish_pos+(ok(r.field_size)?' of '+r.field_size:'')}}
  if(v==='prepPage')$('fbText').textContent='Track prep page';
  if(v==='teamPage')$('fbText').textContent='Team hub'+(pages.team_updated?' · updated '+fmtWhen(pages.team_updated):'');
}
async function loadPages(){try{const j=await api('pages');if(j.ok)pages=j}catch(e){}return pages}

$('frame').addEventListener('load',()=>{
  const f=$('frame');let doc=null,path='';
  try{doc=f.contentDocument;path=f.contentWindow.location.href}catch(e){}
  applyTheme(doc,getTheme());
  if(!path.startsWith(BASE))return;
  const rest=path.slice(BASE.length);const rel=rest.split('#')[0].split('?')[0];const hash=(rest.split('#')[1]||'');
  const m=rel.match(/^lib\/reviews\/([^/]+)\/index\.html$/);
  if(m){const dir=decodeURIComponent(m[1]);const changed=current.dir!==dir;current={view:'review',dir};if(changed)loadResult(dir)}
  else if(rel==='lib/index.html'||rel==='lib/')current={view:hash==='season'?'season':'library',dir:null};
  else if(/^lib\/prep\/[^/]+\.html$/.test(rel))current={view:'prepPage',dir:null,page:decodeURIComponent(rel.slice(4))};
  else if(rel==='lib/team/index.html')current={view:'teamPage',dir:null,page:'team/index.html'};
  if(doc&&doc.title)document.title=doc.title==='Debrief Library'?'Debrief':'Debrief · '+doc.title;
  // links that leave the app open in the normal browser, not inside the window
  if(doc)doc.addEventListener('click',e=>{const a=e.target.closest&&e.target.closest('a[href]');if(!a)return;
    const u=new URL(a.getAttribute('href'),doc.baseURI);if(u.origin!==location.origin){a.target='_blank';a.rel='noreferrer'}},true);
  markNav();
});

document.addEventListener('click',e=>{
  const n=e.target.closest('[data-nav]');
  if(n){({library:openLibrary,season:openSeason,goals:openGoals,prep:openPrep,team:openTeam,settings:openSettings})[n.dataset.nav]();return}
  const a=e.target.closest('[data-act]');if(!a)return;
  if(a.dataset.act==='review')startJob('review');
  else if(a.dataset.act==='team')startJob('team');
  else if(a.dataset.act==='retry'&&lastStart)startJob(lastStart.kind,lastStart.body);
});

/* ---------- past reviews */
let libFolder=null,tokenSaved=true,groupsCache=[];
async function loadReviews(){
  let j;try{j=await api('reviews')}catch(e){connectionLost();return []}
  libFolder=j.folder;groupsCache=j.groups||[];
  const host=$('revlist');host.replaceChildren();let n=0;
  groupsCache.forEach(g=>{const box=h('div','grp');box.setAttribute('role','group');box.setAttribute('aria-label',g.track+', '+g.car);
    const gh=h('div','gh');const gt=h('div','gt',g.track);gt.title=g.track;const gc=h('div','gc',g.car);gc.title=g.car;gh.append(gt,gc);box.append(gh);
    g.reviews.forEach(r=>{n++;const b=h('button','rev');b.type='button';b.dataset.dir=r.dir;b.setAttribute('role','listitem');
      b.title=(r.session||'Review')+' · '+r.laps+' laps';
      b.append(h('span','d',r.date),h('span','b',r.best));b.addEventListener('click',()=>openReview(r.dir));box.append(b)});
    host.append(box)});
  if(!n)host.append(h('p','revempty',libFolder?'Your reviews will appear here.':'Set up Settings first.'));
  $('revcount').textContent=n?String(n):'';
  markNav();return groupsCache;
}

/* ---------- jobs: review / prep / team share the progress screen; check stays in Settings */
const TITLES={review:'Reviewing your last race',prep:'Building your track prep page',team:'Updating the team hub',check:'Checking the connection'};
const STOPPED={review:'The review stopped',prep:'The prep page stopped',team:'The team refresh stopped'};
let handled=new Set(),polling=false,lastJob=null,lastStart=null;
async function startJob(kind,body){
  if(lastJob&&lastJob.state==='running'){
    if(lastJob.kind===kind&&kind!=='check')openJob();
    else banner('Debrief is busy: '+(lastJob.title||TITLES[lastJob.kind]||'a job').toLowerCase()+'. Wait for it to finish.',true);
    return false}
  let r;try{r=await post(kind,body)}catch(e){connectionLost();return false}
  if(!r.ok){banner(r.error||'Could not start.',true);return false}
  lastStart={kind,body};lastJob={state:'running',kind,id:r.id};
  resetJobView(kind);openJob();poll();return true;
}
async function startCheck(){
  if(lastJob&&lastJob.state==='running'){banner('Debrief is busy: '+(lastJob.title||'a job').toLowerCase()+'. Wait for it to finish.',true);return}
  setMsg('chkMsg','Checking...');setMsg('tokMsg','');$('btnCheck').disabled=true;
  let r;try{r=await post('check')}catch(e){connectionLost();return}
  if(!r.ok){$('btnCheck').disabled=false;setMsg('chkMsg',r.error,'bad');return}
  $('chkOut').hidden=true;$('chkOut').replaceChildren();$('chkProg').hidden=false;$('chkBar').firstElementChild.style.width='4%';
  $('gChkOut').hidden=true;$('gChkOut').replaceChildren();$('gChkProg').hidden=false;$('gChkBar').firstElementChild.style.width='4%';
  $('btnCheck').disabled=true;setMsg('chkMsg','Checking...');poll();
}
$('btnCheck').addEventListener('click',startCheck);
$('btnReview').addEventListener('click',()=>startJob('review'));

function resetJobView(kind){
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
  if(s.kind==='review'||s.kind==='prep'||s.kind==='team'){
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
      else loadPages().then(()=>{if(s.result&&s.result.page){if(stay)openPage(s.result.page,s.kind==='team'?'teamPage':'prepPage');
        banner(s.kind==='team'?'Team hub updated.':'Prep page ready.')}if(current.view==='prep')renderPrepPages()});
    }
  }
  if(s.kind==='check'){
    $('chkBar').firstElementChild.style.width=Math.round((s.frac||0)*100)+'%';
    $('gChkBar').firstElementChild.style.width=Math.round((s.frac||0)*100)+'%';
    if(s.state==='running'){setMsg('chkMsg',s.message||'Checking...');if(guideOn)setMsg('gTokMsg',s.message||'Checking...')}
    if((s.state==='done'||s.state==='error')&&!handled.has(s.id)){handled.add(s.id);
      $('btnCheck').disabled=false;$('chkProg').hidden=true;
      const out=$('chkOut');out.replaceChildren();
      const lines=s.state==='done'?(s.result&&s.result.lines)||[]:(s.steps||[]);
      lines.forEach(t=>out.append(h('li',/NOT available/.test(t)?'bad':null,t)));
      if(s.state==='error')out.append(h('li','bad',s.error));
      out.hidden=!out.children.length;
      setMsg('chkMsg',s.state==='done'?'Connection works.':'Connection check failed.',s.state==='done'?'ok':'bad');
      // the same result in the first-run guide
      const gout=$('gChkOut');gout.replaceChildren(...Array.from(out.children).map(li=>li.cloneNode(true)));
      gout.hidden=!gout.children.length;$('gChkProg').hidden=true;
      checkOk=s.state==='done';
      setMsg('gTokMsg',checkOk?'Connection works. You\'re ready for step 4.':'The connection check failed. Check the token and paste it again.',checkOk?'ok':'bad');
      updateGuide();
      if(s.state==='done'){loadSettings();loadTeams(false)}
    }
  }
}

/* ---------- Prep */
let combosCache=null;
async function loadCombos(fetchLists){
  let j;try{j=await api('combos'+(fetchLists?'?fetch=1':''))}catch(e){connectionLost();return null}
  if(!j.ok){if(fetchLists)setMsg('prepLoadMsg',j.error,'bad');return combosCache}
  combosCache=j;return j}
function comboLabel(c){return c.track+' · '+c.car}
async function openPrep(){current={view:'prep',dir:null};show('vPrep');markNav();
  const [j]=await Promise.all([loadCombos(false),loadPages()]);renderPrep(j);renderPrepPages()}
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
  if(lastJob&&lastJob.state==='running'&&lastJob.kind==='team'){openJob();return}
  await loadPages();
  if(current.view!=='team')return;  // the person moved on while we looked
  if(pages.team)openPage(pages.team,'teamPage');else{show('vTeam');markNav()}}
$('fbTeam').addEventListener('click',()=>startJob('team'));

/* ---------- race result (on an open review) */
const RES_FIELDS=['finish_pos','start_pos','field_size','incidents','laps_led','sof','irating_before','irating_after','sr_before','sr_after','notes'];
async function loadResult(dir){resultFor={dir,result:null};frameBar();
  let j;try{j=await api('result?review='+encodeURIComponent(dir))}catch(e){return}
  if(resultFor.dir!==dir)return;
  resultFor={dir,result:j.ok?j.result||null:null};frameBar()}
function openResult(){const dir=current.dir;if(!dir)return;const r=(resultFor.dir===dir&&resultFor.result)||{};
  RES_FIELDS.forEach(k=>{$('r_'+k).value=r[k]!=null?String(r[k]):''});
  $('resTitle').textContent=resultFor.result?'Edit race result':'Add race result';$('resDelete').hidden=!resultFor.result;
  $('resDelete').dataset.armed='';$('resDelete').textContent='Delete result';setMsg('resMsg','');
  const d=$('dlgResult');if(d.showModal)d.showModal();else d.setAttribute('open','');$('r_finish_pos').focus()}
function closeResult(){const d=$('dlgResult');if(d.close)d.close();else d.removeAttribute('open')}
$('fbResult').addEventListener('click',openResult);
$('resClose').addEventListener('click',closeResult);
function reloadFrame(){try{$('frame').contentWindow.location.reload()}catch(e){}}
$('fResult').addEventListener('submit',async e=>{e.preventDefault();const dir=current.dir;if(!dir)return;
  const fields={};RES_FIELDS.forEach(k=>{const v=$('r_'+k).value.trim();if(v!=='')fields[k]=k==='notes'?v:Number(v)});
  if(!('finish_pos' in fields)){setMsg('resMsg','Add your finishing position.','bad');$('r_finish_pos').focus();return}
  const bad=RES_FIELDS.find(k=>k!=='notes'&&!$('r_'+k).checkValidity());
  if(bad){setMsg('resMsg',$('r_'+bad).closest('label').querySelector('span').textContent+': '+$('r_'+bad).validationMessage,'bad');$('r_'+bad).focus();return}
  if(ok(fields.field_size)&&(fields.finish_pos>fields.field_size||(ok(fields.start_pos)&&fields.start_pos>fields.field_size))){
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
  tokenSaved=!!v.token_saved;
  firstName=typeof v.first_name==='string'&&v.first_name?v.first_name:null;installed=!!v.installed;greet();
  $('aboutVersion').textContent='Debrief '+(v.version||'');
  $('aboutText').textContent=(installed?'Installed app. ':'Running from source. ')+'Your settings, the Garage 61 data Debrief has saved, and its log (debrief.log) are in this folder:';
  $('dataPath').textContent=v.data_folder||'';
  if(guideOn)updateGuide();
  const st=$('tokStatus');st.textContent=v.token_saved?'Token saved (ends in '+v.token_hint+')':'No token saved yet';
  st.className='pill '+(v.token_saved?'ok':'no');
  $('refs').value=String(v.max_reference_laps);$('prac').checked=!!v.include_practice;
  $('libPath').textContent=v.library_folder||'Not set yet. It will be Documents\\Debrief Reviews once settings are saved.';
  const fe=$('setFileErr');fe.hidden=!v.error;fe.textContent=v.error||'';fe.className='note msg bad';
  teamSlug=v.team_slug||teamSlug;
  const idS=!!v.iracing_client_id_saved,seS=!!v.iracing_client_secret_saved;
  $('irIdStatus').textContent='Client ID: '+(idS?'saved':'not saved');$('irIdStatus').className='pill'+(idS?' ok':'');
  $('irSecStatus').textContent='Client secret: '+(seS?'saved':'not saved');$('irSecStatus').className='pill'+(seS?' ok':'');
  $('irClear').hidden=!(idS||seS);
}
let teamSlug='midfield-menace';
async function loadSettings(){try{fillSettings(await api('settings'))}catch(e){connectionLost()}}
async function loadTeams(fetchIt){let j;try{j=await api('teams'+(fetchIt?'?fetch=1':''))}catch(e){connectionLost();return}
  if(!j.ok){setMsg('teamSetMsg',j.error,'bad');return}
  teamSlug=j.team_slug||teamSlug;const sel=$('teamSel');sel.replaceChildren();
  const teams=j.teams;const have=Array.isArray(teams);
  if(have){teams.forEach(t=>{const o=h('option',null,t.name);o.value=t.slug;sel.append(o)});
    if(!teams.some(t=>t.slug===teamSlug)){const o=h('option',null,teamSlug+' (not on your account)');o.value=teamSlug;sel.prepend(o)}
    sel.value=teamSlug}
  const cur=have&&teams.find(t=>t.slug===teamSlug);
  $('teamStatus').textContent=cur?cur.name:teamSlug;$('teamStatus').className='pill'+(cur?' ok':'');
  $('teamSelWrap').hidden=!have||!teams.length;$('teamSave').hidden=!have||!teams.length;
  $('btnTeams').hidden=have||!j.token;
  if(!j.token)setMsg('teamSetMsg','Save your Garage 61 token first to pick a team.');
  else if(!have)setMsg('teamSetMsg','Your team list isn\'t saved on this PC yet. Loading it uses 1 Garage 61 request.');
  else if(!teams.length)setMsg('teamSetMsg','Garage 61 doesn\'t list any teams for your account.');
  else if(fetchIt)setMsg('teamSetMsg','Teams loaded ('+(j.requests||0)+' Garage 61 request'+(j.requests===1?'':'s')+').','ok');
  else setMsg('teamSetMsg','');
}
$('btnTeams').addEventListener('click',()=>{setMsg('teamSetMsg','Loading...');loadTeams(true)});
$('fTeam').addEventListener('submit',async e=>{e.preventDefault();const v=$('teamSel').value;if(!v)return;setMsg('teamSetMsg','Saving...');let r;
  try{r=await post('settings',{team_slug:v})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('teamSetMsg',r.error,'bad');return}
  fillSettings(r.settings);await loadTeams(false);setMsg('teamSetMsg','Saved. Refresh the Team hub to see the new team.','ok')});
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
$('fToken').addEventListener('submit',async e=>{e.preventDefault();
  const inp=$('tok');const v=inp.value.trim();
  if(!v){setMsg('tokMsg','Paste your token in the box first.','bad');inp.focus();return}
  setMsg('tokMsg','Saving...');let r;
  try{r=await post('settings',{token:v})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('tokMsg',r.error,'bad');return}
  inp.value='';fillSettings(r.settings);await loadReviews();loadTeams(false);
  setMsg('tokMsg','Saved. Check connection to make sure it works.','ok');
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
  try{r=await post('settings',{max_reference_laps:+$('refs').value,include_practice:$('prac').checked})}catch(err){connectionLost();return}
  if(!r.ok){setMsg('optMsg',r.error,'bad');return}
  fillSettings(r.settings);setMsg('optMsg','Saved.','ok')});
$('btnFolder').addEventListener('click',async()=>{let r;try{r=await post('open-folder')}catch(e){connectionLost();return}
  setMsg('folderMsg',r.ok?'Opened in File Explorer.':r.error,r.ok?'ok':'')});

/* ---------- start */
(async function init(){
  await loadSettings();
  const groups=await loadReviews();
  let s=null;try{s=await api('status')}catch(e){}
  if(s&&s.state==='running'){lastJob=s;if(s.kind!=='check'){resetJobView(s.kind);openJob()}poll();return}
  if(groups.length)openLibrary();else welcome();
})();
})();
