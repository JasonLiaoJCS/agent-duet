"""Regression tests for the last Codex check before 3.1."""
import isolate  # noqa: F401  (must come first)
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent-duet/scripts'))
import duet
import install
import native

LIMIT = "You've hit your 5-hour limit · resets 11pm (Asia/Taipei)"


class Pair(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = patch.dict(os.environ, {'AGENT_DUET_HOME': self.home.name})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.base = {'root': str(self.root)}
        r = self.call('join', name='c', provider='codex', session=str(uuid.uuid4()), project_name='P', goal='G')
        self.base['project_id'] = r['project']['id']
        self.call('join', name='a', provider='claude', session=str(uuid.uuid4()), address='p [1]', title='p', project_name='P', goal='G')

    def call(self, op, **kw):
        return duet.dispatch(op, self.base | kw)


class BudgetTests(Pair):
    def test_retries_of_an_unsent_wake_cannot_exceed_the_courier_budget(self):
        self.call('native_start', name='c', peer='a', courier_budget_usd=0.03)
        m = self.call('send', sender='c', recipient='a', body='x')['message_id']
        granted = []

        def courier(d, root, address, title, text, budget=0.10, session=None):
            granted.append(budget)
            return {'status': 'not_sent', 'response': 'no such session', 'reported_cost_usd': 0.02, 'exit_code': 0}
        with patch.object(native, 'relay_claude', side_effect=courier):
            results = [self.call('native_wake', name='c', message_id=m)['status'] for _ in range(6)]
        self.assertIn('courier_budget_reached', results)                  # retrying does not reset the budget
        self.assertLessEqual(len(granted) * 0.02, 0.03 + 0.02)            # at most one call past the threshold
        for later in granted[1:]:
            self.assertLessEqual(later, 0.011)                            # a later grant is what is left, not a fresh one


class ProbeRaceTests(Pair):
    def test_a_finish_during_the_codex_capability_probe_stops_the_wake(self):
        m = self.call('send', sender='a', recipient='c', body='go')['message_id']
        self.call('native_start', name='a', peer='c')
        calls = []

        def run(command, **kw):
            calls.append(command)
            if 'queue' in command and '--help' in command:
                other = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
                other.execute("UPDATE native_run SET state='completed'")   # the user finishes while the probe runs
                other.commit()
                other.close()
                return type('R', (), dict(returncode=0, stdout='--thread --message --resume', stderr=''))()
            return type('R', (), dict(returncode=0, stdout='Queued', stderr=''))()
        with patch.object(duet, 'cli', return_value=['codex']), patch.object(native.subprocess, 'run', side_effect=run):
            result = self.call('native_wake', name='a', message_id=m)
        self.assertEqual(result['status'], 'not_sent')
        self.assertFalse(any('--thread' in c for c in calls))


class UpgradeCompatibilityTests(Pair):
    def test_a_result_written_by_3_0_is_still_recognised_as_the_same_retry(self):
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        db.execute("INSERT INTO messages(sender,recipient,kind,body,dedupe,created,footer) VALUES('c','a','result',?,'r1',?,NULL)",
                   ('done\n\n[git: main@abc1234]', time.time()))                # how 3.0 stored it
        db.commit()
        db.close()
        self.assertTrue(self.call('send', sender='c', recipient='a', body='done', kind='result', dedupe='r1', git=False)['duplicate'])

    def test_a_new_message_without_a_footer_is_never_taken_for_one(self):
        self.call('send', sender='c', recipient='a', body='note\n\n[git: main@abc1234]', kind='result', git=False, dedupe='r2')
        with self.assertRaisesRegex(ValueError, 'dedupe key reused'):
            self.call('send', sender='c', recipient='a', body='note', kind='result', git=False, dedupe='r2')


class ModelLimitTests(Pair):
    def test_a_limit_on_one_model_steps_down_instead_of_blocking_the_vendor(self):
        text = "You've reached your Opus weekly limit. Sonnet is still available."
        self.assertIsNone(duet.account_limit(text))
        self.assertTrue(duet.not_for_this_account(text))
        script = self.root / 'fake.py'
        script.write_text('import sys, json\nmodel = sys.argv[sys.argv.index("--model") + 1]\nsys.stdin.read()\n'
                          'if model == "opus":\n    print(json.dumps({"result": %r, "is_error": True, "usage": {"output_tokens": 0}}))\n    sys.exit(1)\n'
                          'print(json.dumps({"result": "ok from " + model, "modelUsage": {"claude-" + model + "-4": {}}}))\n' % text, encoding='utf-8')
        with patch.object(duet, 'popen', return_value=type('P', (), {'pid': 1})()), patch.object(duet, 'process_birth', return_value='b'):
            job = self.call('consult', requester='a', provider='claude', prompt='x')['job_id']
        with patch.object(duet, 'cli', return_value=[sys.executable, str(script)]):
            duet.run_worker(str(self.root), job)
        result = self.call('job', job_id=job)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['result']['answer'], 'ok from sonnet')
        self.assertIsNone(duet.account_limited('claude'))
        with patch.object(duet, 'popen', return_value=type('P', (), {'pid': 1})()), patch.object(duet, 'process_birth', return_value='b'):
            self.assertTrue(self.call('consult', requester='a', provider='claude', prompt='again')['job_id'])

    def test_a_limit_on_stderr_is_read_even_when_the_json_has_no_result(self):
        script = self.root / 'fake2.py'
        script.write_text('import sys, json\nsys.stdin.read()\nsys.stderr.write(%r)\nprint(json.dumps({"is_error": True}))\nsys.exit(1)\n' % LIMIT,
                          encoding='utf-8')
        with patch.object(duet, 'popen', return_value=type('P', (), {'pid': 1})()), patch.object(duet, 'process_birth', return_value='b'):
            job = self.call('consult', requester='a', provider='claude', prompt='x')['job_id']
        with patch.object(duet, 'cli', return_value=[sys.executable, str(script)]):
            duet.run_worker(str(self.root), job)
        self.assertIsNotNone(duet.account_limited('claude'))


class CourierTests(Pair):
    def test_a_courier_that_ended_normally_never_blocks_claude(self):
        m = self.call('send', sender='c', recipient='a', body='x')['message_id']
        reply = {'status': 'not_sent', 'exit_code': 0, 'is_error': False, 'reported_cost_usd': 0.01,
                 'response': "No session titled \"You've hit your 5-hour limit\" was found."}
        with patch.object(native, 'relay_claude', return_value=reply):
            self.call('native_wake', name='c', message_id=m)
        self.assertIsNone(duet.account_limited('claude'))

    def test_a_courier_that_claude_itself_refused_does(self):
        m = self.call('send', sender='c', recipient='a', body='x')['message_id']
        with patch.object(native, 'relay_claude', return_value={'status': 'not_sent', 'exit_code': 1, 'is_error': True, 'response': LIMIT, 'reported_cost_usd': 0}):
            self.call('native_wake', name='c', message_id=m)
        self.assertIsNotNone(duet.account_limited('claude'))


class UninstallTests(unittest.TestCase):
    def test_a_similar_command_line_is_not_ours(self):
        for other in ('node /x/skills/agent-duet/scripts/duet.py.backup mcp-helper', 'python /x/skills/agent-duet/scripts/duet.py mcp-helper',
                      'python /x/skills/agent-duet/scripts/duet.pyc mcp'):
            self.assertFalse(install.ours(other), other)
        self.assertTrue(install.ours('python /x/skills/agent-duet/scripts/duet.py mcp'))
        self.assertTrue(install.ours('{"args": ["C:\\\\u\\\\.claude\\\\skills\\\\agent-duet\\\\scripts\\\\duet.py", "mcp"]}'))


if __name__ == '__main__':
    unittest.main()
