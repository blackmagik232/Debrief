/* Race Room (2.30): the pit board for a team race, for the driver and for a teammate in the same session, and the
   Endurance review of a finished team race. Only text is written to the page (textContent); the numbers come from
   /api/raceroom (about once a second while this page is open) and /api/endurance-review. */
(function(){
'use strict';
const {$,api,post,h,ok,fmtT,show,markNav,goNav,plainText,connectionLost,setMsg,setView,view}=window.DebriefHost;
const rr={seq:0,timer:null,tab:'live',st:null,iam:null,busy:false,races:null,iamFilled:''};
const rrT=s=>{if(!ok(s))return '--';s=Math.max(0,Math.round(s));const hh=Math.floor(s/3600),m=Math.floor((s%3600)/60),x=s%60;return (hh?hh+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0')};
const rrLap=s=>ok(s)&&s>0?fmtT(s).replace(/0$/,''):'--';
function rrDur(s){if(!ok(s))return '--';const m=Math.round(s/60);return m>=60?Math.floor(m/60)+' h '+(m%60)+' min':m+' min'}
function rrCell(tr,t,cls){const td=h('td',cls||null,t==null||t===''?'':String(t));tr.append(td);return td}
function rrRows(id,rows,empty,cols,build){const tb=$(id);if(!rows||!rows.length){const tr=h('tr'),td=h('td','dnone',empty);td.colSpan=cols;tr.append(td);tb.replaceChildren(tr);return}
  tb.replaceChildren(...rows.map(build))}
function openRoom(){setView('room');show('vRoom');markNav();rrTab(rr.tab);roomPoll()}
async function roomPoll(){const seq=++rr.seq;clearTimeout(rr.timer);if(view()!=='room')return;let j;
  try{j=await api('raceroom')}catch(e){connectionLost();return}
  if(seq!==rr.seq)return;if(j&&j.ok)rrRender(j);
  if(view()==='room')rr.timer=setTimeout(roomPoll,1000)}
function rrTab(t){rr.tab=t;$('rrLive').hidden=t!=='live';$('rrReview').hidden=t!=='rev';
  [['rrTabLive','live'],['rrTabRev','rev']].forEach(([id,k])=>{$(id).classList.toggle('on',k===t);$(id).setAttribute('aria-selected',k===t?'true':'false')});
  if(t==='rev')rrRevList()}
$('rrTabLive').addEventListener('click',()=>rrTab('live'));$('rrTabRev').addEventListener('click',()=>rrTab('rev'));
$('rrMake').addEventListener('click',()=>goNav('prep'));
$('rrStart').addEventListener('click',async()=>{if(rr.busy)return;rr.busy=true;let r;try{r=await post('coach/start')}catch(e){connectionLost();return}finally{rr.busy=false}
  if(!r.ok){$('rrText').textContent=plainText(r.error||'The coach didn\'t start.');return}roomPoll()});
$('rrIam').addEventListener('change',async()=>{let r;try{r=await post('raceroom',{action:'iam',name:$('rrIam').value})}catch(e){connectionLost();return}
  $('rrReMsg').textContent=r.ok?'Saved: you are '+r.iam+'.':plainText(r.error||'');rr.iamFilled='';roomPoll()});
$('rrDelayGo').addEventListener('click',async()=>{let r;const who=$('rrDelayWho').value;
  try{r=await post('raceroom',{action:'delay',driver:who,minutes:+$('rrDelayMin').value||0})}catch(e){connectionLost();return}
  setMsg('rrReMsg',r.ok?'Told the plan. It works the rest of the race out again.':(r.error||'That didn\'t work.'),r.ok?'ok':'bad');roomPoll()});
$('rrReGo').addEventListener('click',async()=>{let r;try{r=await post('raceroom',{action:'replan'})}catch(e){connectionLost();return}
  setMsg('rrReMsg',r.ok?'Working it out again.':(r.error||'That didn\'t work.'),r.ok?'ok':'bad');roomPoll()});

function rrCard(id,big,sub,cls){$(id).textContent=big;if(sub!=null&&$(id+'Sub'))$(id+'Sub').textContent=sub;if(cls!=null&&$(id+'Card'))$(id+'Card').className='crcard'+(cls?' '+cls:'')}
function rrRival(who,r,idle){const sub=who+'Sub';$(who+'Card').className='crcard';
  if(!r){$(who).textContent='--';$(sub).textContent=idle;return}
  $(who).textContent=(r.num?'#'+r.num+' ':'')+(ok(r.gap_s)?r.gap_s.toFixed(1)+' s':'');
  const t=r.tags||[],bits=[];if(r.name)bits.push(r.name);
  if(ok(r.rate)&&r.rate>=0.1)bits.push('closing '+r.rate.toFixed(1)+' s a lap');else if(t.indexOf('pulling away')>=0)bits.push('pulling away');
  if(t.indexOf('pitting')>=0)bits.push('in the pits');if(t.indexOf('alongside')>=0)bits.push('alongside');
  $(sub).textContent=bits.join(' · ');
  if(t.indexOf('threat')>=0)$(who+'Card').className='crcard hot';else if(t.indexOf('catchable')>=0)$(who+'Card').className='crcard good'}

function rrRender(j){if(view()!=='room')return;const st=j.room||{};rr.st=st;const role=(st.role||{}).role||'none';
  const racing=st.state==='racing'&&j.running;
  const pill=$('rrPill');pill.className='pill'+(racing?' ok':'');
  pill.textContent=!j.running?'Coach off':racing?(role==='crew'?'Crew':'Driving'):'Waiting';
  $('rrRole').textContent=!j.running?'':role==='driver'?'You are driving.':role==='crew'?'You are in the session as crew: fuel is estimated and the calls are text only.':role==='spectator'?'You are watching.':'';
  $('rrStart').hidden=!!j.running;
  $('rrText').textContent=!j.running?'Start the coach to open the Race Room in a race.':(racing?'':(st.text||'Waiting for iRacing.'));
  $('rrBoard').hidden=!racing;$('rrBody').hidden=!racing;
  rrIam(j,st);
  if(!racing){$('rrAlert').hidden=true;return}
  rrAlert(st);rrBoard(st);rrStint(st);rrHand(st);rrReplan(st);rrLists(st);
  const pf=st.perf;$('rrPerf').textContent=(pf&&ok(pf.cpu_pct)?'Race Room uses about '+pf.cpu_pct.toFixed(2)+'% of one core. ':'')+(st.log?'Race log: '+st.log+'.':'')}
function rrIam(j,st){const pl=st.plan||{},names=pl.drivers||[],wrap=$('rrIamWrap');wrap.hidden=!names.length;if(!names.length)return;
  const key=names.join('|')+'#'+(j.iam||'');if(key===rr.iamFilled)return;rr.iamFilled=key;const sel=$('rrIam');
  const opts=[h('option',null,'Choose...')];opts[0].value='';names.forEach(n=>{const o=h('option',null,n);o.value=n;opts.push(o)});sel.replaceChildren(...opts);sel.value=names.indexOf(j.iam)>=0?j.iam:''
  const dd=$('rrDelayWho');dd.replaceChildren(...names.map(n=>{const o=h('option',null,n);o.value=n;return o}))}
function rrAlert(st){const a=(st.swap||{}).alert,e=$('rrAlert');e.hidden=!a;if(a){e.textContent=a.text||'Your stint is coming up.';e.className='rralert'+(a.kind==='swap_5'?' hot':'')}}
function rrBoard(st){const b=st.board||{},crew=!!b.estimated;const cl=b.call;
  $('rrCall').textContent=cl?cl.text:(b.call_text||'All good.');
  $('rrCallSub').textContent=crew?'Text only here: a teammate\'s PC never speaks fuel calls.':'';
  $('rrCallCard').className='crcard crcall'+(cl&&cl.rank<=3?' hot':'');
  const P=b.pos||{};rrCard('rrPos',P.class?'P'+P.class:'--',(P.of?'of '+P.of+' in class':'')+(P.overall?' · P'+P.overall+' overall':'')+(b.pos_change&&b.pos_change.places?' · '+(b.pos_change.places>0?'up ':'down ')+Math.abs(b.pos_change.places)+' since lap '+b.pos_change.since_lap:''));
  const L=b.laps_left;rrCard('rrLeft',L?String(L.n):'--',L?(L.kind==='time'?'laps to go (timed race)':'laps to go'):'Not known yet');
  const B=b.box;if(b.no_fuel||!B){$('rrBox').textContent='--';$('rrBoxWin').textContent=b.no_fuel?'Needs fuel to work out. No fuel reading on this PC yet.':(b.uncertain||'');$('rrBoxAdd').textContent='';$('rrBoxCard').className='crcard'}
  else{$('rrBox').textContent=B.need?(B.in_laps===0?'THIS LAP':'Lap '+B.lap_latest):(B.text?'No stop':'--');
    $('rrBoxWin').textContent=(crew?'Estimated. ':'')+(B.need?(B.in_laps===0?'':'In '+B.in_laps+' lap'+(B.in_laps===1?'':'s')+'. ')+(B.window_text||''):(B.window_text||''));
    $('rrBoxAdd').textContent=B.need&&B.add?(crew?'About ':'')+B.add_text.replace(/^Add /,'Add '):'';
    $('rrBoxCard').className='crcard'+(B.need&&B.in_laps!=null&&B.in_laps<=1?' hot':'')}
  const F=st.fuel||{};$('rrFuelLbl').textContent=F.estimated?'Fuel (estimated)':'Fuel';$('rrFuel').textContent=F.text||'--';
  $('rrFuelSub').textContent=F.estimated?[F.uncertain?'uncertain '+F.uncertain:'',F.laps_text,F.basis].filter(Boolean).join(' · '):[F.laps_text,F.burn_text].filter(Boolean).join(' · ');
  rrRival('rrAhead',b.ahead,'No car ahead in your class');rrRival('rrBehind',b.behind,'No car behind in your class');
  const LL=b.last_lap||{};$('rrLap').textContent=LL.text||'--';
  $('rrLapSub').textContent=LL.target_text&&LL.target_text!=='--'?('Target '+LL.target_text+(ok(LL.delta)?' ('+(LL.delta>0?'+':'')+LL.delta.toFixed(2)+' s)':'')):'';
  const s=st.session||{};$('rrTime').textContent=s.time_left_text||rrT(s.time_left_s);$('rrTimeSub').textContent=(s.caution?'Caution. ':'')+(s.flag&&s.flag!=='caution'?s.flag:'')}
function rrKV(label,big,sub,cls){const d=h('div','crcard'+(cls?' '+cls:''));d.append(h('span','eyebrow',label),h('b',null,big),h('small',null,sub||''));return d}
function rrStint(st){const S=st.stint||{},P=st.plan||{};
  const cards=[rrKV('Driving now',S.driver||'--',S.plan_driver&&S.plan_driver!==S.driver?'plan name: '+S.plan_driver:(S.driver_how||'')),
    rrKV('Stint timer',S.timer_text||'--',ok(S.laps)?S.laps+' lap'+(S.laps===1?'':'s')+(ok(S.planned_laps)?' of '+S.planned_laps+' planned':''):'')];
  const nx=S.next;
  if(nx&&nx.driver){const bits=[];if(nx.eta_text)bits.push('about '+nx.eta_text+' from now');
    if(nx.their_time)bits.push('about '+nx.their_time+(nx.is_me||!nx.tz_shift_h?'':' their time'));
    if(nx.is_me&&nx.your_time)bits.push('about '+nx.your_time+' your time');
    cards.push(rrKV('Next driver',nx.driver+(nx.is_me?' (you)':''),bits.join(' · ')+(nx.swap===false?' · same driver stays out':''),nx.is_me?'good':''))}
  else if(nx&&nx.text)cards.push(rrKV('Next driver','--',nx.text));
  const d=P.delta;if(P.has_plan&&d)cards.push(rrKV('Against the plan',d.state==='on'?'On plan':d.state==='ahead'?'Ahead':'Behind',d.text||'',d.state==='behind'?'hot':d.state==='ahead'?'good':''));
  $('rrStint').replaceChildren(...cards);
  $('rrNoPlan').hidden=!!P.has_plan;if(!P.has_plan)$('rrNoPlanText').textContent=(P.text||'No saved stint plan for this track.')+' Make a plan to get the next driver, the swap alerts and the rest of the race.';
  $('rrStintNote').textContent=P.has_plan?'Plan: '+(P.title||'saved plan')+(ok(P.stops)?', '+P.stops+' stops':'')+'.':'';
  const fair=st.fair||[];$('rrFairTbl').hidden=!fair.length;
  $('rrFair').replaceChildren(...fair.map(f=>{const tr=h('tr');rrCell(tr,f.name);rrCell(tr,f.laps);rrCell(tr,f.fair);rrCell(tr,f.text||'',f.state==='short'?'bad':'');return tr}))}
function rrHand(st){const H=(st.swap||{}).handover;$('rrHand').hidden=!H;if(!H)return;
  $('rrHandPill').textContent=H.in_pit?'In the pits':'Just left';
  const how=x=>x==='plan'?'from the plan':x==='measured'?'measured':'not known on this PC';
  const out=[rrKV('Swap',(H.from||'?')+' to '+(H.to||'?'),H.text||'')];
  out.push(rrKV('Fuel added',H.fuel_added?H.fuel_added.text:'--',how(H.fuel_how)+(H.fuel_in_tank?' · in the tank '+H.fuel_in_tank.text:'')));
  out.push(rrKV('Tyres',H.tyres==null?'--':H.tyres?'New tyres':'Same tyres',how(H.tyres_how)));
  out.push(rrKV('Repairs',H.repairs||'None known',''));
  out.push(rrKV('Incidents this stint',ok(H.incidents)?String(H.incidents):'--',ok(H.incidents)?'':'not known on this PC'));
  out.push(rrKV('Next stint target',H.target_lap||'--',H.burn_text||''));
  if(ok(H.lane_s))out.push(rrKV('Pit lane',H.lane_s.toFixed(1)+' s',(ok(H.stall_s)?'in the stall '+H.stall_s.toFixed(1)+' s':'')+(ok(H.plan_stop_s)?' · plan '+H.plan_stop_s.toFixed(0)+' s':'')));
  if(H.estimated)out.push(rrKV('Note','Estimated','Fuel numbers here are worked out, not read from the car.'));
  $('rrHandGrid').replaceChildren(...out)}
function rrReplan(st){const R=st.replan||{};$('rrRePill').textContent=R.ok&&R.why?R.why:'';
  $('rrReTbl').hidden=!R.ok;$('rrReStints').hidden=!R.ok;
  if(!R.ok){$('rrReText').textContent=R.text||'Made once the race is running and a plan is saved.';return}
  $('rrReText').textContent=(R.estimated?'Estimated fuel. ':'')+'Finish in about '+(R.finish_text||rrDur(R.finish_s))+' with '+R.stops+' stop'+(R.stops===1?'':'s')+' over '+R.laps+' laps.'+((R.notes||[]).length?' '+R.notes.join(' '):'')+((R.warnings||[]).length?' '+R.warnings.join(' '):'');
  const rows=[{label:'Full tanks (the plan)',stops:R.stops,finish_s:R.finish_s,delta_s:0}].concat(R.options||[]);
  rrRows('rrReRows',rows,'',4,o=>{const tr=h('tr');rrCell(tr,o.label);rrCell(tr,o.stops);rrCell(tr,rrDur(o.finish_s));
    rrCell(tr,o.delta_s==null?'':(o.delta_s===0?'--':(o.delta_s<0?Math.abs(o.delta_s).toFixed(0)+' s quicker':o.delta_s.toFixed(0)+' s slower')+(o.detail?' ('+o.detail+')':'')),o.better?'ok':'');return tr});
  rrRows('rrReStintRows',R.stints,'',4,s=>{const tr=h('tr');rrCell(tr,s.n);rrCell(tr,s.driver);rrCell(tr,s.laps);rrCell(tr,s.start_in_s>0?'in '+rrDur(s.start_in_s):'now');return tr})}
function rrLists(st){
  rrRows('rrRivals',st.rivals,'No class rivals yet.',6,r=>{const tr=h('tr');rrCell(tr,(r.num?'#'+r.num+' ':'')+(r.name||''));rrCell(tr,'P'+r.pos+(r.role?' ('+r.role+')':''));
    rrCell(tr,ok(r.gap_s)?r.gap_s.toFixed(1)+' s':'');rrCell(tr,rrLap(r.last));rrCell(tr,r.stops);rrCell(tr,(r.tags||[]).join(', '));return tr});
  rrRows('rrLaps',st.laps,'No laps yet.',4,l=>{const tr=h('tr');rrCell(tr,l.lap);rrCell(tr,l.driver||'');rrCell(tr,rrLap(l.s));rrCell(tr,[l.pit?'pit':'',l.slow?'slow':''].filter(Boolean).join(', '));return tr});
  const so=st.stops||{};
  rrRows('rrStops',so.rivals,'No stops seen yet.',5,r=>{const tr=h('tr');rrCell(tr,(r.num?'#'+r.num+' ':'')+(r.name||'')+(r.me?' (us)':''));rrCell(tr,'P'+r.pos);
    rrCell(tr,r.stops+(r.passes?' (+'+r.passes+' drive-through)':'')+(r.in_pit?' · in the pits now':''));rrCell(tr,r.last_lap==null?'':'lap '+r.last_lap);
    rrCell(tr,(r.list||[]).map(x=>x.lap+(x.caution?'c':'')).join(', '));return tr});
  rrChart(st.positions||{})}
// positions over the laps: one line a car, ours thick; position 1 at the top. Built as SVG with createElementNS.
function rrChart(P){const el=$('rrChart'),laps=P.laps||[],ser=P.series||[];
  if(!laps.length||!ser.length){el.replaceChildren(h('p','dnone','No laps yet.'));return}
  const NS='http://www.w3.org/2000/svg',W=640,H=220,L=30,B=18,mk=(t,a)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);return e};
  let mx=1;ser.forEach(s=>s.pos.forEach(p=>{if(ok(p)&&p>mx)mx=p}));
  const x=i=>L+(laps.length<2?0:i*(W-L-6)/(laps.length-1)),y=p=>8+(p-1)*(H-B-12)/Math.max(1,mx-1);
  const svg=mk('svg',{viewBox:'0 0 '+W+' '+H,class:'rrsvg',focusable:'false'});
  for(let p=1;p<=mx;p+=Math.max(1,Math.ceil(mx/6))){svg.append(mk('line',{x1:L,x2:W-4,y1:y(p),y2:y(p),class:'rrgrid'}));const t=mk('text',{x:2,y:y(p)+4,class:'rrtick'});t.textContent='P'+p;svg.append(t)}
  const t0=mk('text',{x:L,y:H-3,class:'rrtick'});t0.textContent='lap '+laps[0];const t1=mk('text',{x:W-4,y:H-3,class:'rrtick','text-anchor':'end'});t1.textContent='lap '+laps[laps.length-1];svg.append(t0,t1);
  ser.slice().sort((a,b)=>(a.me?1:0)-(b.me?1:0)).forEach(s=>{let d='',pen=false;s.pos.forEach((p,i)=>{if(!ok(p)){pen=false;return}d+=(pen?'L':'M')+x(i).toFixed(1)+' '+y(p).toFixed(1)+' ';pen=true});
    if(d)svg.append(mk('path',{d:d,class:'rrline'+(s.me?' me':'')}))});
  const alt=ser.filter(s=>s.me).map(s=>'Our car: P'+[...s.pos].reverse().find(ok)+' now').join('');
  el.setAttribute('aria-label','Class positions lap by lap. '+alt);el.replaceChildren(svg)}

// ---------- Endurance review
async function rrRevList(){if(rr.races)return rrRevFill();$('rrRevMsg').textContent='Looking...';let j;try{j=await api('endurance-review')}catch(e){connectionLost();return}
  rr.races=(j&&j.races)||[];$('rrRevMsg').textContent='';rrRevFill()}
function rrRevFill(){const sel=$('rrRevPick');const rs=rr.races||[];
  if(!rs.length){const o=h('option',null,'No team race logs yet');o.value='';sel.replaceChildren(o);$('rrRevGo').disabled=true;$('rrRevBody').replaceChildren(h('p','note','A log is written while you drive or crew a team race with the coach on. Then it shows up here.'));return}
  $('rrRevGo').disabled=false;sel.replaceChildren(...rs.map(r=>{const o=h('option',null,r.title+' ('+r.files.length+' log'+(r.files.length===1?'':'s')+')');o.value=r.id;return o}))}
$('rrRevGo').addEventListener('click',async()=>{const id=$('rrRevPick').value;if(!id)return;$('rrRevMsg').textContent='Reading the log...';let j;
  try{j=await api('endurance-review?id='+encodeURIComponent(id))}catch(e){connectionLost();return}
  if(!j||!j.ok){$('rrRevMsg').textContent=plainText((j&&j.error)||'That race could not be read.');return}$('rrRevMsg').textContent='';rrRevShow(j.review||{})});
function rrTbl(cap,heads,rows,build){const w=h('div','tblwrap'),t=h('table','maptbl'),c=h('caption','sr',cap);t.append(c);const th=h('thead'),hr=h('tr');heads.forEach(x=>hr.append(h('th',null,x)));th.append(hr);t.append(th);
  const tb=h('tbody');(rows||[]).forEach(r=>tb.append(build(r)));if(!(rows||[]).length){const tr=h('tr'),td=h('td','dnone','Nothing to show.');td.colSpan=heads.length;tr.append(td);tb.append(tr)}t.append(tb);w.append(t);return w}
function rrSec(title,...kids){const p=h('div','panel');const hd=h('div','phead');hd.append(h('h3',null,title));p.append(hd,...kids);return p}
function rrRevShow(R){const body=$('rrRevBody'),out=[];
  if(!R.ok){body.replaceChildren(h('p','note',R.text||'No review.'));return}
  const F=R.finish;out.push(h('h2',null,(R.track||'Team race')+(R.team?' · '+R.team:'')));
  out.push(h('p','lede',(F?'Finished P'+F.cpos+' in class after '+F.laps+' laps. ':'')+(R.laps?R.laps.count+' laps read. ':'')+'Logs used: '+((R.used||[]).join(', ')||'none')+'.'));
  const nt=[...(R.notes||[])];(R.skipped||[]).forEach(s=>nt.push('Left out '+(s.who||s.file||'a log')+': '+(s.why||s.reason||'')));
  if(nt.length)out.push(rrSec('What to know',...nt.map(n=>h('p','note',n))));
  out.push(rrSec('Pace by driver',rrTbl('Pace by driver',['Driver','Stints','Laps','Median','Best','Spread','Fade'],R.drivers,d=>{const tr=h('tr');rrCell(tr,d.name);rrCell(tr,d.stints);rrCell(tr,d.laps);rrCell(tr,rrLap(d.median));rrCell(tr,rrLap(d.best));rrCell(tr,ok(d.spread)?d.spread.toFixed(2)+' s':'');rrCell(tr,ok(d.fade)?(d.fade>0?'+':'')+d.fade.toFixed(2)+' s':'');return tr})));
  out.push(rrSec('Pace by stint',rrTbl('Pace by stint',['Stint','Driver','Laps','Median','Best','Spread','Fade'],R.stints,s=>{const tr=h('tr');rrCell(tr,s.n);rrCell(tr,(s.driver||'')+(s.guessed?' (guessed)':''));rrCell(tr,s.from_lap+'-'+s.to_lap);rrCell(tr,rrLap(s.median));rrCell(tr,rrLap(s.best));rrCell(tr,ok(s.spread)?s.spread.toFixed(2)+' s':'');rrCell(tr,ok(s.fade_total)?(s.fade_total>0?'+':'')+s.fade_total.toFixed(2)+' s':'');return tr})));
  out.push(rrSec('Our stops against the plan',rrTbl('Our stops',['Stop','Lap','Pit lane','In the stall','Plan','Against plan'],R.stops,s=>{const tr=h('tr');rrCell(tr,s.n+(s.under_caution?' (caution)':''));rrCell(tr,s.lap);rrCell(tr,ok(s.lane_s)?s.lane_s.toFixed(1)+' s':'');rrCell(tr,ok(s.stall_s)?s.stall_s.toFixed(1)+' s':'');rrCell(tr,ok(s.plan_stop_s)?s.plan_stop_s.toFixed(0)+' s'+(s.plan_swap?' (swap)':''):'');rrCell(tr,ok(s.delta_s)?(s.delta_s>0?'+':'')+s.delta_s+' s'+(s.long?' long':''):'',s.long?'bad':'');return tr})));
  out.push(rrSec('Fuel used against the plan',rrTbl('Fuel',['Stint','Driver','Used a lap','Plan','Difference'],R.fuel,f=>{const tr=h('tr');rrCell(tr,f.stint);rrCell(tr,f.driver||'');
    if(!f.measured){const td=rrCell(tr,f.text||'');td.colSpan=3;return tr}rrCell(tr,f.burn_l.toFixed(2)+' L');rrCell(tr,ok(f.plan_burn_l)?f.plan_burn_l.toFixed(2)+' L':'');rrCell(tr,ok(f.delta_pct)?(f.delta_pct>0?'+':'')+f.delta_pct+'%':'');return tr})));
  const T=R.traffic||{};out.push(rrSec('Traffic from other classes',h('p','note',(T.laps?T.laps+' laps with other-class cars close, costing about '+(+T.cost_s).toFixed(0)+' s. ':'No clear traffic cost. ')+(T.note||'')),
    rrTbl('Traffic by class',['Class','Cost'],T.by_class,c=>{const tr=h('tr');rrCell(tr,c.class);rrCell(tr,c.cost_s+' s');return tr})));
  out.push(rrSec('What our class rivals did',rrTbl('Class rivals',['Car','Finish','Laps','Stops','Stop laps','Lane time','Median lap'],R.rivals,r=>{const tr=h('tr');rrCell(tr,(r.num?'#'+r.num+' ':'')+(r.name||'')+(r.me?' (us)':''));rrCell(tr,r.final_pos?'P'+r.final_pos:'');rrCell(tr,r.laps);rrCell(tr,r.stops+(r.under_caution?' ('+r.under_caution+' under caution)':''));rrCell(tr,(r.stop_laps||[]).join(', '));rrCell(tr,ok(r.avg_lane_s)?r.avg_lane_s+' s':'');rrCell(tr,rrLap(r.median_lap));return tr})));
  body.replaceChildren(...out)}

window.DebriefRoom={open:openRoom};
})();
