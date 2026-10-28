const {test} = require('node:test');
const assert = require('node:assert/strict');
const {checkSummary, runFailures, failureSummary, renderChecks} = require('../src/ablation_workbench/web/run-results.js');

test('release failure explains the actual model choice, preserving its expected/actual evidence', () => {
  const check = {kind:'tool_argument_equals',name:'cu_release_app',pointer:'/action',value:'park',actual:['handoff'],passed:false};
  const before = JSON.stringify(check), result = checkSummary(check);
  assert.equal(result.title, '收尾动作');
  assert.match(result.expected, /park.*后台保留/);
  assert.match(result.actual, /handoff.*主动切到前台/);
  assert.match(result.reason, /模型实际返回/);
  assert.equal(JSON.stringify(check), before);
});

test('missing calls and malformed/missing tool parameters have distinct explanations', () => {
  const base = {kind:'tool_argument_equals',name:'finish',pointer:'/status',value:'done',actual:[],passed:false};
  assert.match(checkSummary(base).reason, /没有调用 finish/);
  const invalid = checkSummary({...base,errors:['Tool arguments were not valid JSON or the pointer was absent']});
  assert.match(invalid.reason, /不是有效 JSON，或不包含指定路径/);
  assert.doesNotMatch(invalid.reason, /没有调用/);
  assert.match(checkSummary({...base,actual:['wrong'],errors:['bad args']}).reason, /另有工具参数/);
});

test('JSON null, false, zero and missing paths remain distinguishable', () => {
  const base = {kind:'json_equals',pointer:'/result',value:null,passed:false};
  assert.equal(checkSummary({...base,actual:false}).actual,'false');
  assert.equal(checkSummary({...base,actual:0}).actual,'0');
  assert.equal(checkSummary({...base,actual:null}).actual,'null');
  const missing = checkSummary({...base,error:'JSON Pointer was absent from the response'});
  assert.equal(missing.actual, '路径不存在');
  assert.match(missing.reason, /不存在/);
});

test('text, tool, imported wrapper and unknown checks use saved evidence', () => {
  assert.match(checkSummary({kind:'text_contains',value:'confirmed',actual:'pending',passed:false}).reason,/未包含/);
  assert.equal(checkSummary({kind:'tool_called',name:'inspect',actual:['finish'],passed:false}).actual,'finish');
  assert.equal(checkSummary({check:{kind:'json_equals',pointer:'/ok',value:true},actual:false,passed:false}).expected,'true');
  assert.equal(checkSummary({kind:'future_check',passed:false,error:{message:'Recorded validation issue'}}).reason,'Recorded validation issue');
  assert.equal(checkSummary({kind:'schema',passed:false,errors:['Invalid enum']}).reason,'Invalid enum');
});

test('execution failures, unscored runs and assertion failures stay separate', () => {
  assert.deepEqual(runFailures({status:'completed',passed:null,checks:[]}),[]);
  const failedCheck={kind:'text_contains',value:'ok',actual:'no',passed:false};
  assert.equal(runFailures({status:'completed',passed:false,checks:[failedCheck]})[0].source,'check');
  const errors=runFailures({status:'error',error:'HTTP 503',checks:[failedCheck]});
  assert.equal(errors.length,1);
  assert.equal(errors[0].source,'execution');
  assert.equal(errors[0].passed,null);
  assert.equal(failureSummary({status:'error',error:'HTTP 503'}),'HTTP 503');
  assert.match(runFailures({status:'completed',passed:false,checks:[]})[0].reason,/没有保存具体/);
});

test('failure assertions are open and first; passing assertions stay collapsed', () => {
  const html=renderChecks({status:'completed',checks:[
    {kind:'json_equals',pointer:'/ok',value:true,actual:true,passed:true},
    {kind:'tool_called',name:'inspect',actual:[],passed:false},
  ]});
  assert.ok(html.indexOf('工具调用') < html.indexOf('响应字段'));
  assert.equal((html.match(/class="run-check-result" open/g)||[]).length,1);
  assert.match(html,/data-run-check-output="1" data-check-tool="inspect"/);
  assert.match(html,/期望/);
  assert.match(html,/实际/);
});

test('saved evidence is escaped in content and output-target attributes', () => {
  const html=renderChecks({checks:[{kind:'tool_argument_equals',name:'<img src=x onerror="alert(1)">',pointer:'/x',value:'<script>bad()</script>',actual:['<svg onload="bad()">'],passed:false}]});
  assert.doesNotMatch(html,/<img|<svg|<script>/);
  assert.match(html,/&lt;script&gt;/);
  assert.match(html,/data-check-tool="&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;"/);
});
