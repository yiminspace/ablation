// Offline permalink regression; no model or production data requests.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const http=require('node:http');
const path=require('node:path');
const web=path.resolve(__dirname,'../src/ablation_workbench/web');
const fixture={id:'exp_lines',title:'Line links',revision:1,baseline:{hash:'fixture'},source:{kind:'demo'},reviews:[],runs:[],candidates:[{id:'baseline',label:'Baseline'},{id:'v1',label:'Version 1'}],scenarios:[{id:'original',label:'Original'},{id:'scenario_other',label:'Other'}]};
const rows=Array.from({length:650},(_,i)=>({old_line:i+1,new_line:i+1,before:`Original ${i+1}`,after:i===0?'Changed 1':`Original ${i+1}`,kind:i===0?'replace':'same'}));
let browser,server;
(async()=>{
  server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://localhost');
    const route=url.pathname.replace(/^\/ablation/,'');
    const json=value=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify(value));};
    if(route==='/api/experiments')return json({experiments:[fixture]});
    if(route==='/api/experiments/exp_lines')return json(fixture);
    if(route==='/api/experiments/exp_lines/diff')return json({rows,artifact:url.searchParams.get('artifact'),base_label:'Baseline',candidate_label:url.searchParams.get('candidate'),notes:[],change_groups:[{id:'group',title:'First change',reason:'Fixture',effect:'Fixture',row_indices:[0],sources:[]}]});
    if(route==='/api/config')return json({});
    if(route==='/api/jobs')return json({jobs:[]});
    if(route==='/api/update')return json({current:'0.0.0',status:'up_to_date'});
    if(route==='/api/version')return json({version:'0.0.0'});
    const target=path.resolve(web,route==='/'?'index.html':route.slice(1));
    if(!target.startsWith(web+path.sep)||!fs.existsSync(target)){res.writeHead(404);return res.end();}
    res.setHeader('Content-Type',({'.js':'text/javascript','.css':'text/css','.html':'text/html'})[path.extname(target)]||'text/plain');res.end(fs.readFileSync(target));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base=`http://127.0.0.1:${server.address().port}/ablation/?experiment=exp_lines`;
  browser=await chromium.launch({headless:true});
  const context=await browser.newContext({permissions:['clipboard-read','clipboard-write']});
  const page=await context.newPage(),errors=[];page.setDefaultTimeout(10000);page.on('pageerror',error=>errors.push(error.message));
  const line=(side,n)=>page.locator(`[data-line-side="${side}"][data-line="${n}"]`);
  await page.goto(base);await line('R',1).click();
  assert.equal(new URL(page.url()).hash,'#diff-changes-R1');
  assert.equal(new URL(page.url()).searchParams.get('candidate'),'v1');
  await line('R',4).click({modifiers:['Shift']});
  assert.equal(new URL(page.url()).hash,'#diff-changes-R1-R4');
  assert.equal(await page.locator('.line-selected').count(),4);
  await page.getByRole('button',{name:'复制行链接',exact:true}).click();
  const copied=await page.evaluate(()=>navigator.clipboard.readText());assert.equal(copied,page.url());
  // A copied link pins a candidate even when a new candidate arrives.
  fixture.candidates.push({id:'v2',label:'Version 2'});fixture.revision++;
  await page.goto('about:blank');await page.goto(copied);await page.locator('.line-selected').first().waitFor();
  assert.equal(await page.locator('.line-selected').count(),4);
  assert((await page.locator('.diff-headrow').innerText()).includes('v1'));
  await line('L',4).click();await line('L',2).click({modifiers:['Shift']});
  assert.equal(new URL(page.url()).hash,'#diff-changes-L2-L4');
  assert.equal(await page.locator('.line-selected').count(),3);
  await page.goBack();await page.waitForFunction(()=>document.querySelectorAll('.line-selected').length===1);
  await page.goForward();await page.waitForFunction(()=>document.querySelectorAll('.line-selected').length===3);
  // Restore a different artifact/scenario and a range beyond collapsed context/500-row cap.
  await page.goto(base+'&candidate=v1&scenario=scenario_other#diff-request-R600-R602');
  await line('R',600).waitFor();
  assert.equal(await page.locator('.line-selected').count(),3);
  assert.equal(await page.locator('.artifact-title').innerText(),'完整请求');
  assert.equal(new URL(page.url()).searchParams.get('scenario'),'scenario_other');
  const box=await line('R',600).boundingBox();assert(box.y>=0&&box.y<720,'Restored range is scrolled into view');
  await page.locator('.artifact-nav [data-artifact="response"]').click();
  await page.waitForFunction(()=>!location.hash&&document.querySelector('.artifact-title')?.textContent==='输出结果');
  assert.equal(await page.locator('.line-selected').count(),0);
  // Unknown/malformed fragments remain harmless.
  await page.goto(base+'#diff-changes-R0');await line('R',1).waitFor();
  assert.equal(await page.locator('.line-selected').count(),0);
  assert.deepEqual(errors,[]);
  console.log('Line permalink browser checks passed (single/range/side/copy/history/version/artifact/scenario/scroll).');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{await browser?.close();server?.closeAllConnections();await new Promise(resolve=>server?server.close(resolve):resolve());});
