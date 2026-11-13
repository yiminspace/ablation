const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');
const source=fs.readFileSync(path.join(__dirname,'../src/ablation_workbench/web/translation.js'),'utf8');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const fixture=(id='one')=>({id,rows:[{id:'row_1',index:1,before:'old',after:'new'}],translations:[],busy:false});
function harness(api, stream){
  let click;const timers=new Map();let next=0;
  const context={window:{},URLSearchParams,document:{addEventListener:(type,callback)=>{if(type==='click')click=callback;}},setTimeout:fn=>{timers.set(++next,fn);return next;},clearTimeout:id=>timers.delete(id)};
  vm.runInNewContext(source,context);
  const translation=context.window.AblationTranslation({api,onChange(){},stream:stream || (async(path,body,onEvent)=>onEvent({type:'complete',session:await api(path,body)}))});
  const selection=candidate=>({endpoint:'/translation',selection:{candidate,artifact:'changes'},diff:{rows:[{kind:'replace',before:candidate,after:'new'}]}});
  return {translation,selection,click:()=>click({target:{closest:()=>({disabled:false})}}),poll:()=>{const callbacks=[...timers.values()];timers.clear();callbacks.forEach(fn=>fn());}};
}

test('One click reveals a third column with escaped old/new translations',async()=>{
  const session=fixture(),calls=[],pending=deferred();
  const ui=harness(async(url,body)=>{calls.push(body);return body?pending.promise:structuredClone(session);});
  ui.translation.sync(ui.selection('one'));await flush();
  assert.equal(ui.translation.columnVisible(),false);
  assert.equal(ui.translation.cellMarkup(1,false),'');
  assert.equal(calls.length,1,'Loading the diff only reads saved translations');
  assert.equal((ui.translation.toolbar().match(/<button/g)||[]).length,1);
  assert(ui.translation.toolbar().includes('>翻译</button>'));
  ui.click();
  assert.equal(ui.translation.columnVisible(),true,'Column appears immediately');
  assert.equal(calls.length,2);
  assert.equal(Object.keys(calls[1]).sort().join(','),'artifact,candidate,session_id');
  session.translations=[{id:'row_1',before:'旧版',after:'新版<img onerror=bad()>'}];
  pending.resolve(session);await flush();
  const markup=ui.translation.cellMarkup(1,false);
  assert(markup.startsWith('<div class="diff-translation">'));
  assert(markup.includes('translation-before')&&markup.includes('translation-after'));
  assert(markup.includes('&lt;img'));
  assert(!markup.includes('<img'));
  assert.equal(ui.translation.cellMarkup(0,true),'<div class="diff-translation"></div>');
  ui.click();await flush();assert.equal(calls.length,2,'Completed translations are reused');
});

test('Refresh restores the column, and switching versions clears it',async()=>{
  const old=fixture();old.translations=[{id:'row_1',before:'旧',after:'新'}];
  const ui=harness(async url=>structuredClone(url.includes('candidate=two')?fixture('two'):old));
  ui.translation.sync(ui.selection('one'));await flush();
  assert.equal(ui.translation.columnVisible(),true);
  ui.translation.sync(ui.selection('two'));await flush();
  assert.equal(ui.translation.columnVisible(),false);
  assert.equal(ui.translation.cellMarkup(1,false),'');
});

test('Late old-version inference cannot populate the newly selected diff',async()=>{
  const pending=deferred();
  const ui=harness(async(url,body)=>body?pending.promise:fixture(url.includes('candidate=two')?'two':'one'));
  ui.translation.sync(ui.selection('one'));await flush();ui.click();
  ui.translation.sync(ui.selection('two'));await flush();
  const old=fixture();old.translations=[{id:'row_1',before:'旧',after:'新'}];
  pending.resolve(old);await flush();
  assert.equal(ui.translation.columnVisible(),false);
  assert(!ui.translation.toolbar().includes('disabled'));
});

test('A refreshed page polls an in-flight translation and restores completed rows',async()=>{
  const session=fixture();session.busy=true;
  const ui=harness(async()=>structuredClone(session));
  ui.translation.sync(ui.selection('one'));await flush();
  assert.equal(ui.translation.columnVisible(),true);
  assert(ui.translation.toolbar().includes('翻译中'));
  session.busy=false;session.translations=[{id:'row_1',before:'旧',after:'新'}];
  ui.poll();await flush();
  assert(ui.translation.cellMarkup(1,false).includes('旧'));
  assert(!ui.translation.toolbar().includes('disabled'));
});

test('An inference error stays visible when late progress arrives and the same button retries',async()=>{
  const session=fixture(),inference=deferred(),progress=deferred();let reads=0;
  const ui=harness(async(url,body)=>body?inference.promise:++reads===1?structuredClone(session):progress.promise);
  ui.translation.sync(ui.selection('one'));await flush();ui.click();ui.poll();
  inference.reject(new Error('HTTP 502'));await flush();
  progress.resolve(session);await flush();
  assert(ui.translation.toolbar().includes('HTTP 502'));
  assert(!ui.translation.toolbar().includes('disabled'));
  assert.equal(ui.translation.columnVisible(),true);
});


test('Streamed drafts appear before a segment is complete and task progress stays in the same button',async()=>{
  const session=fixture(),finished=deferred();let receive;
  const ui=harness(async()=>structuredClone(session),async(path,body,onEvent)=>{receive=onEvent;await finished.promise;});
  ui.translation.sync(ui.selection('one'));await flush();ui.click();
  receive({type:'start',session:{...session,busy:true,tasks:[{id:'part1',status:'running'},{id:'part2',status:'running'}]}});
  receive({type:'partial',task_id:'part1',rows:[{id:'row_1',before:'逐字',after:''}]});
  assert(ui.translation.cellMarkup(1,false).includes('逐字'));
  assert(ui.translation.toolbar().includes('0/2 段'));
  receive({type:'rows',task_id:'part1',rows:[{id:'row_1',before:'完整旧文',after:'完整新文'}]});
  receive({type:'task',task_id:'part1',status:'completed'});
  assert(ui.translation.toolbar().includes('1/2 段'));
  assert(ui.translation.cellMarkup(1,false).replace(/<[^>]+>/g,'').includes('完整新文'));
  receive({type:'complete',session:{...session,translations:[{id:'row_1',before:'完整旧文',after:'完整新文'}]}});
  finished.resolve();await flush();
  assert(!ui.translation.toolbar().includes('disabled'));
});

test('SSE reader handles fragmented UTF-8 and reports an interrupted stream',async()=>{
  const frame=event=>'data: '+JSON.stringify(event)+'\r\n\r\n';
  const bytes=new TextEncoder().encode(frame({type:'partial',rows:[{id:'row_1',before:'中文'}]})+frame({type:'complete',session:{}}));
  let offset=0;
  const context={window:{},document:{addEventListener(){}},TextDecoder,URLSearchParams,fetch:async()=>({ok:true,headers:{get:()=>'text/event-stream'},body:{getReader:()=>({read:async()=>offset<bytes.length?{value:bytes.slice(offset,++offset),done:false}:{done:true},cancel:async()=>{},releaseLock(){}})}})};
  vm.runInNewContext(source,context);
  const events=[];
  await context.window.readTranslationStream('/translation/stream',{},event=>events.push(event));
  assert.equal(events[0].rows[0].before,'中文');
  assert.equal(events[1].type,'complete');
  offset=bytes.length;
  await assert.rejects(context.window.readTranslationStream('/translation/stream',{},()=>{}),/连接中断/);
});


test('A dropped connection follows the still-running job without submitting inference again',async()=>{
  const initial=fixture(),active=fixture();let reads=0,submissions=0;
  active.busy=true;active.drafts=[{id:'row_1',before:'后台继续',after:''}];
  const ui=harness(async()=>structuredClone(++reads===1?initial:active),async()=>{submissions++;throw new Error('Network disconnected');});
  ui.translation.sync(ui.selection('one'));await flush();ui.click();await flush();
  assert.equal(submissions,1);
  assert(ui.translation.cellMarkup(1,false).includes('后台继续'));
  assert(ui.translation.toolbar().includes('disabled'));
  active.busy=false;active.drafts=[];active.translations=[{id:'row_1',before:'完成',after:'新文'}];
  ui.poll();await flush();
  assert.equal(submissions,1);
  assert(ui.translation.cellMarkup(1,false).includes('完成'));
  assert(!ui.translation.toolbar().includes('Network disconnected'));
});

test('An older running API falls back only when the streaming route is absent',async()=>{
  const calls=[],events=[];
  const context={window:{},document:{addEventListener(){}},TextDecoder,URLSearchParams,fetch:async(path,options)=>{
    calls.push({path,options});
    return calls.length===1?{ok:false,status:404,json:async()=>({error:{code:'not_found',message:'Unknown experiment route'}})}:{ok:true,json:async()=>({id:'one',rows:[],translations:[]})};
  }};
  vm.runInNewContext(source,context);
  await context.window.readTranslationStream('/translation/stream',{session_id:'one'},event=>events.push(event));
  assert.equal(calls[0].path,'/ablation/translation/stream');
  assert.equal(calls[1].path,'/ablation/translation');
  assert.equal(calls[0].options.body,calls[1].options.body);
  assert.equal(events[0].type,'complete');
  calls.length=0;
  context.fetch=async()=>{calls.push(true);return {ok:false,status:404,json:async()=>({error:{code:'not_found',message:'Experiment missing'}})};};
  await assert.rejects(context.window.readTranslationStream('/translation/stream',{},()=>{}),/Experiment missing/);
  assert.equal(calls.length,1);
});

test('Chinese translation highlights changed words while preserving shared sentences',async()=>{
  const context={window:{},document:{addEventListener(){}},URLSearchParams};
  vm.runInNewContext(source,context);
  const before='在选择技能前，先确认用户的目标。';
  const after='在选择技能前，先确认用户的目标和交付位置。';
  const [oldHtml,newHtml]=context.window.AblationInlineChanges(before,after,true);
  assert.equal(oldHtml.replace(/<[^>]+>/g,''),before);
  assert.equal(newHtml.replace(/<[^>]+>/g,''),after);
  assert(newHtml.includes('<mark class="inline-change">'));
  assert(!newHtml.includes('<mark class="inline-change">在选择'));
  assert(newHtml.includes('交付位置'));
  const session=fixture();session.translations=[{id:'row_1',before,after}];
  const ui=harness(async()=>structuredClone(session));
  ui.translation.sync(ui.selection('one'));await flush();
  assert(ui.translation.cellMarkup(1,false).includes('<mark class="inline-change">'));
  const [removed,added]=context.window.AblationInlineChanges('old rule','new rule');
  assert.equal(removed,'<mark class="inline-change">old</mark> rule');
  assert.equal(added,'<mark class="inline-change">new</mark> rule');
  const [,unsafe]=context.window.AblationInlineChanges('原文','<img src=x onerror=bad()>',true);
  assert(!unsafe.includes('<img'));
});
