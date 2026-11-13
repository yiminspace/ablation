// Real local HTTP transport with a deterministic translation provider.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const http=require('node:http');
const {spawn}=require('node:child_process');
const root=path.resolve(__dirname,'..'),workspace=fs.mkdtempSync(path.join(os.tmpdir(),'ablation-translation-'));
const packets=[];
let browser,child,provider,fail=false,delay=0;
async function until(fn){for(let index=0;index<150;index++){const value=await fn();if(value)return value;await new Promise(resolve=>setTimeout(resolve,100));}throw Error('Timed out waiting for translation');}
(async()=>{
  provider=http.createServer((request,response)=>{
    let raw='';request.on('data',chunk=>raw+=chunk);request.on('end',()=>{
      const packet=JSON.parse(raw);packets.push(packet);
      if(fail){response.writeHead(502);response.end('{}');return;}
      const rows=JSON.parse(packet.messages[1].content).rows;
      const text=JSON.stringify({rows:rows.map(row=>({id:row.id,before:row.before?'旧版规则':'',after:row.after?'新版规则<img src=x onerror="window.injected=true">':''}))});
      setTimeout(()=>{response.writeHead(200,{'Content-Type':'application/json'});response.end(JSON.stringify({choices:[{message:{content:text},finish_reason:'stop'}]}));},delay);
    });
  });
  await new Promise(resolve=>provider.listen(0,'127.0.0.1',resolve));
  fs.writeFileSync(path.join(workspace,'config.json'),JSON.stringify({llm:{base_url:`http://127.0.0.1:${provider.address().port}/v1`,model:'translation-model',auth:'none'}}));
  child=spawn(process.env.ABLATION_PYTHON||'python3',['-m','ablation_workbench','--workspace',workspace,'gui','--no-open','--port','0'],{cwd:root,env:{...process.env,PYTHONPATH:path.join(root,'src'),ABLATION_UPDATE_CHECK:'0'},stdio:['ignore','pipe','pipe']});
  let output='',errors='';child.stdout.on('data',chunk=>output+=chunk);child.stderr.on('data',chunk=>errors+=chunk);
  const url=await until(()=>{if(child.exitCode!==null)throw Error(errors);try{return JSON.parse(output.trim()).url;}catch{return null;}});
  const api=async(route,body)=>{
    const response=await fetch(new URL('/ablation'+route,url),{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Ablation-Client':'local'},body:body===undefined?undefined:JSON.stringify(body)});
    const value=await response.json();assert(response.ok,JSON.stringify(value));return value;
  };
  let experiment=await api('/api/import',{source:{kind:'request',data:{model:'original-model',instructions:'unchanged sentinel\nold\nkeep one\nkeep two\nremoved\nfinal anchor',input:'private input sentinel'}}});
  const eid=experiment.id;
  experiment=await api(`/api/experiments/${eid}/candidates`,{label:'Changed',rationale:'Change',patch:[{op:'replace',path:'/instructions',value:'unchanged sentinel\nnew\nkeep one\nkeep two\nfinal anchor\nadded'}]});
  browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),pageErrors=[];
  page.on('pageerror',error=>pageErrors.push(error.message));
  await page.goto(url+'?experiment='+eid);
  const button=page.getByRole('button',{name:'翻译',exact:true});
  await until(async()=>await button.count()&&!(await button.isDisabled()));
  assert.equal(await page.locator('#assistant-panel').count(),0);
  assert.equal(await page.locator('.translation-column-title').count(),0);
  const before=await page.locator('.diff-cell').allTextContents();
  assert.equal(packets.length,0);
  delay=400;await button.click();
  await page.locator('.translation-column-title').waitFor();
  await page.locator('.translation-after').first().waitFor();
  assert.equal((await page.locator('.diff-headrow>span').count()),3);
  assert.deepEqual(await page.locator('.diff-cell').allTextContents(),before,'Original columns stay visible and unchanged');
  assert.equal(await page.locator('.diff-row').first().locator(':scope>div').count(),3);
  assert.equal(await page.locator('.diff-translation img').count(),0);
  assert.equal(await page.evaluate(()=>window.injected),undefined);
  assert.equal(packets[0].model,'translation-model');
  for(const packet of packets){assert(!packet.messages[1].content.includes('unchanged sentinel'));assert(!packet.messages[1].content.includes('private input sentinel'));}
  await page.reload();await page.locator('.translation-after').first().waitFor();
  assert.equal(packets.length,1,'Refresh reuses completed segments');
  await button.click();assert.equal(packets.length,1);
  async function assertThreeColumns() {
    const headers=await page.locator('.diff-headrow>span').evaluateAll(cells=>cells.map(cell=>cell.getBoundingClientRect().left));
    const rows=await page.locator('.diff-row').evaluateAll(rows=>rows.map(row=>Array.from(row.children,cell=>({display:getComputedStyle(cell).display,left:cell.getBoundingClientRect().left}))));
    assert(rows.every(cells=>cells.length===3&&cells.every((cell,index)=>cell.display!=='none'&&Math.abs(cell.left-headers[index])<1)),'Every source and translation cell occupies its header column');
  }
  await assertThreeColumns();
  await page.evaluate(()=>document.querySelector('#diff-file').classList.add('unified'));
  await assertThreeColumns();
  await page.evaluate(()=>document.querySelector('#diff-file').classList.remove('unified'));
  // Row and translation remain horizontally aligned on a narrow viewport.
  await page.setViewportSize({width:390,height:844});
  await assertThreeColumns();
  await page.evaluate(()=>document.querySelector('#diff-file').classList.add('unified'));
  await assertThreeColumns();
  await page.evaluate(()=>document.querySelector('#diff-file').classList.remove('unified'));
  const grid=await page.locator('.diff-row').filter({has:page.locator('.translation-after')}).first().evaluate(row=>Array.from(row.children,cell=>({top:cell.getBoundingClientRect().top,height:cell.getBoundingClientRect().height})));
  assert(grid.every(cell=>Math.abs(cell.top-grid[0].top)<1&&Math.abs(cell.height-grid[0].height)<1));
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  if(process.env.ABLATION_TRANSLATION_SCREENSHOT)await page.screenshot({path:process.env.ABLATION_TRANSLATION_SCREENSHOT,fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  experiment=await api(`/api/experiments/${eid}/candidates`,{label:'Next',rationale:'Next',patch:[{op:'replace',path:'/instructions',value:'unchanged sentinel\nnext'}]});
  const candidate=experiment.candidates.at(-1).id;
  await page.goto(url+`?experiment=${eid}&candidate=${candidate}`);
  await until(async()=>await button.count()&&!(await button.isDisabled()));
  assert.equal(await page.locator('.translation-column-title').count(),0);
  fail=true;await button.click();await page.locator('.translation-error').waitFor();
  assert((await page.locator('.translation-error').innerText()).includes('502'));
  fail=false;delay=2000;await button.click();await until(()=>packets.length===3);
  await page.reload();await page.locator('.translation-after').first().waitFor();
  assert.equal(packets.length,3,'Refreshing during translation does not resubmit inference');
  assert.equal((await api(`/api/experiments/${eid}`)).runs.length,0);
  assert.deepEqual(pageErrors,[]);
  console.log('Translation browser checks passed (one button/third column/source preservation/diff-only payload/escaping/cache/refresh/version/error/mobile).');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  await browser?.close();child?.kill('SIGTERM');provider?.closeAllConnections();await new Promise(resolve=>provider?provider.close(resolve):resolve());fs.rmSync(workspace,{recursive:true,force:true});
});
