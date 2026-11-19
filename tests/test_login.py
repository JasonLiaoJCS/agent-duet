"""A signed-out CLI was the quiet reason wakes and reviews failed: it must be detected and said plainly."""
import isolate  # noqa: F401  (must come first)
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent-duet/scripts'))
import duet
import native


def reply(code=0, out='', err=''):
    return type('R', (), dict(returncode=code, stdout=out, stderr=err))()


class DetectionTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'AGENT_DUET_ASSUME_LOGIN': '0'})
        env.start()
        self.addCleanup(env.stop)
        cli = patch.object(duet, 'cli', side_effect=lambda provider: [provider])
        cli.start()
        self.addCleanup(cli.stop)

    def state(self, provider, result):
        with patch.object(duet.subprocess, 'run', return_value=result):
            return duet.logged_in(provider)

    def test_claude_reads_the_json_status(self):
        self.assertIs(self.state('claude', reply(1, json.dumps({'loggedIn': False, 'authMethod': 'none'}))), False)
        self.assertIs(self.state('claude', reply(0, json.dumps({'loggedIn': True}))), True)

    def test_codex_reads_the_status_line(self):
        self.assertIs(self.state('codex', reply(0, 'Logged in using ChatGPT')), True)
        self.assertIs(self.state('codex', reply(1, '', 'Not logged in')), False)

    def test_unclear_answers_are_unknown_not_a_false_alarm(self):
        self.assertIsNone(self.state('codex', reply(0, '')))
        with patch.object(duet.subprocess, 'run', side_effect=OSError('cannot run')):
            self.assertIsNone(duet.logged_in('claude'))
        with patch.object(duet, 'logged_in', return_value=None):
            self.assertIsNone(duet.login_problem('claude'))

    def test_the_message_names_the_one_command(self):
        with patch.object(duet, 'logged_in', return_value=False):
            self.assertIn('claude auth login', duet.login_problem('claude'))
            self.assertIn('codex login', duet.login_problem('codex'))
            self.assertIn('desktop app keeps a separate login', duet.login_problem('claude'))


class OpenLoginTests(unittest.TestCase):
    def setUp(self):
        cli = patch.object(duet, 'cli', side_effect=lambda provider: [provider])
        cli.start()
        self.addCleanup(cli.stop)

    def test_opens_the_cli_own_sign_in_in_a_visible_window(self):
        with patch.object(duet, 'logged_in', return_value=False), patch.object(duet.subprocess, 'Popen') as start, \
             patch.object(duet.shutil, 'which', return_value='/usr/bin/xterm'):
            result = duet.dispatch('doctor', {'login': 'claude'})
        self.assertTrue(result['opened'])
        launched = ' '.join(str(part) for part in start.call_args.args[0])
        self.assertIn('auth', launched)
        self.assertIn('login', launched)
        self.assertEqual(result['run_this'], 'claude auth login')
        self.assertIn('Finish it in the browser', result['tell_the_user'])

    def test_does_nothing_when_already_signed_in_and_rejects_other_names(self):
        with patch.object(duet, 'logged_in', return_value=True), patch.object(duet.subprocess, 'Popen') as start:
            self.assertTrue(duet.open_login('codex')['already_signed_in'])
        start.assert_not_called()
        with self.assertRaises(ValueError):
            duet.open_login('somebody')

    def test_a_window_that_cannot_open_still_gives_the_command(self):
        with patch.object(duet, 'logged_in', return_value=False), patch.object(duet.subprocess, 'Popen', side_effect=OSError('no display')), \
             patch.object(duet.shutil, 'which', return_value='/usr/bin/xterm'):
            result = duet.open_login('codex')
        self.assertFalse(result['opened'])
        self.assertEqual(result['run_this'], 'codex login')


class InstallerLoginTests(unittest.TestCase):
    def test_summary_points_out_a_signed_out_cli_and_the_command(self):
        import install
        result = {'version': '2.4.1', 'codex_mcp': 'registered', 'claude_mcp': 'registered', 'claude_desktop': 'not configured',
                  'default_consult_mode': 'review', 'auto_update_cli': True, 'signed_in': {'codex': True, 'claude': False}}
        text = install.summary(result, [])
        self.assertIn('SIGNED OUT', text)
        self.assertIn('claude auth login', text)
        self.assertIn('desktop app has its own login', text)
        self.assertNotIn('codex login', text)

    def test_no_prompt_without_a_terminal(self):
        import install
        args = type('A', (), {'json': False, 'no_login_prompt': False})()
        with patch.object(install.sys, 'stdin', None), patch.object(install.subprocess, 'run') as run:
            install.offer_login({'signed_in': {'claude': False}}, args)
        run.assert_not_called()


class BehaviourTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = {'root': self.tmp.name}
        r = self.call('join', name='c', provider='codex', session=str(uuid.uuid4()), project_name='P', goal='G')
        self.base['project_id'] = r['project']['id']
        self.call('join', name='a', provider='claude', session=str(uuid.uuid4()), address='peer [abc123]', title='peer', project_name='P', goal='G')

    def call(self, op, **kw):
        return duet.dispatch(op, self.base | kw)

    def signed_out(self, who):
        return patch.object(duet, 'logged_in', side_effect=lambda provider: provider != who)

    def test_wake_to_a_signed_out_side_costs_nothing_and_can_be_repeated(self):
        m = self.call('send', sender='c', recipient='a', body='please review')['message_id']
        with self.signed_out('claude'), patch.object(native, 'relay_claude') as courier:
            first = self.call('native_wake', name='c', message_id=m)
        courier.assert_not_called()
        self.assertEqual(first['status'], 'not_sent')
        self.assertTrue(first['needs_user'])
        self.assertIn('claude auth login', first['error'])
        with patch.object(native, 'relay_claude', return_value={'status': 'attempted', 'reported_cost_usd': 0.01}) as courier:
            self.assertEqual(self.call('native_wake', name='c', message_id=m)['status'], 'attempted')  # after signing in
        courier.assert_called_once()

    def test_consultation_is_refused_at_once_with_the_fix(self):
        with self.signed_out('claude'), patch.object(duet, 'popen') as start:
            with self.assertRaisesRegex(ValueError, 'claude auth login'):
                self.call('consult', requester='c', provider='claude', prompt='review this')
        start.assert_not_called()

    def test_doctor_and_status_check_say_so(self):
        with self.signed_out('claude'), patch.object(duet, 'cli', side_effect=lambda provider: [provider]), \
             patch.object(duet.subprocess, 'run', return_value=reply(0, '2.1.400 (Claude Code)')), \
             patch.object(duet, 'models_check_due', return_value=False):
            report = duet.doctor()
        self.assertFalse(report['ready']['consult_claude'])
        self.assertFalse(report['ready']['native_pairing'])
        self.assertTrue(report['ready']['consult_codex'])
        self.assertTrue(any('signed out' in p for p in report['problems']))
        with self.signed_out('claude'), patch.object(native, 'live_claude_title', return_value='peer'):
            reachable = self.call('native_status', check=True)['reachable']
        self.assertFalse(reachable['a']['reachable'])
        self.assertIn('no re-pairing', reachable['a']['fix'])


class OldStateTests(unittest.TestCase):
    def test_a_state_file_written_by_an_earlier_version_does_not_break_routing(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'AGENT_DUET_HOME': home}):
            for legacy in ({'codex': 1795000000.0}, 'nonsense', {'codex': 'x'}, None):
                duet.save_models_state({'auto_update': True, 'cli_checked': legacy})
                catalog = [{'id': 'gpt-6.2-sol', 'model': 'gpt-6.2-sol', 'description': 'Latest.', 'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]}]
                with patch.object(duet, 'codex_models', return_value=catalog), \
                     patch.object(duet, 'installed_version', return_value='codex-cli 0.170.0'), \
                     patch.object(duet, 'latest_published', return_value='0.170.0'):
                    self.assertEqual(duet.route('codex', 'standard', 'high')['model'], 'gpt-6.2-sol')
                self.assertEqual(duet.cli_check(duet.models_state(), 'codex')['latest'], '0.170.0')


if __name__ == '__main__':
    unittest.main()
