"""Regression tests for the second pre-release review (Codex, flagship model) of 2.4."""
import isolate  # noqa: F401  (must come first)
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'agent-duet/scripts'
sys.path.insert(0, str(SCRIPTS))
import channels
import duet
import install
import native

EFFORTS = [{'reasoningEffort': e} for e in ('low', 'medium', 'high', 'xhigh', 'max')]


def model(slug, description='Latest model.'):
    return {'id': slug, 'model': slug, 'description': description, 'hidden': False, 'supportedReasoningEfforts': EFFORTS}


class Home(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        env = patch.dict(os.environ, {'AGENT_DUET_HOME': self.home.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.home.cleanup)


class Project(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = {'root': self.tmp.name}
        self.ids = {'c': str(uuid.uuid4()), 'a': str(uuid.uuid4())}
        r = self.call('join', name='c', provider='codex', session=self.ids['c'], project_name='P', goal='G')
        self.base['project_id'] = r['project']['id']
        self.call('join', name='a', provider='claude', session=self.ids['a'], address='peer [abc123]', title='peer', project_name='P', goal='G')

    def call(self, op, **kw):
        return duet.dispatch(op, self.base | kw)

    def send(self, s='a', r='c', **kw):
        return self.call('send', sender=s, recipient=r, body='handoff', **kw)

    def transport(self):
        return patch.object(native.subprocess, 'run', return_value=type('R', (), dict(returncode=0, stdout='Queued', stderr=''))())


class IdentityTests(Project):
    def test_a_reused_claude_address_never_inherits_another_conversations_mail(self):
        address = 'worker [aaaaaa]'
        self.call('join', name='old', provider='claude', session=address, project_name='P', goal='G')
        m = self.call('send', sender='c', recipient='old', body='private')['message_id']
        later = self.call('join', name='new', provider='claude', session=address, project_name='P', goal='G')
        self.assertNotIn('same_conversation_also_joined_as', later)
        self.assertEqual(self.call('inbox', name='new')['messages'], [])
        self.assertEqual(self.call('ack', name='new', ids=[m])['acknowledged'], 0)
        with self.assertRaises(ValueError):
            self.call('send', sender='new', recipient='c', body='x', reply_to=m, kind='result')

    def test_renamed_conversation_keeps_the_pairing_its_usage_and_a_stop(self):
        with self.transport():
            self.send(wake=True)
            run = self.call('native_status')['run']['id']
            self.call('join', name='a2', provider='claude', session=self.ids['a'], address='peer [def456]', title='peer', project_name='P', goal='G')
            again = self.call('send', sender='a2', recipient='c', body='next', wake=True)
            self.assertIn(again['wake']['status'], ('queued', 'cooldown'))
            status = self.call('native_status')
            self.assertEqual(status['run']['id'], run)                 # same pairing, not a second one
            self.call('native_stop', name='a2', state='paused')
            blocked = self.call('send', sender='a2', recipient='c', body='later', wake=True)
        self.assertEqual(blocked['wake']['status'], 'not_sent')
        self.assertIn('paused', blocked['wake']['error'])
        self.assertEqual(self.call('native_status')['run']['id'], run)  # a rename cannot reset a stop


class ReceiptTests(Project):
    def test_background_watcher_cannot_produce_a_read_receipt(self):
        self.send('a', 'c')
        emitted = []
        watcher = channels.ChannelWatch(duet.dispatch, emitted.append, interval=0.05)
        try:
            watcher.bind(self.base | {'name': 'c'})
            deadline = threading.Event()
            for _ in range(40):
                if emitted:
                    break
                deadline.wait(0.05)
        finally:
            watcher.close()
        self.assertTrue(emitted)                                        # the notification went out
        self.assertFalse(self.call('inbox', name='a')['awaiting_peer'][0]['read_by_peer'])
        self.call('inbox', name='c')                                     # the conversation itself fetches it
        self.assertTrue(self.call('inbox', name='a')['awaiting_peer'][0]['read_by_peer'])

    def test_unexpected_wake_failure_is_unknown_not_not_sent(self):
        real = duet.dispatch

        def flaky(operation, args):
            if operation == 'native_wake':
                raise OSError('database write failed after the transport ran')
            return real(operation, args)
        with patch.object(duet, 'dispatch', side_effect=flaky):
            sent = real('send', self.base | dict(sender='a', recipient='c', body='x', wake=True))
        self.assertEqual(sent['wake']['status'], 'unknown')

    def test_an_unsent_wake_can_be_retried_with_the_same_dedupe_key(self):
        with patch.object(duet, 'cli', side_effect=RuntimeError('codex CLI is missing from PATH')):
            first = self.send(dedupe='handoff-1', wake=True)
        self.assertEqual(first['wake']['status'], 'not_sent')
        with patch.object(duet, 'cli', return_value=['codex']), self.transport() as run:
            second = self.send(dedupe='handoff-1', wake=True)
            third = self.send(dedupe='handoff-1', wake=True)
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['wake']['status'], 'queued')
        self.assertTrue(third['wake']['duplicate'])
        self.assertEqual(sum(1 for c in run.call_args_list if '--thread' in c.args[0]), 1)


class UpdateRaceTests(Home):
    def test_concurrent_consultations_do_one_lookup_and_one_update(self):
        duet.save_models_state({'auto_update': True})
        gate = threading.Barrier(2)
        lookups, updates = [], []

        def registry(package):
            lookups.append(package)
            return '0.2.0'

        def updater(provider, timeout):
            updates.append(provider)
            threading.Event().wait(0.3)
            return {'updated': True, 'before': 'codex-cli 0.1.0', 'after': 'codex-cli 0.2.0'}

        def consult():
            gate.wait()
            duet.route('codex', 'standard', 'high')
        with patch.object(duet, 'codex_models', return_value=[model('gpt-6.2-sol')]), \
             patch.object(duet, 'installed_version', return_value='codex-cli 0.1.0'), \
             patch.object(duet, 'latest_published', side_effect=registry), \
             patch.object(duet, '_update_cli', side_effect=updater):
            threads = [threading.Thread(target=consult) for _ in range(2)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(len(lookups), 1)
        self.assertEqual(len(updates), 1)

    def test_offline_lookup_backs_off_and_a_busy_cli_is_retried_by_the_next_job(self):
        duet.save_models_state({'auto_update': True})
        with patch.object(duet, 'codex_models', return_value=[model('gpt-6.2-sol')]), \
             patch.object(duet, 'installed_version', return_value='codex-cli 0.1.0'), \
             patch.object(duet, 'latest_published', return_value=None) as registry, \
             patch.object(duet, '_update_cli') as update:
            duet.route('codex', 'standard', 'high')
            duet.route('codex', 'standard', 'high')
            self.assertEqual(registry.call_count, 1)                    # no lookup storm while offline
            update.assert_not_called()
        results = iter([{'updated': False, 'before': 'codex-cli 0.1.0', 'busy': True}, {'updated': True, 'before': 'codex-cli 0.1.0', 'after': 'codex-cli 0.2.0'}])
        duet.change_models_state(lambda state: state.update(cli_checked={'codex': {'at': duet.time.time(), 'ok': True, 'latest': '0.2.0'}}))
        with patch.object(duet, 'codex_models', return_value=[model('gpt-6.2-sol')]), \
             patch.object(duet, 'installed_version', return_value='codex-cli 0.1.0'), \
             patch.object(duet, 'latest_published') as registry, \
             patch.object(duet, '_update_cli', side_effect=lambda provider, timeout: next(results)) as update:
            first = duet.route('codex', 'standard', 'high')
            second = duet.route('codex', 'standard', 'high')
        registry.assert_not_called()                                    # the pending version is remembered
        self.assertTrue(first['cli_update']['busy'])
        self.assertTrue(second['cli_update']['updated'])
        self.assertEqual(update.call_count, 2)

    def test_update_failure_reason_reaches_the_job_and_short_jobs_do_not_start_an_update(self):
        duet.save_models_state({'auto_update': True, 'cli_checked': {'codex': {'at': duet.time.time(), 'ok': True, 'latest': '0.2.0'}}})
        with patch.object(duet, 'codex_models', return_value=[model('gpt-6.2-sol')]), \
             patch.object(duet, 'installed_version', return_value='codex-cli 0.1.0'), \
             patch.object(duet, '_update_cli', return_value={'updated': False, 'before': 'codex-cli 0.1.0', 'error': 'npm: permission denied'}) as update:
            short = duet.route('codex', 'standard', 'high', deadline=duet.time.monotonic() + 120)
            self.assertNotIn('cli_update', short)
            update.assert_not_called()
            routed = duet.route('codex', 'standard', 'high')
        self.assertEqual(routed['cli_update']['error'], 'npm: permission denied')


class LineupTests(Home):
    def setUp(self):
        super().setUp()
        p = patch.object(duet, 'installed_version', return_value='cli 1.0.0')
        p.start()
        self.addCleanup(p.stop)

    def report(self, catalog, **args):
        with patch.object(duet, 'codex_models', return_value=catalog):
            return duet.models_report(args)

    def test_new_version_in_the_same_families_needs_no_review(self):
        self.assertTrue(self.report([model('gpt-6.2-sol'), model('gpt-6.2-astra'), model('gpt-6-luna')])['up_to_date'])
        later = self.report([model('gpt-6.3-sol'), model('gpt-6.3-astra'), model('gpt-6-luna')])
        self.assertTrue(later['up_to_date'])
        self.assertEqual((later['codex']['standard'], later['codex']['hard']), ('gpt-6.3-sol', 'gpt-6.3-astra'))

    def test_a_new_family_asks_the_agent_to_rejudge_the_roles_once(self):
        self.report([model('gpt-6.2-sol'), model('gpt-6.2-astra')])
        changed = self.report([model('gpt-6.2-sol'), model('gpt-6.2-astra'), model('gpt-7-zenith')])
        self.assertFalse(changed['up_to_date'])
        self.assertTrue(any('zenith' in reason for reason in changed['codex']['review_roles']))
        self.assertTrue(any('flagship is the best model' in action for action in changed['actions']))
        confirmed = self.report([model('gpt-6.2-sol'), model('gpt-6.2-astra'), model('gpt-7-zenith')],
                                families={'codex': {'standard': 'sol', 'hard': 'zenith'}})
        self.assertTrue(confirmed['up_to_date'])
        self.assertEqual(confirmed['codex']['hard'], 'gpt-7-zenith')
        self.assertIsNotNone(confirmed['roles_reviewed_at'])
        self.assertTrue(self.report([model('gpt-6.2-sol'), model('gpt-6.2-astra'), model('gpt-7-zenith')])['up_to_date'])

    def test_another_family_overtaking_a_tier_is_flagged_and_stays_quiet_once_confirmed(self):
        self.report([model('gpt-6.2-sol'), model('gpt-6.2-astra'), model('gpt-6-luna')])
        overtaken = self.report([model('gpt-6.2-sol'), model('gpt-6.2-astra'), model('gpt-6.5-luna')])
        self.assertTrue(any('luna' in reason and 'standard' in reason for reason in overtaken['codex']['review_roles']))
        kept = self.report([model('gpt-6.2-sol'), model('gpt-6.2-astra'), model('gpt-6.5-luna')],
                           families={'codex': {'standard': 'sol', 'hard': 'astra'}})
        self.assertTrue(kept['up_to_date'])
        self.assertTrue(self.report([model('gpt-6.2-sol'), model('gpt-6.2-astra'), model('gpt-6.5-luna')])['up_to_date'])

    def test_medium_effort_is_available_and_needs_no_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            base = {'root': folder}
            r = duet.dispatch('join', base | dict(name='me', provider='claude', session=str(uuid.uuid4()), project_name='P', goal='G'))
            base['project_id'] = r['project']['id']
            with patch.object(duet, 'popen', return_value=type('P', (), {'pid': 1})()), patch.object(duet, 'process_birth', return_value='b'):
                job = duet.dispatch('consult', base | dict(requester='me', provider='codex', prompt='rename a variable', effort='medium'))
            self.assertEqual(job['status'], 'queued')
            with self.assertRaisesRegex(ValueError, 'reason'):
                duet.dispatch('consult', base | dict(requester='me', provider='codex', prompt='x', effort='max'))


class InstallerSafetyTests(unittest.TestCase):
    def write(self, folder, text, newline='\n'):
        config = Path(folder) / 'config.toml'
        config.write_bytes(text.replace('\n', newline).encode('utf-8'))
        return config

    @unittest.skipIf(sys.version_info < (3, 11), 'tomllib is needed to edit config.toml')
    def test_multiline_argument_with_a_blank_line_is_never_touched(self):
        import tomllib
        with tempfile.TemporaryDirectory() as folder:
            config = self.write(folder, '[mcp_servers.agent-duet]\ncommand = "python"\nargs = ["""first\n\nsecond""", "mcp"]\n\n[other]\nk = 1\n')
            before = tomllib.loads(config.read_text(encoding='utf-8'))
            self.assertEqual(install.set_codex_tool_timeout(config), 'set to 600 s')
            after = tomllib.loads(config.read_text(encoding='utf-8'))
            self.assertEqual(after['mcp_servers']['agent-duet']['args'], before['mcp_servers']['agent-duet']['args'])
            self.assertEqual(after['mcp_servers']['agent-duet']['tool_timeout_sec'], 600)
            self.assertEqual(after['other'], {'k': 1})

    @unittest.skipIf(sys.version_info < (3, 11), 'tomllib is needed to edit config.toml')
    def test_crlf_comment_on_header_existing_value_and_inline_table(self):
        import tomllib
        with tempfile.TemporaryDirectory() as folder:
            config = self.write(folder, '[mcp_servers.agent-duet]  # bridge\ncommand = "python"\n', newline='\r\n')
            self.assertEqual(install.set_codex_tool_timeout(config), 'set to 600 s')
            raw = config.read_bytes()
            self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))               # line endings kept
            self.assertEqual(tomllib.loads(raw.decode())['mcp_servers']['agent-duet']['tool_timeout_sec'], 600)
            config = self.write(folder, '[mcp_servers.agent-duet]\ncommand = "python"\ntool_timeout_sec = 60\n')
            self.assertEqual(install.set_codex_tool_timeout(config), 'already set to 60 s')   # the owner's value stays
            original = 'mcp_servers = { agent-duet = { command = "python" } }\n'
            config = self.write(folder, original)
            self.assertIn('not changed', install.set_codex_tool_timeout(config))
            self.assertEqual(config.read_text(encoding='utf-8'), original)

    def test_upgrade_from_a_copy_that_is_not_a_checkout_fails_instead_of_reinstalling_the_old_one(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'agent-duet'
            source.mkdir()
            with self.assertRaises(SystemExit) as stop:
                install.upgrade_source(source)
            self.assertIn('Nothing was changed', str(stop.exception))


class HotUpgradeSafetyTests(unittest.TestCase):
    def start(self, scripts):
        proc = subprocess.Popen([sys.executable, '-X', 'utf8', str(scripts / 'duet.py'), 'mcp'], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                                env=dict(os.environ, PYTHONIOENCODING='utf-8'))

        def ask(number, method, params=None):
            proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params or {}}) + '\n')
            proc.stdin.flush()
            seen = []
            while True:
                message = json.loads(proc.stdout.readline())
                if message.get('id') == number:
                    return message, [n.get('method') for n in seen]
                seen.append(message)
        return proc, ask

    def test_every_upgrade_is_announced_and_a_broken_file_falls_back_to_the_loaded_code(self):
        with tempfile.TemporaryDirectory() as folder:
            scripts = Path(folder) / 'scripts'
            shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns('__pycache__'))
            source = (scripts / 'duet.py').read_text(encoding='utf-8')
            stamp = 'VERSION = "%s"' % duet.VERSION
            proc, ask = self.start(scripts)
            try:
                ask(1, 'initialize', {'protocolVersion': '2025-06-18'})
                for number, version in ((2, '98.0.0'), (3, '99.0.0')):
                    (scripts / 'duet.py').write_text(source.replace(stamp, 'VERSION = "%s"' % version), encoding='utf-8')
                    reply, notices = ask(number, 'tools/call', {'name': 'duet_doctor', 'arguments': {}})
                    self.assertEqual(json.loads(reply['result']['content'][0]['text'])['version'], version)
                    self.assertIn('notifications/tools/list_changed', notices)   # announced each time, not only once
                # A half-written file during an upgrade: keep answering with the code already loaded.
                (scripts / 'duet.py').write_text('VERSION = "100.0.0"\ndef broken(:\n', encoding='utf-8')
                reply, notices = ask(4, 'tools/call', {'name': 'duet_doctor', 'arguments': {}})
                self.assertEqual(json.loads(reply['result']['content'][0]['text'])['version'], duet.VERSION)
                self.assertNotIn('notifications/tools/list_changed', notices)
                tools, _ = ask(5, 'tools/list')
                self.assertIn('duet_doctor', [t['name'] for t in tools['result']['tools']])
            finally:
                proc.stdin.close()
                proc.wait(timeout=20)
                proc.stdout.close()

    def test_cli_and_handoff_validate_arguments_before_touching_a_project(self):
        with tempfile.TemporaryDirectory() as folder:
            for extra in ([], ['--handoff']):
                done = subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPTS / 'duet.py'), 'call', 'inbox', '--file', '-'] + extra,
                                      input=json.dumps({'root': folder}), capture_output=True, text=True, encoding='utf-8')
                if extra:
                    self.assertIn('missing arguments', json.loads(done.stdout)['error'])
                else:
                    self.assertNotEqual(done.returncode, 0)
                self.assertFalse((Path(folder) / '.agent-duet').exists())      # nothing was created
            done = subprocess.run([sys.executable, '-X', 'utf8', str(SCRIPTS / 'duet.py'), 'call', 'status', '--file', '-', '--handoff'],
                                  input=json.dumps({'root': folder, 'project_id': 'x'}), capture_output=True, text=True, encoding='utf-8')
            self.assertIn('Unknown or missing arguments', json.loads(done.stdout)['error'])


if __name__ == '__main__':
    unittest.main()
