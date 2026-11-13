import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ablation_workbench.cli import main
from ablation_workbench.engine import Workbench, split_diff
from ablation_workbench.server import close_jobs, make_server
from ablation_workbench.storage import atomic_json
from ablation_workbench.translation import Translation, changed_rows, llm_config, model_credential, partial_rows, translation_tasks


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workbench = Workbench(self.temp.name)
        self.config = {'llm': {'base_url': 'http://127.0.0.1:1/v1', 'model': 'translation-model', 'auth': 'none'}}
        atomic_json(Path(self.temp.name) / 'config.json', self.config)
        self.experiment = self.workbench.import_source({'kind': 'request', 'data': {
            'model': 'original-model', 'instructions': 'unchanged sentinel\nold\nkeep one\nkeep two\ndeleted', 'input': 'private input sentinel'}})
        self.experiment = self.workbench.add_candidate(self.experiment['id'], label='Changed', rationale='Private rationale sentinel',
            patch=[{'op': 'replace', 'path': '/instructions', 'value': 'unchanged sentinel\nnew\nkeep one\nkeep two'}])
        self.eid = self.experiment['id']
        self.selection = {'candidate': self.experiment['candidates'][-1]['id']}
        self.translation = Translation(self.workbench)

    def tearDown(self):
        self.temp.cleanup()

    def options(self):
        return dict(session_id=self.translation.get(self.eid, **self.selection)['id'], **self.selection)

    @staticmethod
    def translated(request, settings, kind, secrets, *, on_text=None):
        content = request['messages'][1]['content'] if kind == 'chat' else request['input'][0]['content']
        source = json.loads(content)['rows']
        text = json.dumps({'rows': [{'id': row['id'], 'before': '旧版译文' if row['before'].strip() else '',
                                   'after': '新版译文' if row['after'].strip() else ''} for row in source]})
        response = {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]} if kind == 'chat' else {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]}
        return {'status': 'completed', 'response': response}

    def test_only_changed_rows_are_translated_with_independent_source_model(self):
        before = copy.deepcopy(self.workbench.get(self.eid))
        with patch('ablation_workbench.translation.replay_request', side_effect=self.translated) as transport:
            result = self.translation.run(self.eid, **self.options())
            request, settings, kind, _ = transport.call_args.args
            self.assertEqual(request['model'], 'translation-model')
            self.assertEqual(kind, 'chat')
            self.assertEqual(settings['base_url'], 'http://127.0.0.1:1/v1/chat/completions')
            content = request['messages'][1]['content']
            for sentinel in ('unchanged sentinel', 'private input sentinel', 'Private rationale sentinel', 'keep one'):
                self.assertNotIn(sentinel, content)
            self.assertNotIn('tools', request)
            self.assertEqual(len(result['translations']), 2)
            self.assertTrue(request['stream'])
            self.assertNotIn('explanation', result['translations'][0])
        self.assertEqual(before, self.workbench.get(self.eid))

    def test_changed_rows_handle_additions_and_deletions(self):
        rows = changed_rows(split_diff('old\nkeep', 'new\nkeep\nadded'))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {'id': 'row_0', 'index': 0, 'before': 'old', 'after': 'new'})
        self.assertEqual(rows[1]['before'], '')
        removed = changed_rows(split_diff('remove\nkeep', 'keep'))
        self.assertEqual(removed[0]['after'], '')

    def test_cache_survives_restart_and_reuses_completed_translation(self):
        with patch('ablation_workbench.translation.replay_request', side_effect=self.translated) as transport:
            saved = self.translation.run(self.eid, **self.options())
            restarted = Translation(Workbench(self.temp.name))
            self.assertEqual(restarted.get(self.eid, **self.selection)['translations'], saved['translations'])
            restarted.run(self.eid, **self.options())
            self.assertEqual(transport.call_count, 1)
        self.assertFalse(restarted.get(self.eid, **self.selection)['busy'])

    def test_partial_batches_resume_after_provider_failure(self):
        before = 'old ' * 2500 + '\nkeep\n' + 'earlier ' * 2500
        after = 'new ' * 2500 + '\nkeep\n' + 'later ' * 2500
        experiment = self.workbench.import_source({'kind': 'request', 'data': {'model': 'fixture', 'instructions': before, 'input': 'fixture'}})
        experiment = self.workbench.add_candidate(experiment['id'], label='Batch', rationale='Batch', patch=[{'op': 'replace', 'path': '/instructions', 'value': after}])
        eid = experiment['id']
        selection = {'candidate': experiment['candidates'][-1]['id']}
        options = dict(session_id=self.translation.get(eid, **selection)['id'], **selection)
        calls = 0
        def flaky(*args, **kwargs):
            nonlocal calls
            calls += 1
            return {'status': 'error', 'error': 'Provider failed'} if calls == 2 else self.translated(*args, **kwargs)
        with patch('ablation_workbench.translation.replay_request', side_effect=flaky):
            with self.assertRaisesRegex(ValueError, 'Provider failed'):
                self.translation.run(eid, **options)
        self.assertEqual(len(self.translation.get(eid, **selection)['translations']), 1)
        with patch('ablation_workbench.translation.replay_request', side_effect=self.translated) as transport:
            result = self.translation.run(eid, **options)
            self.assertEqual(transport.call_count, 1)
        self.assertEqual(len(result['translations']), 2)

    def test_stale_submission_and_concurrent_translation_are_rejected(self):
        options = self.options()
        with self.assertRaisesRegex(RuntimeError, '更新'):
            self.translation.run(self.eid, **{**options, 'session_id': 'stale'})
        entered, release = threading.Event(), threading.Event()
        errors = []
        def slow(*args, **kwargs):
            entered.set(); release.wait(5)
            return self.translated(*args, **kwargs)
        def first():
            try: self.translation.run(self.eid, **options)
            except Exception as error: errors.append(error)
        with patch('ablation_workbench.translation.replay_request', side_effect=slow):
            worker = threading.Thread(target=first); worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(self.translation.get(self.eid, **self.selection)['busy'])
                with self.assertRaises(RuntimeError): self.translation.run(self.eid, **options)
            finally:
                release.set(); worker.join(5)
        self.assertEqual(errors, [])

    def test_invalid_translation_is_not_cached(self):
        for text in ('not json', '{"rows":[]}', '{"rows":[{"id":"wrong","before":"旧","after":"新"}]}'):
            with patch('ablation_workbench.translation.replay_request', return_value={
                'status': 'completed', 'response': {'choices': [{'message': {'content': text}}]}}):
                with self.assertRaises(ValueError): self.translation.run(self.eid, **self.options())
            self.assertEqual(self.translation.get(self.eid, **self.selection)['translations'], [])

    def test_cli_configures_source_outside_gui_and_preserves_profiles(self):
        self.config['profiles'] = {'existing': {'base_url': 'https://replay.invalid/v1'}}
        atomic_json(Path(self.temp.name) / 'config.json', self.config)
        with patch('ablation_workbench.cli.emit'):
            status = main(['--workspace', self.temp.name, 'config', 'llm', '--base-url', 'https://model.invalid/api/v3',
                           '--model', 'configured-model', '--api-key-env', 'TRANSLATION_TEST_KEY', '--thinking', 'disabled'])
        self.assertEqual(status, 0)
        config = self.workbench.store.config()
        self.assertEqual(config['profiles'], self.config['profiles'])
        self.assertEqual(config['llm']['model'], 'configured-model')
        self.assertEqual(config['llm']['api_key_env'], 'TRANSLATION_TEST_KEY')
        self.assertNotIn('api_key', config['llm'])

    def test_review_snapshot_translation_uses_the_saved_diff(self):
        diff = self.workbench.compare(self.eid, **self.selection, artifact='changes')
        row = next(row for row in diff['rows'] if row['kind'] != 'equal')
        review = self.workbench.submit_review(self.eid, {
            'id': 'review_translation', 'revision': self.experiment['revision'], 'base': 'baseline',
            'candidate': self.selection['candidate'], 'scenario': 'original', 'intent': 'comment', 'summary': 'Translate this change',
            'comments': [{'artifact': 'changes', **{key: row[key] for key in ('old_line', 'new_line', 'before', 'after')}, 'text': 'Translate'}]})
        session = self.translation.get(self.eid, **self.selection, review=review['id'])
        self.assertEqual(session['rows'], changed_rows(review['snapshot']['diffs']['changes']['rows']))
        with patch('ablation_workbench.translation.replay_request', side_effect=self.translated):
            translated = self.translation.run(self.eid, **self.selection, review=review['id'], session_id=session['id'])
        self.assertEqual(len(translated['translations']), len(session['rows']))
        with self.assertRaises(ValueError):
            self.translation.get(self.eid, candidate='baseline', review=review['id'])

    def test_credential_sources_and_redaction(self):
        settings = llm_config({'llm': {**self.config['llm'], 'auth': 'env', 'api_key_env': 'TRANSLATION_TEST_KEY'}})
        with patch.dict(os.environ, {'TRANSLATION_TEST_KEY': 'test-secret'}):
            self.assertEqual(model_credential(settings), 'test-secret')
        settings = llm_config({'llm': {**self.config['llm'], 'auth': 'command', 'credential_command': ['/usr/bin/printf', 'test-secret']}})
        self.assertEqual(model_credential(settings), 'test-secret')
        for argv in (['relative-command'], ['/usr/bin/printf', 'bad\nargument']):
            with self.assertRaises(ValueError):
                llm_config({'llm': {**self.config['llm'], 'auth': 'command', 'credential_command': argv}})
        self.config['llm'].update(auth='env', api_key_env='TRANSLATION_TEST_KEY')
        atomic_json(Path(self.temp.name) / 'config.json', self.config)
        with patch.dict(os.environ, {'TRANSLATION_TEST_KEY': 'test-secret'}):
            def echo_secret(*args, **kwargs):
                response = self.translated(*args, **kwargs)
                response['response']['choices'][0]['message']['content'] = response['response']['choices'][0]['message']['content'].replace('旧版译文', 'test-secret')
                return response
            with patch('ablation_workbench.translation.replay_request', side_effect=echo_secret) as transport:
                result = self.translation.run(self.eid, **self.options())
            self.assertNotIn('test-secret', json.dumps(result))
            self.assertEqual(transport.call_args.args[1]['_tracking_headers']['Authorization'], 'Bearer test-secret')
            saved = next((Path(self.temp.name) / 'experiments' / self.eid / 'translations').glob('*.json')).read_text()
            self.assertNotIn('test-secret', saved)

    def test_failed_stream_drops_drafts_and_retries_only_the_failed_segment(self):
        experiment = self.workbench.add_candidate(self.eid, label='Separate tasks', rationale='Two task regression', patch=[{'op':'replace','path':'/instructions','value':'unchanged sentinel\n' + 'new ' * 1000 + '\nkeep one\nkeep two'}])
        self.selection = {'candidate':experiment['candidates'][-1]['id']}
        def broken(request, settings, kind, secrets, *, on_text):
            rows = json.loads(request['messages'][1]['content'])['rows']
            if rows[0]['before'] == 'deleted':
                on_text('{"rows":[{"id":"' + rows[0]['id'] + '","before":"未完成')
                return {'status': 'error', 'error': 'Stream ended before completion'}
            return self.translated(request, settings, kind, secrets)
        with patch('ablation_workbench.translation.replay_request', side_effect=broken):
            events = list(self.translation.events(self.eid, **self.options()))
        self.assertTrue(any(event['type'] == 'partial' for event in events))
        self.assertTrue(any(event['type'] == 'task_error' for event in events))
        self.assertEqual(events[-1]['type'], 'error')
        saved = self.translation.get(self.eid, **self.selection)
        self.assertEqual(len(saved['translations']), 1)
        self.assertEqual(saved['drafts'], [])
        self.assertNotIn('未完成', json.dumps(saved, ensure_ascii=False))
        with patch('ablation_workbench.translation.replay_request', side_effect=self.translated) as transport:
            result = self.translation.run(self.eid, **self.options())
            self.assertEqual(transport.call_count, 1)
        self.assertEqual(len(result['translations']), 2)

    def test_parallel_segments_share_credential_and_refresh_once_after_unauthorized(self):
        self.config['llm'].update(auth='command', credential_command=['/usr/bin/printf', 'credential'])
        atomic_json(Path(self.temp.name) / 'config.json', self.config)
        experiment = self.workbench.add_candidate(self.eid, label='Credential reuse', rationale='Two large segments',
            patch=[{'op':'replace','path':'/instructions','value':'unchanged sentinel\n' + 'new ' * 1000 + '\nkeep one\nkeep two'}])
        self.selection = {'candidate': experiment['candidates'][-1]['id']}
        seen = []
        def provider(request, settings, kind, secrets, **kwargs):
            authorization = settings['_tracking_headers']['Authorization']
            seen.append(authorization)
            if authorization == 'Bearer expired':
                return {'status':'error','http_status':401,'error':'Model endpoint returned HTTP 401'}
            return self.translated(request, settings, kind, secrets, **kwargs)
        with patch('ablation_workbench.translation.model_credential', side_effect=['expired','fresh']) as credential:
            with patch('ablation_workbench.translation.replay_request', side_effect=provider):
                result = self.translation.run(self.eid, **self.options())
        self.assertEqual(len(result['translations']), 2)
        self.assertEqual(credential.call_count, 2)
        self.assertIn('Bearer expired', seen)
        self.assertIn('Bearer fresh', seen)
        with patch('ablation_workbench.translation.model_credential', return_value='shared') as credential:
            with patch('ablation_workbench.translation.replay_request', side_effect=self.translated):
                self.translation.run(self.eid, **self.options())
        credential.assert_not_called()
        experiment = self.workbench.add_candidate(self.eid, label='Fresh batch', rationale='Credential reuse',
            patch=[{'op':'replace','path':'/instructions','value':'unchanged sentinel\n' + 'newer ' * 1000 + '\nkeep one\nkeep two'}])
        self.selection = {'candidate':experiment['candidates'][-1]['id']}
        with patch('ablation_workbench.translation.model_credential', return_value='shared') as credential:
            with patch('ablation_workbench.translation.replay_request', side_effect=self.translated) as transport:
                self.translation.run(self.eid, **self.options())
        credential.assert_called_once()
        self.assertEqual(transport.call_count, 2)

    def test_partial_json_decodes_characters_and_withholds_secret_prefixes(self):
        expected = [{'id': 'row_1', 'before': 'old', 'after': 'new'}]
        prefix = '{"rows":[{"id":"row_1","before":"旧文 test-sec'
        rows = partial_rows(prefix, expected, ['test-secret'])
        self.assertEqual(rows[0]['before'], '旧文 ')
        escaped = r'{"rows":[{"id":"row_1","before":"\u4e2d\u65'
        self.assertEqual(partial_rows(escaped, expected, [])[0]['before'], '中')
        escaped = r'{"rows":[{"id":"row_1","before":"\ud83d\ude'
        self.assertEqual(partial_rows(escaped, expected, [])[0]['before'], '')
        complete = '{"rows":[{"id":"row_1","before":"test-secret","after":"新"}]}'
        self.assertNotIn('test-secret', json.dumps(partial_rows(complete, expected, ['test-secret'])))

    def test_task_partition_combines_short_edits_and_bounds_batches(self):
        rows = [{'id': f'row_{i}', 'index': i, 'before': 'old', 'after': 'new'} for i in range(40)]
        tasks = translation_tasks(rows)
        self.assertEqual([len(task['rows']) for task in tasks], [16, 16, 8])
        self.assertEqual(len(translation_tasks([rows[0], rows[2]])), 1)

    def test_real_http_stream_delivers_drafts_and_completed_segments_before_slow_task(self):
        for kind in ('chat', 'responses'):
            with self.subTest(api=kind):
                self._check_http_stream(kind)

    def _check_http_stream(self, kind):
        fast, slow = threading.Event(), threading.Event()
        both_started = threading.Event()
        packets, guard = [], threading.Lock()
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                packet = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                data = packet['messages'][1]['content'] if kind == 'chat' else packet['input'][0]['content']
                rows = json.loads(data)['rows']
                with guard:
                    packets.append(packet)
                    if len(packets) == 2: both_started.set()
                text = json.dumps({'rows': [{'id': row['id'], 'before': '旧译文' if row['before'] else '', 'after': '新译文' if row['after'] else ''} for row in rows]}, ensure_ascii=False)
                first = text.index('译')
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
                def send(event):
                    self.wfile.write(('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()); self.wfile.flush()
                def delta(value):
                    send({'type': 'response.output_text.delta', 'delta': value} if kind == 'responses' else {'choices': [{'index': 0, 'delta': {'content': value}, 'finish_reason': None}]})
                delta(text[:first])
                (fast if rows[0]['before'] == 'old' else slow).wait(8)
                delta(text[first:])
                if kind == 'responses':
                    send({'type': 'response.completed', 'response': {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]}})
                else:
                    send({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
                    self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush()
        provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        self.config['llm'].update(base_url=f'http://127.0.0.1:{provider.server_port}/v1', api=kind)
        atomic_json(Path(self.temp.name) / 'config.json', self.config)
        # Use an uncached candidate in each API-family subtest.
        experiment = self.workbench.add_candidate(self.eid, label=kind, rationale=kind,
            patch=[{'op': 'replace', 'path': '/instructions', 'value': 'unchanged sentinel\nnew ' + kind + ' translated filler' * 400 + '\nkeep one\nkeep two'}])
        selection = {'candidate': experiment['candidates'][-1]['id']}
        server = make_server(self.workbench)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}/ablation/api/experiments/{self.eid}/translation'
        try:
            with urlopen(base + '?candidate=' + selection['candidate']) as response: session = json.load(response)
            body = dict(session_id=session['id'], **selection)
            request = Request(base + '/stream', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'X-Ablation-Client': 'local'})
            with urlopen(request, timeout=15) as response:
                self.assertIn('text/event-stream', response.headers['Content-Type'])
                events = []
                def next_event():
                    while line := response.readline():
                        if line.startswith(b'data:'):
                            event = json.loads(line[5:]); events.append(event); return event
                    self.fail('Stream ended early')
                while True:
                    event = next_event()
                    if event['type'] == 'partial' and any(row['before'] or row['after'] for row in event['rows']): break
                self.assertTrue(both_started.wait(3), 'Separate segments start concurrently')
                self.assertFalse(any(event['type'] == 'rows' for event in events))
                fast.set()
                while next_event()['type'] != 'rows': pass
                with urlopen(base + '?candidate=' + selection['candidate']) as state:
                    saved = json.load(state)
                self.assertEqual(len(saved['translations']), 1)
                self.assertTrue(saved['busy'])
                self.assertFalse(slow.is_set())
                slow.set()
                while True:
                    event = next_event()
                    self.assertNotEqual(event['type'], 'error', event)
                    if event['type'] == 'complete': break
                self.assertEqual(len(event['session']['translations']), 2)
                self.assertTrue(all(packet['stream'] for packet in packets))
        finally:
            fast.set(); slow.set()
            close_jobs(server); server.shutdown(); server.server_close()
            provider.shutdown(); provider.server_close()

    def test_http_entry_point_uses_configured_api_and_rejects_gui_model_overrides(self):
        calls = []
        outer = self
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.path, request))
                response = outer.translated(request, {}, 'responses', [])['response']
                raw = json.dumps(response).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(raw)
        provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        threading.Thread(target=provider.serve_forever, daemon=True).start()
        self.config['llm'].update(base_url=f'http://127.0.0.1:{provider.server_port}/api/v3', api='responses')
        atomic_json(Path(self.temp.name) / 'config.json', self.config)
        server = make_server(self.workbench)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        route = f'http://127.0.0.1:{server.server_port}/ablation/api/experiments/{self.eid}/translation'
        try:
            with urlopen(route + '?candidate=' + self.selection['candidate']) as response:
                session = json.load(response)
            options = {**self.selection, 'session_id': session['id']}
            def post(body, origin=None):
                headers = {'Content-Type': 'application/json', 'X-Ablation-Client': 'local'}
                if origin: headers['Origin'] = origin
                return urlopen(Request(route, data=json.dumps(body).encode(), headers=headers))
            with self.assertRaises(HTTPError) as error: post({**options, 'model': 'gui-override'})
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(calls, [])
            with post(options) as response: result = json.load(response)
            self.assertEqual(len(result['translations']), 2)
            self.assertEqual(calls[0][0], '/api/v3/responses')
            self.assertEqual(calls[0][1]['model'], 'translation-model')
            with self.assertRaises(HTTPError) as error: post(options, 'https://other.example')
            self.assertEqual(error.exception.code, 403)
            with urlopen(f'http://127.0.0.1:{server.server_port}/ablation/') as response:
                html = response.read().decode()
                self.assertNotIn('assistant-panel', html)
                self.assertIn('/translation.js', html)
        finally:
            close_jobs(server); server.shutdown(); server.server_close()
            provider.shutdown(); provider.server_close()
