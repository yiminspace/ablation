import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from test_engine import EngineFixture
from ablation_workbench.continuation import prepare_continuation
from ablation_workbench.models import digest


class ContinuationTests(EngineFixture):
    def setUp(self):
        super().setUp()
        (self.workbench.workspace/'config.json').write_text(json.dumps({'schema_version':1,'profiles':{'fixture':{'base_url':'http://127.0.0.1:8000'}}}))
        e=self.workbench.add_scenario(self.id,label='final output',patch=[],description='Synthetic final assertion',checks=[{'kind':'text_contains','value':'complete'}])
        self.scenario=e['scenarios'][-1]['id']
        self.response={'output':[{'type':'reasoning','encrypted_content':'synthetic opaque reasoning'}, {'type':'function_call','call_id':'call_fixture','name':'inspect','arguments':'{}'}]}
        with patch('ablation_workbench.engine.replay_request',return_value={'status':'completed','response':self.response,'elapsed_ms':2}):
            self.parent=self.workbench.run(self.id,candidates=['baseline'],scenarios=[self.scenario],profile='fixture')['runs'][-1]
        self.options={'parent_run':self.parent['id'],'tool_results':[{'call_id':'call_fixture','output':'Synthetic loader result'}],
          'context_patch':[{'op':'add','path':'/instructions','value':'Runtime-loaded synthetic instructions.'}],
          'rationale':'Tool request requires its runtime instructions and result before checking final output.'}

    def follow(self, **options):
        response={'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'complete'}]}]}
        with patch('ablation_workbench.engine.replay_request',return_value={'status':'completed','response':response,'elapsed_ms':2}):
            return self.workbench.continue_run(self.id,**{**self.options,**options})

    def test_failed_round_continues_without_mutating_candidate_or_its_failure(self):
        before=self.workbench.get(self.id)
        diff=self.workbench.compare(self.id,artifact='changes')
        after=self.follow();child=after['runs'][-1]
        self.assertFalse(self.parent['passed']);self.assertTrue(child['passed'])
        self.assertEqual(before['runs'][0],after['runs'][0])
        for key in ['baseline','candidates','scenarios','reviews']:self.assertEqual(before[key],after[key])
        self.assertEqual(diff,self.workbench.compare(self.id,artifact='changes'))
        self.assertEqual(child['parent_run_id'],self.parent['id']);self.assertEqual(child['root_run_id'],self.parent['id'])
        self.assertEqual(child['request']['input'][:-3],self.parent['request']['input'])
        self.assertEqual(child['request']['input'][-3:-1],self.response['output'])
        self.assertEqual(child['request']['input'][-1]['call_id'],'call_fixture')
        self.assertEqual(child['request_hash'],digest(child['request']))
        bundle=self.workbench.export_bundle(self.id,Path(self.temp.name)/'bundle.json')
        imported=self.workbench.import_bundle(bundle)
        self.assertEqual(imported['runs'],after['runs'])

    def test_stage_checks_are_explicit_and_do_not_change_the_parent_checks(self):
        child=self.follow(checks=[])['runs'][-1]
        self.assertIsNone(child['passed']);self.assertFalse(self.workbench.get(self.id)['runs'][0]['passed'])
        self.assertEqual(child['continuation']['checks'],[])

    def test_rejects_mismatched_missing_duplicate_results_and_non_context_edits(self):
        for results in [[],[{'call_id':'wrong','output':'x'}],self.options['tool_results']*2]:
            with self.assertRaises(ValueError):self.follow(tool_results=results)
        for path in ['/input/0/content','/model','/temperature','', '/instructions_alias']:
            with self.assertRaises(ValueError):self.follow(context_patch=[{'op':'add','path':path,'value':'x'}])
        with self.assertRaises(ValueError):self.follow(rationale='')
        with self.assertRaises(ValueError):self.follow(profile='other')
        self.assertEqual(len(self.workbench.get(self.id)['runs']),1)

    def test_credentials_are_rejected_before_replay(self):
        with self.assertRaises(ValueError):self.follow(context_patch=[{'op':'add','path':'/tools/0/password','value':'synthetic'}])
        with patch.dict(os.environ,{'TEST_CONTINUATION_SECRET':'synthetic-private-token'}):
            config=json.loads((self.workbench.workspace/'config.json').read_text());config['profiles']['fixture']['api_key_env']='TEST_CONTINUATION_SECRET'
            (self.workbench.workspace/'config.json').write_text(json.dumps(config))
            with self.assertRaises(ValueError):self.follow(tool_results=[{'call_id':'call_fixture','output':'synthetic-private-token'}])
        self.assertEqual(len(self.workbench.get(self.id)['runs']),1)

    def test_bundle_rejects_tampered_history_context_root_or_attribution(self):
        self.follow();path=self.workbench.export_bundle(self.id,Path(self.temp.name)/'bundle.json');data=json.loads(path.read_text())
        for field,value in [('parent_run_id','nonexistent'),('root_run_id','wrong'),('request',self.parent['request']),('continuation',{**data['experiment']['runs'][-1]['continuation'],'tool_results':[]} )]:
            bad=copy.deepcopy(data);bad['experiment']['runs'][-1][field]=value
            if field=='request':bad['experiment']['runs'][-1]['request_hash']=digest(value)
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):self.workbench.import_bundle(path)

    def test_chat_completions_and_multiple_tools_preserve_actual_assistant_message(self):
        message={'role':'assistant','content':None,'tool_calls':[{'id':'call_a','type':'function','function':{'name':'inspect','arguments':'{}'}},{'id':'call_b','type':'function','function':{'name':'read','arguments':'{}'}}]}
        parent={'status':'completed','request':{'model':'fixture','messages':[{'role':'user','content':'Inspect'}]},'response':{'choices':[{'message':message}]}}
        record={'rationale':'Resolve actual calls','context_patch':[],'checks':[],'tool_results':[{'call_id':'call_a','output':'A'},{'call_id':'call_b','output':'B'}]}
        request=prepare_continuation(parent,record)
        self.assertEqual(request['messages'][1],message)
        self.assertEqual([m['tool_call_id'] for m in request['messages'][2:]],['call_a','call_b'])
        with self.assertRaises(ValueError):prepare_continuation({**parent,'response':{'choices':[{'message':{'role':'assistant','content':'finished'}}]}},record)

    def test_branching_retains_root_and_both_failed_and_successful_children(self):
        self.follow();self.follow(checks=[{'kind':'text_contains','value':'absent'}])
        runs=self.workbench.get(self.id)['runs'];self.assertEqual(len(runs),3)
        self.assertEqual([r['passed'] for r in runs],[False,True,False])
        self.assertTrue(all(r['root_run_id']==runs[0]['id'] for r in runs[1:]))

    def test_cli_continuation_against_local_http_fixture_and_exit_codes(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if any(m.get('type')=='function_call_output' for m in request['input']):
                    response={'output':[{'type':'message','content':[{'type':'output_text','text':'complete'}]}]}
                else:response={'output':[{'type':'function_call','name':'inspect','call_id':'call_fixture','arguments':'{}'}]}
                data=json.dumps(response).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        config={'schema_version':1,'profiles':{'fixture':{'base_url':f'http://127.0.0.1:{server.server_port}'}}}
        (self.workbench.workspace/'config.json').write_text(json.dumps(config))
        e=self.workbench.import_source({'kind':'request','data':self.request});eid=e['id']
        e=self.workbench.add_scenario(eid,label='final',patch=[],description='Synthetic fixture',checks=[{'kind':'text_contains','value':'complete'}]);sid=e['scenarios'][-1]['id']
        cmd=[sys.executable,'-m','ablation_workbench','--workspace',str(self.workbench.workspace)]
        env={**os.environ,'PYTHONPATH':str(Path('src').resolve())}
        first=subprocess.run(cmd+['run',eid,'--candidate','baseline','--scenario',sid,'--profile','fixture'],capture_output=True,text=True,env=env)
        self.assertEqual(first.returncode,3,first.stderr);parent=json.loads(first.stdout)['runs'][-1]
        for name,value in [('results',self.options['tool_results']),('context',self.options['context_patch'])]:
            (self.workbench.workspace/(name+'.json')).write_text(json.dumps(value))
        next_round=subprocess.run(cmd+['continue',eid,parent['id'],'--tool-results',str(self.workbench.workspace/'results.json'),'--context-patch',str(self.workbench.workspace/'context.json'),'--rationale','Resolve actual synthetic tool call'],capture_output=True,text=True,env=env)
        self.assertEqual(next_round.returncode,0,next_round.stderr)
        result=json.loads(next_round.stdout);self.assertFalse(result['runs'][0]['passed']);self.assertTrue(result['runs'][1]['passed'])

    def test_http_continuation_job_uses_shared_engine(self):
        from ablation_workbench.server import make_server,close_jobs
        from urllib.request import Request,urlopen
        import threading,time
        server=make_server(self.workbench);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}/ablation/api/experiments/{self.id}/continue'
            request=Request(url,data=json.dumps(self.options).encode(),headers={'Content-Type':'application/json','X-Ablation-Client':'local'})
            with patch('ablation_workbench.engine.replay_request',return_value={'status':'completed','elapsed_ms':1,'response':{'output':[{'type':'message','content':[{'type':'output_text','text':'complete'}]}]}}):
                with urlopen(request) as r:job=json.load(r)
                for _ in range(100):
                    saved=next(j for j in server.replay_jobs.list() if j['id']==job['id'])
                    if saved['status'] not in {'queued','running','cancelling'}:break
                    time.sleep(.02)
                self.assertEqual(saved['status'],'completed',saved)
            self.assertTrue(self.workbench.get(self.id)['runs'][-1]['passed'])
        finally:close_jobs(server);server.shutdown();server.server_close()
