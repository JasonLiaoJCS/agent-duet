"""Model routing must follow new releases without anyone editing a version number."""
import isolate  # noqa: F401  (must come first)
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'agent-duet/scripts'
spec = importlib.util.spec_from_file_location('duet_models_test', SCRIPTS / 'duet.py')
duet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(duet)

EFFORTS = [{'reasoningEffort': e} for e in ('low', 'medium', 'high', 'xhigh', 'max')]


def model(slug, description='Latest model.', hidden=False):
    return {'id': slug, 'model': slug, 'description': description, 'hidden': hidden, 'supportedReasoningEfforts': EFFORTS}


CURRENT = [model('gpt-6.2-sol'), model('gpt-6.2-astra'), model('gpt-6.1-sol', 'Previous generation model.'),
           model('gpt-6-astra', 'Previous generation model.'), model('gpt-6.2-luna')]
OUTDATED = [model('gpt-6.1-sol', 'Previous generation model for coding and everyday work.'),
            model('gpt-6-astra', 'Previous generation model for complex coding.'), model('gpt-6-sol', 'Legacy model.')]


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        env = patch.dict(os.environ, {'AGENT_DUET_HOME': self.home.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.home.cleanup)
        for name in ('installed_version',):
            p = patch.object(duet, name, return_value='cli 1.0.0')
            p.start()
            self.addCleanup(p.stop)

    def test_newest_member_of_each_tier_is_chosen(self):
        self.assertEqual(duet.choose_codex(CURRENT, 'standard', 'high'), 'gpt-6.2-sol')
        self.assertEqual(duet.choose_codex(CURRENT, 'hard', 'high'), 'gpt-6.2-astra')

    def test_a_future_release_needs_no_change(self):
        future = CURRENT + [model('gpt-7-sol'), model('gpt-6.10-astra')]
        self.assertEqual(duet.choose_codex(future, 'standard', 'high'), 'gpt-7-sol')
        self.assertEqual(duet.choose_codex(future, 'hard', 'high'), 'gpt-6.10-astra')

    def test_outdated_cli_is_reported_with_the_fix(self):
        with patch.object(duet, 'codex_models', return_value=OUTDATED):
            routing = duet.route('codex', 'standard', 'high')
            report = duet.models_report({})
        self.assertEqual(routing['model'], 'gpt-6.1-sol')
        self.assertIn('npm install -g @openai/codex@latest', routing['warning'])
        self.assertFalse(report['up_to_date'])
        self.assertTrue(any('npm install -g @openai/codex@latest' in action for action in report['actions']))

    def test_current_catalog_is_up_to_date_and_remembered(self):
        with patch.object(duet, 'codex_models', return_value=CURRENT):
            self.assertNotIn('warning', duet.route('codex', 'hard', 'high'))
            self.assertTrue(duet.models_check_due())
            report = duet.models_report({})
        self.assertTrue(report['up_to_date'])
        self.assertEqual(report['codex']['standard'], 'gpt-6.2-sol')
        self.assertEqual(report['codex']['hard'], 'gpt-6.2-astra')
        self.assertFalse(duet.models_check_due())
        stored = json.loads((Path(self.home.name) / 'models.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['resolved']['codex']['standard'], 'gpt-6.2-sol')

    def test_check_becomes_due_again_after_a_week(self):
        duet.save_models_state({'checked_at': duet.time.time() - 8 * 86400})
        self.assertTrue(duet.models_check_due())

    def test_refresh_flags_cli_behind_published_version(self):
        with patch.object(duet, 'codex_models', return_value=CURRENT), \
             patch.object(duet, 'latest_published', return_value='1.2.0'):
            report = duet.models_report({'refresh': True})
        self.assertEqual(report['codex']['latest_published'], '1.2.0')
        self.assertTrue(any('codex CLI' in a and 'behind 1.2.0' in a for a in report['actions']))
        self.assertTrue(any('claude update' in a for a in report['actions']))

    def test_renamed_family_is_recorded_once_and_used(self):
        renamed = [model('gpt-7-nova'), model('gpt-7-zenith')]
        with patch.object(duet, 'codex_models', return_value=renamed):
            before = duet.route('codex', 'standard', 'high')       # work goes on, and the stand-in is declared
            self.assertEqual(before['fallback']['wanted'], 'sol')
            self.assertIn(before['model'], ('gpt-7-nova', 'gpt-7-zenith'))
            report = duet.models_report({'families': {'codex': {'standard': 'nova', 'hard': 'zenith'}}})
            self.assertEqual(report['codex']['standard'], 'gpt-7-nova')
            self.assertEqual(duet.route('codex', 'hard', 'high')['model'], 'gpt-7-zenith')
        self.assertEqual(duet.families()['claude'], {'standard': 'opus', 'hard': 'fable'})

    def test_bad_family_input_is_rejected(self):
        for bad in ({'openai': {'standard': 'sol'}}, {'codex': {'fast': 'sol'}}, {'codex': {'standard': 'Sol; rm'}}, 'sol'):
            with self.assertRaises(ValueError):
                duet.models_report({'families': bad})

    def test_claude_uses_the_configured_alias(self):
        self.assertEqual(duet.route('claude', 'standard', 'high')['model'], 'opus')
        self.assertEqual(duet.route('claude', 'hard', 'high')['model'], 'fable')

    def test_discovery_failure_is_an_action_not_a_crash(self):
        with patch.object(duet, 'codex_models', side_effect=RuntimeError('codex CLI is missing from PATH')):
            report = duet.models_report({})
        self.assertIn('missing', report['codex']['error'])
        self.assertFalse(report['up_to_date'])


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        env = patch.dict(os.environ, {'AGENT_DUET_HOME': self.home.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.home.cleanup)

    def test_update_true_updates_then_reports_the_new_resolution(self):
        catalogs = iter([OUTDATED, [m for m in CURRENT if 'luna' not in m['model']]])  # same families, newer versions
        versions = {'codex': ['codex-cli 0.1.0', 'codex-cli 0.2.0'], 'claude': ['2.0.0', '2.0.0']}
        calls = []

        def fake_update(provider):
            calls.append(provider)
            versions[provider][0] = versions[provider][1]
            return {'updated': True, 'before': 'codex-cli 0.1.0', 'after': 'codex-cli 0.2.0'}
        with patch.object(duet, 'codex_models', side_effect=lambda *a, **k: next(catalogs)),              patch.object(duet, 'installed_version', side_effect=lambda p: versions[p][0]),              patch.object(duet, 'latest_published', side_effect=lambda pkg: '0.2.0' if 'codex' in pkg else '2.0.0'),              patch.object(duet, 'update_cli', side_effect=fake_update):
            report = duet.models_report({'update': True})
        self.assertEqual(calls, ['codex'])
        self.assertTrue(report['updates']['codex']['updated'])
        self.assertEqual(report['codex']['standard'], 'gpt-6.2-sol')
        self.assertTrue(report['up_to_date'])

    def test_nothing_is_updated_without_permission(self):
        with patch.object(duet, 'codex_models', return_value=OUTDATED),              patch.object(duet, 'installed_version', return_value='codex-cli 0.1.0'),              patch.object(duet, 'latest_published', return_value='0.2.0'),              patch.object(duet, 'update_cli') as update:
            report = duet.models_report({'refresh': True})
            routing = duet.route('codex', 'standard', 'high')
        update.assert_not_called()
        self.assertFalse(report['auto_update'])
        self.assertIn('warning', routing)

    def test_auto_update_heals_a_stale_cli_before_a_consultation(self):
        duet.models_report.__globals__['save_models_state']({'auto_update': True})
        catalogs = iter([OUTDATED, CURRENT])
        with patch.object(duet, 'codex_models', side_effect=lambda *a, **k: next(catalogs)),              patch.object(duet, 'update_cli', return_value={'updated': True, 'before': 'a 0.1.0', 'after': 'a 0.2.0'}) as update:
            routing = duet.route('codex', 'standard', 'high')
        update.assert_called_once_with('codex')
        self.assertEqual(routing['model'], 'gpt-6.2-sol')
        self.assertNotIn('warning', routing)
        self.assertTrue(routing['cli_update']['updated'])

    def test_a_newer_cli_is_picked_up_within_a_day_even_before_the_catalog_looks_old(self):
        duet.save_models_state({'auto_update': True})
        catalogs = iter([[model('gpt-6.2-sol'), model('gpt-6.2-astra')], [model('gpt-6.3-sol'), model('gpt-6.2-astra')], [model('gpt-6.3-sol')]])
        with patch.object(duet, 'codex_models', side_effect=lambda *a, **k: next(catalogs)),              patch.object(duet, 'installed_version', return_value='codex-cli 0.170.0'),              patch.object(duet, 'latest_published', return_value='0.171.0') as registry,              patch.object(duet, 'update_cli', return_value={'updated': True, 'before': 'codex-cli 0.170.0', 'after': 'codex-cli 0.171.0'}) as update:
            first = duet.route('codex', 'standard', 'high')
            second = duet.route('codex', 'standard', 'high')
        self.assertEqual(first['model'], 'gpt-6.3-sol')       # the release is used by the very next consultation
        self.assertEqual(update.call_count, 1)
        self.assertEqual(registry.call_count, 1)              # and the registry is asked at most once a day
        self.assertEqual(second['model'], 'gpt-6.3-sol')

    def test_no_network_lookup_or_update_without_standing_permission(self):
        with patch.object(duet, 'codex_models', return_value=[model('gpt-6.2-sol')]),              patch.object(duet, 'latest_published') as registry, patch.object(duet, 'update_cli') as update:
            duet.route('codex', 'standard', 'high')
        registry.assert_not_called()
        update.assert_not_called()

    def test_failed_update_is_not_retried_on_every_job(self):
        duet.save_models_state({'auto_update': True})
        with patch.object(duet, 'codex_models', return_value=OUTDATED),              patch.object(duet, 'update_cli', return_value={'updated': False, 'before': 'a 0.1.0', 'error': 'offline'}) as update:
            first = duet.route('codex', 'standard', 'high')
            second = duet.route('codex', 'standard', 'high')
        self.assertEqual(update.call_count, 1)
        self.assertEqual(first['model'], 'gpt-6.1-sol')
        self.assertIn('warning', second)

    def test_busy_cli_is_left_alone_and_retried_later(self):
        duet.save_models_state({'auto_update': True})
        with patch.object(duet, 'update_command', return_value=(['npm', 'install'], True)),              patch.object(duet, 'cli_in_use', return_value=True),              patch.object(duet, 'installed_version', return_value='codex-cli 0.1.0'),              patch.object(duet.subprocess, 'run') as run:
            result = duet.update_cli('codex')
        run.assert_not_called()
        self.assertTrue(result['busy'])
        duet.remember_update({'codex': result})
        self.assertFalse(duet.update_recently_tried('codex'))

    def test_unknown_install_method_gives_a_manual_hint(self):
        with patch.object(duet, 'update_command', return_value=(None, False)),              patch.object(duet, 'installed_version', return_value='codex-cli 0.1.0'):
            self.assertIn('installer', duet.update_cli('codex')['manual'])

    def test_preference_is_stored(self):
        with patch.object(duet, 'codex_models', return_value=CURRENT), patch.object(duet, 'installed_version', return_value='x 1.0.0'):
            self.assertTrue(duet.models_report({'auto_update': True})['auto_update'])
            self.assertTrue(duet.auto_update_enabled())
            self.assertFalse(duet.models_report({'auto_update': False})['auto_update'])


class JobWaitTests(unittest.TestCase):
    def test_wait_returns_as_soon_as_the_job_ends(self):
        states = iter([{'status': 'running'}, {'status': 'running'}, {'status': 'completed', 'result': {'answer': 'ok'}}])
        real = duet.dispatch

        def fake(operation, args):
            if operation == 'job' and 'wait_seconds' not in args:
                return dict(next(states))
            return real(operation, args)
        with patch.object(duet, 'dispatch', side_effect=fake), patch.object(duet.time, 'sleep'):
            result = real('job', {'root': '.', 'job_id': 'x', 'wait_seconds': 30})
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['waited'])

    def test_wait_gives_up_at_the_deadline_without_error(self):
        real = duet.dispatch

        def fake(operation, args):
            return {'status': 'running'} if 'wait_seconds' not in args else real(operation, args)
        with patch.object(duet, 'dispatch', side_effect=fake), patch.object(duet.time, 'sleep'), \
             patch.object(duet.time, 'monotonic', side_effect=[0, 1, 700, 701]):
            result = real('job', {'root': '.', 'job_id': 'x', 'wait_seconds': 30})
        self.assertEqual(result['status'], 'running')


if __name__ == '__main__':
    unittest.main()
