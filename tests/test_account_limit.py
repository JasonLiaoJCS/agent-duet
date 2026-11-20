"""An account-wide usage limit (a 5-hour, daily or weekly cap) is not a model problem: say when it resets,
point at the other vendor, and do not spend calls that are certain to fail."""
import isolate  # noqa: F401  (must come first)
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent-duet/scripts'))
import duet
import native

LIMIT = "You've hit your 5-hour limit · resets 11pm (Asia/Taipei)"


class Home(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        env = patch.dict(os.environ, {'AGENT_DUET_HOME': self.home.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.home.cleanup)


class ClassifierTests(unittest.TestCase):
    def test_account_limits_are_recognised_with_their_reset_time(self):
        self.assertEqual(duet.account_limit(LIMIT), '11pm (Asia/Taipei)')
        self.assertEqual(duet.account_limit('You have reached your weekly limit. It resets Monday 9:00'), 'Monday 9:00')
        self.assertEqual(duet.account_limit('Daily limit reached'), '')

    def test_task_text_and_model_refusals_are_not_account_limits(self):
        for text in ('tests about rate limit handling failed', 'The model fable is not available on your plan',
                     'Failed to authenticate: OAuth session expired', '', None):
            self.assertIsNone(duet.account_limit(text), text)
        self.assertFalse(duet.not_for_this_account(LIMIT))      # never a reason to step down a model


class WorkerTests(Home):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        r = duet.dispatch('join', dict(root=str(self.root), name='me', provider='claude', session=str(uuid.uuid4()), project_name='P', goal='G'))
        self.base = {'root': str(self.root), 'project_id': r['project']['id']}

    def consult(self, provider='claude'):
        with patch.object(duet, 'popen', return_value=type('P', (), {'pid': 1})()), patch.object(duet, 'process_birth', return_value='b'):
            return duet.dispatch('consult', self.base | dict(requester='me', provider=provider, prompt='review this'))['job_id']

    def test_a_limited_job_says_when_it_resets_and_the_next_call_is_not_spent(self):
        script = self.root / 'fake.py'
        script.write_text('import sys, json\nsys.stdin.read()\n'
                          'print(json.dumps({"result": %r, "is_error": True, "usage": {"output_tokens": 0}}))\nsys.exit(1)\n' % LIMIT,
                          encoding='utf-8')
        job = self.consult()
        with patch.object(duet, 'cli', return_value=[sys.executable, str(script)]), patch.object(duet, 'next_route') as step_down:
            duet.run_worker(str(self.root), job)
        step_down.assert_not_called()                                    # another Claude model would hit the same limit
        result = duet.dispatch('job', self.base | {'job_id': job})['result']
        self.assertEqual(result['account_limit'], {'resets': '11pm (Asia/Taipei)'})
        self.assertIn('provider=codex', result['hint'])
        self.assertIn('resets 11pm', result['hint'])
        with patch.object(duet, 'popen') as start, self.assertRaisesRegex(ValueError, 'usage limit'):
            duet.dispatch('consult', self.base | dict(requester='me', provider='claude', prompt='again'))
        start.assert_not_called()
        self.assertTrue(self.consult('codex'))                           # the other vendor still works

    def test_the_pause_ends_by_itself(self):
        duet.mark_account_limited('claude', LIMIT, '11pm')
        self.assertIsNotNone(duet.account_limited('claude'))
        duet.change_models_state(lambda state: state['account_limit']['claude'].update(at=time.time() - 31 * 60))
        self.assertIsNone(duet.account_limited('claude'))

    def test_doctor_reports_it(self):
        duet.mark_account_limited('claude', LIMIT, '11pm (Asia/Taipei)')
        with patch.object(duet, 'cli', side_effect=lambda provider: [provider]), \
             patch.object(duet.subprocess, 'run', return_value=type('R', (), dict(returncode=0, stdout='9.9.999', stderr=''))()), \
             patch.object(duet, 'models_check_due', return_value=False):
            report = duet.doctor()
        self.assertEqual(report['providers']['claude']['account_limit'], '11pm (Asia/Taipei)')
        self.assertTrue(any('usage limit' in problem for problem in report['problems']))


class WakeTests(Home):
    def pair(self, folder):
        base = {'root': folder}
        r = duet.dispatch('join', base | dict(name='c', provider='codex', session=str(uuid.uuid4()), project_name='P', goal='G'))
        base['project_id'] = r['project']['id']
        duet.dispatch('join', base | dict(name='a', provider='claude', session=str(uuid.uuid4()), address='p [1]', title='p', project_name='P', goal='G'))
        return base, duet.dispatch('send', base | dict(sender='c', recipient='a', body='review please'))['message_id']

    def test_a_wake_to_claude_during_the_limit_costs_nothing_and_can_be_repeated(self):
        with tempfile.TemporaryDirectory() as folder:
            base, m = self.pair(folder)
            duet.mark_account_limited('claude', LIMIT, '11pm')
            with patch.object(native, 'relay_claude') as courier:
                first = duet.dispatch('native_wake', base | dict(name='c', message_id=m))
            courier.assert_not_called()
            self.assertEqual(first['status'], 'not_sent')
            self.assertIn('usage limit', first['error'])
            duet.change_models_state(lambda state: state.pop('account_limit'))
            with patch.object(native, 'relay_claude', return_value={'status': 'attempted', 'reported_cost_usd': 0.01}):
                self.assertEqual(duet.dispatch('native_wake', base | dict(name='c', message_id=m))['status'], 'attempted')

    def test_a_courier_that_hits_the_limit_records_it(self):
        with tempfile.TemporaryDirectory() as folder:
            base, m = self.pair(folder)
            with patch.object(native, 'relay_claude', return_value={'status': 'not_sent', 'response': LIMIT, 'is_error': True, 'exit_code': 1, 'reported_cost_usd': 0}):
                result = duet.dispatch('native_wake', base | dict(name='c', message_id=m))
            self.assertEqual(result['account_limit'], {'resets': '11pm (Asia/Taipei)'})
            self.assertIsNotNone(duet.account_limited('claude'))


if __name__ == '__main__':
    unittest.main()
