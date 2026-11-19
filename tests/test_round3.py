"""Regression tests for the third Codex review (2.8), plus change-aware and scaled schedules."""
import isolate  # noqa: F401  (must come first)
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent-duet/scripts'))
import duet
import native
import schedule


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = patch.dict(os.environ, {'AGENT_DUET_HOME': self.home.name})
        env.start()
        self.addCleanup(env.stop)
        self.root = Path(self.tmp.name).resolve()
        self.base = {'root': str(self.root)}
        self.ids = {'c': str(uuid.uuid4()), 'a': str(uuid.uuid4())}
        r = self.call('join', name='c', provider='codex', session=self.ids['c'], project_name='Long task', goal='Train and evaluate')
        self.base['project_id'] = r['project']['id']
        self.call('join', name='a', provider='claude', session=self.ids['a'], address='peer [abc123]', title='peer', project_name='Long task', goal='Train and evaluate')

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

    def transport(self):
        return patch.multiple(native, relay_claude=lambda *a, **k: {'status': 'attempted', 'reported_cost_usd': 0.01})

    def due(self, minutes=0):
        """Run one due check the way the timer does, at now + minutes."""
        self.idle()
        now = time.time() + minutes * 60
        root, state, db = duet.connect(str(self.root))
        try:
            data = schedule.write(db)
            with patch.object(duet, 'cli', side_effect=lambda provider: [provider]), self.transport(), \
                 patch.object(native.subprocess, 'run', return_value=type('R', (), dict(returncode=0, stdout='Queued', stderr=''))()):
                entry, message_id = schedule.prepare(root, db, data, now)
                schedule.save(db, data)
                db.commit()
                if message_id is not None:
                    entry['result'] = schedule.deliver(root, db, message_id)
            return entry
        finally:
            db.close()

    def scheduler_mail(self, name, open_only=True):
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        try:
            sql = "SELECT body FROM messages WHERE sender='scheduler' AND recipient=?" + (' AND ack IS NULL' if open_only else '')
            return [r[0] for r in db.execute(sql, (name,))]
        finally:
            db.close()


class StopStaysStoppedTests(Case):
    def test_finish_during_a_check_in_is_not_undone_by_the_timer(self):
        self.call('schedule', name='a', preset='alternate')
        self.idle()
        root, state, db = duet.connect(str(self.root))
        try:
            data = schedule.write(db)
            entry, message_id = schedule.prepare(root, db, data, time.time() + 3600)
            schedule.save(db, data)
            db.commit()
            self.call('finish', name='a')                       # the user ends the project while the wake is in flight
            current = schedule.write(db)                        # what the timer does after the transport returns
            if current and current.get('id') == data.get('id'):
                for item in current.get('history', []):
                    if item.get('tick') == entry['tick']:
                        item['result'] = 'woken: queued'
                schedule.save(db, current)
            db.commit()
        finally:
            db.close()
        self.assertEqual(self.row()['state'], 'stopped')
        schedule.runner(str(self.root))                         # and a timer that looks again simply exits
        self.assertEqual(self.row()['state'], 'stopped')

    def test_next_check_set_during_a_wake_is_kept(self):
        self.call('schedule', name='a', mode='adaptive')
        self.idle()
        root, state, db = duet.connect(str(self.root))
        try:
            data = schedule.write(db)
            entry, _ = schedule.prepare(root, db, data, time.time() + 3600)
            schedule.save(db, data)
            db.commit()
            self.call('schedule_next', name='c', who='codex', minutes=200, reason='long run')
            current = schedule.write(db)
            for item in current.get('history', []):
                if item.get('tick') == entry['tick']:
                    item['result'] = 'woken: queued'
            schedule.save(db, current)
            db.commit()
        finally:
            db.close()
        row = self.row()
        self.assertEqual((row['next_who'], row['next_reason']), ('codex', 'long run'))
        self.assertEqual(row['history'][-1]['result'], 'woken: queued')

    def test_only_one_timer_is_started_however_many_calls_arrive_together(self):
        self.call('schedule', name='a', preset='alternate')
        started = []
        living = set()

        def launch(root):
            started.append(root)
            living.add(5000 + len(started))
            return type('P', (), {'pid': 5000 + len(started)})()
        with patch.dict(os.environ, {'AGENT_DUET_NO_SCHEDULER': '0'}), patch.object(schedule, 'launch', side_effect=launch), \
             patch.object(duet, 'process_birth', return_value='birth'), \
             patch.object(schedule, 'alive', side_effect=lambda data: data.get('pid') in living):
            for _ in range(5):                                    # the first call starts it; the rest see it alive
                self.call('inbox', name='a')
            self.assertEqual(len(started), 1)
            # While another process is in the middle of launching one, nobody starts a second.
            living.clear()
            root, state, db = duet.connect(str(self.root))
            data = schedule.write(db)
            data.update(pid=None, birth=None, claimed_at=time.time())
            schedule.save(db, data)
            db.commit()
            db.close()
            for _ in range(5):
                self.call('inbox', name='a')
            self.assertEqual(len(started), 1)

    def test_a_timer_that_dies_right_after_starting_is_restarted_by_the_next_call(self):
        launched = []

        def launch(root):
            launched.append(root)
            return type('P', (), {'pid': 6000 + len(launched)})()
        with patch.dict(os.environ, {'AGENT_DUET_NO_SCHEDULER': '0'}), patch.object(schedule, 'launch', side_effect=launch), \
             patch.object(duet, 'process_birth', return_value='birth'), patch.object(schedule, 'alive', return_value=False):
            self.call('schedule', name='a', preset='alternate')        # starts timer 1
            self.call('inbox', name='a')                               # seconds later it is gone: start timer 2
        self.assertEqual(len(launched), 2)
        self.assertEqual(self.row()['pid'], 6002)

    def test_wrong_project_id_does_not_restart_a_timer(self):
        self.call('schedule', name='a', preset='alternate')
        with patch.dict(os.environ, {'AGENT_DUET_NO_SCHEDULER': '0'}), patch.object(schedule, 'launch') as launch, \
             patch.object(schedule, 'alive', return_value=False):
            with self.assertRaises(ValueError):
                duet.dispatch('inbox', {'root': str(self.root), 'project_id': 'wrong', 'name': 'a'})
        launch.assert_not_called()

    def test_schedule_does_not_wake_conversations_into_a_background_run(self):
        import autopilot
        root, state, db = autopilot.database(str(self.root))
        db.execute("INSERT INTO autoruns(id,project_id,state,data,updated) VALUES('r1',?,'running','{}',?)", (self.base['project_id'], time.time()))
        db.commit()
        db.close()
        with self.assertRaisesRegex(ValueError, 'background supervisor'):
            self.call('schedule', name='a', preset='alternate')


class CheckInQualityTests(Case):
    def test_only_the_latest_check_in_stays_open_when_a_side_is_closed_for_days(self):
        self.call('schedule', name='a', mode='custom', pattern='codex:60')
        for hour in range(1, 6):
            self.due(hour * 60)
        open_mail = self.scheduler_mail('c')
        self.assertEqual(len(open_mail), 1)
        self.assertIn('4 earlier check-in(s) were not picked up', open_mail[0])
        self.assertEqual(len(self.scheduler_mail('c', open_only=False)), 5)

    def test_the_conversation_named_at_setup_is_the_one_woken(self):
        self.call('join', name='a-other', provider='claude', session=str(uuid.uuid4()), address='other [999999]', title='other', project_name='Long task', goal='Train and evaluate')
        self.call('schedule', name='a', mode='custom', pattern='claude:60')    # set by "a", although "a-other" was seen later
        self.call('inbox', name='a-other')
        self.due(60)
        self.assertEqual(len(self.scheduler_mail('a')), 1)
        self.assertEqual(self.scheduler_mail('a-other'), [])

    def test_a_watcher_peek_is_not_activity(self):
        self.idle()
        before = duet.dispatch('status', {'root': str(self.root)})['peers']
        self.call('inbox', name='c', peek=True)
        after = duet.dispatch('status', {'root': str(self.root)})['peers']
        seen = lambda peers: next(p['seen'] for p in peers if p['name'] == 'c')
        self.assertEqual(seen(before), seen(after))
        self.call('inbox', name='c')
        self.assertGreater(seen(duet.dispatch('status', {'root': str(self.root)})['peers']), seen(before))

    def test_an_interrupted_wake_does_not_hold_its_place_forever(self):
        self.call('native_start', name='a', peer='c', max_wakes=2, max_hours=8)
        run_id = self.call('native_status')['run']['id']
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        for message_id in (9001, 9002):
            db.execute('INSERT INTO native_attempts VALUES(?,?,?,?,?)', (run_id, message_id, 'sending', time.time() - 9 * 3600, '{}'))
        db.commit()
        db.close()
        with patch.object(duet, 'cli', return_value=['codex']), \
             patch.object(native.subprocess, 'run', return_value=type('R', (), dict(returncode=0, stdout='Queued', stderr=''))()):
            sent = self.call('send', sender='a', recipient='c', body='go', wake=True)
        self.assertEqual(sent['wake']['status'], 'queued')


class ChangeAwareTests(Case):
    def test_a_quiet_run_costs_no_turn_and_a_change_does(self):
        (self.root / 'runs').mkdir()
        log = self.root / 'runs' / 'train.log'
        log.write_text('epoch 1\n', encoding='utf-8')
        plan = self.call('schedule', name='a', mode='custom', pattern='codex:60', watch='runs/*.log', max_quiet_minutes=300)
        self.assertEqual(plan['watch'], ['runs/*.log'])
        self.assertEqual(self.due(60)['result'], 'skipped: nothing changed in the watched files')
        self.assertEqual(self.due(120)['result'], 'skipped: nothing changed in the watched files')
        self.assertEqual(self.scheduler_mail('c'), [])
        log.write_text('epoch 1\nepoch 2\n', encoding='utf-8')
        self.assertEqual(self.due(180)['result'], 'woken: queued')
        self.assertIn('watched files changed', self.scheduler_mail('c')[0])
        status = duet.dispatch('schedule_status', self.base)
        self.assertEqual((status['checks_done'], status['skipped_without_a_wake']), (1, 2))

    def test_silence_for_too_long_is_itself_a_reason_to_look(self):
        (self.root / 'train.log').write_text('epoch 1\n', encoding='utf-8')
        self.call('schedule', name='a', mode='custom', pattern='codex:60', watch='train.log', max_quiet_minutes=120)
        self.assertTrue(self.due(60)['result'].startswith('skipped'))
        self.assertEqual(self.due(180)['result'], 'woken: queued')
        self.assertIn('have not changed', self.scheduler_mail('c')[0])

    def test_watch_paths_stay_inside_the_project(self):
        for bad in ('../secrets', '/etc/passwd', 'C:/Windows', ','.join('f%d' % i for i in range(11))):
            with self.assertRaises(ValueError):
                self.call('schedule', name='a', preset='alternate', watch=bad)


class ScaleTests(Case):
    def test_a_rough_scale_is_a_hint_and_the_ai_stays_free(self):
        plan = self.call('schedule', name='a', mode='adaptive', around_minutes=60)
        self.assertEqual((plan['around_minutes'], plan['around_strict']), (60, False))
        self.assertEqual(plan['next_may_be_between_minutes'], [10, 720])
        self.assertEqual(self.call('schedule_next', name='c', minutes=400)['next_check']['in_minutes'], 400)
        self.due(400)
        body = self.scheduler_mail('c')[0] if self.scheduler_mail('c') else self.scheduler_mail('a')[0]
        self.assertIn('rough scale is a check about every 60 minutes', body)

    def test_a_strict_scale_keeps_every_check_between_half_and_double(self):
        plan = self.call('schedule', name='a', mode='adaptive', around_minutes=60, strict=True)
        self.assertEqual(plan['next_may_be_between_minutes'], [30, 120])
        late = self.call('schedule_next', name='c', minutes=400, reason='long run')
        self.assertEqual(late['next_check']['in_minutes'], 120)
        self.assertIn('adjusted', late['adjusted'])
        self.assertEqual(self.call('schedule_next', name='c', minutes=5)['next_check']['in_minutes'], 30)
        self.assertEqual(self.call('schedule_next', name='c', minutes=90)['next_check']['in_minutes'], 90)

    def test_nothing_runs_until_a_schedule_is_asked_for(self):
        self.assertEqual(duet.dispatch('schedule_status', self.base)['state'], 'none')
        with patch.object(schedule, 'launch') as launch:
            self.call('inbox', name='a')
            self.call('send', sender='a', recipient='c', body='hello')
            duet.dispatch('status', {'root': str(self.root)})
        launch.assert_not_called()


class FallbackAccuracyTests(Case):
    def test_task_text_is_not_a_refusal_and_plan_wording_is(self):
        self.assertFalse(duet.not_for_this_account('Tests failed: rate limit handling regressed'))
        self.assertFalse(duet.not_for_this_account('The quota module is not covered by tests'))
        self.assertTrue(duet.not_for_this_account('The account plan does not include this model'))
        self.assertTrue(duet.not_for_this_account("You've hit your usage limit for this model"))
        self.assertTrue(duet.not_for_this_account('{"type": "error", "error": {"code": "rate_limit_exceeded"}}'))

    def test_work_that_already_produced_output_is_never_redone_on_another_model(self):
        script = self.root / 'fake.py'
        script.write_text('import sys, json\nsys.stdin.read()\n'
                          'print(json.dumps({"result": "Your organization does not have access to this model", "is_error": True, '
                          '"usage": {"output_tokens": 812}}))\nsys.exit(1)\n', encoding='utf-8')
        with patch.object(duet, 'popen', return_value=type('P', (), {'pid': 1})()), patch.object(duet, 'process_birth', return_value='b'):
            job = self.call('consult', requester='a', provider='claude', prompt='x', tier='hard', reason='deep')['job_id']
        with patch.object(duet, 'cli', return_value=[sys.executable, str(script)]), patch.object(duet, 'next_route') as step_down:
            duet.run_worker(str(self.root), job)
        step_down.assert_not_called()
        self.assertEqual(self.call('job', job_id=job)['status'], 'failed')

    def test_an_earlier_attempts_answer_is_never_reported_as_the_next_ones(self):
        job_folder = None
        script = self.root / 'fake_codex.py'
        script.write_text(
            'import sys, json, pathlib\n'
            'args = sys.argv\n'
            'model = args[args.index("-m") + 1]\n'
            'out = pathlib.Path(args[args.index("-o") + 1])\n'
            'sys.stdin.read()\n'
            'if model.endswith("astra"):\n'
            '    print(json.dumps({"type": "error", "message": "The model gpt-6.2-astra is not available on your plan"}))\n'
            '    sys.exit(1)\n'
            'print(json.dumps({"type": "turn.completed", "usage": {}}))\n', encoding='utf-8')   # succeeds but writes no answer
        efforts = [{'reasoningEffort': 'high'}]
        catalog = [{'id': m, 'model': m, 'description': 'Latest.', 'hidden': False, 'supportedReasoningEfforts': efforts} for m in ('gpt-6.2-sol', 'gpt-6.2-astra')]
        with patch.object(duet, 'popen', return_value=type('P', (), {'pid': 1})()), patch.object(duet, 'process_birth', return_value='b'):
            job = self.call('consult', requester='a', provider='codex', prompt='x', tier='hard', reason='deep')['job_id']
        stale = self.root / '.agent-duet' / 'jobs' / job
        stale.mkdir(parents=True)
        (stale / 'answer.txt').write_text('answer left by an earlier attempt', encoding='utf-8')
        with patch.object(duet, 'cli', return_value=[sys.executable, str(script)]), patch.object(duet, 'codex_models', return_value=catalog):
            duet.run_worker(str(self.root), job)
        result = self.call('job', job_id=job)
        self.assertEqual(result['status'], 'failed')                       # no answer was written by the model that ran
        self.assertEqual(result['result']['answer'], '')
        self.assertEqual(result['result']['routing']['model'], 'gpt-6.2-sol')


class HonestReportTests(Case):
    def test_headless_machine_gets_the_command_not_a_claim_and_no_cooldown(self):
        with patch.dict(os.environ, {'AGENT_DUET_NO_LOGIN_PROMPT': '0'}), \
             patch.object(duet, 'open_login', return_value={'opened': False, 'run_this': 'claude auth login'}), \
             patch.object(duet, 'notify_user', return_value=False):
            first = duet.prompt_login('claude')
            second = duet.prompt_login('claude')
        for result in (first, second):                                       # asked again, it tries again
            self.assertFalse(result['prompted'])
            self.assertEqual(result['run_this'], 'claude auth login')
            self.assertIn('no window or notification could be shown', result['tell_the_user'])

    def test_notification_only_is_reported_as_exactly_that(self):
        with patch.dict(os.environ, {'AGENT_DUET_NO_LOGIN_PROMPT': '0'}), \
             patch.object(duet, 'open_login', return_value={'opened': False}), patch.object(duet, 'notify_user', return_value=True):
            result = duet.prompt_login('codex')
        self.assertTrue(result['prompted'])
        self.assertFalse(result['sign_in_window_opened'])
        self.assertIn('codex login', result['tell_the_user'])
        self.assertNotIn('window was opened', result['tell_the_user'])

    def test_unknown_sign_in_state_is_not_reported_as_all_good(self):
        efforts = [{'reasoningEffort': 'high'}]
        catalog = [{'id': m, 'model': m, 'description': 'Latest.', 'hidden': False, 'supportedReasoningEfforts': efforts} for m in ('gpt-6.2-sol', 'gpt-6.2-astra')]
        with patch.object(duet, 'cli', side_effect=lambda provider: [provider]), patch.object(duet, 'codex_models', return_value=catalog), \
             patch.object(duet, 'installed_version', return_value='cli 1.0.0'), patch.object(duet, 'latest_published', return_value='1.0.0'), \
             patch.object(duet, 'logged_in', return_value=None), \
             patch.object(duet.subprocess, 'run', return_value=type('R', (), dict(returncode=0, stdout='9.9.999', stderr=''))()):
            report = duet.dispatch('doctor', {'fix': True})
        self.assertFalse(report['all_good'])
        self.assertEqual(len(report['could_not_check']), 2)
        self.assertEqual(report['needs_user'], [])

    def test_an_empty_slot_file_is_reclaimed(self):
        slots = Path(self.home.name) / 'slots'
        slots.mkdir()
        for number in range(3):
            (slots / ('slot-%d' % number)).write_text('', encoding='utf-8')
        old = time.time() - 300
        for path in slots.iterdir():
            os.utime(path, (old, old))
        with duet.worker_slot(lambda: self.fail('must not wait')) as slot:
            self.assertIsNotNone(slot.path)

    def test_worktree_style_git_file_still_gets_the_exclude(self):
        import shutil
        import subprocess
        if not shutil.which('git'):
            self.skipTest('git is not installed')
        with tempfile.TemporaryDirectory() as folder:
            main = Path(folder) / 'main'
            main.mkdir()
            run = lambda *args, cwd=main: subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t', *args], cwd=cwd, capture_output=True, check=True)
            run('init', '-q')
            (main / 'f.txt').write_text('x', encoding='utf-8')
            run('add', 'f.txt')
            run('commit', '-q', '-m', 'init')
            tree = Path(folder) / 'tree'
            run('worktree', 'add', '-q', str(tree))
            self.assertTrue((tree / '.git').is_file())
            joined = duet.dispatch('join', dict(root=str(tree), name='c', provider='codex', session=str(uuid.uuid4()), project_name='P', goal='G'))
            self.assertEqual(joined['git'], 'added to the local git exclude list')
            status = subprocess.run(['git', 'status', '--porcelain'], cwd=tree, capture_output=True, text=True).stdout
            self.assertNotIn('.agent-duet', status)


if __name__ == '__main__':
    unittest.main()
