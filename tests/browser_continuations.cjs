// Synthetic UI fixture only; no inference or business actions.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs'),http=require('node:http'),path=require('node:path');
const web=path.resolve(__dirname,'../src/ablation_workbench/web');
const fixture={id:'exp_continuation_browser',title:'Synthetic continuation record',revision:1,created_at:'2026-01-01T00:00:00Z',source:{kind:'demo',fidelity:'synthetic'},baseline:{hash:'fixture'},reviews:[],candidates:[{id:'baseline',label:'Baseline'}],scenarios:[{id:'original',label:'Final reply',origin:'synthetic'}],runs:[]};
const parent={id:'round_one',batch_id:'batch_one',candidate:'baseline',scenario:'original',repeat:1,status:'completed',passed:false,elapsed_ms:100,created_at:'2026-01-01T00:00:01Z',request:{model:'fixture',instructions:'Production candidate only.',input:[{role:'user',content:'Inspect'}]},response:{output:[{type:'function_call',call_id:'call_fixture',name:'load_skill',arguments:'{}'}]},checks:[{kind:'text_contains',value:'complete',actual:'',passed:false}]};
const child={...parent,id:'round_two',batch_id:'batch_two',passed:true,parent_run_id:'round_one',root_run_id:'round_one',request:{...parent.request,instructions:'Runtime-only context. <do not render markup>'},response:{output:[{role:'assistant',type:'message',content:[{type:'output_text',text:'complete'}]}]},checks:[{kind:'text_contains',value:'complete',actual:'complete',passed:true}],continuation:{rationale:'The actual tool call needs a runtime return before the final assertion.',tool_results:[{call_id:'call_fixture',output:'Loaded synthetic fixture'}],context_patch:[{op:'replace',path:'/instructions',value:'Runtime-only context. <do not render markup>'}]}};
fixture.runs=[parent,child,{...parent,id:'standalone',batch_id:'batch_standalone',response:child.response,passed:true,checks:child.checks}];
let server,browser;const sockets=new Set(),errors=[];
(async()=>{try{
 server=http.createServer((req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1');const route=url.pathname.replace(/^\/ablation(?=\/|$)/,'')||'/';
  if(req.method!=='GET'){res.writeHead(405);return res.end();}
  if(route==='/api/events'){res.writeHead(200,{'Content-Type':'text/event-stream'});return res.write('retry: 1000\n\n');}
  const send=data=>{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(data));};
  if(route==='/api/experiments')return send({experiments:[fixture]});
  if(route===`/api/experiments/${fixture.id}`)return send(fixture);
  if(route.endsWith('/diff'))return send({artifact:url.searchParams.get('artifact')||'changes',base_label:'Baseline',candidate_label:'Baseline',before:'Production candidate only.',after:'Production candidate only.',rows:[{old_line:1,new_line:1,before:'Production candidate only.',after:'Production candidate only.',kind:'equal'}],notes:[],rationale:'Fixed production diff'});
  if(route==='/api/jobs')return send({jobs:[]});if(route==='/api/config')return send({profiles:{},agent:{}});
  if(route==='/api/version')return send({version:'fixture'});if(route==='/api/update')return send({status:'up_to_date',available:false});
  const file=path.resolve(web,route==='/'?'index.html':route.slice(1));
  if(!file.startsWith(web+path.sep)||!fs.existsSync(file)){res.writeHead(404);return res.end();}
  res.writeHead(200,{'Content-Type':{'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}[path.extname(file)]||'text/plain'});res.end(fs.readFileSync(file));
 });
 server.on('connection',s=>{sockets.add(s);s.on('close',()=>sockets.delete(s));});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));browser=await chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000},reducedMotion:'reduce'});page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${server.address().port}/ablation/?experiment=${fixture.id}`);
 await page.locator('.diff-row').first().waitFor();
 assert(!(await page.locator('#diff-file').innerText()).includes('Runtime-only'));
 await page.locator('.artifact-tab[data-artifact="response"]').click();
 await page.waitForFunction(()=>document.querySelector('#comparison-status').textContent.includes('末轮通过'));
 assert((await page.locator('#comparison-status').innerText()).includes('末轮通过'));
 await page.locator('.page-tab[data-tab="runs"]').click();
 await page.locator('.run-batch-open[data-run-detail="round_one"]').click();
 await page.locator('[data-run-view="continuation"]').click();
 assert.equal(await page.locator('#run-continuation-count').innerText(),'1');
 await page.locator('.continuation-step[data-run-detail="round_two"]').click();
 await page.waitForFunction(()=>document.querySelector('#run-drawer-panel').dataset.runId==='round_two');
 assert.equal(await page.locator('#run-continuation-count').innerText(),'1');
 assert.match(await page.locator('.run-drawer-tabs [data-run-view="continuation"]').getAttribute('aria-label'),/1 次续接/);
 assert((await page.locator('#run-drawer-continuation').innerText()).includes('当前第 2 轮'));
 assert((await page.locator('#run-drawer-continuation').innerText()).includes('不属于候选 prompt diff'));
 await page.locator('#run-drawer-continuation details').first().locator('summary').click();
 assert((await page.locator('#run-drawer-continuation').innerText()).includes('Loaded synthetic fixture'));
 await page.locator('.continuation-step[data-run-detail="round_one"]').click();
 await page.waitForFunction(()=>document.querySelector('#run-drawer-panel').dataset.runId==='round_one');
 await page.locator('.run-drawer-tabs [data-run-view="checks"]').click();
 assert((await page.locator('#run-drawer-checks').innerText()).includes('未通过'));
 assert((await page.locator('#run-drawer-checks').innerText()).includes('先检查实际输出'));
 await page.locator('[data-run-view="continuation"]').click();
 await page.setViewportSize({width:390,height:844});
 await page.waitForFunction(()=>document.querySelector('#run-drawer-continuation').getBoundingClientRect().width<=390);
 assert((await page.locator('#run-drawer-continuation').boundingBox()).width<=390);
 await page.locator('[data-action="close-run-drawer"]').click();
 await page.locator('.run-batch-open[data-run-detail="standalone"]').click();
 assert.equal(await page.locator('#run-continuation-count').isVisible(),false);
 assert.equal(await page.locator('.run-drawer-tabs [data-run-view="continuation"]').getAttribute('aria-label'),'续接记录');
 assert.deepEqual(errors,[]);console.log(JSON.stringify({continuationBadge:true,continuationNavigation:true,parentFailurePreserved:true,runtimeContextSeparated:true,mobile:true,pageErrors:errors}));
}finally{if(browser)await browser.close();if(server){for(const s of sockets)s.destroy();await new Promise(r=>server.close(r));}}})().catch(e=>{console.error(e);process.exitCode=1;});
