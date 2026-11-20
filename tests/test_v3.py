"""3.0: the phrase book, self-upgrade, results bound to a commit, uninstall, pairing help, and docs that match the code."""
import isolate  # noqa: F401  (must come first)
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / 'agent-duet'
sys.path.insert(0, str(SKILL / 'scripts'))
import duet
import install
import schedule


class DocsMatchCodeTests(unittest.TestCase):
    """A rule that names a tool that does not exist sends an agent down a dead end."""
    def documents(self):
        return [SKILL / 'SKILL.md'] + sorted((SKILL / 'references').glob('*.md')) + [REPO / 'README.md']

    def test_every_tool_named_in_the_docs_exists(self):
        known = {'duet_' + name for name in duet.DEFS}
        for path in self.documents():
            used = set(re.findall(r'\bduet_[a-z_]+\b', path.read_text(encoding='utf-8')))
            # `duet_*` is the family, and `duet_` on its own is how the README describes the prefix.
            missing = sorted(name for name in used - known if name not in ('duet_',))
            self.assertEqual(missing, [], '%s names tools that do not exist' % path.name)

    def test_every_reference_link_in_the_skill_resolves(self):
        text = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
        links = set(re.findall(r'\]\((references/[^)]+)\)', text))
        self.assertGreaterEqual(len(links), 6)
        for link in links:
            self.assertTrue((SKILL / link).is_file(), link)

    def test_every_documented_argument_exists(self):
        """`duet_x arg=value` in the skill must be an argument the tool accepts."""
        for path in [SKILL / 'SKILL.md'] + sorted((SKILL / 'references').glob('*.md')):
            for tool, argument in re.findall(r'`duet_([a-z_]+) ([a-z_]+)=', path.read_text(encoding='utf-8')):
                self.assertIn(argument, duet.DEFS[tool][1], '%s: duet_%s has no argument %s' % (path.name, tool, argument))

    def test_schedule_presets_in_the_docs_are_the_real_ones(self):
        text = (SKILL / 'references' / 'schedule.md').read_text(encoding='utf-8')
        self.assertEqual(set(re.findall(r'`preset=([a-z-]+)`', text)), set(schedule.PRESETS))
        self.assertEqual(set(duet.DEFS['schedule'][1]['preset']['enum']), set(schedule.PRESETS))

    def test_skill_stays_small_and_has_valid_front_matter(self):
        text = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
        self.assertLess(len(text.encode('utf-8')), 16000, 'SKILL.md is read on every use; keep detail in references/')
        self.assertTrue(text.startswith('---\nname: agent-duet\ndescription: '))
        self.assertLess(len(text.split('---')[1]), 1100)

    def test_version_is_stated_once_and_is_3(self):
        self.assertTrue(duet.VERSION.startswith('3.'))
        self.assertEqual(duet.disk_version(), duet.VERSION)


class GuideTests(unittest.TestCase):
    def test_guide_covers_the_whole_life_of_a_project(self):
        guide = duet.dispatch('guide', {})
        says = ' '.join(row['say'] for row in guide['phrases'])
        for phrase in ('配對這個專案', '目前進度', '先暫停', '繼續', '排班', '收工', '有模型更新', '檢查一下 agent-duet', '更新 agent-duet'):
            self.assertIn(phrase, says)
        self.assertEqual(guide['version'], duet.VERSION)
        self.assertEqual(guide['defaults']['schedule'], 'none until asked')
        for row in guide['phrases']:                      # every phrase leads to a real tool or a clear action
            for tool in re.findall(r'\bduet_[a-z_]+\b', row['agent_does']):
                self.assertIn(tool[5:], duet.DEFS, row)

    def test_every_phrase_in_the_guide_is_taught_in_the_readme(self):
        readme = (REPO / 'README.md').read_text(encoding='utf-8')
        for phrase in ('目前進度', '先暫停', '繼續', '有模型更新', '檢查一下 agent-duet', '更新 agent-duet', 'agent-duet 收工',
                       'agent-duet 可以做什麼', '用 agent-duet 排班', '這個用旗艦模型'):
            self.assertTrue(phrase in readme, 'README does not teach: ' + phrase)


class Project(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.base = {'root': str(self.root)}

    def call(self, op, **kw):
        return duet.dispatch(op, self.base | kw)

    def join(self, name='c', provider='codex', **kw):
        result = self.call('join', name=name, provider=provider, session=str(uuid.uuid4()), project_name='P', goal='Ship the importer', **kw)
        self.base['project_id'] = result['project']['id']
        return result


class ArgumentErrorTests(Project):
    def test_the_error_names_the_wrong_argument_and_the_accepted_ones(self):
        with self.assertRaises(ValueError) as caught:
            duet.check_arguments('send', {'root': 'x', 'sender': 'a', 'recipient': 'b', 'boddy': 'typo'})
        text = str(caught.exception)
        self.assertIn('unknown: boddy', text)
        self.assertIn('missing: body', text)
        self.assertIn('Accepted:', text)

    def test_join_and_status_accept_the_project_id_and_check_it(self):
        first = self.join('c', 'codex')
        pid = first['project']['id']
        again = duet.dispatch('join', dict(root=str(self.root), project_id=pid, name='c', provider='codex',
                                           session=duet.dispatch('status', {'root': str(self.root)})['peers'][0]['session'],
                                           project_name='P', goal='Ship the importer'))
        self.assertEqual(again['project']['id'], pid)
        duet.check_arguments('status', {'root': 'x', 'project_id': pid})
        with self.assertRaisesRegex(ValueError, 'belongs to project'):
            duet.dispatch('status', {'root': str(self.root), 'project_id': 'not-this-one'})


class PairingHelpTests(Project):
    def test_first_side_gets_the_exact_sentence_for_the_other_side(self):
        first = self.join('c', 'codex')
        self.assertEqual(first['paste_into_the_other_conversation'], '用 agent-duet 配對這個專案：%s。目標：Ship the importer' % self.root)
        self.assertIn('Claude Code', first['waiting_for'])
        second = self.join('a', 'claude', address='peer [abc123]', title='peer')
        self.assertNotIn('paste_into_the_other_conversation', second)      # both sides are here now

    def test_join_reports_a_signed_out_cli_without_being_asked(self):
        with patch.dict(os.environ, {'AGENT_DUET_ASSUME_LOGIN': '0'}), \
             patch.object(duet, 'logged_in', side_effect=lambda provider: provider != 'claude'):
            joined = self.join('c', 'codex')
        self.assertEqual(len(joined['needs_user']), 1)
        self.assertIn('claude auth login', joined['needs_user'][0])
        self.assertIn('duet_doctor login', joined['then'])

    def test_healthy_join_stays_quiet(self):
        joined = self.join('c', 'codex')
        self.assertNotIn('needs_user', joined)


class CodeVersionTests(Project):
    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t', *args], cwd=self.root, capture_output=True, text=True, check=True)

    def setUp(self):
        super().setUp()
        if not shutil.which('git'):
            self.skipTest('git is not installed')
        self.git('init', '-q', '-b', 'main')
        (self.root / 'app.py').write_text('print(1)\n', encoding='utf-8')
        self.git('add', 'app.py')
        self.git('commit', '-q', '-m', 'init')
        self.join('c', 'codex')
        self.join('a', 'claude', address='peer [abc123]', title='peer')

    def body(self, message_id):
        return next(m['body'] for m in self.call('inbox', name='a', peek=True)['messages'] if m['id'] == message_id)

    def test_a_result_names_the_commit_and_uncommitted_files(self):
        commit = self.git('rev-parse', '--short', 'HEAD').stdout.strip()
        (self.root / 'app.py').write_text('print(2)\n', encoding='utf-8')
        sent = self.call('send', sender='c', recipient='a', body='Importer done, tests pass', kind='result')
        self.assertEqual(sent['code_version'], '[git: main@%s, 1 uncommitted tracked: app.py]' % commit)
        self.assertTrue(self.body(sent['message_id']).endswith(sent['code_version']))

    def test_only_results_carry_it_and_it_can_be_left_out(self):
        plain = self.call('send', sender='c', recipient='a', body='fyi')
        self.assertNotIn('code_version', plain)
        without = self.call('send', sender='c', recipient='a', body='result without version', kind='result', git=False)
        self.assertNotIn('code_version', without)
        self.assertEqual(self.body(without['message_id']), 'result without version')

    def test_retrying_the_same_result_after_the_tree_changed_is_still_a_duplicate(self):
        first = self.call('send', sender='c', recipient='a', body='done', kind='result', dedupe='r1')
        (self.root / 'app.py').write_text('print(3)\n', encoding='utf-8')
        again = self.call('send', sender='c', recipient='a', body='done', kind='result', dedupe='r1')
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['message_id'], first['message_id'])
        with self.assertRaises(ValueError):
            self.call('send', sender='c', recipient='a', body='something else', kind='result', dedupe='r1')

    def test_the_mailbox_itself_never_shows_up_as_an_uncommitted_file(self):
        sent = self.call('send', sender='c', recipient='a', body='done', kind='result')
        self.assertNotIn('.agent-duet', sent['code_version'])


class NoGitTests(Project):
    def test_a_result_outside_git_is_sent_unchanged(self):
        self.join('c', 'codex')
        self.join('a', 'claude', address='peer [abc123]', title='peer')
        sent = self.call('send', sender='c', recipient='a', body='done', kind='result')
        self.assertNotIn('code_version', sent)


class UpgradeTests(unittest.TestCase):
    def test_refresh_says_when_agent_duet_itself_is_behind(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'AGENT_DUET_HOME': home}), \
             patch.object(duet, 'codex_models', return_value=[]), patch.object(duet, 'installed_version', return_value='cli 1.0.0'), \
             patch.object(duet, 'latest_published', return_value=None), patch.object(duet, 'latest_release', return_value='99.0.0'):
            report = duet.models_report({'refresh': True})
        self.assertEqual(report['agent_duet'], {'installed': duet.VERSION, 'latest': '99.0.0'})
        self.assertTrue(any('更新 agent-duet' in action and 'upgrade=true' in action for action in report['actions']))

    def test_current_version_and_offline_are_not_a_problem(self):
        for latest in (duet.VERSION, None, '1.0.0'):
            with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {'AGENT_DUET_HOME': home}), \
                 patch.object(duet, 'codex_models', return_value=[]), patch.object(duet, 'installed_version', return_value='cli 1.0.0'), \
                 patch.object(duet, 'latest_published', return_value=None), patch.object(duet, 'latest_release', return_value=latest):
                report = duet.models_report({'refresh': True})
            self.assertFalse(any('Agent Duet' in action for action in report['actions']), latest)

    def test_offline_switch_stops_every_lookup(self):
        with patch.dict(os.environ, {'AGENT_DUET_OFFLINE': '1'}):
            self.assertIsNone(duet.latest_release())
            self.assertIsNone(duet.latest_published('@openai/codex'))

    def test_upgrade_without_a_checkout_explains_instead_of_guessing(self):
        with patch.object(duet, 'settings', return_value={}), patch.object(duet.subprocess, 'run') as run:
            result = duet.dispatch('doctor', {'upgrade': True})
        run.assert_not_called()
        self.assertFalse(result['upgraded'])
        self.assertIn('github.com', result['manual'])

    def test_upgrade_runs_the_installer_of_the_recorded_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'agent-duet'
            (source / 'scripts').mkdir(parents=True)
            (source / 'scripts' / 'install.py').write_text('', encoding='utf-8')
            done = type('R', (), dict(returncode=0, stdout='{}', stderr=''))()
            with patch.object(duet, 'settings', return_value={'source_checkout': str(source)}), \
                 patch.object(duet.subprocess, 'run', return_value=done) as run, patch.object(duet, 'disk_version', return_value='99.0.0'):
                result = duet.dispatch('doctor', {'upgrade': True})
            command = [str(part) for part in run.call_args.args[0]]
            self.assertIn('--upgrade', command)
            self.assertIn('--no-login-prompt', command)
            self.assertEqual((result['upgraded'], result['after']), (True, '99.0.0'))
            failed = type('R', (), dict(returncode=1, stdout='', stderr='could not pull'))()
            with patch.object(duet, 'settings', return_value={'source_checkout': str(source)}), patch.object(duet.subprocess, 'run', return_value=failed):
                self.assertIn('could not pull', duet.dispatch('doctor', {'upgrade': True})['error'])


class UninstallTests(unittest.TestCase):
    def test_uninstall_moves_the_skill_to_a_backup_and_leaves_projects_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            for name in ('codex', 'claude'):
                skill = home / name / 'skills' / 'agent-duet'
                skill.mkdir(parents=True)
                (skill / 'SKILL.md').write_text('---\nname: agent-duet\n---\n', encoding='utf-8')
            other = home / 'claude' / 'skills' / 'something-else'
            other.mkdir()
            (other / 'SKILL.md').write_text('---\nname: something-else\n---\n', encoding='utf-8')
            args = type('A', (), {'codex_home': home / 'codex', 'claude_home': home / 'claude', 'skip_desktop': True})()
            done = type('R', (), dict(returncode=0, stdout='command: python  args: /x/skills/agent-duet/scripts/duet.py mcp', stderr=''))()
            with patch.object(install, 'optional_cli', side_effect=lambda name: ([name], None)), \
                 patch.object(install.subprocess, 'run', return_value=done) as run:
                result = install.uninstall(args, home / 'claude')
            removed = [str(part) for call in run.call_args_list for part in call.args[0]]
            self.assertEqual(removed.count('remove'), 2)
            for name in ('codex', 'claude'):
                self.assertFalse((home / name / 'skills' / 'agent-duet').exists())
                self.assertEqual(len(list((home / name / 'agent-duet-backups').glob('uninstalled-*/SKILL.md'))), 1)   # nothing is deleted
            self.assertTrue(other.exists())                                   # another skill is untouched
            text = install.uninstall_summary(result)
            self.assertIn('removed from this computer', text)
            self.assertIn('Project mailboxes', text)

    def test_uninstall_when_nothing_is_installed(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            args = type('A', (), {'codex_home': home / 'codex', 'claude_home': home / 'claude', 'skip_desktop': True})()
            with patch.object(install, 'optional_cli', return_value=(None, 'missing')):
                result = install.uninstall(args, None)
            self.assertEqual(result['removed'], [])
            self.assertIn('was not installed here', install.uninstall_summary(result))


class OneLineInstallerTests(unittest.TestCase):
    def test_scripts_point_at_this_repository_and_never_run_unknown_code(self):
        for name in ('get.sh', 'get.ps1'):
            text = (REPO / name).read_text(encoding='utf-8')
            self.assertIn('https://github.com/%s.git' % duet.REPOSITORY, text)
            self.assertIn('--auto-update on', text)
            self.assertNotIn('sudo', text)
            self.assertNotIn('rm -rf', text)

    @unittest.skipIf(os.name == 'nt' and not shutil.which('sh'), 'no POSIX shell')
    def test_get_sh_is_valid_shell(self):
        self.assertEqual(subprocess.run(['sh', '-n', str(REPO / 'get.sh')], capture_output=True).returncode, 0)


if __name__ == '__main__':
    unittest.main()
