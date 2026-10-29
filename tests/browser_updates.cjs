// Real SSE, disconnects and service restarts; all version data is synthetic.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');
const root=path.resolve(__dirname,'..');
const workspace=fs.mkdtempSync(path.join(os.tmpdir(),'ablation-updates-'));
const python=process.env.ABLATION_PYTHON || 'python3';
const children=[];
let browser;
const harness=`
import json, sys, threading, time
from pathlib import Path
from ablation_workbench import __version__, updates, server
from ablation_workbench.engine import Workbench
workspace, port, version = sys.argv[1:]
version = version or __version__
updates.__version__ = server.__version__ = version
updates._editable = lambda: False
service = server.make_server(Workbench(workspace), int(port))
thread = threading.Thread(target=service.serve_forever, daemon=True)
thread.start()
print(json.dumps({'port':service.server_port, 'version':version}), flush=True)
try:
    for line in sys.stdin:
        command = json.loads(line)
        if command['type'] == 'stop':
            break
        if command['type'] == 'release':
            server.save_json(Path(workspace) / '.update-cache.json', {'latest':command['version'], 'status':'checked', 'checked_at':time.time()})
            service.update_events.publish()
finally:
    server.close_jobs(service)
    service.shutdown()
    service.server_close()
    thread.join()
`;
async function until(fn,timeout=10000){
  const start=Date.now();
  while(Date.now()-start<timeout){if(await fn())return;await new Promise(r=>setTimeout(r,50));}
  throw Error('Timed out waiting for update evidence');
}
async function start(port=0,version=''){
  const env={...process.env,ABLATION_UPDATE_CHECK:'1'};
  if(process.env.ABLATION_USE_INSTALLED)delete env.PYTHONPATH;else env.PYTHONPATH=path.join(root,'src');
  const child=spawn(python,['-u','-c',harness,workspace,String(port),version],{cwd:workspace,env,stdio:['pipe','pipe','pipe']});
  children.push(child);
  let output='',errors='';
  child.stdout.on('data',data=>output+=data);child.stderr.on('data',data=>errors+=data);
  await until(()=>{if(child.exitCode!==null)throw Error(errors);return output.includes('\n');});
  return {child,...JSON.parse(output.trim())};
}
function send(service,command){service.child.stdin.write(JSON.stringify(command)+'\n');}
async function stop(service){send(service,{type:'stop'});await until(()=>service.child.exitCode!==null);assert.equal(service.child.exitCode,0);}
async function main(){
  let service=await start();
  const initial=service.version,parts=initial.split('.').map(Number);parts[2]+=1;
  const next=parts.join('.'),url=`http://127.0.0.1:${service.port}/ablation/`;
  browser=await chromium.launch({headless:true});
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  let updates=0,jobs=0,connections=0,versions=0;
  page.on('request',request=>{const p=new URL(request.url()).pathname;if(p==='/ablation/api/update')updates++;if(p==='/ablation/api/jobs')jobs++;});
  page.on('response',response=>{const p=new URL(response.url()).pathname;if(p==='/ablation/api/events'&&response.ok())connections++;if(p==='/ablation/api/version'&&response.ok())versions++;});
  await page.clock.install();
  await page.goto(url);
  await page.locator('#empty-state').waitFor({state:'visible'});
  await until(()=>connections===1&&versions>=2);
  assert.equal(await page.locator('#update-indicator').isVisible(),false);
  send(service,{type:'release',version:next});
  await page.locator('#update-indicator').getByText(`更新 ${next}`,{exact:true}).waitFor();
  const checked=updates,polled=jobs;
  await page.clock.fastForward(61000);
  await until(()=>jobs>polled);
  assert.equal(updates,checked,'Version metadata must not be polled on a timer');
  await page.getByRole('button',{name:'打开合成示例',exact:true}).click();
  await page.locator('#experiment-title').waitFor();
  await page.locator('[data-plus]').first().click();
  await page.locator('#inline-comment').fill('尚未提交的升级测试评论');
  const experiment=new URL(page.url()).searchParams.get('experiment');
  let connected=connections,versionReads=versions;
  await stop(service);service=await start(service.port,initial);
  await until(()=>connections>connected&&versions>versionReads);
  assert.equal(await page.locator('#upgrade-banner').isVisible(),false,'A same-version reconnect must not announce an upgrade');
  assert.equal(await page.locator('#inline-comment').inputValue(),'尚未提交的升级测试评论');
  connected=connections;
  await stop(service);service=await start(service.port,next);
  await until(()=>connections>connected);
  await page.locator('#upgrade-banner').waitFor({state:'visible'});
  assert.equal(await page.locator('#upgrade-version').innerText(),next);
  assert.equal(await page.locator('#inline-comment').inputValue(),'尚未提交的升级测试评论');
  assert.equal(new URL(page.url()).searchParams.get('experiment'),experiment);
  await page.locator('[data-action="reload-ui"]').click();
  await page.locator('#experiment-title').waitFor();
  await until(()=>new URL(page.url()).searchParams.get('experiment')===experiment);
  assert.equal(await page.locator('#inline-comment').inputValue(),'尚未提交的升级测试评论');
  assert.equal(await page.locator('#upgrade-banner').isVisible(),false);
  assert.deepEqual(errors,[]);
  await stop(service);
  console.log(JSON.stringify({sseNotification:true,noMetadataPolling:true,sameVersionReconnect:true,upgradeBanner:true,draftPreserved:true,pageErrors:errors}));
}
main().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  for(const child of children)if(child.exitCode===null)child.kill('SIGTERM');
  fs.rmSync(workspace,{recursive:true,force:true});
});
