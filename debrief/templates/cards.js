/* 2.25 "Save as picture": draws an engagement card (a spec made by debrief/cards.py) on a 1200 x 675 canvas and
   saves it as a PNG. Nothing leaves the computer: in the Debrief window the window saves it into the library's
   shares folder and says where; opened straight from disk the browser downloads it. */
(function(){
const W=1200,H=675;
const DISP='"Barlow Condensed","Arial Narrow","Roboto Condensed",sans-serif',BOD='"IBM Plex Sans","Segoe UI",system-ui,sans-serif',MON='"JetBrains Mono",Consolas,ui-monospace,monospace';
const C={bg:'#0b0e12',panel:'#151a20',ink:'#f2f4f6',ink2:'#bcc4cc',muted:'#8b949d',line:'#2a313a',a:'#3987e5',b:'#ee6a32',gold:'#f5c542',pb:'#9b8cf2'};
const str=v=>v==null?'':String(v);
async function fonts(){try{if(document.fonts&&document.fonts.load)await Promise.race([Promise.all(['700 96px "Barlow Condensed"','600 30px "IBM Plex Sans"','400 22px "IBM Plex Sans"','600 40px "JetBrains Mono"'].map(f=>document.fonts.load(f))),new Promise(r=>setTimeout(r,1200))])}catch(e){}}
async function draw(spec){
  spec=spec&&typeof spec==='object'?spec:{};await fonts();
  const cv=document.createElement('canvas');cv.width=W;cv.height=H;const x=cv.getContext('2d');
  const sp=v=>{if('letterSpacing' in x)x.letterSpacing=v};
  const fit=(t,font,size,maxW,min=18)=>{let s=size;x.font=font.replace('$',s);while(x.measureText(t).width>maxW&&s>min){s-=2;x.font=font.replace('$',s)}return s};
  const text=(t,X,Y,font,size,color,maxW,al)=>{t=str(t);x.fillStyle=color;x.textAlign=al||'left';const s=maxW?fit(t,font,size,maxW):size;x.font=font.replace('$',s);x.fillText(t,X,Y);x.textAlign='left';return s};
  const rr=(X,Y,w,h,r)=>{x.beginPath();if(x.roundRect)x.roundRect(X,Y,w,h,r);else x.rect(X,Y,w,h)};
  const lay=spec.layout||'board';
  // background: dark, with speed stripes and a blue-to-orange top bar
  x.fillStyle=C.bg;x.fillRect(0,0,W,H);
  const gr=x.createLinearGradient(0,0,W,H);gr.addColorStop(0,'rgba(57,135,229,.28)');gr.addColorStop(.5,'rgba(57,135,229,0)');gr.addColorStop(1,'rgba(238,106,50,.18)');x.fillStyle=gr;x.fillRect(0,0,W,H);
  x.save();x.globalAlpha=.07;x.fillStyle='#fff';for(let i=0;i<7;i++){x.beginPath();x.moveTo(760+i*70,0);x.lineTo(800+i*70,0);x.lineTo(560+i*70,H);x.lineTo(520+i*70,H);x.closePath();x.fill()}x.restore();
  const bar=x.createLinearGradient(0,0,W,0);bar.addColorStop(0,C.a);bar.addColorStop(1,C.b);x.fillStyle=bar;x.fillRect(0,0,W,10);
  // top line: kicker left, team right
  x.textBaseline='alphabetic';sp('3px');text(str(spec.kicker).toUpperCase(),64,62,'600 $px '+MON,18,C.gold);sp('0px');
  if(spec.team){x.font='600 22px '+BOD;sp('1px');text(str(spec.team).toUpperCase(),W-64,62,'600 $px '+BOD,22,C.ink2,420,'right');sp('0px')}
  const hero=spec.hero,rows=Array.isArray(spec.rows)?spec.rows:[],cols=Array.isArray(spec.cols)?spec.cols:[],stats=Array.isArray(spec.stats)?spec.stats:[];
  const BAND=548;   // the punchy line sits in a band from here down to the footer
  const headW=hero&&lay!=='duel'&&lay!=='best'?640:1070;
  const head=str(spec.headline).toUpperCase();
  const hs=text(head,60,160,'700 $px '+DISP,lay==='best'?84:112,C.ink,headW);
  text(spec.sub,64,160+36,'500 $px '+BOD,28,C.ink2,headW);
  if(spec.empty){text(spec.line||'Nothing to show on this card yet.',64,330,'500 $px '+BOD,34,C.ink2,1070)}
  else if(lay==='board'||lay==='corner'){
    if(hero){x.fillStyle=C.panel;rr(740,96,396,168,14);x.fill();x.fillStyle=C.gold;x.fillRect(740,96,8,168);
      sp('3px');text(hero.label,772,134,'600 $px '+MON,15,C.muted);sp('0px');
      text(hero.value,772,200,(lay==='board'?'700 $px '+MON:'700 $px '+DISP),lay==='board'?64:62,C.gold,340);
      text(hero.sub,772,240,'500 $px '+BOD,24,C.ink,340)}
    if(stats[0]&&lay==='corner'){x.fillStyle=C.ink;sp('2px');text(stats[0].label,64,262,'600 $px '+MON,15,C.muted);sp('0px');
      text(stats[0].value,64,320,'700 $px '+MON,52,C.a,520);text(stats[0].sub,64,350,'400 $px '+BOD,22,C.ink2,520)}
    const y0=lay==='corner'?414:292,rh=lay==='corner'?40:46,n=Math.min(rows.length,lay==='corner'?3:5);
    rows.slice(0,n).forEach((r,i)=>{const y=y0+i*rh;
      if(r.hl){x.fillStyle='rgba(245,197,66,.14)';rr(56,y-31,W-112,rh-4,8);x.fill();x.fillStyle=C.gold;x.fillRect(56,y-31,6,rh-4)}
      const col=r.hl?C.gold:C.ink;
      text('P'+r.pos,84,y,'700 $px '+MON,24,r.hl?C.gold:C.muted);
      text(r.name,150,y,(r.hl?'700':'600')+' $px '+BOD,26,col,360);
      text(r.value,640,y,'600 $px '+MON,26,col,170,'right');
      text(r.gap,790,y,'500 $px '+MON,20,r.hl?C.gold:C.ink2,130,'right');
      text(r.ir?(lay==='corner'?r.ir:'iR '+r.ir):'',930,y,'400 $px '+MON,19,C.muted,110,'right');
      text(r.date,1100,y,'400 $px '+BOD,20,C.muted,120,'right')})}
  else if(lay==='duel'){
    const cw=440;cols.slice(0,2).forEach((c,i)=>{const X=64+i*(W-128-cw);x.fillStyle=C.panel;rr(X,236,cw,200,14);x.fill();
      x.fillStyle=c.hl?C.gold:(i?C.b:C.a);x.fillRect(X,236,cw,8);
      text(c.name,X+28,290,'700 $px '+BOD,34,C.ink,cw-56);text(c.value,X+28,376,'700 $px '+MON,70,c.hl?C.gold:C.ink,cw-56);text(c.sub,X+28,414,'400 $px '+BOD,22,C.ink2,cw-56)});
    if(hero){const cx=W/2;x.fillStyle=C.bg;rr(cx-88,262,176,148,16);x.fill();x.strokeStyle=C.line;x.lineWidth=2;x.stroke();
      sp('3px');text(hero.label,cx,294,'600 $px '+MON,14,C.muted,160,'center');sp('0px');text(hero.value,cx,346,'700 $px '+MON,34,C.gold,160,'center');text(hero.sub,cx,386,'500 $px '+BOD,17,C.ink2,160,'center')}
    if(stats[0])text(stats[0].sub,W/2,490,'500 $px '+BOD,26,C.ink2,1070,'center')}
  else if(lay==='member'||lay==='best'){
    if(lay==='best'&&hero){sp('3px');text(hero.label,64,262,'600 $px '+MON,16,C.muted);sp('0px');
      text(hero.value,60,368,'700 $px '+MON,128,C.gold,760);text(hero.sub,64,412,'500 $px '+BOD,28,C.ink,760);
      if(stats[0]){x.fillStyle=C.panel;rr(850,250,286,170,14);x.fill();sp('3px');text(stats[0].label,876,290,'600 $px '+MON,14,C.muted);sp('0px');
        text(stats[0].value,876,346,'700 $px '+DISP,52,C.ink,240);text(stats[0].sub,876,386,'400 $px '+BOD,22,C.ink2,240)}}
    if(lay==='member'){const n=stats.length||1,tw=(W-128-(n-1)*16)/n;stats.forEach((s,i)=>{const X=64+i*(tw+16);x.fillStyle=C.panel;rr(X,232,tw,104,12);x.fill();
        sp('2px');text(s.label,X+20,262,'600 $px '+MON,14,C.muted);sp('0px');
        if(s.value){text(s.value,X+20,314,'700 $px '+MON,40,C.gold,tw-40);if(s.sub)text(s.sub,X+20+x.measureText(s.value).width+10,314,'400 $px '+BOD,18,C.ink2,Math.max(40,tw-60-x.measureText(s.value).width))}
        else text(s.sub,X+20,310,'500 $px '+BOD,20,C.ink,tw-40)});
      rows.slice(0,4).forEach((r,i)=>{const y=380+i*40;text(r.name,84,y,'600 $px '+BOD,24,r.hl?C.gold:C.ink,620);text(r.value,860,y,'600 $px '+MON,24,r.hl?C.gold:C.ink,170,'right');text(r.date,1100,y,'400 $px '+BOD,20,C.muted,120,'right')})}}
  // the punchy line, in a band
  if(!spec.empty&&spec.line){x.fillStyle=bar;rr(56,BAND,W-112,70,12);x.fill();x.fillStyle='rgba(8,10,14,.82)';rr(58,BAND+2,W-116,66,10);x.fill();
    text(spec.line,84,BAND+46,'700 $px '+BOD,30,C.ink,W-170)}
  // footer
  sp('2px');text(str(spec.brand||'Debrief · by Black Magik Studios').toUpperCase(),W-64,H-26,'400 $px '+MON,15,C.muted,520,'right');sp('0px');
  return cv}
function toast(msg){let t=document.getElementById('cardToast');if(!t){t=document.createElement('div');t.id='cardToast';t.setAttribute('role','status');
    t.style.cssText='position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:#161a1f;color:#eceff2;padding:10px 16px;border-radius:8px;font:14px system-ui,sans-serif;z-index:99;max-width:90vw;box-shadow:0 8px 28px rgba(0,0,0,.4)';document.body.append(t)}
  t.textContent=msg;t.hidden=false;clearTimeout(t._h);t._h=setTimeout(()=>{t.hidden=true},9000)}
async function save(spec){
  const name=str(spec&&spec.file).replace(/[^a-z0-9._-]+/gi,'-')||'debrief-card.png';
  try{const cv=await draw(spec);
    if(window.parent!==window){let png=null;try{png=cv.toDataURL('image/png')}catch(_){}
      if(typeof png!=='string'||!png.startsWith('data:image/png;base64,'))throw new Error('no image');
      window.parent.postMessage({type:'debrief:share-save',name,png,kind:'card'},'*');toast('Saving the picture...');return true}
    const bl=await new Promise((res,rej)=>cv.toBlob(b=>b?res(b):rej(new Error('no image')),'image/png'));
    const a=document.createElement('a');a.href=URL.createObjectURL(bl);a.download=name;document.body.append(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},4000);
    toast('Saved '+name+' in your browser\'s Downloads folder');return true}
  catch(e){toast('Couldn’t make the picture in this window. Try opening the page in your browser.');return false}}
function button(id,label){const specs=window.CARDS||{};const fn=typeof id==='function'?id:null;if(!fn&&!specs[id])return null;
  const b=document.createElement('button');b.type='button';b.className='cardbtn';b.textContent=label||'Save as picture';
  b.title='Save a 1200 x 675 picture of this to share (Discord, X)';b.addEventListener('click',async()=>{b.disabled=true;try{const sp=specs[fn?fn():id];if(sp)await save(sp);else toast('There is no picture for this one yet.')}finally{b.disabled=false}});return b}
window.DebriefCards={draw,save,button,W,H};
})();
