"""Small things that make daily use smoother."""
import isolate  # noqa: F401  (must come first)
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent-duet/scripts'))
import duet
import install
import native


class GitExcludeTests(unittest.TestCase):
    session = str(uuid.uuid4())

    def join(self, folder):
        return duet.dispatch('join', dict(root=folder, name='c', provider='codex', session=self.session, project_name='P', goal='G'))

    def test_mailbox_is_excluded_locally_once_and_no_tracked_file_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / '.git' / 'info').mkdir(parents=True)
            (Path(folder) / '.gitignore').write_text('node_modules/\n', encoding='utf-8')
            exclude = Path(folder) / '.git' / 'info' / 'exclude'
            exclude.write_text('# local excludes\n*.log', encoding='utf-8')
            self.assertEqual(self.join(folder)['git'], 'added to the local git exclude list')
            self.assertEqual(self.join(folder)['git'], 'already excluded')
            self.assertEqual(exclude.read_text(encoding='utf-8'), '# local excludes\n*.log\n.agent-duet/\n')
            self.assertEqual((Path(folder) / '.gitignore').read_text(encoding='utf-8'), 'node_modules/\n')

    def test_a_folder_without_git_is_left_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(self.join(folder)['git'], 'not a git repository')
            self.assertFalse((Path(folder) / '.git').exists())


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = {'root': self.tmp.name}
        r = self.call('join', name='c', provider='codex', session=str(uuid.uuid4()), project_name='P', goal='G')
        self.base['project_id'] = r['project']['id']
        self.call('join', name='a', provider='claude', session=str(uuid.uuid4()), address='peer [abc123]', title='peer', project_name='P', goal='G')

    def call(self, op, **kw):
        return duet.dispatch(op, self.base | kw)

    def overview(self):
        return duet.dispatch('status', {'root': self.tmp.name})['overview']

    def test_progress_question_is_answered_from_one_call(self):
        first = self.overview()
        self.assertEqual(first['mail_waiting'], {})
        self.assertTrue(any('No wake pairing yet' in line for line in first['attention']))
        self.call('send', sender='a', recipient='c', body='please review')
        self.call('send', sender='a', recipient='c', body='and this')
        with patch.object(duet, 'cli', return_value=['codex']), \
             patch.object(native.subprocess, 'run', return_value=type('R', (), dict(returncode=0, stdout='Queued', stderr=''))()):
            self.call('send', sender='a', recipient='c', body='your turn', wake=True)
        seen = self.overview()
        self.assertEqual(seen['mail_waiting']['c']['waiting'], 3)
        self.assertEqual(seen['mail_waiting']['c']['not_yet_read'], 3)
        self.assertEqual(seen['pairing']['state'], 'active')
        self.assertEqual((seen['pairing']['wakes_used'], seen['pairing']['max_wakes']), (1, 40))
        self.assertEqual(seen['attention'], [])
        self.call('inbox', name='c')
        self.assertEqual(self.overview()['mail_waiting']['c']['not_yet_read'], 0)

    def test_mail_that_waited_for_hours_is_pointed_out(self):
        import time
        self.call('send', sender='a', recipient='c', body='please review')
        with patch.object(duet.time, 'time', return_value=time.time() + 5 * 3600):
            attention = self.overview()['attention']
        self.assertTrue(any('c' in line and 'four hours' in line for line in attention))


class InstallerHelpTests(unittest.TestCase):
    def test_missing_cli_comes_with_the_command_to_install_it(self):
        result = {'version': '2.6.0', 'codex_mcp': 'skipped: codex CLI is missing', 'claude_mcp': 'registered', 'claude_desktop': 'not configured',
                  'default_consult_mode': 'review', 'auto_update_cli': False, 'signed_in': {'claude': True}}
        text = install.summary(result, ['codex'])
        self.assertIn('npm install -g @openai/codex', text)
        self.assertIn('codex login', text)
        self.assertNotIn('Codex CLI sign-in', text)   # nothing to say about a CLI that is not installed


if __name__ == '__main__':
    unittest.main()
