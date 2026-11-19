"""Things that used to need a manual step and now happen by themselves."""
import isolate  # noqa: F401  (must come first)
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'agent-duet/scripts'
sys.path.insert(0, str(SCRIPTS))
import duet
import install
import native


class Pair(unittest.TestCase):
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

    def transport(self, code=0):
        return patch.object(native.subprocess, 'run', return_value=type('R', (), dict(returncode=code, stdout='Queued', stderr=''))())

    def cli_present(self):
        return patch.object(duet, 'cli', return_value=['codex'])

    def test_override_pointing_nowhere_is_reported_clearly(self):
        with patch.dict(os.environ, {'AGENT_DUET_CODEX_BIN': os.path.join(self.tmp.name, 'gone')}):
            with self.assertRaisesRegex(RuntimeError, 'does not exist'):
                duet.cli('codex')


class ReceiptTests(Pair):
    def test_reading_the_inbox_is_the_delivery_receipt(self):
        m = self.send()['message_id']
        self.assertFalse(self.call('inbox', name='a')['awaiting_peer'][0]['read_by_peer'])
        self.assertEqual([x['id'] for x in self.call('inbox', name='c')['messages']], [m])  # the peer reads it
        waiting = self.call('inbox', name='a')['awaiting_peer']
        self.assertTrue(waiting[0]['read_by_peer'])
        self.assertEqual(len(self.call('inbox', name='c')['messages']), 1)  # reading did not consume it

    def test_a_reply_acknowledges_the_message_it_answers(self):
        m = self.send()['message_id']
        progress = self.send('c', 'a', reply_to=m)  # "got it, tomorrow" must not close the task
        self.assertNotIn('acknowledged_original', progress)
        self.assertEqual(len(self.call('inbox', name='c')['messages']), 1)
        reply = self.send('c', 'a', reply_to=m, kind='result')
        self.assertTrue(reply['acknowledged_original'])
        self.assertEqual(self.call('inbox', name='c')['messages'], [])
        self.assertNotIn(m, [w['message_id'] for w in self.call('inbox', name='a')['awaiting_peer']])
        self.assertEqual(self.call('ack', name='c', ids=[m])['acknowledged'], 0)  # nothing left to do

    def test_send_can_wake_in_the_same_call(self):
        with self.cli_present(), self.transport() as run:
            sent = self.send(wake=True)
        self.assertEqual(sent['wake']['status'], 'queued')
        self.assertTrue(run.called)
        self.assertEqual(self.call('native_status')['run']['state'], 'active')

    def test_a_wake_problem_never_loses_the_message(self):
        self.call('native_stop', name='a') if self.call('native_status')['run'] else None
        with self.cli_present(), self.transport():
            self.send(wake=True)
        self.call('native_stop', name='a', state='paused')
        sent = self.send(wake=True)
        self.assertEqual(sent['wake']['status'], 'not_sent')
        self.assertIn('paused', sent['wake']['error'])
        self.assertIn(sent['message_id'], [m['id'] for m in self.call('inbox', name='c')['messages']])

    def test_wake_must_be_a_real_boolean(self):
        with self.assertRaises(ValueError):
            self.send(wake='false')

    def test_missing_cli_reports_not_sent_and_can_be_retried(self):
        m = self.send()['message_id']
        with patch.object(duet, 'cli', side_effect=RuntimeError('codex CLI is missing from PATH')):
            first = self.call('native_wake', name='a', message_id=m)
        self.assertEqual(first['status'], 'not_sent')
        self.assertIn('codex', first['fix'])
        with self.cli_present(), self.transport():
            self.assertEqual(self.call('native_wake', name='a', message_id=m)['status'], 'queued')


class IdentityTests(Pair):
    def test_mail_follows_a_conversation_that_came_back_under_a_new_name(self):
        m = self.send('c', 'a')['message_id']
        back = self.call('join', name='a-again', provider='claude', session=self.ids['a'], project_name='P', goal='G')
        self.assertEqual(back['same_conversation_also_joined_as'], ['a'])
        self.assertEqual([x['id'] for x in self.call('inbox', name='a-again')['messages']], [m])
        reply = self.send('a-again', 'c', reply_to=m, kind='done')
        self.assertTrue(reply['acknowledged_original'])
        self.assertEqual(self.call('inbox', name='a')['messages'], [])

    def test_another_conversation_never_sees_that_mail(self):
        self.send('c', 'a')
        self.call('join', name='other', provider='claude', session=str(uuid.uuid4()), project_name='P', goal='G')
        self.assertEqual(self.call('inbox', name='other')['messages'], [])
        self.assertEqual(self.call('ack', name='other', ids=[1])['acknowledged'], 0)


class PathTests(unittest.TestCase):
    def test_unresolved_root_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder).resolve()
            (real / 'sub').mkdir()
            (real / 'note.txt').write_text('資料', encoding='utf-8')
            alias = Path(os.path.join(str(real), '.', 'sub', '..'))  # a different spelling of the same folder
            self.assertEqual(duet.valid_workdir(alias, str(real / 'sub')), real / 'sub')
            self.assertIn('資料', duet.file_context(alias, alias, ['note.txt']))

    @unittest.skipIf(os.name == 'nt', 'creating symlinks needs elevation on Windows')
    def test_symlinked_project_root(self):
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder).resolve() / 'real'
            (real / 'sub').mkdir(parents=True)
            link = Path(folder).resolve() / 'link'
            link.symlink_to(real, target_is_directory=True)
            self.assertEqual(duet.valid_workdir(link, str(link / 'sub')), real / 'sub')


class HotUpgradeTests(unittest.TestCase):
    """An open conversation keeps its bridge process; after an upgrade it must use the new code."""
    def test_running_bridge_hands_calls_to_the_newly_installed_version(self):
        with tempfile.TemporaryDirectory() as folder:
            scripts = Path(folder) / 'scripts'
            shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns('__pycache__'))
            env = dict(os.environ, PYTHONIOENCODING='utf-8')
            proc = subprocess.Popen([sys.executable, '-X', 'utf8', str(scripts / 'duet.py'), 'mcp'], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', env=env)

            def ask(number, method, params=None):
                proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params or {}}) + '\n')
                proc.stdin.flush()
                seen = []
                while True:
                    message = json.loads(proc.stdout.readline())
                    if message.get('id') == number:
                        return message, seen
                    seen.append(message)
            try:
                ask(1, 'initialize', {'protocolVersion': '2025-06-18'})
                before, _ = ask(2, 'tools/call', {'name': 'duet_doctor', 'arguments': {}})
                self.assertEqual(json.loads(before['result']['content'][0]['text'])['version'], duet.VERSION)
                source = (scripts / 'duet.py').read_text(encoding='utf-8')
                upgraded = source.replace('VERSION = "%s"' % duet.VERSION, 'VERSION = "99.0.0"')
                import re
                upgraded = re.sub(r'"doctor": \("[^"]*"', '"doctor": ("NEW DESCRIPTION"', upgraded, count=1)
                self.assertNotEqual(source, upgraded)
                (scripts / 'duet.py').write_text(upgraded, encoding='utf-8')
                after, notices = ask(3, 'tools/call', {'name': 'duet_doctor', 'arguments': {}})
                self.assertEqual(json.loads(after['result']['content'][0]['text'])['version'], '99.0.0')
                self.assertIn('notifications/tools/list_changed', [n.get('method') for n in notices])
                tools, _ = ask(4, 'tools/list')
                doctor = next(t for t in tools['result']['tools'] if t['name'] == 'duet_doctor')
                self.assertEqual(doctor['description'], 'NEW DESCRIPTION')
                bad, _ = ask(5, 'tools/call', {'name': 'duet_status', 'arguments': {'root': str(Path(folder) / 'missing')}})
                self.assertTrue(bad['result']['isError'])  # errors from the new version come back as tool errors
            finally:
                proc.stdin.close()
                proc.wait(timeout=20)
                proc.stdout.close()


class InstallerTests(unittest.TestCase):
    def test_without_tomllib_the_config_is_left_alone_and_the_user_is_told(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.toml'
            original = '[mcp_servers.agent-duet]\ncommand = "python"\n'
            config.write_text(original, encoding='utf-8')
            with patch.dict(sys.modules, {'tomllib': None}):   # what Python 3.10 looks like
                message = install.set_codex_tool_timeout(config)
            self.assertIn('not changed', message)
            self.assertIn('tool_timeout_sec = 600', message)
            self.assertEqual(config.read_text(encoding='utf-8'), original)

    @unittest.skipIf(sys.version_info < (3, 11), 'tomllib is needed to edit config.toml')
    def test_codex_tool_timeout_is_added_once_and_only_to_this_server(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.toml'
            config.write_text('model = "x"\n\n[mcp_servers.other]\ncommand = "o"\n\n[mcp_servers.agent-duet]\ncommand = "python"\nargs = ["duet.py", "mcp"]\n\n[tail]\nkey = 1\n', encoding='utf-8')
            self.assertEqual(install.set_codex_tool_timeout(config), 'set to 600 s')
            self.assertEqual(install.set_codex_tool_timeout(config), 'already set to 600 s')
            text = config.read_text(encoding='utf-8')
            self.assertEqual(text.count('tool_timeout_sec'), 1)
            try:
                import tomllib
            except ImportError:
                return
            data = tomllib.loads(text)
            self.assertEqual(data['mcp_servers']['agent-duet']['tool_timeout_sec'], 600)
            self.assertNotIn('tool_timeout_sec', data['mcp_servers']['other'])
            self.assertEqual(data['tail'], {'key': 1})

    @unittest.skipIf(sys.version_info < (3, 11), 'tomllib is needed to edit config.toml')
    def test_missing_entry_leaves_the_file_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.toml'
            config.write_text('model = "x"\n', encoding='utf-8')
            self.assertIn('not found', install.set_codex_tool_timeout(config))
            self.assertEqual(config.read_text(encoding='utf-8'), 'model = "x"\n')

    def test_summary_is_plain_and_says_nothing_must_be_redone(self):
        result = {'version': '2.4.0', 'codex_mcp': 'registered', 'claude_mcp': 'skipped: claude CLI is missing', 'claude_desktop': 'not configured',
                  'default_consult_mode': 'review', 'auto_update_cli': False}
        text = install.summary(result, ['claude'])
        self.assertIn('Agent Duet 2.4.0 is installed', text)
        self.assertIn('Install and log in to claude', text)
        self.assertIn('nothing has to be set up again', text)
        self.assertNotIn('{', text)


if __name__ == '__main__':
    unittest.main()
