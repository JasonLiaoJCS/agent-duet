"""Not every account can use every model. The best one it CAN use must run, and the result must say so."""
import isolate  # noqa: F401  (must come first)
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent-duet/scripts'))
import duet

EFFORTS = [{'reasoningEffort': e} for e in ('medium', 'high', 'xhigh', 'max')]


def model(slug, description='Latest model.', efforts=EFFORTS):
    return {'id': slug, 'model': slug, 'description': description, 'hidden': False, 'supportedReasoningEfforts': efforts}


FULL = [model('gpt-6.2-sol'), model('gpt-6.2-astra'), model('gpt-6-luna')]
NO_FLAGSHIP = [model('gpt-6.2-sol'), model('gpt-6-luna')]


class Home(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        env = patch.dict(os.environ, {'AGENT_DUET_HOME': self.home.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.home.cleanup)


class ChoiceTests(Home):
    def test_plan_without_the_flagship_gets_the_everyday_model_and_is_told(self):
        with patch.object(duet, 'codex_models', return_value=NO_FLAGSHIP):
            routing = duet.route('codex', 'hard', 'high')
        self.assertEqual(routing['model'], 'gpt-6.2-sol')
        self.assertEqual(routing['fallback']['wanted'], 'astra')
        self.assertIn('not available to this account', routing['fallback']['tell_the_user'])

    def test_full_plan_is_untouched(self):
        with patch.object(duet, 'codex_models', return_value=FULL):
            self.assertEqual(duet.route('codex', 'hard', 'high')['model'], 'gpt-6.2-astra')
            self.assertNotIn('fallback', duet.route('codex', 'standard', 'high'))
        self.assertNotIn('fallback', duet.route('claude', 'hard', 'high'))

    def test_a_refusal_is_remembered_for_a_day_so_no_call_is_wasted_then_tried_again(self):
        duet.mark_unavailable('claude', 'fable', 'model not available on your plan')
        routing = duet.route('claude', 'hard', 'high')
        self.assertEqual((routing['model'], routing['fallback']['wanted']), ('opus', 'fable'))
        self.assertEqual(duet.route('claude', 'standard', 'high')['model'], 'opus')
        with patch.object(duet, 'codex_models', return_value=FULL), patch.object(duet, 'installed_version', return_value='cli 1.0.0'):
            self.assertEqual(duet.models_report({})['not_available_to_this_account']['claude'], ['fable'])
        duet.change_models_state(lambda state: state['unavailable']['claude']['fable'].update(at=duet.time.time() - 25 * 3600))
        self.assertEqual(duet.route('claude', 'hard', 'high')['model'], 'fable')

    def test_claude_steps_down_one_model_at_a_time_and_stops_at_the_last(self):
        first = duet.route('claude', 'hard', 'high')
        second = duet.next_route('claude', first, 'hard', 'high', 'model not available')
        third = duet.next_route('claude', second, 'hard', 'high', 'model not available')
        self.assertEqual([first['model'], second['model'], third['model']], ['fable', 'opus', 'sonnet'])
        self.assertEqual(third['fallback']['wanted'], 'fable')
        self.assertIsNone(duet.next_route('claude', third, 'hard', 'high', 'model not available'))

    def test_effort_the_stand_in_cannot_do_is_skipped_not_forced(self):
        catalog = [model('gpt-6.2-sol', efforts=[{'reasoningEffort': 'high'}]), model('gpt-6-luna')]
        with patch.object(duet, 'codex_models', return_value=catalog):
            self.assertEqual(duet.route('codex', 'hard', 'high')['model'], 'gpt-6.2-sol')
            with self.assertRaisesRegex(ValueError, 'effort'):
                duet.route('codex', 'standard', 'max')

    def test_what_counts_as_not_for_this_account(self):
        for text in ('The model gpt-6.2-astra is not available on your plan', 'Your organization does not have access to this model',
                     "You've hit your usage limit for GPT-6.2 Astra", 'error: unknown model fable', 'model_not_found',
                     'This model requires a subscription upgrade', 'HTTP 429 Too Many Requests'):
            self.assertTrue(duet.not_for_this_account(text), text)
        for text in ('Failed to authenticate: OAuth session expired', 'Not logged in', 'SyntaxError: invalid syntax',
                     'tests failed: 3 errors', '', None):
            self.assertFalse(duet.not_for_this_account(text), text)


class WorkerTests(Home):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        r = duet.dispatch('join', dict(root=str(self.root), name='me', provider='claude', session=str(uuid.uuid4()), project_name='P', goal='G'))
        self.base = {'root': str(self.root), 'project_id': r['project']['id']}

    def fake_cli(self, text):
        script = self.root / ('fake-%s.py' % uuid.uuid4().hex)
        script.write_text(text, encoding='utf-8')
        return [sys.executable, str(script)]

    def consult(self, provider, tier):
        with patch.object(duet, 'popen', return_value=type('P', (), {'pid': 1})()), patch.object(duet, 'process_birth', return_value='b'):
            args = dict(requester='me', provider=provider, prompt='review this', tier=tier)
            if tier == 'hard':
                args['reason'] = 'deep bug'
            return duet.dispatch('consult', self.base | args)['job_id']

    def test_claude_job_refused_for_the_flagship_reruns_on_the_everyday_model(self):
        cli = self.fake_cli(
            'import sys, json\n'
            'model = sys.argv[sys.argv.index("--model") + 1]\n'
            'sys.stdin.read()\n'
            'if model == "fable":\n'
            '    print(json.dumps({"result": "The model fable is not available on your plan", "is_error": True}))\n'
            '    sys.exit(1)\n'
            'print(json.dumps({"result": "reviewed with " + model, "modelUsage": {"claude-" + model + "-5-5": {}}}))\n')
        job = self.consult('claude', 'hard')
        with patch.object(duet, 'cli', return_value=cli):
            duet.run_worker(str(self.root), job)
        result = duet.dispatch('job', self.base | {'job_id': job})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['result']['answer'], 'reviewed with opus')
        self.assertEqual(result['result']['routing']['fallback']['wanted'], 'fable')
        self.assertEqual(result['result']['actual_models'], ['claude-opus-5-5'])
        self.assertIn('fable', duet.unavailable_families('claude'))          # the next hard job goes straight to opus
        self.assertEqual(duet.route('claude', 'hard', 'high')['model'], 'opus')

    def test_an_ordinary_failure_is_not_retried_on_another_model(self):
        cli = self.fake_cli('import sys, json\nsys.stdin.read()\nprint(json.dumps({"result": "tests failed: 3 errors", "is_error": True}))\nsys.exit(1)\n')
        job = self.consult('claude', 'hard')
        with patch.object(duet, 'cli', return_value=cli), patch.object(duet, 'next_route') as step_down:
            duet.run_worker(str(self.root), job)
        step_down.assert_not_called()
        result = duet.dispatch('job', self.base | {'job_id': job})
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('fallback', result['result']['routing'])
        self.assertEqual(duet.unavailable_families('claude'), set())

    def test_when_nothing_is_usable_the_job_fails_with_the_real_reason(self):
        cli = self.fake_cli('import sys, json\nsys.stdin.read()\nprint(json.dumps({"result": "Your organization does not have access to this model", "is_error": True}))\nsys.exit(1)\n')
        job = self.consult('claude', 'standard')
        with patch.object(duet, 'cli', return_value=cli):
            duet.run_worker(str(self.root), job)
        result = duet.dispatch('job', self.base | {'job_id': job})
        self.assertEqual(result['status'], 'failed')
        self.assertIn('does not have access', result['result']['error'])
        self.assertEqual(result['result']['routing']['model'], 'sonnet')    # it did try the next ones first


class SlotTests(Home):
    def test_only_a_few_clis_run_at_once_and_a_dead_holder_is_reclaimed(self):
        with patch.dict(os.environ, {'AGENT_DUET_MAX_WORKERS': '2'}):
            order = []
            with duet.worker_slot(lambda: None) as one, duet.worker_slot(lambda: None) as two:
                self.assertNotEqual(one.path, two.path)

                def third():
                    with duet.worker_slot(lambda: None):
                        order.append('third ran')
                waiting = threading.Thread(target=third)
                with patch.object(duet.time, 'sleep', side_effect=lambda seconds: threading.Event().wait(0.05)):
                    waiting.start()
                    threading.Event().wait(0.3)
                    self.assertEqual(order, [])                          # it waits while both slots are taken
                    two.__exit__(None, None, None)
                    waiting.join(timeout=5)
                self.assertEqual(order, ['third ran'])
            slots = Path(self.home.name) / 'slots'
            (slots / 'slot-0').write_text('999999 gone', encoding='utf-8')   # a worker that crashed
            (slots / 'slot-1').write_text('999998 gone', encoding='utf-8')
            with patch.object(duet, 'alive_identity', return_value=False), duet.worker_slot(lambda: None) as slot:
                self.assertIsNotNone(slot.path)

    def test_waiting_for_a_slot_respects_cancel_and_timeout(self):
        with patch.dict(os.environ, {'AGENT_DUET_MAX_WORKERS': '1'}):
            def give_up():
                raise duet.JobCancelled('cancelled')
            with duet.worker_slot(lambda: None):
                with self.assertRaises(duet.JobCancelled):
                    with duet.worker_slot(give_up):
                        self.fail('must not get a slot')

    def test_limit_is_sane_whatever_the_setting(self):
        for value, expected in (('0', 1), ('100', 8), ('abc', 3), ('', 3)):
            with patch.dict(os.environ, {'AGENT_DUET_MAX_WORKERS': value}):
                self.assertEqual(duet.max_workers(), expected)


if __name__ == '__main__':
    unittest.main()
