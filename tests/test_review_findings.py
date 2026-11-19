"""Regression tests for the problems Codex found when it reviewed 2.3 before release."""
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
import native

EFFORTS = [{'reasoningEffort': e} for e in ('low', 'medium', 'high', 'xhigh', 'max')]
OUTDATED = [{'id': 'gpt-6.1-sol', 'model': 'gpt-6.1-sol', 'description': 'Previous generation model.', 'supportedReasoningEfforts': EFFORTS},
            {'id': 'gpt-6-astra', 'model': 'gpt-6-astra', 'description': 'Previous generation model.', 'supportedReasoningEfforts': EFFORTS}]


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        env = patch.dict(os.environ, {'AGENT_DUET_HOME': self.home.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.home.cleanup)


class UpdatePermissionTests(HomeCase):
    def test_turning_auto_update_off_during_a_check_is_not_undone(self):
        duet.save_models_state({'auto_update': True})

        def slow_discovery(*a, **k):
            duet.change_models_state(lambda state: state.update(auto_update=False))  # the user turns it off meanwhile
            return OUTDATED
        with patch.object(duet, 'codex_models', side_effect=slow_discovery), \
             patch.object(duet, 'installed_version', return_value='codex-cli 0.1.0'), \
             patch.object(duet, 'latest_published', return_value='0.2.0'), \
             patch.object(duet, 'update_cli') as update:
            duet.models_report({'refresh': True})
        update.assert_not_called()
        self.assertFalse(duet.auto_update_enabled())

    def test_string_false_never_triggers_an_update(self):
        for bad in ({'update': 'false'}, {'update': 1}, {'refresh': 'yes'}, {'auto_update': 'true'}):
            with patch.object(duet, 'update_cli') as update, self.assertRaises(ValueError):
                duet.models_report(bad)
            update.assert_not_called()

    def test_failed_update_with_offline_recheck_is_not_reported_as_current(self):
        with patch.object(duet, 'codex_models', return_value=[m for m in OUTDATED]), \
             patch.object(duet, 'installed_version', return_value='codex-cli 0.1.0'), \
             patch.object(duet, 'latest_published', side_effect=['0.2.0', '9.9.9', None, None]), \
             patch.object(duet, 'update_cli', return_value={'updated': False, 'before': 'codex-cli 0.1.0', 'error': 'network down'}):
            duet.save_models_state({'checked_at': 123.0})
            report = duet.models_report({'update': True})
        self.assertFalse(report['up_to_date'])
        self.assertTrue(any('did not succeed' in a and 'network down' in a for a in report['actions']))
        self.assertEqual(report['previous_check'], 123.0)
        self.assertEqual(report['codex']['latest_published'], '0.2.0')

    def test_automatic_refresh_respects_the_retry_interval(self):
        duet.save_models_state({'auto_update': True})
        with patch.object(duet, 'codex_models', return_value=OUTDATED), \
             patch.object(duet, 'installed_version', return_value='codex-cli 0.1.0'), \
             patch.object(duet, 'latest_published', return_value='0.2.0'), \
             patch.object(duet, 'update_cli', return_value={'updated': False, 'before': 'codex-cli 0.1.0', 'error': 'offline'}) as update:
            duet.models_report({'refresh': True})
            duet.models_report({'refresh': True})
            self.assertEqual(update.call_count, 2)  # codex and claude, once each
            duet.models_report({'update': True})    # an explicit request always tries
            self.assertEqual(update.call_count, 4)

    def test_update_counts_only_when_the_version_really_went_up(self):
        for before, after, code, expected in (('cli 0.3.0', 'cli 0.2.0', 1, False), ('cli 0.3.0', 'cli 0.3.0', 0, False),
                                              ('cli 0.3.0', 'cli 0.4.0', 1, False), ('cli 0.3.0', 'cli 0.4.0', 0, True)):
            versions = iter([before, after])
            done = type('R', (), dict(returncode=code, stdout='', stderr=''))()
            with patch.object(duet, 'update_command', return_value=(['updater'], False)), \
                 patch.object(duet, 'installed_version', side_effect=lambda p: next(versions)), \
                 patch.object(duet.subprocess, 'run', return_value=done):
                result = duet.update_cli('codex')
            self.assertEqual(result['updated'], expected, (before, after, code))
            self.assertEqual('error' in result, not expected)

    def test_concurrent_writers_do_not_lose_each_others_keys(self):
        def writer(key):
            for i in range(25):
                duet.change_models_state(lambda state, key=key, i=i: state.update({key: i}))
        threads = [threading.Thread(target=writer, args=('k%d' % n,)) for n in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(duet.models_state(), {'k0': 24, 'k1': 24, 'k2': 24, 'k3': 24})


class UpdateCommandTests(unittest.TestCase):
    def test_symlinked_launcher_resolves_to_its_npm_install(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            entry = root / 'lib/node_modules/@openai/codex/bin/codex.js'
            entry.parent.mkdir(parents=True)
            entry.write_text('// cli', encoding='utf-8')
            (root / 'bin').mkdir()
            npm = root / 'bin/npm'
            npm.write_text('#!/bin/sh', encoding='utf-8')
            with patch.object(duet, 'cli', return_value=[str(root / 'bin/codex')]), \
                 patch.object(duet.os.path, 'realpath', side_effect=lambda p: str(entry) if p.endswith('codex') else p):
                args, managed = duet.update_command('codex')
            self.assertTrue(managed)
            self.assertEqual(args, [str(npm), 'install', '-g', '@openai/codex@latest'])

    def test_homebrew_installs_use_brew_for_either_cli(self):
        for provider, expected in (('codex', ['upgrade', 'codex']), ('claude', ['upgrade', '--cask', 'claude-code'])):
            with patch.object(duet, 'cli', return_value=['/opt/homebrew/bin/' + provider]), \
                 patch.object(duet.os.path, 'realpath', side_effect=lambda p: p), \
                 patch.object(duet.shutil, 'which', return_value='/opt/homebrew/bin/brew'):
                self.assertEqual(duet.update_command(provider), (['/opt/homebrew/bin/brew'] + expected, False))

    def test_native_claude_uses_its_own_updater_and_unknown_codex_is_manual(self):
        with patch.object(duet.os.path, 'realpath', side_effect=lambda p: p):
            with patch.object(duet, 'cli', return_value=['/home/u/.local/bin/claude']):
                self.assertEqual(duet.update_command('claude'), (['/home/u/.local/bin/claude', 'update'], False))
            with patch.object(duet, 'cli', return_value=['/usr/local/bin/codex']):
                self.assertEqual(duet.update_command('codex'), (None, False))


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = {'root': self.tmp.name}
        self.ids = {n: str(uuid.uuid4()) for n in ('c', 'a')}
        for n, provider in (('c', 'codex'), ('a', 'claude')):
            r = duet.dispatch('join', self.base | dict(name=n, provider=provider, session=self.ids[n], address=None if n == 'c' else 'peer [abc123]',
                                                       title='peer', project_name='P', goal='G') if n == 'a' else
                              self.base | dict(name=n, provider=provider, session=self.ids[n], project_name='P', goal='G'))
            self.base['project_id'] = r['project']['id']

    def call(self, op, **kw):
        return duet.dispatch(op, self.base | kw)

    def send(self, s='a', r='c'):
        return self.call('send', sender=s, recipient=r, body='handoff')['message_id']

    def transport(self):
        return patch.object(native.subprocess, 'run', return_value=type('R', (), dict(returncode=0, stdout='Queued', stderr=''))())

    def test_changing_limits_keeps_the_run_usage_and_deduplication(self):
        with self.transport() as run:
            m = self.send()
            self.assertEqual(self.call('native_wake', name='a', message_id=m)['status'], 'queued')
            before = self.call('native_status')
            sent = run.call_count
            changed = self.call('native_start', name='a', peer='c', max_wakes=41)
            self.assertEqual(changed['run_id'], before['run']['id'])
            again = self.call('native_wake', name='a', message_id=m)
            self.assertTrue(again['duplicate'])
            self.assertEqual(run.call_count, sent)  # the same message never triggers a second transport call
            after = self.call('native_status')
        self.assertEqual(after['usage']['wakes_in_window'], 1)
        self.assertEqual(after['usage']['max_wakes'], 41)

    def test_stale_run_id_fails_before_anything_is_written(self):
        import sqlite3
        with self.transport():
            self.call('native_wake', name='a', message_id=self.send())
        db = sqlite3.connect(Path(self.tmp.name) / '.agent-duet/state.sqlite3')
        db.execute("UPDATE native_run SET state='limit_reached',max_wakes=2,window=3600,courier_budget=0.5")
        db.commit(); db.close()
        with self.transport() as run, self.assertRaises(ValueError):
            self.call('native_wake', name='a', message_id=self.send(), run_id='an-old-run-id')
        run.assert_not_called()
        kept = self.call('native_status')['run']
        self.assertEqual((kept['state'], kept['max_wakes'], kept['window'], kept['courier_budget']), ('limit_reached', 2, 3600, 0.5))

    def test_expired_legacy_pairing_is_revived_with_its_own_limits(self):
        import sqlite3
        import time
        with self.transport():
            self.call('native_wake', name='a', message_id=self.send())
            original = self.call('native_status')['run']['id']
            db = sqlite3.connect(Path(self.tmp.name) / '.agent-duet/state.sqlite3')
            db.execute("UPDATE native_run SET state='limit_reached',max_wakes=7,window=3600,courier_budget=0.5")
            db.commit(); db.close()
            with patch.object(native.time, 'time', return_value=time.time() + 60):
                self.assertEqual(self.call('native_wake', name='a', message_id=self.send())['status'], 'queued')
        run = self.call('native_status')['run']
        self.assertEqual((run['id'], run['state'], run['max_wakes'], run['courier_budget']), (original, 'active', 7, 0.5))

    def test_a_transport_call_in_flight_keeps_its_budget_reservation(self):
        import sqlite3
        import time
        self.call('native_start', name='c', peer='a', max_wakes=5, max_hours=0.01, courier_budget_usd=0.10)
        run_id = self.call('native_status')['run']['id']
        m = self.send('c', 'a')
        db = sqlite3.connect(Path(self.tmp.name) / '.agent-duet/state.sqlite3')
        db.execute('INSERT INTO native_attempts VALUES(?,?,?,?,?)', (run_id, m, 'sending', time.time() - 40, json.dumps({'accounted_cost_usd': 0.10})))
        db.commit(); db.close()
        with patch.object(native, 'relay_claude') as courier:
            result = self.call('native_wake', name='c', message_id=self.send('c', 'a'))
        courier.assert_not_called()
        self.assertEqual(result['status'], 'courier_budget_reached')

    def test_inbox_shows_handoffs_still_waiting_on_the_peer(self):
        with self.transport():
            woken, quiet = self.send(), self.send()
            self.call('native_wake', name='a', message_id=woken)
        waiting = {w['message_id']: w for w in self.call('inbox', name='a')['awaiting_peer']}
        self.assertEqual(waiting[woken]['wake'], 'queued')
        self.assertEqual(waiting[quiet]['wake'], 'not woken')
        self.call('ack', name='c', ids=[woken, quiet])
        self.assertEqual(self.call('inbox', name='a')['awaiting_peer'], [])


if __name__ == '__main__':
    unittest.main()
