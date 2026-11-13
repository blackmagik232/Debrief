/* Race Room (2.30; 2.36 "Race Room"): the live pit board from iRacing, for practice, qualifying and races, laid out like
   the 8 Nov 2026 weekend screen (call strip, tiles, rivals, pace and stints, pit and fuel bar, position, strategy crew).
   It needs no coach and no Garage 61: the numbers come from /api/raceroom (about once a second while this page is open),
   which the window's own live link to iRacing works out. Team panels show only in a team race. The Endurance review of a
   finished team race is the second tab. Only text is written to the page (textContent); charts are built with
   createElementNS. Every few seconds the window also asks /api/raceroom?peek=1 and opens this page by itself when an
   iRacing session starts (once per session, unless switched off at the bottom of the page). */
(function(){
'use strict';
const {$,api,post,h,ok,fmtT,show,markNav,goNav,plainText,connectionLost,setMsg,setView,view}=window.DebriefHost;
const rr={seq:0,timer:null,tab:'live',st:null,busy:false,races:null,iamFilled:'',seen:new Set(),peekBusy:false};
const NS='http://www.w3.org/2000/svg';
const rrT=s=>{if(!ok(s))return '--';s=Math.max(0,Math.round(s));const hh=Math.floor(s/3600),m=Math.floor((s%3600)/60),x=s%60;return (hh?hh+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0')};
const rrLap=s=>ok(s)&&s>0?fmtT(s).replace(/0$/,''):'--';
const sgn=(x,d)=>(x>=0?'+':'\u2212')+Math.abs(x).toFixed(d);
function rrDur(s){if(!ok(s))return '--';const m=Math.round(s/60);return m>=60?Math.floor(m/60)+' h '+(m%60)+' min':m+' min'}
function rrCell(tr,t,cls){const td=h('td',cls||null,t==null||t===''?'':String(t));tr.append(td);return td}
function rrRows(id,rows,empty,cols,build){const tb=$(id);if(!rows||!rows.length){const tr=h('tr'),td=h('td','dnone',empty);td.colSpan=cols;tr.append(td);tb.replaceChildren(tr);return}
  tb.replaceChildren(...rows.map(build))}
const mk=(t,a)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);return e};
const txt=(x,y,s,a)=>{const e=mk('text',Object.assign({x:x,y:y,class:'rrtick'},a||{}));e.textContent=s;return e};
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
$('rrAuto').addEventListener('change',async()=>{let r;try{r=await post('raceroom',{action:'auto_open',on:$('rrAuto').checked})}catch(e){connectionLost();return}
  if(!r.ok)$('rrAuto').checked=!$('rrAuto').checked});
$('rrIam').addEventListener('change',async()=>{let r;try{r=await post('raceroom',{action:'iam',name:$('rrIam').value})}catch(e){connectionLost();return}
  $('rrReMsg').textContent=r.ok?'Saved: you are '+r.iam+'.':plainText(r.error||'');rr.iamFilled='';roomPoll()});
$('rrDelayGo').addEventListener('click',async()=>{let r;const who=$('rrDelayWho').value;
  try{r=await post('raceroom',{action:'delay',driver:who,minutes:+$('rrDelayMin').value||0})}catch(e){connectionLost();return}
  setMsg('rrReMsg',r.ok?'Told the plan. It works the rest of the race out again.':(r.error||'That didn\'t work.'),r.ok?'ok':'bad');roomPoll()});
$('rrReGo').addEventListener('click',async()=>{let r;try{r=await post('raceroom',{action:'replan'})}catch(e){connectionLost();return}
  setMsg('rrReMsg',r.ok?'Working it out again.':(r.error||'That didn\'t work.'),r.ok?'ok':'bad');roomPoll()});

// ---------- the board
function rrRender(j){if(view()!=='room')return;const B=j.board||{},S=B.session||{},room=B.room||null;rr.st=B;
  if(document.activeElement!==$('rrAuto'))$('rrAuto').checked=j.auto_open!==false;
  const live=B.state==='live';
  $('rrSess').textContent=S.label||'No session';$('rrSess').className='rrsess '+(S.kind||'none');
  $('rrMeta').textContent=[S.track,S.car].filter(Boolean).join(' \u00b7 ');
  $('rrH1').textContent=live&&S.track?'Race Room \u00b7 '+S.track:'Race Room';
  const role=(B.role||{}).role||'none',pill=$('rrPill');
  pill.textContent=!live?'Waiting for iRacing':role==='crew'?'Live \u00b7 crew':role==='spectator'?'Live \u00b7 watching':'Live';pill.className='pill'+(live?' ok':'');
  $('rrRole').textContent=role==='crew'?'You are in the session as crew: fuel is estimated.':'';
  $('rrText').textContent=live?'':[B.text||'Waiting for iRacing.',B.detail||''].filter(Boolean).join(' ');
  rrCallStrip(B);rrTiles(B.tiles||[]);rrRivals(B);rrPace(B);rrFuel(B.fuel_bar||{});rrChart(B.positions||{});rrCrew(B.crew||[]);
  $('rrBody').hidden=!room;rrIam(j,room||{});
  if(room){rrAlert(room);rrStint(room);rrHand(room);rrReplan(room);rrStopsTbl(room)}else $('rrAlert').hidden=true;
  const pf=B.perf;$('rrPerf').textContent=live&&pf&&ok(pf.cpu_pct)?'This page uses about '+pf.cpu_pct.toFixed(2)+'% of one core.':''}
function rrCallStrip(B){const c=B.call;$('rrCallLbl').textContent=c&&c.from==='engineer'?'Race engineer':'Crew chief';$('rrCall').textContent=c&&c.text?c.text:(B.state==='live'?'All good.':'--');
  $('rrCallSub').textContent=c&&c.from==='engineer'?'From the strategy crew.':c?'From the crew chief.':'';
  $('rrCallCard').className='rrcall'+(c&&c.rank<=3?' hot':'');
  $('rrCalls').replaceChildren(...(B.calls||[]).map(x=>h('li',null,x.text||'')))}
function rrTiles(T){$('rrTiles').replaceChildren(...T.map(t=>{const d=h('div','rrtile'+(t.state?' '+t.state:''));d.dataset.k=t.key;
  d.append(h('span','eyebrow',t.label),h('b',null,t.big||'--'),h('small',null,t.sub||''));return d}))}
const READ_CLS={'threat':'bad','on track together':'warn','catchable':'good','in the pits':'info','closing slowly':'warn','safe':'good','quicker':'warn','slower':'good','you':'me'};
function rrRivals(B){const R=B.rivals||[],race=B.rivals_kind!=='timesheet';
  $('rrRvTitle').textContent=race?'Rivals':'Timesheet';
  const heads=race?['Pos','#','Driver','Car','Gap','s a lap','Laps to','Read']:['Pos','#','Driver','Car','Best','Gap','Last','Read'];
  $('rrRvHead').replaceChildren(...heads.map(x=>h('th',null,x)));
  $('rrRvNote').textContent=race?'gaps from iRacing every second':'by best lap, in your class';
  $('rrRvFoot').textContent=race?'s a lap: plus means the gap is shrinking. Laps to: laps until they meet at that rate.':'Gap: their best lap against yours (minus is quicker).';
  rrRows('rrRivals',R,B.state==='live'?'No cars in your class yet.':'Waiting for iRacing.',8,r=>{const tr=h('tr',r.me?'us':null);
    rrCell(tr,'P'+r.pos);rrCell(tr,r.num||'');rrCell(tr,r.name||'');rrCell(tr,r.car||'');
    if(race){rrCell(tr,ok(r.gap_s)?(r.role==='behind'?'+':'\u2212')+r.gap_s.toFixed(1)+' s':'');rrCell(tr,ok(r.rate)?sgn(r.rate,2):'--');
      rrCell(tr,ok(r.laps_to_contact)&&r.laps_to_contact<99?String(Math.ceil(r.laps_to_contact)):'--')}
    else{rrCell(tr,rrLap(r.best));rrCell(tr,r.me||!ok(r.gap_s)?'--':sgn(r.gap_s,3));rrCell(tr,rrLap(r.last))}
    const td=h('td');td.append(h('span','rrtag '+(READ_CLS[r.read]||'mut'),r.read||''));tr.append(td);return tr})}
function rrPace(B){const P=B.pace||{};
  $('rrPaceNote').textContent=[P.neigh?'cars around you '+rrLap(P.neigh):'',P.top?'top 10 '+rrLap(P.top):'','in and out laps left out'].filter(Boolean).join(' \u00b7 ');
  rrRows('rrStints',B.stints,B.state==='live'?'No laps yet.':'Waiting for iRacing.',8,s=>{const tr=h('tr');rrCell(tr,s.driver||'You');rrCell(tr,s.laps);rrCell(tr,'L'+s.from+'\u2013'+s.to);
    rrCell(tr,rrLap(s.median));rrCell(tr,rrLap(s.best));rrCell(tr,(s.sd||0).toFixed(2)+' s');
    rrCell(tr,sgn(s.trend||0,3)+' s',s.trend<-0.02?'ok':s.trend>0.05?'bad':'');rrCell(tr,(s.lost||0).toFixed(1)+' s');return tr});
  rrLapChart(B.lap_chart||{})}
function rrLapChart(C){const el=$('rrLapChart'),L=C.laps||[];if(!L.length){el.replaceChildren(h('p','dnone','Your lap times draw here.'));$('rrLapLegend').replaceChildren();return}
  const W=Math.max(640,L.length*14+60),H=170,pl=50,pb=20,pt=8,ts=L.map(l=>l.s),med=C.median||Math.min(...ts);
  const lo=Math.floor(Math.min(...ts,C.top||1e9)-0.5),hi=Math.ceil(Math.min(Math.max(...ts),med+9));
  const y=t=>pt+(H-pt-pb)*(1-(Math.min(t,hi)-lo)/Math.max(0.5,hi-lo)),bw=(W-pl-8)/L.length,drv=C.drivers||[];
  const svg=mk('svg',{viewBox:'0 0 '+W+' '+H,class:'rrsvg',focusable:'false'});
  const step=Math.max(1,Math.ceil((hi-lo)/5));for(let t=Math.ceil(lo);t<=hi;t+=step){svg.append(mk('line',{x1:pl,x2:W-4,y1:y(t),y2:y(t),class:'rrgrid'}),txt(pl-4,y(t)+3,rrLap(t).replace(/\.\d+$/,''),{'text-anchor':'end'}))}
  L.forEach((l,i)=>{const x=pl+i*bw,r=mk('rect',{x:x+1,y:y(l.s),width:Math.max(2,bw-2),height:Math.max(1,H-pb-y(l.s)),class:'rrbar d'+(drv.indexOf(l.driver)%4)+(l.clean?'':' faded')});
    const ti=document.createElementNS(NS,'title');ti.textContent='Lap '+l.lap+' '+(l.driver||'')+' '+rrLap(l.s);r.append(ti);svg.append(r);if(l.lap%5===0)svg.append(txt(x+bw/2,H-6,String(l.lap),{'text-anchor':'middle'}))});
  const ref=(t,cls,lbl)=>{if(!t)return;svg.append(mk('line',{x1:pl,x2:W-4,y1:y(t),y2:y(t),class:'rrref '+cls}),txt(W-6,y(t)-3,lbl,{'text-anchor':'end',class:'rrtick '+cls}))};
  if(C.neigh&&C.top&&Math.abs(C.neigh-C.top)<0.15)ref(C.top,'good','cars around you and top 10 '+rrLap(C.top));
  else{ref(C.neigh,'warn','cars around you '+rrLap(C.neigh));ref(C.top,'good','top 10 '+rrLap(C.top))}
  el.setAttribute('aria-label','Your lap times, '+L.length+' laps. Pace '+rrLap(C.ref)+'.');el.replaceChildren(svg);
  $('rrLapLegend').replaceChildren(...drv.map((d,i)=>{const s=h('span');s.append(h('i','rrk d'+(i%4)),document.createTextNode(d||'You'));return s}),h('span',null,'faded: in, out or traffic lap \u00b7 lower is quicker'))}
function rrFuel(F){const bar=$('rrFuelBar');$('rrPfNote').textContent=F.estimated?'estimated: this PC can\'t read the car\'s fuel':(F.practice?'this run':'from your fuel gauge');
  $('rrPfText').textContent=F.text||'';$('rrPfObs').textContent=F.obs||'';$('rrPfLegend').hidden=!F.ok;
  if(!F.ok){bar.replaceChildren();bar.parentNode.hidden=true;return}bar.parentNode.hidden=false;
  const L0=Math.max(0,(F.last_stop||0)-4),L1=Math.max(F.finish,F.run_out)+1,span=Math.max(1,L1-L0),x=l=>Math.max(0,Math.min(100,(l-L0)/span*100));
  const seg=(cls,a,b)=>{const d=h('div','seg '+cls);d.style.left=x(a)+'%';d.style.width=Math.max(0,x(b)-x(a))+'%';return d};
  const mark=(l,t,up)=>{const m=h('div','mk');m.style.left=x(l)+'%';const b=h('div','lb'+(up?' up':''),t);b.style.left=x(l)+'%';return [m,b]};
  const out=[seg('done',L0,F.now),seg('tank',F.last_stop||L0,Math.min(F.run_out,L1))];
  if(F.need&&ok(F.open)&&ok(F.close))out.push(seg('win',F.open,F.close+1));
  out.push(...mark(F.now,'now L'+F.now),...mark(F.finish,'flag L'+F.finish));
  if(F.run_out<F.finish)out.push(...mark(F.run_out,'dry L'+F.run_out,true));
  bar.replaceChildren(...out);bar.setAttribute('aria-label',F.text||'Pit and fuel')}
// positions over the laps: one line a car, ours thick; P1 at the top
function rrChart(P){const el=$('rrChart'),laps=P.laps||[],ser=P.series||[];
  if(!laps.length||!ser.length){el.replaceChildren(h('p','dnone','Your position draws here, lap by lap.'));return}
  const W=640,H=220,L=30,B=18;let mx=1,mn=99;ser.forEach(s=>s.pos.forEach(p=>{if(ok(p)){if(p>mx)mx=p;if(p<mn)mn=p}}));mn=Math.max(1,mn-1);mx=mx+1;
  const x=i=>L+(laps.length<2?0:i*(W-L-6)/(laps.length-1)),y=p=>8+(p-mn)*(H-B-12)/Math.max(1,mx-mn);
  const svg=mk('svg',{viewBox:'0 0 '+W+' '+H,class:'rrsvg',focusable:'false'});
  for(let p=mn;p<=mx;p+=Math.max(1,Math.ceil((mx-mn)/6))){svg.append(mk('line',{x1:L,x2:W-4,y1:y(p),y2:y(p),class:'rrgrid'}),txt(2,y(p)+4,'P'+p))}
  svg.append(txt(L,H-3,'lap '+laps[0]),txt(W-4,H-3,'lap '+laps[laps.length-1],{'text-anchor':'end'}));
  ser.slice().sort((a,b)=>(a.me?1:0)-(b.me?1:0)).forEach(s=>{let d='',pen=false;s.pos.forEach((p,i)=>{if(!ok(p)){pen=false;return}d+=(pen?'L':'M')+x(i).toFixed(1)+' '+y(p).toFixed(1)+' ';pen=true});
    if(d)svg.append(mk('path',{d:d,class:'rrline'+(s.me?' me':'')}))});
  const me=ser.find(s=>s.me),now=me?[...me.pos].reverse().find(ok):null;
  el.setAttribute('aria-label','Positions lap by lap.'+(now?' Our car: P'+now+' now.':''));el.replaceChildren(svg)}
function rrCrew(D){$('rrCrew').replaceChildren(...D.map(d=>{const c=h('div','rrdesk '+(d.state||'ok')),top=h('div','rrdtop');
  top.append(h('h4',null,d.name),h('span','rrtag '+(d.state==='act'?'bad':d.state==='watch'?'warn':'good'),d.state==='act'?'act now':d.state==='watch'?'watch':'all good'));
  c.append(top,h('b',null,d.head||''));(d.lines||[]).forEach(l=>c.append(h('p',null,l)));return c}))}

// ---------- team race panels (from the Race Room of a team race)
function rrIam(j,st){const pl=st.plan||{},names=pl.drivers||[],wrap=$('rrIamWrap');wrap.hidden=!names.length;if(!names.length)return;
  const key=names.join('|')+'#'+(j.iam||'');if(key===rr.iamFilled)return;rr.iamFilled=key;const sel=$('rrIam');
  const opts=[h('option',null,'Choose...')];opts[0].value='';names.forEach(n=>{const o=h('option',null,n);o.value=n;opts.push(o)});sel.replaceChildren(...opts);sel.value=names.indexOf(j.iam)>=0?j.iam:'';
  const dd=$('rrDelayWho');dd.replaceChildren(...names.map(n=>{const o=h('option',null,n);o.value=n;return o}))}
function rrAlert(st){const a=(st.swap||{}).alert,e=$('rrAlert');e.hidden=!a;if(a){e.textContent=a.text||'Your stint is coming up.';e.className='rralert'+(a.kind==='swap_5'?' hot':'')}}
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
function rrStopsTbl(st){const so=st.stops||{};
  rrRows('rrStops',so.rivals,'No stops seen yet.',5,r=>{const tr=h('tr',r.me?'us':null);rrCell(tr,(r.num?'#'+r.num+' ':'')+(r.name||'')+(r.me?' (us)':''));rrCell(tr,'P'+r.pos);
    rrCell(tr,r.stops+(r.passes?' (+'+r.passes+' drive-through)':'')+(r.in_pit?' \u00b7 in the pits now':''));rrCell(tr,r.last_lap==null?'':'lap '+r.last_lap);
    rrCell(tr,(r.list||[]).map(x=>x.lap+(x.caution?'c':'')).join(', '));return tr})}

// ---------- opening by itself when a session starts (the window asks every few seconds, whatever page is open)
async function rrPeek(){if(rr.peekBusy||document.hidden)return;rr.peekBusy=true;let j;
  try{j=await api('raceroom?peek=1')}catch(e){return}finally{rr.peekBusy=false}
  const S=j&&j.ok&&j.connected?j.session:null;if(!S||!S.key||rr.seen.has(S.key))return;
  if(document.querySelector('dialog[open]'))return;            // try again after the dialog is closed
  rr.seen.add(S.key);if(j.auto_open===false||view()==='room')return;
  if(window.DebriefHost.canLeave&&!window.DebriefHost.canLeave())return;
  goNav('room')}
setInterval(rrPeek,3000);setTimeout(rrPeek,1500);

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

window.DebriefRoom={open:openRoom,peek:rrPeek};
})();
