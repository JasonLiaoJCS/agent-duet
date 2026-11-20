"""Regression tests for the final pre-3.0 review."""
import isolate  # noqa: F401  (must come first)
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'agent-duet/scripts'))
import duet
import install
import native
import schedule


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.base = {'root': str(self.root)}
        self.ids = {'c': str(uuid.uuid4()), 'a': str(uuid.uuid4())}
        r = self.call('join', name='c', provider='codex', session=self.ids['c'], project_name='Long task', goal='Train')
        self.base['project_id'] = r['project']['id']
        self.call('join', name='a', provider='claude', session=self.ids['a'], address='peer [abc123]', title='peer', project_name='Long task', goal='Train')

    def call(self, op, **kw):
        return duet.dispatch(op, self.base | kw)

    def row(self):
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        try:
            return json.loads(db.execute('SELECT data FROM schedule').fetchone()[0])
        finally:
            db.close()

    def idle(self):
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        db.execute('UPDATE peers SET seen=?', (time.time() - 7200,))
        db.commit()
        db.close()

    def codex_ok(self):
        return patch.object(native.subprocess, 'run', return_value=type('R', (), dict(returncode=0, stdout='Queued', stderr=''))())


class FinishIsFinalTests(Case):
    def test_a_check_in_prepared_just_before_finish_is_never_delivered(self):
        self.call('schedule', name='a', preset='alternate')
        self.idle()
        root, state, db = duet.connect(str(self.root))
        try:
            data = schedule.write(db)
            entry, message_id = schedule.prepare(root, db, data, time.time() + 3600)
            schedule.save(db, data)
            db.commit()
            done = self.call('finish', name='a')                         # lands between prepare and deliver
            with patch.object(duet, 'cli', side_effect=lambda provider: [provider]), self.codex_ok() as transport:
                result = schedule.deliver(root, db, message_id, data['id'])
            self.assertFalse(any('queue' in call.args[0] for call in transport.call_args_list if call.args))
        finally:
            db.close()
        self.assertIn('not_sent', result)
        self.assertEqual(done['still_running'], [])
        status = duet.dispatch('status', {'root': str(self.root)})
        self.assertIsNone(status['overview']['pairing'])                 # no pairing was created behind the user's back
        self.assertEqual(status['overview']['mail_waiting'], {})         # and the orphaned check-in is closed

    def test_pause_parks_the_schedule_and_resume_brings_both_back(self):
        self.call('schedule', name='a', preset='alternate')
        with patch.object(duet, 'cli', side_effect=lambda provider: [provider]), self.codex_ok():
            self.call('send', sender='a', recipient='c', body='go', wake=True)
        parked = self.call('finish', name='a', pause_only=True)
        self.assertIn('paused', ' '.join(parked['stopped']))
        self.assertEqual(self.row()['state'], 'paused')
        self.assertIn('resumes the wakeups and the paused schedule', parked['to_resume'])
        resumed = self.call('native_start', name='a', peer='c')
        self.assertTrue(resumed['schedule_resumed'])
        row = self.row()
        self.assertEqual(row['state'], 'active')
        self.assertGreater(row['next_due'], time.time() + 50 * 60)       # counted from now, not from before the pause

    def test_a_finished_schedule_is_not_resumed_by_accident(self):
        self.call('schedule', name='a', preset='alternate')
        with patch.object(duet, 'cli', side_effect=lambda provider: [provider]), self.codex_ok():
            self.call('send', sender='a', recipient='c', body='go', wake=True)
        self.call('finish', name='a')
        self.assertFalse(self.call('native_start', name='a', peer='c')['schedule_resumed'])
        self.assertEqual(self.row()['state'], 'stopped')


class CheckInReplyTests(Case):
    def test_answering_a_check_in_with_a_handoff_to_the_peer_closes_it(self):
        self.call('schedule', name='c', mode='custom', pattern='claude:60')
        self.idle()
        root, state, db = duet.connect(str(self.root))
        try:
            data = schedule.write(db)
            entry, message_id = schedule.prepare(root, db, data, time.time() + 3600)
            schedule.save(db, data)
            db.commit()
        finally:
            db.close()
        mail = self.call('inbox', name='a')['messages']
        self.assertEqual([m['sender'] for m in mail], ['scheduler'])
        reply = self.call('send', sender='a', recipient='c', body='Loss is flat; no action needed.', kind='result', reply_to=message_id)
        self.assertTrue(reply['acknowledged_original'])
        self.assertEqual(self.call('inbox', name='a')['messages'], [])
        self.assertEqual(self.call('inbox', name='c')['messages'][0]['body'].splitlines()[0], 'Loss is flat; no action needed.')
        with self.assertRaises(ValueError):                              # someone else's check-in cannot be answered
            self.call('send', sender='c', recipient='a', body='x', reply_to=message_id)


class TimerOwnershipTests(Case):
    def test_a_slow_starter_cannot_overwrite_the_timer_another_one_registered(self):
        self.call('schedule', name='a', preset='alternate')
        root, state, db = duet.connect(str(self.root))
        try:
            def slow_launch(_root, token=None):
                # While A is launching, its claim expires and B registers timer 202.
                other = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
                data = json.loads(other.execute('SELECT data FROM schedule').fetchone()[0])
                data.update(pid=202, birth='b', claim='token-of-b', claimed_at=None)
                other.execute('UPDATE schedule SET data=?', (json.dumps(data),))
                other.commit()
                other.close()
                return type('P', (), {'pid': 101})()
            with patch.dict(os.environ, {'AGENT_DUET_NO_SCHEDULER': '0'}), patch.object(schedule, 'launch', side_effect=slow_launch), \
                 patch.object(duet, 'process_birth', return_value='a'), patch.object(schedule, 'alive', return_value=False):
                schedule.start_timer(root, db)
        finally:
            db.close()
        self.assertEqual(self.row()['pid'], 202)

    def test_a_strict_scale_also_binds_the_first_check_and_the_fallback(self):
        plan = self.call('schedule', name='a', mode='adaptive', around_minutes=60, strict=True, first_minutes=10, interval_minutes=10)
        self.assertEqual(plan['next_check']['in_minutes'], 30)
        self.assertEqual([step['after_minutes'] for step in plan['cycle']], [30, 30])
        loose = self.call('schedule', name='a', mode='adaptive', around_minutes=60, first_minutes=10)
        self.assertEqual(loose['next_check']['in_minutes'], 10)


class WatchTests(Case):
    def due(self, minutes, busy=False):
        if not busy:
            self.idle()
        now = time.time() + minutes * 60
        root, state, db = duet.connect(str(self.root))
        try:
            data = schedule.write(db)
            with patch.object(duet, 'cli', side_effect=lambda provider: [provider]), self.codex_ok():
                entry, message_id = schedule.prepare(root, db, data, now)
                schedule.save(db, data)
                db.commit()
                if message_id is not None:
                    entry['result'] = schedule.deliver(root, db, message_id, data['id'])
            return entry
        finally:
            db.close()

    def test_a_rewrite_with_the_same_size_in_the_same_second_is_seen(self):
        log = self.root / 'train.log'
        log.write_text('epoch 1\n', encoding='utf-8')
        os.utime(log, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        self.call('schedule', name='a', mode='custom', pattern='codex:60', watch='train.log', max_quiet_minutes=600)
        log.write_text('epoch 2\n', encoding='utf-8')                       # same length
        os.utime(log, ns=(1_700_000_000_500_000_000, 1_700_000_000_500_000_000))   # same second, later
        self.assertEqual(self.due(60)['result'], 'woken: queued')

    def test_a_change_seen_while_that_side_was_busy_is_delivered_at_the_next_check(self):
        log = self.root / 'train.log'
        log.write_text('epoch 1\n', encoding='utf-8')
        self.call('schedule', name='a', mode='custom', pattern='codex:60', watch='train.log', max_quiet_minutes=600)
        log.write_text('epoch 1\nepoch 2\n', encoding='utf-8')
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        db.execute("UPDATE peers SET seen=? WHERE name='c'", (time.time() + 3600 - 60,))   # Codex is mid-turn at the first check
        db.commit()
        db.close()
        self.assertIn('active', self.due(60, busy=True)['result'])
        self.assertEqual(self.due(120)['result'], 'woken: queued')       # the change was not forgotten

    def test_setup_warns_about_a_pattern_that_matches_nothing_or_too_much(self):
        nothing = self.call('schedule', name='a', preset='alternate', watch='runs/trian.log')
        self.assertTrue(any('matches no file yet' in w for w in nothing['warnings']))
        (self.root / 'many').mkdir()
        for index in range(12):
            (self.root / 'many' / ('f%02d.txt' % index)).write_text('x', encoding='utf-8')
        with patch.object(schedule, 'SCAN_LIMIT', 10):
            many = self.call('schedule', name='a', preset='alternate', watch='many')
            digest, matched, truncated = schedule.scan(self.root, ['many'])
        self.assertTrue(any('more than 10 files' in w for w in many['warnings']))
        self.assertEqual((matched, truncated), (10, True))
        (self.root / 'ok.log').write_text('x', encoding='utf-8')
        self.assertNotIn('warnings', self.call('schedule', name='a', preset='alternate', watch='ok.log'))

    def test_the_timer_scans_files_before_it_takes_the_write_lock(self):
        (self.root / 'train.log').write_text('x', encoding='utf-8')
        self.call('schedule', name='a', mode='custom', pattern='codex:60', watch='train.log')
        self.idle()
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        data = json.loads(db.execute('SELECT data FROM schedule').fetchone()[0])
        data.update(next_due=time.time() - 1, until=time.time() + 3600)
        db.execute('UPDATE schedule SET data=?', (json.dumps(data),))
        db.commit()
        db.close()
        seen = []

        def scanning(root, patterns):
            probe = sqlite3.connect(self.root / '.agent-duet/state.sqlite3', timeout=0.2)
            try:
                probe.execute('BEGIN IMMEDIATE')                         # would fail if the timer already held it
                probe.rollback()
                seen.append('free')
            except sqlite3.OperationalError:
                seen.append('locked')
            finally:
                probe.close()
            return 'changed'
        with patch.object(schedule, 'fingerprint', side_effect=scanning), patch.object(schedule.time, 'sleep', side_effect=KeyboardInterrupt), \
             patch.object(duet, 'cli', side_effect=lambda provider: [provider]), self.codex_ok():
            with self.assertRaises(KeyboardInterrupt):
                schedule.runner(str(self.root))
        self.assertEqual(seen, ['free'])


class JoinTests(unittest.TestCase):
    def test_the_sign_in_probe_runs_after_the_mailbox_is_released(self):
        with tempfile.TemporaryDirectory() as folder:
            root = str(Path(folder).resolve())
            first = duet.dispatch('join', dict(root=root, name='c', provider='codex', session=str(uuid.uuid4()), project_name='P', goal='G'))
            wrote = []

            def probe(provider):
                # A second connection writes while the probe is running; that only works if join let go of the database.
                other = sqlite3.connect(Path(root) / '.agent-duet/state.sqlite3', timeout=0.3)
                other.execute("INSERT INTO checkpoints(author,summary,next_steps,evidence,created) VALUES('x','during probe','','',0)")
                other.commit()
                other.close()
                wrote.append(provider)
                return provider != 'claude'
            with patch.dict(os.environ, {'AGENT_DUET_ASSUME_LOGIN': '0'}), patch.object(duet, 'logged_in', side_effect=probe):
                joined = duet.dispatch('join', dict(root=root, name='a', provider='claude', session=str(uuid.uuid4()), address='p [1]',
                                                    project_name='P', goal='G'))
            self.assertEqual(sorted(wrote), ['claude', 'codex'])           # one probe per CLI, no second look
            self.assertEqual(len(joined['needs_user']), 1)
            self.assertIn('claude auth login', joined['needs_user'][0])

    def test_coming_back_under_a_new_name_keeps_every_hint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = str(Path(folder).resolve())
            session = str(uuid.uuid4())
            duet.dispatch('join', dict(root=root, name='c', provider='codex', session=session, project_name='P', goal='G'))
            with patch.dict(os.environ, {'AGENT_DUET_ASSUME_LOGIN': '0'}), patch.object(duet, 'logged_in', return_value=False):
                again = duet.dispatch('join', dict(root=root, name='c2', provider='codex', session=session, project_name='P', goal='G'))
            self.assertEqual(again['same_conversation_also_joined_as'], ['c'])
            self.assertIn('paste_into_the_other_conversation', again)       # the Claude side still has to join
            self.assertEqual(len(again['needs_user']), 2)

    def test_check_false_skips_the_probe(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(duet, 'logged_in') as probe:
            duet.dispatch('join', dict(root=folder, name='c', provider='codex', session=str(uuid.uuid4()), project_name='P', goal='G', check=False))
        probe.assert_not_called()


@unittest.skipUnless(shutil.which('git'), 'git is not installed')
class GitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / '專案 with space'
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        (self.root / '資料 檔.py').write_text('print(1)\n', encoding='utf-8')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'init')

    def git(self, *args, cwd=None):
        return subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t', *args], cwd=cwd or self.root,
                              capture_output=True, text=True, encoding='utf-8', check=True)

    def test_paths_with_spaces_and_non_ascii_names_are_shown_as_they_are(self):
        (self.root / '資料 檔.py').write_text('print(2)\n', encoding='utf-8')
        self.assertIn('1 uncommitted tracked: 資料 檔.py', duet.git_context(self.root))

    def test_detached_head_and_an_empty_repository(self):
        commit = self.git('rev-parse', '--short', 'HEAD').stdout.strip()
        self.git('checkout', '-q', '--detach')
        self.assertEqual(duet.git_context(self.root), '[git: detached@%s]' % commit)
        empty = self.root.parent / 'empty'
        empty.mkdir()
        self.git('init', '-q', cwd=empty)
        self.assertIsNone(duet.git_context(empty))

    def test_a_huge_status_is_cut_off_not_read_whole(self):
        for index in range(60):
            (self.root / ('f%03d.py' % index)).write_text('x\n', encoding='utf-8')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'many')
        for index in range(60):
            (self.root / ('f%03d.py' % index)).write_text('y\n', encoding='utf-8')
        note = duet.git_context(self.root)
        self.assertIn('60 uncommitted tracked', note)
        self.assertTrue(note.endswith(' …]'))
        self.assertLess(len(note), 200)

    def test_no_file_system_monitor_hook_is_run(self):
        with patch.object(duet.subprocess, 'Popen', wraps=duet.subprocess.Popen) as popen:
            duet.git_context(self.root)
        for call in popen.call_args_list:
            self.assertIn('core.fsmonitor=false', call.args[0])

    def test_text_that_merely_mentions_git_is_not_taken_for_a_retry(self):
        root = str(self.root)
        r = duet.dispatch('join', dict(root=root, name='c', provider='codex', session=str(uuid.uuid4()), project_name='P', goal='G'))
        base = {'root': root, 'project_id': r['project']['id']}
        duet.dispatch('join', base | dict(name='a', provider='claude', session=str(uuid.uuid4()), address='p [1]', project_name='P', goal='G'))
        duet.dispatch('send', base | dict(sender='c', recipient='a', body='see\n\n[git: notes] and more', dedupe='k1'))
        with self.assertRaisesRegex(ValueError, 'dedupe key reused'):
            duet.dispatch('send', base | dict(sender='c', recipient='a', body='see', dedupe='k1'))
        duet.dispatch('send', base | dict(sender='c', recipient='a', body='plain\n\n[git: main@abc]', dedupe='k2'))   # kind=message
        with self.assertRaisesRegex(ValueError, 'dedupe key reused'):
            duet.dispatch('send', base | dict(sender='c', recipient='a', body='plain', dedupe='k2'))

    def test_upgrade_refuses_an_unofficial_origin_and_a_modified_tree(self):
        skill = self.root / 'agent-duet'
        skill.mkdir()
        with self.assertRaises(SystemExit) as stop:
            install.upgrade_source(skill)
        self.assertIn('official repository', str(stop.exception))
        self.git('remote', 'add', 'origin', 'https://github.com/someone-else/agent-duet.git')
        with self.assertRaises(SystemExit) as stop:
            install.upgrade_source(skill)
        self.assertIn('someone-else', str(stop.exception))
        self.git('remote', 'set-url', 'origin', 'https://github.com/JasonLiaoJCS/agent-duet.git')
        (self.root / '資料 檔.py').write_text('print("edited installer")\n', encoding='utf-8')
        with patch.object(install.subprocess, 'run', wraps=install.subprocess.run) as run, self.assertRaises(SystemExit) as stop:
            install.upgrade_source(skill)
        self.assertIn('local changes', str(stop.exception))
        self.assertFalse(any('pull' in call.args[0] for call in run.call_args_list))   # stopped before fetching anything


class UninstallOwnershipTests(unittest.TestCase):
    def test_a_registration_with_the_same_name_that_is_not_ours_is_left_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            desktop = home / 'claude_desktop_config.json'
            desktop.write_text(json.dumps({'mcpServers': {'agent-duet': {'command': 'node', 'args': ['/opt/other-tool/server.js']},
                                                          'another': {'command': 'x'}}}), encoding='utf-8')
            args = type('A', (), {'codex_home': home / 'codex', 'claude_home': home / 'claude', 'skip_desktop': False, 'desktop_config': desktop})()
            foreign = type('R', (), dict(returncode=0, stdout='agent-duet: node /opt/other-tool/server.js', stderr=''))()
            with patch.object(install, 'optional_cli', side_effect=lambda name: ([name], None)), \
                 patch.object(install.subprocess, 'run', return_value=foreign) as run:
                result = install.uninstall(args, home / 'claude')
            self.assertFalse(any('remove' in [str(part) for part in call.args[0]] for call in run.call_args_list))
            self.assertEqual(result['removed'], [])
            self.assertEqual(sum('not this bridge' in line for line in result['kept']), 3)
            self.assertIn('agent-duet', json.loads(desktop.read_text(encoding='utf-8'))['mcpServers'])

    def test_our_own_desktop_registration_is_removed_and_the_rest_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            desktop = home / 'claude_desktop_config.json'
            desktop.write_text(json.dumps({'mcpServers': {'agent-duet': {'command': 'python', 'args': ['C:\\Users\\x\\.claude\\skills\\agent-duet\\scripts\\duet.py', 'mcp']},
                                                          'another': {'command': 'x'}}}), encoding='utf-8')
            args = type('A', (), {'codex_home': home / 'codex', 'claude_home': home / 'claude', 'skip_desktop': False, 'desktop_config': desktop})()
            with patch.object(install, 'optional_cli', return_value=(None, 'missing')):
                result = install.uninstall(args, None)
            self.assertIn('Claude Desktop tool registration', result['removed'])
            self.assertEqual(list(json.loads(desktop.read_text(encoding='utf-8'))['mcpServers']), ['another'])


class OneLineInstallerTests(unittest.TestCase):
    def test_a_failed_download_stops_before_the_installer_runs(self):
        text = (REPO / 'get.ps1').read_text(encoding='utf-8')
        self.assertEqual(text.count('if ($LASTEXITCODE -ne 0) { throw'), 3)   # after pull, after clone, after install
        self.assertLess(text.index('Could not download Agent Duet'), text.index('$installer ='))
        shell = (REPO / 'get.sh').read_text(encoding='utf-8')
        self.assertIn('set -e', shell)
        self.assertIn('(: < /dev/tty) 2>/dev/null', shell)                    # really open the terminal, not just test the name


if __name__ == '__main__':
    unittest.main()
