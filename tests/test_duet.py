import isolate  # noqa: F401  (must come first)
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1]/'agent-duet/scripts/duet.py'
spec = importlib.util.spec_from_file_location('duet', SCRIPT)
duet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(duet)

class DuetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='duet tests 空白 ')
        self.root = Path(self.temp.name)
        self.base = {'root': str(self.root)}
        for name, provider in [('c', 'codex'), ('a', 'claude')]:
            result = duet.dispatch('join', self.base | {'name': name, 'provider': provider, 'session': name, 'project_name': 'Test', 'goal': 'Prove correctness'})
        self.base['project_id'] = result['project']['id']
    def tearDown(self):
        self.temp.cleanup()
    def call(self, op, **kwargs):
        return duet.dispatch(op, self.base | kwargs)
    def test_roundtrip_ack_and_dedupe(self):
        payload = dict(sender='c', recipient='a', body='中文 `literal` $(not-shell)', dedupe='task1')
        first = self.call('send', **payload)
        self.assertEqual(first['message_id'], self.call('send', **payload)['message_id'])
        self.assertEqual(len(self.call('inbox', name='a')['messages']), 1)
        self.assertEqual(len(self.call('inbox', name='a')['messages']), 1)
        self.call('ack', name='c', ids=[first['message_id']])
        self.assertEqual(len(self.call('inbox', name='a')['messages']), 1)
        self.call('send', sender='a', recipient='c', body='Confirmed', reply_to=first['message_id'])
        self.call('ack', name='a', ids=[first['message_id']])
        self.assertEqual(self.call('inbox', name='a')['messages'], [])
    def test_duplicate_key_content_conflict(self):
        self.call('send', sender='c', recipient='a', body='A', dedupe='key')
        with self.assertRaises(ValueError):
            self.call('send', sender='c', recipient='a', body='B', dedupe='key')
    def test_participant_collision(self):
        with self.assertRaises(ValueError):
            self.call('join', name='c', provider='codex', session='other', project_name='Test', goal='goal')
    def test_project_id_rejects_other_project(self):
        with self.assertRaisesRegex(ValueError, 'Project ID mismatch'):
            self.call('send', project_id='another-project', sender='c', recipient='a', body='Oops')
        self.assertEqual(self.call('inbox', name='a')['messages'], [])
    def test_copied_state_rejected(self):
        second = self.root/'copy'
        second.mkdir()
        shutil.copytree(self.root/'.agent-duet', second/'.agent-duet')
        with self.assertRaisesRegex(ValueError, 'different root'):
            duet.dispatch('status', {'root': str(second)})
    def test_unknown_recipient_and_reply_rejected(self):
        with self.assertRaises(ValueError):
            self.call('send', sender='c', recipient='absent', body='Hi')
        with self.assertRaises(ValueError):
            self.call('send', sender='c', recipient='a', body='Hi', reply_to=1234)
    def test_concurrent_lease_single_owner(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda name: self.call('claim', name=name, resource='file:x.py'), ['c', 'a']))
        self.assertEqual(sum(r['acquired'] for r in outcomes), 1)
        owner = self.call('status')['claims'][0]['owner']
        self.assertFalse(self.call('release', name='a' if owner == 'c' else 'c', resource='file:x.py')['released'])
        self.assertTrue(self.call('claim', name=owner, resource='file:x.py')['acquired'])
        self.assertTrue(self.call('release', name=owner, resource='file:x.py')['released'])
    def test_concurrent_messages_not_lost(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: self.call('send', sender='c', recipient='a', body=str(n), dedupe=str(n)), range(25)))
        self.assertEqual(len(self.call('inbox', name='a', limit=100)['messages']), 25)
    def test_concurrent_duplicate_retry(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            replies=list(pool.map(lambda _: self.call('send',sender='c',recipient='a',body='one',dedupe='same'),range(20)))
        self.assertEqual(len({r['message_id'] for r in replies}),1)
    def test_checkpoint_and_goal_update(self):
        self.call('checkpoint',name='c',summary='Parser done',next_steps='Test Unicode',evidence='unit tests passed')
        self.assertEqual(self.call('status')['latest_checkpoint']['summary'],'Parser done')
        self.call('set_goal',name='c',goal='Next feature',expected_goal='Prove correctness')
        self.assertEqual(self.call('status')['project']['goal'],'Next feature')
        with self.assertRaises(ValueError):
            self.call('set_goal',name='a',goal='Stale update',expected_goal='Prove correctness')
    def test_cross_project_workdir_rejected(self):
        other=self.root.parent
        with self.assertRaises(ValueError):
            duet.valid_workdir(self.root,str(other))
        child=self.root/'child'
        child.mkdir()
        self.assertEqual(duet.valid_workdir(self.root,str(child)),child.resolve())
    def test_file_snapshot_preserves_unicode_and_rejects_escape(self):
        (self.root/'sample.txt').write_text('正確\n$(literal)',encoding='utf-8')
        context=duet.file_context(self.root,self.root,['sample.txt'])
        self.assertIn('正確',context)
        with self.assertRaises(ValueError):
            duet.file_context(self.root,self.root,[str(SCRIPT)])
        (self.root/'large.txt').write_text('x'*200001)
        with self.assertRaises(ValueError):
            duet.file_context(self.root,self.root,['large.txt'])
    def test_empty_session_name_reuse_rejected(self):
        self.call('join',name='new',provider='codex',project_name='Test',goal='Prove correctness')
        with self.assertRaises(ValueError):
            self.call('join',name='new',provider='codex',project_name='Test',goal='Prove correctness')
    def test_numeric_model_selection_and_effort(self):
        models = [{'id': slug, 'model': slug, 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]} for slug in ('gpt-6.9-sol','gpt-6.10-sol','gpt-7-astra')]
        self.assertEqual(duet.choose_codex(models, 'standard', 'high'), 'gpt-6.10-sol')
        self.assertEqual(duet.choose_codex(models, 'hard', 'high'), 'gpt-7-astra')
        with self.assertRaises(ValueError):
            duet.choose_codex(models, 'hard', 'max')
        with self.assertRaises(RuntimeError):
            duet.choose_codex([], 'standard', 'high')
    def test_alias_pin_detected(self):
        with patch.dict(os.environ, {'ANTHROPIC_DEFAULT_OPUS_MODEL': 'old'}):
            with self.assertRaises(RuntimeError):
                duet.route('claude', 'standard', 'high')
    def test_escalation_and_recursion_rejected(self):
        args = dict(requester='c', provider='claude', prompt='test')
        with self.assertRaises(ValueError):
            self.call('consult', **args, tier='hard')
        with patch.dict(os.environ, {'AGENT_DUET_DEPTH': '1'}):
            with self.assertRaises(ValueError):
                self.call('consult', **args)
    def create_job(self, provider='claude', timeout=10):
        root, state, db = duet.connect(self.root)
        with db:
            db.execute('INSERT INTO jobs(id,requester,provider,tier,effort,prompt,reason,status,created,updated,timeout) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       ('testjob','c',provider,'standard','high','Hello `x` $(echo bad)','', 'queued',time.time(),time.time(),timeout))
        db.close()
    def fake_cli(self, text):
        fake = self.root/'fake.py'
        fake.write_text(text, encoding='utf-8')
        return [sys.executable, str(fake)]
    def test_worker_success_and_usage(self):
        self.create_job()
        command = self.fake_cli('import sys,json\np=sys.stdin.read()\nprint(json.dumps({"result":p[-25:],"modelUsage":{"claude-opus-new":{}},"usage":{"input_tokens":10}}))')
        with patch.object(duet, 'cli', return_value=command), patch.object(duet, 'route', return_value={'model':'opus'}):
            duet.run_worker(self.root, 'testjob')
        job = self.call('job', job_id='testjob')
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['result']['actual_models'], ['claude-opus-new'])
        self.assertIn('$(echo bad)', job['result']['answer'])
    def test_worker_provider_error(self):
        self.create_job()
        command = self.fake_cli('import json\nprint(json.dumps({"result":"No auth","is_error":True}))')
        with patch.object(duet, 'cli', return_value=command), patch.object(duet, 'route', return_value={'model':'opus'}):
            duet.run_worker(self.root, 'testjob')
        self.assertEqual(self.call('job', job_id='testjob')['status'], 'failed')
    def test_non_json_error_diagnostics_preserved(self):
        self.create_job()
        command=self.fake_cli('import sys\nprint("bad flag",file=sys.stderr)\nsys.exit(2)')
        with patch.object(duet,'cli',return_value=command),patch.object(duet,'route',return_value={'model':'opus'}):
            duet.run_worker(self.root,'testjob')
        job=self.call('job',job_id='testjob')
        self.assertEqual(job['status'],'failed')
        self.assertEqual(job['result']['exit_code'],2)
        self.assertIn('bad flag',job['result']['stderr_tail'])
    def test_model_family_mismatch_fails(self):
        self.create_job()
        command=self.fake_cli('import json\nprint(json.dumps({"result":"done","modelUsage":{"claude-sonnet-99":{}}}))')
        with patch.object(duet,'cli',return_value=command),patch.object(duet,'route',return_value={'model':'opus'}):
            duet.run_worker(self.root,'testjob')
        job=self.call('job',job_id='testjob')
        self.assertEqual(job['status'],'failed')
        self.assertTrue(job['result']['model_mismatch'])
    def test_interrupted_worker_recovery(self):
        self.create_job()
        _,_,db=duet.connect(self.root)
        with db:
            db.execute("UPDATE jobs SET worker_pid=123,worker_birth='old',child_pid=456,child_birth='old'")
        db.close()
        with patch.object(duet,'process_birth',return_value=None):
            job=self.call('job',job_id='testjob')
        self.assertEqual(job['status'],'interrupted')
    def test_live_orphan_not_falsely_completed(self):
        self.create_job()
        _,_,db=duet.connect(self.root)
        with db:
            db.execute("UPDATE jobs SET worker_pid=123,worker_birth='old',child_pid=456,child_birth='child'")
        db.close()
        with patch.object(duet,'process_birth',side_effect=lambda pid: 'child' if pid==456 else None):
            job=self.call('job',job_id='testjob')
        self.assertEqual(job['status'],'orphaned')
    def test_process_identity_current(self):
        birth=duet.process_birth(os.getpid())
        self.assertTrue(duet.alive_identity(os.getpid(),birth))
        self.assertFalse(duet.alive_identity(os.getpid(),'definitely-not-this-process'))
    def test_cancel_before_start_avoids_inference(self):
        self.create_job()
        self.call('cancel',job_id='testjob')
        with patch.object(duet,'route',side_effect=AssertionError('must not infer')):
            duet.run_worker(self.root,'testjob')
        self.assertEqual(self.call('job',job_id='testjob')['status'],'cancelled')
    def test_continuation_includes_previous_evidence(self):
        self.create_job()
        _,_,db=duet.connect(self.root)
        duet.job_update(db,'testjob','completed',{'answer':'The counterexample is x=0'})
        db.close()
        from types import SimpleNamespace
        with patch.object(duet,'popen',return_value=SimpleNamespace(pid=os.getpid())):
            response=self.call('consult',requester='c',provider='claude',prompt='Fix that case',previous_job_id='testjob',role='debugger')
        _,_,db=duet.connect(self.root)
        prompt=db.execute('SELECT prompt FROM jobs WHERE id=?',(response['job_id'],)).fetchone()[0]
        db.close()
        self.assertIn('The counterexample is x=0',prompt)
        self.assertIn('Fix that case',prompt)
    def test_concurrent_admission_stops_at_two(self):
        from types import SimpleNamespace
        def submit(_):
            try:
                self.call('consult',requester='c',provider='claude',prompt='Test')
                return True
            except ValueError as exc:
                self.assertIn('At most two',str(exc))
                return False
        with patch.object(duet,'popen',return_value=SimpleNamespace(pid=os.getpid())):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results=list(pool.map(submit,range(4)))
        self.assertEqual(sum(results),2)
    def test_model_discovery_timeout_status(self):
        self.create_job(provider='codex')
        with patch.object(duet,'route',side_effect=TimeoutError('deadline')):
            duet.run_worker(self.root,'testjob')
        self.assertEqual(self.call('job',job_id='testjob')['status'],'timed_out')
    def test_full_work_mode_applies_user_authorized_flags(self):
        for provider in ('claude','codex'):
            with self.subTest(provider=provider):
                _,_,db=duet.connect(self.root)
                with db:
                    db.execute('DELETE FROM jobs')
                db.close()
                self.create_job(provider=provider)
                _,_,db=duet.connect(self.root)
                with db:
                    db.execute("UPDATE jobs SET mode='work'")
                db.close()
                fake='import sys,json,pathlib\na=sys.argv[1:]\n'
                if provider=='claude':
                    fake+='print(json.dumps({"result":json.dumps(a),"modelUsage":{"claude-opus-new":{}}}))'
                else:
                    fake+='pathlib.Path(a[a.index("-o")+1]).write_text(json.dumps(a),encoding="utf-8")\nprint(json.dumps({"type":"turn.completed"}))'
                command=self.fake_cli(fake)
                with patch.object(duet,'cli',return_value=command),patch.object(duet,'route',return_value={'model':'opus' if provider=='claude' else 'gpt-6.1-sol'}):
                    duet.run_worker(self.root,'testjob')
                job=self.call('job',job_id='testjob')
                self.assertEqual(job['status'],'completed')
                argv=json.loads(job['result']['answer'])
                flag='--dangerously-skip-permissions' if provider=='claude' else '--dangerously-bypass-approvals-and-sandbox'
                self.assertIn(flag,argv)
                self.assertNotIn('--safe-mode',argv)
                self.assertNotIn('read-only',argv)
    def test_cancellation(self):
        self.create_job()
        command = self.fake_cli('import time\ntime.sleep(60)')
        with patch.object(duet, 'cli', return_value=command), patch.object(duet, 'route', return_value={'model':'opus'}):
            worker = threading.Thread(target=duet.run_worker, args=(self.root, 'testjob'))
            worker.start()
            end = time.time()+8
            while self.call('job', job_id='testjob')['status'] != 'running' and time.time()<end:
                time.sleep(.05)
            self.call('cancel', job_id='testjob')
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.call('job', job_id='testjob')['status'], 'cancelled')
    def test_mcp_protocol_roundtrip(self):
        messages = [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26'}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'duet_send','arguments':self.base | {'sender':'c','recipient':'a','body':'MCP hello'}}},
            {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'duet_inbox','arguments':self.base | {'name':'a'}}}]
        proc = subprocess.run([sys.executable,str(SCRIPT),'mcp'], input='\n'.join(json.dumps(m) for m in messages)+'\n', capture_output=True,text=True,encoding='utf-8',timeout=10)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(len(responses),4)
        self.assertEqual(responses[0]['result']['protocolVersion'],'2025-03-26')
        self.assertIn('duet_send', {tool['name'] for tool in responses[1]['result']['tools']})
        inbox = json.loads(responses[3]['result']['content'][0]['text'])
        self.assertEqual(inbox['messages'][0]['body'],'MCP hello')

if __name__ == '__main__':
    unittest.main(verbosity=2)
