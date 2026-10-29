// Offline interaction regression: all experiment/API evidence below is synthetic.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const http=require('node:http');
const path=require('node:path');
const web=path.resolve(__dirname,'../src/ablation_workbench/web');
const fixture={
  id:'exp_browser_workbench',title:'Synthetic drawer and latest-version fixture',revision:1,
  created_at:'2026-01-01T00:00:00Z',source:{kind:'demo',fidelity:'synthetic'},
  baseline:{hash:'synthetic-baseline'},reviews:[],
  candidates:[{id:'baseline',label:'Baseline'},{id:'candidate_old',label:'旧候选'},{id:'candidate_latest',label:'最新候选'}],
  scenarios:[{id:'original',label:'合成回放场景',origin:'synthetic',description:'Offline browser fixture'}],runs:[],
};
for(let index=1;index<=6;index++){
  const failed=index<=3,action=failed?'handoff':'park';
  fixture.runs.push({
    id:`run_fixture_${index}`,batch_id:'batch_fixture',candidate:failed?'candidate_old':'candidate_latest',
    scenario:'original',repeat:failed?index:index-3,status:'completed',passed:!failed,
    created_at:'2026-01-01T00:01:00Z',completed_at:'2026-01-01T00:01:01Z',recorded_revision:1,elapsed_ms:1000,
    request:{model:'fixture-model',instructions:'Synthetic evidence. Do not execute tools.',
      input:Array.from({length:35},(_,i)=>({role:i%2?'assistant':'user',content:`Synthetic history ${i}: ${'Recorded context only. '.repeat(8)}`}))},
    response:{model:'fixture-model',output:[{type:'function_call',name:'cu_release_app',call_id:`call_${index}`,arguments:JSON.stringify({action,action_intent:'Retain the verified synthetic result.'})}],usage:{total_tokens:100}},
    checks:[{kind:'tool_argument_equals',name:'cu_release_app',pointer:'/action',value:'park',actual:[action],passed:!failed}],
  });
}
fixture.runs[1].checks.push(
  {kind:'tool_called',name:'missing_fixture_tool',actual:['cu_release_app'],passed:false},
  {kind:'json_equals',pointer:'/status',value:'expected',actual:'different',passed:false},
);
let browser,server;
const sockets=new Set(),mutations=[],errors=[],externalRequests=[];
async function until(fn,timeout=15000){
  const started=Date.now();
  while(Date.now()-started<timeout){if(await fn())return;await new Promise(resolve=>setTimeout(resolve,50));}
  throw Error('Timed out waiting for offline workbench evidence');
}
function diff(query){
  const candidate=fixture.candidates.find(item=>item.id===query.get('candidate'))||fixture.candidates.at(-1);
  return {base_label:'Baseline',candidate_label:candidate.label,artifact:query.get('artifact')||'changes',
    before:'Original synthetic instruction',after:`${candidate.label}: updated synthetic instruction`,
    rationale:'Synthetic browser-only version.',notes:[],change_groups:[],
    rows:[{old_line:1,new_line:1,before:'Original synthetic instruction',after:`${candidate.label}: updated synthetic instruction`,kind:'replace'}]};
}
async function settleDrawer(page){
  await page.waitForFunction(()=>!document.getAnimations().some(animation=>animation.playState==='running'&&animation.effect?.target?.closest('#run-drawer'))&&!document.querySelector('.run-drawer-outgoing,.run-drawer-backplate')&&![...document.querySelectorAll('[data-motion-hidden]')].some(element=>getComputedStyle(element).visibility==='hidden'));
}
const liveBookmarks='#run-drawer-stack .run-drawer-sheet[data-visible="true"] > .run-drawer-spine[data-run-detail], #run-drawer-panel > .run-drawer-spine[data-run-detail]';
async function selectReplay(page,runId){
  if(await page.locator('#run-drawer-panel').getAttribute('data-run-id')===runId)return;
  const bookmark=page.locator(`#run-drawer-stack .run-drawer-sheet[data-visible="true"] > .run-drawer-spine[data-run-detail="${runId}"]`);
  assert.equal(await bookmark.count(),1,`Replay ${runId} must have a visible attached bookmark`);
  await bookmark.locator('.spine-number').click();await settleDrawer(page);
}
async function assertPhysicalBookmarks(page,count){
  const geometry=await page.evaluate(selector=>{
    const panel=document.querySelector('#run-drawer-panel'),front=panel.getBoundingClientRect();
    const viewport=document.querySelector('#run-drawer-viewport'),body=document.querySelector('#run-drawer-surface');
    const box=rect=>({left:rect.left,right:rect.right,top:rect.top,bottom:rect.bottom,width:rect.width,height:rect.height});
    return {active:panel.dataset.runId,viewport:{width:innerWidth,height:innerHeight},panel:box(front),
      clip:{...box(viewport.getBoundingClientRect()),overflow:getComputedStyle(viewport).overflowX},body:box(body.getBoundingClientRect()),
      stackRole:document.querySelector('#run-drawer-stack').getAttribute('role'),
      bookmarks:[...document.querySelectorAll(selector)].map(button=>{
        const frame=button.parentElement,number=button.querySelector('.spine-number'),rect=number.getBoundingClientRect();
        const hit=document.elementFromPoint(rect.left+rect.width/2,rect.top+rect.height/2),neck=getComputedStyle(button,'::after');
        return {frame:box(frame.getBoundingClientRect()),label:box(button.getBoundingClientRect()),number:box(rect),
          main:frame===panel,owner:frame.dataset.stackRun||frame.dataset.runId,current:button.getAttribute('aria-current'),pressed:button.getAttribute('aria-pressed'),role:button.getAttribute('role'),
          status:!!button.querySelector('.spine-status-dot'),id:button.dataset.runDetail,
          neck:{content:neck.content,width:parseFloat(neck.width)||0},
          reachable:hit?.closest('[data-run-detail]')?.dataset.runDetail===button.dataset.runDetail,
          horizontal:[button,...button.querySelectorAll('*')].every(item=>getComputedStyle(item).writingMode==='horizontal-tb')};
      })};
  },liveBookmarks);
  assert.equal(geometry.bookmarks.length,count,`All ${count} page bookmarks remain visible`);
  assert.equal(geometry.bookmarks.filter(item=>item.current==='page').length,1,'Exactly one ordinary bookmark is current');
  const current=geometry.bookmarks.find(item=>item.current==='page');
  assert(current.main&&current.id===geometry.active&&current.pressed==='true','Current bookmark is attached directly to the live page');
  assert.notEqual(geometry.stackRole,'tablist','Bookmarks are attached page controls rather than a separate navbar');
  assert(Math.abs(geometry.panel.top)<1&&geometry.panel.height>=geometry.viewport.height-1,'Live page remains top-aligned and full height');
  assert(geometry.panel.left>=0&&geometry.panel.right<=geometry.viewport.width+1,'Live page fits the right side of the viewport');
  assert(Math.abs(geometry.clip.left-geometry.panel.left)<2&&geometry.clip.width>=geometry.panel.width-2&&Math.abs(geometry.clip.top)<2&&geometry.clip.bottom<=geometry.viewport.height+1,'Clipped body fills the entire live page without a rail gutter');
  assert(['hidden','clip'].includes(geometry.clip.overflow),'Page viewport clips content during physical expansion');
  assert(geometry.bookmarks.every(item=>item.owner===item.id&&item.role!=='tab'),'Each ordinary button belongs to its corresponding page');
  assert(geometry.bookmarks.every(item=>Math.abs(item.frame.left-geometry.panel.left)<2&&Math.abs(item.frame.top)<1&&Math.abs(item.frame.width-geometry.panel.width)<2&&item.frame.height>=geometry.viewport.height-1),'Background pages share the live page rectangle; there are no detached full-height peer rails');
  assert(geometry.bookmarks.every(item=>item.label.height>=100&&item.label.height<=140&&item.label.top>=0&&item.label.bottom<=180&&item.label.right<=geometry.panel.left+2&&item.horizontal&&item.status&&item.reachable&&item.number.left>=-1&&item.number.right<=geometry.viewport.width+1),'Every attached bookmark/status protrudes only in the top-left and remains directly clickable');
  assert(geometry.bookmarks.every(item=>item.neck.content!=='none'&&item.neck.width>=geometry.panel.left-item.label.right-2),'Bookmark necks connect each index sticker to its page');
  return geometry;
}
async function sampleMotion(page,selector){
  return page.evaluate(async selector=>{
    const button=document.querySelector(selector);
    if(!button)throw Error(`Missing motion trigger ${selector}`);
    const trigger=button.getBoundingClientRect(),origin={left:trigger.left,right:trigger.right,top:trigger.top};
    button.click();
    const panel=document.querySelector('#run-drawer-panel');
    const sameBookmark=panel.querySelector(':scope > .run-drawer-spine')===button;
    const animations=document.getAnimations().filter(animation=>{
      const target=animation.effect?.target;
      return target===panel||target?.closest('#run-drawer-panel')||target?.classList.contains('run-drawer-outgoing');
    });
    const clone=document.querySelector('.run-drawer-outgoing'),plate=document.querySelector('.run-drawer-backplate');
    const backplateContent=plate?{inert:plate.inert,hidden:plate.getAttribute('aria-hidden'),ids:plate.querySelectorAll('[id]').length+(plate.hasAttribute('id')?1:0),content:plate.textContent.trim().length}:null;
    const outgoingContent=clone?{inert:clone.inert,hidden:clone.getAttribute('aria-hidden'),ids:clone.querySelectorAll('[id]').length+(clone.hasAttribute('id')?1:0),rawScrollTop:clone.querySelector('.run-drawer-raw')?.scrollTop}:null;
    let keyboardUsesCurrentPanel=null;
    if(clone){
      const first=panel.querySelector('.run-drawer-tabs [data-run-view]');first.focus();
      first.dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true}));
      keyboardUsesCurrentPanel=panel.contains(document.activeElement)&&document.activeElement.dataset.runView==='raw';
    }
    const states=animations.map(animation=>animation.playState);
    const duration=Math.max(0,...animations.map(animation=>Number(animation.effect.getComputedTiming().duration)||0));
    animations.forEach(animation=>animation.pause());
    const samples=[];
    const labelBox=node=>{
      const button=node?.querySelector(':scope > .run-drawer-spine');if(!button)return null;
      const rect=button.getBoundingClientRect();return {left:rect.left,right:rect.right,top:rect.top};
    };
    for(const progress of [0,.25,.5,1]){
      animations.forEach(animation=>animation.currentTime=duration*progress);
      const rect=panel.getBoundingClientRect(),incoming=document.querySelector('#run-drawer-surface'),body=incoming.getBoundingClientRect(),outgoing=document.querySelector('.run-drawer-outgoing');
      const old=outgoing?.getBoundingClientRect(),plate=document.querySelector('.run-drawer-backplate'),back=plate?.getBoundingClientRect();
      const plateOpaque=plate&&[plate,...plate.querySelectorAll('*')].some(node=>{
        const area=node.getBoundingClientRect(),style=getComputedStyle(node);
        return area.left<=back.left+1&&area.right>=back.right-1&&area.height>=back.height-1&&style.backgroundColor!=='rgba(0, 0, 0, 0)'&&style.backgroundColor!=='transparent'&&Number(style.opacity)>=.99;
      });
      samples.push({progress,left:rect.left,top:rect.top,width:rect.width,bookmark:labelBox(panel),
        incoming:{left:body.left,width:body.width,opacity:Number(getComputedStyle(incoming).opacity)},backplate:back?{left:back.left,right:back.right,top:back.top,height:back.height,opaque:plateOpaque}:null,
        outgoing:old?{left:old.left,top:old.top,width:old.width,opacity:Number(getComputedStyle(outgoing).opacity),bookmark:labelBox(outgoing)}:null});
    }
    animations.forEach(animation=>animation.finish());
    await new Promise(resolve=>requestAnimationFrame(resolve));
    return {states,duration,samples,outgoingContent,backplateContent,keyboardUsesCurrentPanel,origin,sameBookmark};
  },selector);
}
async function main(){
  server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://127.0.0.1');
    url.pathname=url.pathname.replace(/^\/ablation(?=\/|$)/,'')||'/';
    if(req.method!=='GET'){mutations.push(req.url);res.writeHead(405);res.end();return;}
    if(url.pathname==='/api/events'){res.writeHead(200,{'Content-Type':'text/event-stream'});res.write('retry: 1000\n\n');return;}
    const json=data=>{res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(data));};
    if(url.pathname==='/api/experiments')return json({experiments:[{id:fixture.id,title:fixture.title,created_at:fixture.created_at,run_count:fixture.runs.length}]});
    if(url.pathname===`/api/experiments/${fixture.id}`)return json(fixture);
    if(url.pathname===`/api/experiments/${fixture.id}/diff`)return json(diff(url.searchParams));
    if(url.pathname==='/api/config')return json({profiles:{},agent:{}});
    if(url.pathname==='/api/jobs')return json({jobs:[]});
    if(url.pathname==='/api/update')return json({current:'0.0.0',status:'up_to_date',available:false});
    if(url.pathname==='/api/version')return json({version:'0.0.0'});
    const filename=url.pathname==='/'?'index.html':url.pathname.slice(1);
    const target=path.resolve(web,filename);
    if(!target.startsWith(web+path.sep)||!fs.existsSync(target)){res.writeHead(404);res.end();return;}
    const ext=path.extname(target),type={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}[ext]||'text/plain';
    res.writeHead(200,{'Content-Type':type,'Cache-Control':'no-store'});res.end(fs.readFileSync(target));
  });
  server.on('connection',socket=>{sockets.add(socket);socket.on('close',()=>sockets.delete(socket));});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base=`http://127.0.0.1:${server.address().port}/ablation/`;
  browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'no-preference'});
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{if(new URL(request.url()).origin!==new URL(base).origin)externalRequests.push(request.url());});
  await page.goto(`${base}?experiment=${fixture.id}&candidate=candidate_old`);
  await page.locator('.diff-row').first().waitFor();
  assert((await page.locator('.candidate-context').innerText()).includes('历史'));
  assert((await page.locator('.diff-headrow').innerText()).includes('旧候选'));
  await page.locator('.page-tab[data-tab="diff"]').click();
  await until(async()=>(await page.locator('.diff-headrow').innerText()).includes('最新候选'));
  assert.equal(new URL(page.url()).searchParams.get('candidate'),null);

  // Follow newest automatically, without moving historical evidence or losing drafts.
  await page.locator('.diff-row [data-plus]').first().click();
  await page.locator('#inline-comment').fill('Synthetic draft remains bound to its original version.');
  await page.locator('[data-action="add-comment"]').click();
  fixture.candidates.push({id:'candidate_new',label:'自动更新候选'});fixture.revision++;
  await until(async()=>(await page.locator('.diff-headrow').innerText()).includes('自动更新候选'));
  assert.equal(new URL(page.url()).searchParams.get('candidate'),null);
  await page.locator('.page-tab[data-tab="runs"]').click();
  await page.locator('#run-auxiliary details > summary').first().click();
  await page.locator('[data-view-candidate="candidate_latest"]').click();
  await until(async()=>(await page.locator('#diff-file').innerText()).includes('Synthetic draft remains bound to its original version.'));
  fixture.candidates.push({id:'candidate_newer',label:'后续自动更新候选'});fixture.revision++;
  await page.waitForTimeout(3000);
  assert((await page.locator('.diff-headrow').innerText()).includes('最新候选'),'Explicit historical inspection must not jump versions');
  await page.locator('.diff-row [data-plus]').first().click();
  await page.locator('#inline-comment').fill('Unadded historical editor survives switching to latest.');
  await page.locator('.page-tab[data-tab="diff"]').click();
  await until(async()=>(await page.locator('.diff-headrow').innerText()).includes('后续自动更新候选'));
  assert.equal(await page.locator('#inline-comment').count(),0,'Historical editor must not block new-version comments');
  await page.locator('.diff-row [data-plus]').first().click();
  await page.locator('#inline-comment').fill('A fresh latest-version comment.');
  await page.locator('[data-action="add-comment"]').click();
  await page.locator('.page-tab[data-tab="runs"]').click();
  await page.locator('#run-auxiliary details > summary').first().click();
  await page.locator('[data-view-candidate="candidate_latest"]').click();
  await until(async()=>(await page.locator('#diff-file').innerText()).includes('Unadded historical editor survives switching to latest.'));
  assert.equal(await page.getByText('Unadded historical editor survives switching to latest.',{exact:true}).count(),1);
  await page.locator('.page-tab[data-tab="diff"]').click();
  await until(async()=>(await page.locator('#diff-file').innerText()).includes('A fresh latest-version comment.'));

  await page.locator('.page-tab[data-tab="runs"]').click();
  await page.locator('.history-batch').waitFor();
  const batchText=await page.locator('.history-batch').innerText();
  assert(batchText.includes('park')&&batchText.includes('handoff'),'Batch exposes expected and actual failure values');
  const opening=await sampleMotion(page,'.run-batch-open');
  assert(opening.states.includes('running'),'Normal-motion drawer opening starts a native animation');
  assert(opening.duration>=180,'Opening movement remains perceptible');
  assert(Math.abs(opening.samples[0].left-opening.samples.at(-1).left)>8,'Opening drawer moves into the foreground');
  await page.locator('#run-drawer-panel').waitFor();
  await assertPhysicalBookmarks(page,6);
  const edgeSelector='#run-drawer-stack .run-drawer-sheet[data-visible="true"] > .run-drawer-spine[data-run-detail]';
  const edgeRun=await page.locator(edgeSelector).first().getAttribute('data-run-detail');
  const switching=await sampleMotion(page,edgeSelector);
  assert(switching.states.includes('running'),'Selecting a physical bookmark starts whole-page motion');
  assert(switching.sameBookmark,'The chosen actual bookmark DOM node moves into the live page');
  const [start,quarter,middle,end]=switching.samples;
  assert(start.width<end.width-100&&quarter.width>start.width+20,'Incoming whole page expands from the chosen bookmark');
  assert(switching.samples.every(sample=>Math.abs(sample.incoming.width-end.incoming.width)<2),'Page content keeps a fixed reading width while its viewport opens');
  assert(Math.abs(start.bookmark.left-switching.origin.left)<2&&Math.abs(start.bookmark.top-switching.origin.top)<2,'Chosen bookmark starts at its original physical position');
  assert(switching.samples.every(sample=>Math.abs(sample.bookmark.right-sample.left)<2&&Math.abs(sample.bookmark.top-sample.top-12)<2),'Incoming page and attached bookmark move together');
  assert(start.outgoing&&quarter.outgoing,'Previous foreground remains visible while it retreats');
  assert(quarter.outgoing.width<start.outgoing.width-20&&end.outgoing.width<5,'Previous whole page collapses into its attached index sticker');
  assert(switching.samples.every(sample=>Math.abs(sample.outgoing.bookmark.right-sample.outgoing.left)<2&&Math.abs(sample.outgoing.bookmark.top-sample.outgoing.top-12)<2),'Outgoing page and its bookmark move together');
  assert.deepEqual({inert:switching.backplateContent.inert,hidden:switching.backplateContent.hidden,ids:switching.backplateContent.ids},{inert:true,hidden:'true',ids:0});
  assert(switching.backplateContent.content>30,'Stable backplate retains actual previous page content');
  assert(switching.samples.every(sample=>sample.backplate&&sample.backplate.left<=end.left+1&&sample.backplate.right>=end.left+end.width-1&&sample.backplate.top<=1&&sample.backplate.height>=999&&sample.backplate.opaque),'Stable opaque content covers the right edge throughout the physical page transition');
  assert.equal(await page.locator('#run-drawer-panel').getAttribute('data-run-id'),edgeRun);
  assert.equal(switching.keyboardUsesCurrentPanel,true,'Keyboard End selects real panel tab while clone exists');
  assert.deepEqual({inert:switching.outgoingContent.inert,hidden:switching.outgoingContent.hidden,ids:switching.outgoingContent.ids},{inert:true,hidden:'true',ids:0});
  const rapid=await page.evaluate(()=>{
    const next=()=>[...document.querySelectorAll('#run-drawer-stack .run-drawer-sheet[data-visible="true"] > .run-drawer-spine[data-run-detail]')].find(button=>getComputedStyle(button).visibility!=='hidden');
    next().click();const first=document.querySelectorAll('.run-drawer-outgoing').length;
    next().click();const second=document.querySelectorAll('.run-drawer-outgoing').length;
    const active=document.querySelector('#run-drawer-panel').dataset.runId;
    document.querySelector('[data-action="close-run-drawer"]').click();return {first,second,active};
  });
  assert(rapid.first<=1&&rapid.second<=1,'Rapid switching replaces the previous outgoing clone');
  await page.locator('#run-drawer').waitFor({state:'hidden'});
  await until(()=>page.evaluate(()=>!document.querySelector('.run-drawer-outgoing,.run-drawer-backplate')&&![...document.querySelectorAll('[data-motion-hidden]')].some(element=>getComputedStyle(element).visibility==='hidden')&&!document.getAnimations().some(animation=>animation.playState==='running'&&animation.effect?.target?.closest('#run-drawer'))));
  await page.locator('.run-batch-open').first().click();await settleDrawer(page);
  await selectReplay(page,'run_fixture_1');

  await until(async()=>(await page.locator('#run-drawer-context').innerText()).includes('run_fixture_1')); // A failed batch opens its first failure directly.
  await page.locator('.run-drawer-tabs [data-run-view="checks"]').click();
  const checks=await page.locator('#run-drawer-checks').innerText();
  assert(checks.includes('期望')&&checks.includes('实际')&&checks.includes('park')&&checks.includes('handoff'));
  await page.locator('[data-run-check-output="0"]').click();
  await page.locator('.run-drawer-tabs [data-run-view="messages"][aria-selected="true"]').waitFor();
  await until(()=>page.locator('[data-message-source="output"][data-message-tool="cu_release_app"]').evaluate(row=>{
    const viewport=row.closest('.bf-message-scroll').getBoundingClientRect(),rect=row.getBoundingClientRect();
    return rect.top>=viewport.top&&rect.bottom<=viewport.bottom;
  }));
  await page.locator('.run-drawer-tabs [data-run-view="raw"]').click();
  const raw=page.locator('#run-drawer-raw');
  await raw.evaluate(node=>node.scrollTop=240);
  const rawScroll=await raw.evaluate(node=>node.scrollTop);assert(rawScroll>0);
  const rawTransition=await sampleMotion(page,'#run-drawer-stack .run-drawer-sheet[data-visible="true"] [data-run-detail="run_fixture_2"]');
  assert(Math.abs(rawTransition.outgoingContent.rawScrollTop-rawScroll)<2,'Outgoing raw clone retains the reading position');
  assert.equal(rawTransition.keyboardUsesCurrentPanel,true);
  assert.deepEqual({inert:rawTransition.outgoingContent.inert,hidden:rawTransition.outgoingContent.hidden,ids:rawTransition.outgoingContent.ids},{inert:true,hidden:'true',ids:0});
  await page.locator('.run-drawer-tabs [data-run-view="checks"]').click();
  // A missing tool and a non-tool assertion still lead to the saved model output.
  for(const index of [1,2]){
    await page.locator(`[data-run-check-output="${index}"]`).click();
    await page.locator('.run-drawer-tabs [data-run-view="messages"][aria-selected="true"]').waitFor();
    await until(()=>page.locator('.bf-key-message').evaluate(row=>{
      const viewport=row.closest('.bf-message-scroll').getBoundingClientRect(),rect=row.getBoundingClientRect();
      return rect.top>=viewport.top&&rect.bottom<=viewport.bottom;
    }));
    await page.locator('.run-drawer-tabs [data-run-view="checks"]').click();
  }
  await selectReplay(page,'run_fixture_1');
  assert.equal(await page.locator('.run-drawer-tabs [data-run-view="raw"]').getAttribute('aria-selected'),'true','Each replay keeps its selected detail tab');
  await until(async()=>Math.abs(await raw.evaluate(node=>node.scrollTop)-rawScroll)<2);
  await page.locator('.run-drawer-tabs [data-run-view="messages"]').click();
  await page.locator('.bf-message-scroll').waitFor();
  const scroll=page.locator('.bf-message-scroll');
  await scroll.dispatchEvent('pointerdown');await scroll.evaluate(node=>node.scrollTop=180);
  const messageScroll=await scroll.evaluate(node=>node.scrollTop);assert(messageScroll>0);
  await selectReplay(page,'run_fixture_2');
  await selectReplay(page,'run_fixture_1');
  assert.equal(await page.locator('.run-drawer-tabs [data-run-view="messages"]').getAttribute('aria-selected'),'true');
  await until(async()=>Math.abs(await scroll.evaluate(node=>node.scrollTop)-messageScroll)<2);
  await page.keyboard.press('Escape');
  await page.locator('#run-drawer').waitFor({state:'hidden'});
  await until(()=>page.locator('.run-batch-open').evaluate(button=>button===document.activeElement));

  // Every replay has its own physically attached bookmark, including a twelve-page stack.
  for(let index=7;index<=12;index++)fixture.runs.push({...fixture.runs[5],id:`run_fixture_${index}`,repeat:index-3});
  fixture.revision++;
  await until(async()=>await page.locator('.history-batch').getAttribute('data-run-count')==='12');
  await page.locator('.run-batch-open').first().click();await settleDrawer(page);
  await assertPhysicalBookmarks(page,12);
  const exposedIds=await page.locator(liveBookmarks).evaluateAll(items=>items.map(item=>item.dataset.runDetail));
  assert.deepEqual(new Set(exposedIds),new Set(fixture.runs.map(run=>run.id)),'All twelve runs have attached visible bookmarks without a picker');
  for(const runId of exposedIds){
    await selectReplay(page,runId);
    assert.equal(await page.locator('#run-drawer-panel').getAttribute('data-run-id'),runId,'Every batch run is selectable through its own attached bookmark');
    await assertPhysicalBookmarks(page,12);
  }
  await page.keyboard.press('Escape');await page.locator('#run-drawer').waitFor({state:'hidden'});

  // Reduced-motion users get the same navigation immediately, without animation.
  await page.emulateMedia({reducedMotion:'reduce'});
  const reducedOpening=await sampleMotion(page,'.run-batch-open');
  assert.equal(reducedOpening.states.length,0);
  const reducedSwitch=await sampleMotion(page,edgeSelector);
  assert.equal(reducedSwitch.states.length,0);
  await page.keyboard.press('Escape');await page.locator('#run-drawer').waitFor({state:'hidden'});
  for(const width of [631,390,320]){
    await page.setViewportSize({width,height:844});
    await page.locator('.run-batch-open').first().click();
    const mobile=await page.locator('#run-drawer-panel').boundingBox();
    assert(mobile.x>=-1&&mobile.x+mobile.width<=width+1,'Mobile drawer fits viewport');
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile page has no horizontal overflow');
    await assertPhysicalBookmarks(page,12);
    await selectReplay(page,'run_fixture_12');
    await assertPhysicalBookmarks(page,12);
    assert.equal(await page.locator('#run-drawer-panel').getAttribute('data-run-id'),'run_fixture_12');
    await page.keyboard.press('Escape');await page.locator('#run-drawer').waitFor({state:'hidden'});
  }
  if(process.env.ABLATION_SCREENSHOTS){
    fs.mkdirSync(process.env.ABLATION_SCREENSHOTS,{recursive:true});
    await page.setViewportSize({width:1440,height:1000});await page.locator('.run-batch-open').first().click();
    await page.screenshot({path:path.join(process.env.ABLATION_SCREENSHOTS,'replay-failure-details.png'),fullPage:true});
    await page.locator('.run-drawer-tabs [data-run-view="messages"]').click();
    await page.locator('.bf-message-scroll').waitFor();
    await page.screenshot({path:path.join(process.env.ABLATION_SCREENSHOTS,'stacked-replay-drawers.png'),fullPage:true});
  }
  assert.deepEqual(mutations,[]);assert.deepEqual(externalRequests,[]);assert.deepEqual(errors,[]);
  console.log(JSON.stringify({latestPolling:true,historicalInspection:true,draftsPreserved:true,failureReasons:true,physicalPageBookmarks:true,normalMotion:true,transitionCloneSafe:true,stableContentCoverage:true,rapidSwitchClose:true,reducedMotion:true,allTwelveBookmarks:true,fullWidthContent:true,compact631:true,readingPosition:true,keyboardClose:true,mobile:true,pageErrors:errors}));
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  for(const socket of sockets)socket.destroy();
  if(server)await new Promise(resolve=>server.close(resolve));
});
