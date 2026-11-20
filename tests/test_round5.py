"""Regression tests for the fifth Codex review (before 3.1), and for the bugs the live test on 2026-11-20 found."""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent-duet/scripts'))
import duet
import install
import native
import schedule

LIMIT = "You've hit your 5-hour limit · resets 11pm (Asia/Taipei)"


class Pair(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = patch.dict(os.environ, {'AGENT_DUET_HOME': self.home.name})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.base = {'root': str(self.root)}
        r = self.call('join', name='c', provider='codex', session=str(uuid.uuid4()), project_name='P', goal='G')
        self.base['project_id'] = r['project']['id']
        self.call('join', name='a', provider='claude', session=str(uuid.uuid4()), address='p [1]', title='p', project_name='P', goal='G')

    def call(self, op, **kw):
        return duet.dispatch(op, self.base | kw)

    def codex_ok(self):
        return patch.object(native.subprocess, 'run', return_value=type('R', (), dict(returncode=0, stdout='Queued', stderr=''))())


class AccountLimitAccuracyTests(Pair):
    def test_ordinary_sentences_about_limits_never_block_a_vendor(self):
        for text in ('the rate limit resets on retry', 'limit resets when the cache expires', 'tests about rate limit handling failed',
                     'The session limit option resets the counter'):
            self.assertIsNone(duet.account_limit(text), text)

    def test_a_successful_courier_that_mentions_a_limit_is_not_a_limit(self):
        m = self.call('send', sender='c', recipient='a', body='x')['message_id']
        reply = {'status': 'attempted', 'response': "Delivered. (You've hit your 5-hour limit is a phrase in the message.)", 'reported_cost_usd': 0.01, 'exit_code': 0}
        with patch.object(native, 'relay_claude', return_value=reply):
            self.call('native_wake', name='c', message_id=m)
        self.assertIsNone(duet.account_limited('claude'))

    def test_a_codex_error_is_judged_on_its_message_not_on_echoed_context(self):
        script = self.root / 'fake_codex.py'
        script.write_text('import sys, json\nsys.stdin.read()\n'
                          'print(json.dumps({"type": "error", "message": "stream disconnected", "context": "%s"}))\nsys.exit(1)\n' % LIMIT.replace("'", "\\'"),
                          encoding='utf-8')
        efforts = [{'reasoningEffort': 'high'}]
        catalog = [{'id': 'gpt-6.2-sol', 'model': 'gpt-6.2-sol', 'description': 'Latest.', 'hidden': False, 'supportedReasoningEfforts': efforts}]
        with patch.object(duet, 'popen', return_value=type('P', (), {'pid': 1})()), patch.object(duet, 'process_birth', return_value='b'):
            job = self.call('consult', requester='a', provider='codex', prompt='x')['job_id']
        with patch.object(duet, 'cli', return_value=[sys.executable, str(script)]), patch.object(duet, 'codex_models', return_value=catalog):
            duet.run_worker(str(self.root), job)
        self.assertIsNone(duet.account_limited('codex'))
        self.assertNotIn('account_limit', self.call('job', job_id=job)['result'])

    def test_a_limit_reported_only_on_stderr_is_still_recognised(self):
        script = self.root / 'fake_claude.py'
        script.write_text('import sys\nsys.stdin.read()\nsys.stderr.write(%r)\nprint("not json")\nsys.exit(1)\n' % LIMIT, encoding='utf-8')
        with patch.object(duet, 'popen', return_value=type('P', (), {'pid': 1})()), patch.object(duet, 'process_birth', return_value='b'):
            job = self.call('consult', requester='a', provider='claude', prompt='x')['job_id']
        with patch.object(duet, 'cli', return_value=[sys.executable, str(script)]):
            duet.run_worker(str(self.root), job)
        self.assertIsNotNone(duet.account_limited('claude'))

    def test_doctor_does_not_call_a_limited_vendor_ready(self):
        duet.mark_account_limited('claude', LIMIT, '11pm')
        with patch.object(duet, 'cli', side_effect=lambda provider: [provider]), \
             patch.object(duet.subprocess, 'run', return_value=type('R', (), dict(returncode=0, stdout='9.9.999', stderr=''))()), \
             patch.object(duet, 'models_check_due', return_value=False):
            report = duet.doctor()
        self.assertFalse(report['ready']['consult_claude'])
        self.assertTrue(report['ready']['consult_codex'])

    def test_a_wake_that_was_certainly_not_sent_can_be_sent_later(self):
        m = self.call('send', sender='c', recipient='a', body='x')['message_id']
        with patch.object(native, 'relay_claude', return_value={'status': 'not_sent', 'response': LIMIT, 'exit_code': 1, 'reported_cost_usd': 0.02}):
            first = self.call('native_wake', name='c', message_id=m)
        self.assertEqual(first['status'], 'not_sent')
        duet.change_models_state(lambda state: state.pop('account_limit', None))
        with patch.object(native, 'relay_claude', return_value={'status': 'attempted', 'reported_cost_usd': 0.01}) as courier:
            again = self.call('native_wake', name='c', message_id=m)
        courier.assert_called_once()
        self.assertEqual(again['status'], 'attempted')
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        stored = json.loads(db.execute('SELECT result FROM native_attempts WHERE message_id=?', (m,)).fetchone()[0])
        db.close()
        self.assertAlmostEqual(stored['carried_cost_usd'], 0.02)          # the first try still counts in the window


class StopRaceTests(Pair):
    def test_a_finish_that_lands_after_the_reservation_still_stops_the_wake(self):
        m = self.call('send', sender='a', recipient='c', body='go')['message_id']
        self.call('native_start', name='a', peer='c')
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        # The moment the wake reserves its transport call, the user finishes the project.
        db.execute("CREATE TRIGGER finish_now AFTER INSERT ON native_attempts BEGIN UPDATE native_run SET state='completed'; END")
        db.commit()
        db.close()
        with patch.object(duet, 'cli', return_value=['codex']), self.codex_ok() as transport:
            result = self.call('native_wake', name='a', message_id=m)
        self.assertFalse(any('--thread' in call.args[0] for call in transport.call_args_list if call.args))
        self.assertEqual(result['status'], 'not_sent')
        self.assertIn('paused or finished', result['error'])

    def test_pausing_before_the_first_pairing_then_starting_brings_the_schedule_back(self):
        self.call('schedule', name='a', preset='alternate')
        self.call('finish', name='a', pause_only=True)
        self.assertIn('paused', duet.dispatch('schedule_status', self.base)['state'])
        started = self.call('native_start', name='a', peer='c')
        self.assertTrue(started['schedule_resumed'])
        self.assertEqual(duet.dispatch('schedule_status', self.base)['state'], 'active')

    def test_finishing_a_paused_project_ends_the_schedule_for_good(self):
        self.call('schedule', name='a', preset='alternate')
        self.call('finish', name='a', pause_only=True)
        done = self.call('finish', name='a')
        self.assertIn('ended', ' '.join(done['stopped']))
        self.assertEqual(duet.dispatch('schedule_status', self.base)['state'], 'stopped')
        self.assertFalse(self.call('native_start', name='a', peer='c')['schedule_resumed'])


class TimerTokenTests(Pair):
    def test_a_timer_not_launched_for_the_current_claim_exits(self):
        self.call('schedule', name='a', preset='alternate')
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        data = json.loads(db.execute('SELECT data FROM schedule').fetchone()[0])
        data.update(pid=None, birth=None, claim='someone-elses-token', claimed_at=time.time())
        db.execute('UPDATE schedule SET data=?', (json.dumps(data),))
        db.commit()
        db.close()
        schedule.runner(str(self.root), token='mine')                    # returns at once instead of taking over
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        self.assertIsNone(json.loads(db.execute('SELECT data FROM schedule').fetchone()[0])['pid'])
        db.close()

    def test_exactly_the_limit_is_not_reported_as_too_many(self):
        (self.root / 'd').mkdir()
        for index in range(5):
            (self.root / 'd' / ('f%d' % index)).write_text('x', encoding='utf-8')
        with patch.object(schedule, 'SCAN_LIMIT', 5):
            self.assertEqual(schedule.scan(self.root, ['d'])[1:], (5, False))
            (self.root / 'd' / 'f5').write_text('x', encoding='utf-8')
            self.assertEqual(schedule.scan(self.root, ['d'])[1:], (5, True))


class FooterTests(Pair):
    def test_a_message_that_merely_ends_like_a_footer_is_not_a_retry(self):
        self.call('send', sender='c', recipient='a', body='note\n\n[git: notes]', kind='result', git=False, dedupe='k')
        with self.assertRaisesRegex(ValueError, 'dedupe key reused'):
            self.call('send', sender='c', recipient='a', body='note', kind='result', git=False, dedupe='k')

    def test_the_footer_is_computed_before_the_mailbox_is_locked(self):
        held = []

        def slow_git(root):
            probe = sqlite3.connect(self.root / '.agent-duet/state.sqlite3', timeout=0.2)
            try:
                probe.execute('BEGIN IMMEDIATE')
                probe.rollback()
                held.append(False)
            except sqlite3.OperationalError:
                held.append(True)
            finally:
                probe.close()
            return '[git: main@abc]'
        with patch.object(duet, 'git_context', side_effect=slow_git):
            sent = self.call('send', sender='c', recipient='a', body='done', kind='result')
        self.assertEqual(held, [False])
        self.assertEqual(sent['code_version'], '[git: main@abc]')
        again = self.call('send', sender='c', recipient='a', body='done', kind='result', dedupe='x')
        with patch.object(duet, 'git_context', return_value='[git: main@def]'):
            self.assertTrue(self.call('send', sender='c', recipient='a', body='done', kind='result', dedupe='x')['duplicate'])
        self.assertTrue(again['message_id'])


class JoinProjectIdTests(unittest.TestCase):
    def test_a_project_id_for_an_empty_folder_is_refused_instead_of_starting_a_second_project(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, 'no Agent Duet project yet'):
                duet.dispatch('join', dict(root=folder, project_id='abc', name='c', provider='codex', session=str(uuid.uuid4()),
                                           project_name='P', goal='G'))
            self.assertEqual(duet.dispatch('status', {'root': folder})['project'], None)


class InstallerOwnershipTests(unittest.TestCase):
    def test_only_the_exact_official_repository_counts(self):
        for url in ('https://github.com/JasonLiaoJCS/agent-duet.git', 'https://github.com/JasonLiaoJCS/agent-duet',
                    'git@github.com:JasonLiaoJCS/agent-duet.git', 'ssh://git@github.com/JasonLiaoJCS/agent-duet.git'):
            self.assertTrue(install.is_official(url), url)
        for url in ('https://github.com/JasonLiaoJCS/agent-duet-fork.git', 'https://github.com/someone/agent-duet',
                    'https://evil.example/JasonLiaoJCS/agent-duet', 'https://github.com/JasonLiaoJCS/agent-duet/../x', ''):
            self.assertFalse(install.is_official(url), url)

    def test_only_our_own_command_line_counts_as_ours(self):
        self.assertTrue(install.ours(r'args: C:\Users\x\.codex\skills\agent-duet\scripts\duet.py mcp'))
        self.assertTrue(install.ours('Args: /home/u/.claude/skills/agent-duet/scripts/duet.py mcp'))
        for other in ('node /opt/other-tool/duet.py', 'python /opt/other-tool/duet.py mcp', 'python /x/skills/agent-duet/scripts/duet.py serve'):
            self.assertFalse(install.ours(other), other)

    @unittest.skipUnless(shutil.which('git'), 'git is not installed')
    def test_an_untracked_file_in_the_checkout_stops_the_upgrade(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            run = lambda *args: subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t', *args], cwd=repo, capture_output=True, check=True)
            run('init', '-q', '-b', 'main')
            (repo / 'agent-duet').mkdir()
            (repo / 'agent-duet' / 'SKILL.md').write_text('x', encoding='utf-8')
            run('add', '-A')
            run('commit', '-q', '-m', 'init')
            run('remote', 'add', 'origin', 'https://github.com/JasonLiaoJCS/agent-duet.git')
            (repo / 'agent-duet' / 'extra.py').write_text('print("would be installed")', encoding='utf-8')
            with self.assertRaises(SystemExit) as stop:
                install.upgrade_source(repo / 'agent-duet')
            self.assertIn('local changes', str(stop.exception))


class LiveTestFindings(Pair):
    """Found by the live scheduled wake on 2026-11-20."""
    def test_a_check_in_can_be_answered_by_a_handoff_to_the_peer(self):
        self.call('schedule', name='c', mode='custom', pattern='claude:60')
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        db.execute('UPDATE peers SET seen=?', (time.time() - 7200,))
        db.commit()
        db.close()
        root, state, con = duet.connect(str(self.root))
        try:
            data = schedule.write(con)
            entry, message_id = schedule.prepare(root, con, data, time.time() + 3600)
            schedule.save(con, data)
            con.commit()
        finally:
            con.close()
        self.assertTrue(self.call('send', sender='a', recipient='c', body='all good', kind='result', reply_to=message_id)['acknowledged_original'])

    def test_errors_name_the_argument_that_was_wrong(self):
        with self.assertRaisesRegex(ValueError, 'unknown: projectid'):
            duet.check_arguments('join', {'root': 'x', 'projectid': 'y', 'project_name': 'P', 'goal': 'G', 'name': 'c', 'provider': 'codex'})


if __name__ == '__main__':
    unittest.main()
