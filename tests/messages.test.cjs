const {test} = require('node:test');
const assert = require('node:assert/strict');
const {fromRun, runInfo, safeImage} = require('../ui/messages-data.cjs');
const image = 'data:image/png;base64,aGVsbG8=';

test('Responses timeline preserves instructions, full history, call/result association and image blocks', () => {
  const rows = fromRun({request:{instructions:'System instructions', input:[
    {role:'user', content:'Earlier request'},
    {type:'function_call', name:'capture', call_id:'call-a', arguments:'{}'},
    {type:'function_call_output', call_id:'call-a', output:[{type:'input_text',text:'Recorded screen'},{type:'input_image',image_url:image}]},
    {role:'user', content:'Current request'},
  ]}, response:{output:[{type:'function_call',name:'adjust',call_id:'call-b',arguments:'{"amount":0,"confirm":false}'}]}});
  assert.equal(rows.length,6);
  assert.equal(rows[0].msg.role,'system');
  assert.equal(rows[1].msg.content[0].text,'Earlier request');
  assert.equal(rows[3].callName,'capture');
  assert.equal(rows[3].msg.content[1].image_url,image);
  assert.equal(rows[4].msg.content[0].text,'Current request');
  assert.equal(rows[5].source,'output');
  assert.deepEqual(JSON.parse(rows[5].msg.arguments),{amount:0,confirm:false});
});

test('Chat Completions calls and tool messages use the same timeline with mixed text/images', () => {
  const rows = fromRun({request:{messages:[
    {role:'assistant',content:'Inspecting',tool_calls:[{id:'call-a',function:{name:'capture',arguments:'{}'}}]},
    {role:'tool',tool_call_id:'call-a',content:[{type:'text',text:'Screen'},{type:'image_url',image_url:{url:image}}]},
  ]},response:{choices:[{message:{content:'Observed result',tool_calls:[{id:'call-b',function:{name:'finish',arguments:'{"done":false}'}}]}}]}});
  assert.equal(rows.length,5);
  assert.equal(rows[2].msg.type,'function_call_output');
  assert.equal(rows[2].callName,'capture');
  assert.equal(rows[2].msg.content[1].image_url,image);
  assert.equal(rows[3].msg.role,'assistant');
  assert.equal(rows[4].msg.name,'finish');
});

test('Malformed arguments stay verbatim and do not prevent viewing later messages', () => {
  const rows = fromRun({response:{output:[{type:'function_call',name:'incomplete',arguments:'{"unfinished":'},{role:'assistant',content:'Still visible'}]}});
  assert.equal(rows[0].msg.arguments,'{"unfinished":');
  assert.equal(rows[1].msg.content[0].text,'Still visible');
});

test('Encrypted reasoning stays opaque and separate from message text', () => {
  const rows = fromRun({response:{output:[{type:'reasoning',encrypted_content:'encrypted-token'},{role:'assistant',content:[{type:'refusal',refusal:'Declined'}]}]}});
  assert.deepEqual(rows[0].msg.summary,[]);
  assert.equal(rows[0].msg.encrypted_content,'encrypted-token');
  assert.equal(rows[1].msg.content[0].refusal,'Declined');
});

test('Unknown blocks are retained and remote images stay references without automatic fetching', () => {
  const rows = fromRun({request:{input:[{role:'user',content:[{type:'input_image',image_url:'https://assets.example.invalid/screenshot.png'},{type:'input_file',file_id:'file-a'}]}]}});
  assert.equal(rows[0].msg.content[0].type,'input_text');
  assert.match(rows[0].msg.content[0].text,/https:\/\/assets/);
  assert.match(rows[0].msg.content[1].text,/file-a/);
  assert.equal(safeImage('javascript:alert(1)'),null);
  assert.equal(safeImage('data:image/svg+xml;base64,PHN2Zz4='),null);
});

test('Raw run data stays immutable while JSON is formatted for readability', () => {
  const run={request:{input:[{role:'user',content:'{"goal":"Inspect","amount":0}'}]},response:{output:[]}};
  const before=JSON.stringify(run);
  assert.match(fromRun(run)[0].msg.content[0].text,/\n/);
  assert.equal(JSON.stringify(run),before);
});

test('Missing responses do not invent an assistant result or a token count', () => {
  const run={request:{model:'requested',input:'Request'},response:null};
  assert.equal(fromRun(run).length,1);
  assert.equal(fromRun(run)[0].source,'input');
  assert.deepEqual(runInfo(run),{model:'requested',tokens:null});
  assert.deepEqual(runInfo({response:{model:'returned',usage:{prompt_tokens:5,completion_tokens:2}}}),{model:'returned',tokens:7});
});
