import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ablation_workbench.cli import main
from ablation_workbench.engine import Workbench
from ablation_workbench.observation import collect_evidence, tracking_headers, validate_observers
from ablation_workbench.sources import configured_secrets


class ObservationTests(unittest.TestCase):
    def test_optional_tracking_and_url_encoding(self):
        self.assertEqual(tracking_headers([], {}), {})
        context = {'experiment_id': 'exp_1', 'batch_id': 'batch_1', 'run_id': 'run_1'}
        observers = [{'name': 'Any platform', 'headers': {'X-Trace': '{run_id}'}, 'record_id_pointer': '/id', 'url_template': 'https://logs.invalid/records/{record_id}', 'batch_url_template': 'https://logs.invalid/search?batch={batch_id}'}]
        self.assertEqual(tracking_headers(observers, context), {'X-Trace': 'run_1'})
        item = collect_evidence(observers, context, {'id': 'a/b?c'}, {}, [])[0]
        self.assertEqual(item['url'], 'https://logs.invalid/records/a%2Fb%3Fc')
        self.assertEqual(item['status'], 'available')
        self.assertIn('batch_1', item['batch_url'])

    def test_template_link_is_not_confirmed_evidence(self):
        item = collect_evidence([{'name': 'Logs', 'url_template': 'https://logs.invalid/search?trace={run_id}'}], {'run_id': 'run_1'}, {}, {}, [])[0]
        self.assertEqual(item['status'], 'linked')
        self.assertNotIn('record_id', item)

    def test_request_id_does_not_identify_external_log(self):
        observer = {'name': 'Logs', 'request_id_header': 'X-Request-Id', 'batch_url_template': 'https://logs.invalid/search?batch={batch_id}'}
        validate_observers([observer])
        item = collect_evidence([observer], {'batch_id': 'batch_1'}, {}, {'x-request-id': 'req_transport'}, [])[0]
        self.assertEqual(item['request_id'], 'req_transport')
        self.assertEqual(item['status'], 'unavailable')
        self.assertNotIn('record_id', item)
        self.assertNotIn('url', item)
        self.assertIn('batch_1', item['batch_url'])

    def test_lookup_is_authoritative_even_with_response_record_id(self):
        observer = {'name': 'Logs', 'request_id_header': 'X-Request-Id', 'record_id_pointer': '/id', 'lookup': {'source_base_url': 'https://logs.invalid', 'source_path': '/api/logs?request={request_id}', 'records_pointer': '/logs', 'match_pointer': '/trace', 'match_value': '{run_id}', 'record_id_pointer': '/id'}, 'url_template': 'https://logs.invalid/?selected_log={record_id}'}
        validate_observers([observer])
        with patch('ablation_workbench.observation.fetch', return_value=(b'{"logs":[{"trace":"run_1","id":"actual_log"}]}', {})) as fetch:
            item = collect_evidence([observer], {'run_id': 'run_1'}, {'id': 'response_id'}, {'x-request-id': 'req_transport'}, [])[0]
        self.assertEqual(fetch.call_args.args[0], 'https://logs.invalid/api/logs?request=req_transport')
        self.assertEqual(item['record_id'], 'actual_log')
        self.assertEqual(item['url'], 'https://logs.invalid/?selected_log=actual_log')
        self.assertEqual(item['status'], 'available')
        for records in ([], [{'trace': 'run_1', 'id': 'a'}, {'trace': 'run_1', 'id': 'b'}]):
            with self.subTest(records=records), patch('ablation_workbench.observation.fetch', return_value=(json.dumps({'logs': records}).encode(), {})):
                item = collect_evidence([observer], {'run_id': 'run_1'}, {'id': 'response_id'}, {'x-request-id': 'req_transport'}, [])[0]
            self.assertEqual(item['status'], 'unavailable')
            self.assertNotIn('record_id', item)
            self.assertNotIn('url', item)

    def test_response_headers_multiple_platforms_and_missing_evidence(self):
        items = collect_evidence([
            {'name': 'Proxy', 'record_id_header': 'X-Entry', 'url_header': 'X-Entry-Url'},
            {'name': 'Provider', 'record_id_pointer': '/request/id'},
            {'name': 'Missing', 'record_id_pointer': '/absent'},
        ], {}, {'request': {'id': 'provider-1'}}, {'x-entry': 'entry-2', 'x-entry-url': 'https://proxy.invalid/entry/2'}, [])
        self.assertEqual([i['status'] for i in items], ['available', 'available', 'unavailable'])
        self.assertEqual(items[0]['record_id'], 'entry-2')
        self.assertEqual(items[1]['record_id'], 'provider-1')

    def test_delayed_log_is_retried_and_uniquely_matched(self):
        observer = {'name': 'Logs', 'lookup': {'source_base_url': 'https://logs.invalid', 'source_path': '/logs', 'records_pointer': '/logs', 'match_pointer': '/trace', 'match_value': '{run_id}', 'record_id_pointer': '/id', 'attempts': 3, 'interval_seconds': 2}, 'url_template': 'https://logs.invalid/view/{record_id}'}
        validate_observers([observer])
        responses = [(b'{"logs":[]}', {}), (b'{"logs":[{"trace":"other","id":"wrong"}]}', {}), (b'{"logs":[{"trace":"run_1","id":"correct"}]}', {})]
        with patch('ablation_workbench.observation.fetch', side_effect=responses) as fetch, patch('ablation_workbench.observation.time.sleep') as sleep:
            item = collect_evidence([observer], {'run_id': 'run_1'}, {}, {}, [])[0]
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(item['status'], 'available')
        self.assertEqual(item['url'], 'https://logs.invalid/view/correct')

        with patch('ablation_workbench.observation.fetch', return_value=(b'{"logs":[{"trace":"run_1","id":"a"},{"trace":"run_1","id":"b"}]}', {})) as fetch, patch('ablation_workbench.observation.time.sleep') as sleep:
            item = collect_evidence([observer], {'run_id': 'run_1'}, {}, {}, [])[0]
        self.assertEqual(fetch.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(item['status'], 'unavailable')
        self.assertNotIn('url', item)

    def test_lookup_retry_bounds(self):
        lookup = {'source_base_url': 'https://logs.invalid', 'source_path': '/logs', 'match_pointer': '/trace', 'match_value': '{run_id}'}
        for key, value in [('attempts', 0), ('attempts', 6), ('attempts', True), ('interval_seconds', -1), ('interval_seconds', 6), ('interval_seconds', True), ('interval_seconds', float('nan'))]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_observers([{'name': 'Logs', 'lookup': {**lookup, key: value}}])

    def test_invalid_config_and_untrusted_links(self):
        for observer in [
            {'name': 'bad', 'headers': {'Authorization': 'secret'}},
            {'name': 'bad', 'headers': {'X-API-Key': 'secret'}},
            {'name': 'bad', 'headers': {'X-Trace': '{run_id.__class__}'}},
            {'name': 'bad', 'headers': {'X-Trace': 'line\nbreak'}},
            {'name': 'bad', 'url_template': 'javascript:alert(1)'},
            {'name': 'bad', 'url_template': 'https://logs.invalid/?token=secret'},
            {'name': 'bad', 'lookup': {'source_base_url': 'https://logs.invalid', 'source_path': '//other.invalid/logs', 'match_pointer': '/id', 'match_value': '{run_id}'}},
            {'name': 'bad', 'lookup': {'source_base_url': 'https://logs.invalid', 'source_path': 'other.invalid/logs', 'match_pointer': '/id', 'match_value': '{run_id}'}},
        ]:
            with self.subTest(observer=observer), self.assertRaises(ValueError):
                validate_observers([observer])
        item = collect_evidence([{'name': 'Bad response', 'url_pointer': '/url'}], {}, {'url': 'javascript:alert(1)'}, {}, [])[0]
        self.assertEqual(item['status'], 'unavailable')
        self.assertNotIn('url', item)

    def test_nested_credentials_are_redacted(self):
        with patch.dict('os.environ', {'OBS_PASSWORD': 'synthetic-secret'}):
            config = {'profiles': {'p': {'observers': [{'lookup': {'source_password_env': 'OBS_PASSWORD'}}]}}}
            self.assertIn('synthetic-secret', configured_secrets(config))

    def test_cli_preserves_profile_and_rejects_bad_observer(self):
        with tempfile.TemporaryDirectory() as directory:
            f = Path(directory) / 'observers.json'
            f.write_text(json.dumps([{'name': 'Generic', 'headers': {'X-Trace': '{run_id}'}}]))
            self.assertEqual(main(['--workspace', directory, 'config', 'profile', 'model', '--base-url', 'https://model.invalid', '--observers-json', str(f)]), 0)
            self.assertEqual(main(['--workspace', directory, 'config', 'profile', 'model', '--api-key-env', 'MODEL_KEY']), 0)
            saved = json.loads((Path(directory) / 'config.json').read_text())
            self.assertEqual(len(saved['profiles']['model']['observers']), 1)
            f.write_text('[{"name":"bad","headers":{"Host":"override"}}]')
            self.assertEqual(main(['--workspace', directory, 'config', 'profile', 'model', '--observers-json', str(f)]), 2)
            self.assertEqual(json.loads((Path(directory) / 'config.json').read_text()), saved)

    def test_http_replay_lookup_and_failure_preserve_model_result(self):
        packets = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                packets.append((body, dict(self.headers)))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('X-Proxy-Entry', 'entry_fixture')
                self.send_header('Set-Cookie', 'synthetic-cookie')
                self.end_headers()
                self.wfile.write(b'{"id":"response_fixture","output":[]}')
            def do_GET(self):
                run_id = packets[-1][1]['X-Trace']
                packets.append((self.path, dict(self.headers)))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'logs': [{'trace': 'unrelated', 'id': 'wrong'}, {'trace': run_id, 'id': 'log_fixture'}]}).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        with tempfile.TemporaryDirectory() as directory:
            w = Workbench(directory)
            observers = [
                {'name': 'Provider', 'headers': {'X-Trace': '{run_id}'}, 'lookup': {'source_base_url': base, 'source_path': '/logs?trace={run_id}', 'records_pointer': '/logs', 'match_pointer': '/trace', 'match_value': '{run_id}', 'record_id_pointer': '/id', 'token_env': 'OBS_TOKEN'}, 'url_template': base + '/view/{record_id}'},
                {'name': 'Proxy', 'record_id_header': 'X-Proxy-Entry', 'url_template': base + '/entry/{record_id}'},
                {'name': 'Unavailable', 'record_id_pointer': '/absent'},
            ]
            (Path(directory) / 'config.json').write_text(json.dumps({'profiles': {'model': {'base_url': base, 'observers': observers, 'api_key_env': 'MODEL_KEY'}}}))
            request = {'model': 'fixture', 'input': 'fixture'}
            exp = w.import_source({'kind': 'request', 'data': request})
            with patch.dict('os.environ', {'MODEL_KEY': 'model-secret', 'OBS_TOKEN': 'observer-secret'}):
                result = w.run(exp['id'], candidates=['baseline'], scenarios=['original'], repeats=2, profile='model')
            runs = result['runs']
            self.assertNotEqual(runs[0]['id'], runs[1]['id'])
            self.assertEqual(runs[0]['batch_id'], runs[1]['batch_id'])
            for run in runs:
                self.assertEqual(run['status'], 'completed')
                self.assertEqual(run['request'], request)
                self.assertGreater(run['tracking']['end_time'], run['tracking']['start_time'])
                self.assertEqual([e['status'] for e in run['external_evidence']], ['available', 'available', 'unavailable'])
                self.assertEqual(run['external_evidence'][0]['record_id'], 'log_fixture')
                self.assertNotIn('set-cookie', run['response_headers'])
            self.assertEqual(packets[0][1]['Authorization'], 'Bearer model-secret')
            self.assertEqual(packets[1][1]['Authorization'], 'Bearer observer-secret')
            self.assertEqual(packets[0][1]['X-Trace'], runs[0]['id'])
            exported = w.export_bundle(exp['id'], Path(directory) / 'bundle.json')
            imported = w.import_bundle(exported)
            self.assertEqual(imported['runs'][0]['external_evidence'], runs[0]['external_evidence'])
            self.assertNotIn('observer-secret', exported.read_text())
            with patch.dict('os.environ', {'OBS_TOKEN': 'observer-secret'}), patch('ablation_workbench.observation.fetch', side_effect=ValueError('lookup unavailable')):
                item = collect_evidence(observers[:1], runs[0]['tracking'], {'id': 'fixture'}, {}, [])[0]
            self.assertEqual(item['status'], 'unavailable')
            self.assertIn('lookup unavailable', item['error'])


if __name__ == '__main__':
    unittest.main()
