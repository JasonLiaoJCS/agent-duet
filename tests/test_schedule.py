"""Shift schedule for long tasks, and the one call that ends everything."""
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
        self.root = Path(self.tmp.name).resolve()
        self.base = {'root': str(self.root)}
        r = self.call('join', name='c', provider='codex', session=str(uuid.uuid4()), project_name='Long task', goal='Train and evaluate')
        self.base['project_id'] = r['project']['id']
        self.call('join', name='a', provider='claude', session=str(uuid.uuid4()), address='peer [abc123]', title='peer', project_name='Long task', goal='Train and evaluate')
        self.clock = [time.time()]

    def call(self, op, **kw):
        return duet.dispatch(op, self.base | kw)

    def data(self):
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        try:
            return json.loads(db.execute('SELECT data FROM schedule').fetchone()[0])
        finally:
            db.close()

    def idle(self, minutes=60):
        """Nobody touched the bridge for a while."""
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        db.execute('UPDATE peers SET seen=?', (time.time() - minutes * 60,))
        db.commit()
        db.close()

    def fire(self, advance_minutes=0):
        """What the timer does when a check-in is due."""
        self.clock[0] += advance_minutes * 60
        root, state, db = duet.connect(str(self.root))
        db.execute('UPDATE peers SET seen=?', (self.clock[0] - 3600,))
        try:
            data = schedule.load(db)
            with patch.object(duet, 'cli', side_effect=lambda provider: [provider]), \
                 patch.object(native.subprocess, 'run', return_value=type('R', (), dict(returncode=0, stdout='Queued', stderr=''))()), \
                 patch.object(native, 'relay_claude', return_value={'status': 'attempted', 'reported_cost_usd': 0.01}), \
                 patch.object(native.time, 'time', side_effect=lambda: self.clock[0]):
                entry = schedule.fire(root, db, data, self.clock[0])
            schedule.save(db, data)
            db.commit()
            return entry
        finally:
            db.close()

    def mail(self, name):
        root, state, db = duet.connect(str(self.root))
        try:
            return [dict(r) for r in db.execute("SELECT * FROM messages WHERE recipient=? AND sender='scheduler' ORDER BY id", (name,))]
        finally:
            db.close()


class PresetTests(Case):
    def test_alternate_starts_with_the_other_side_and_takes_turns(self):
        plan = self.call('schedule', name='a', preset='alternate', note='watch the training loss')
        self.assertTrue(plan['started'])
        self.assertEqual(plan['next_check']['who'], 'codex')            # Claude set it up, so Codex looks first
        self.assertEqual(plan['next_check']['in_minutes'], 60)
        self.assertEqual([step['who'] for step in plan['cycle']], ['codex', 'claude'])
        order = [self.fire(60)['who'] for _ in range(4)]
        self.assertEqual(order, ['codex', 'claude', 'codex', 'claude'])

    def test_codex_heavy_is_claude_once_codex_twice_per_three_hours(self):
        plan = self.call('schedule', name='a', preset='codex-heavy')
        self.assertEqual(sum(step['after_minutes'] for step in plan['cycle']), 180)
        order = [self.fire(60)['who'] for _ in range(6)]
        self.assertEqual(sorted(order[:3]), ['claude', 'codex', 'codex'])
        self.assertEqual(order[:3], order[3:])

    def test_interval_can_be_changed_and_every_preset_is_valid(self):
        self.assertEqual(self.call('schedule', name='a', preset='alternate', interval_minutes=45)['next_check']['in_minutes'], 45)
        for preset in schedule.PRESETS:
            self.assertTrue(self.call('schedule', name='c', preset=preset)['started'])
        with self.assertRaises(ValueError):
            self.call('schedule', name='a', preset='alternate', interval_minutes=2)

    def test_custom_pattern(self):
        plan = self.call('schedule', name='a', mode='custom', pattern='claude:60, codex:60, codex:90')
        self.assertEqual([(s['who'], s['after_minutes']) for s in plan['cycle']], [('codex', 60), ('codex', 90), ('claude', 60)])
        for bad in ('gemini:60', 'claude:5', '', 'claude:60,' * 13):
            with self.assertRaises(ValueError):
                self.call('schedule', name='a', mode='custom', pattern=bad)

    def test_both_sides_must_have_joined(self):
        with tempfile.TemporaryDirectory() as folder:
            r = duet.dispatch('join', dict(root=folder, name='a', provider='claude', session=str(uuid.uuid4()), address='x [1]', project_name='P', goal='G'))
            with self.assertRaisesRegex(ValueError, 'codex'):
                duet.dispatch('schedule', dict(root=folder, project_id=r['project']['id'], name='a'))

    def test_status_without_a_schedule_lists_the_choices(self):
        status = duet.dispatch('schedule_status', self.base)
        self.assertEqual(status['state'], 'none')
        self.assertIn('codex-heavy', status['presets'])


class CheckInTests(Case):
    def test_a_due_check_leaves_mail_and_wakes_the_right_conversation(self):
        self.call('schedule', name='a', preset='alternate', note='watch the training loss')
        entry = self.fire(60)
        self.assertEqual(entry['result'], 'woken: queued')
        mail = self.mail('c')
        self.assertEqual(len(mail), 1)
        self.assertIn('Scheduled check-in #1', mail[0]['body'])
        self.assertIn('watch the training loss', mail[0]['body'])
        self.assertEqual(self.fire(60)['result'], 'woken: attempted')     # Claude's turn goes through its courier
        self.assertEqual(len(self.mail('a')), 1)
        status = duet.dispatch('schedule_status', self.base)
        self.assertEqual(status['checks_done'], 2)
        self.assertIn('wake the other at any time', status['reminder'])

    def test_a_conversation_that_is_already_at_work_is_not_woken_again(self):
        self.call('schedule', name='a', preset='alternate')
        root, state, db = duet.connect(str(self.root))
        try:
            data = schedule.load(db)
            db.execute("UPDATE peers SET seen=? WHERE name='c'", (time.time() - 120,))
            with patch.object(native.subprocess, 'run') as transport:
                entry = schedule.fire(root, db, data, time.time())
            transport.assert_not_called()
        finally:
            db.close()
        self.assertIn('active', entry['result'])
        self.assertEqual(self.mail('c'), [])

    def test_a_paused_collaboration_is_not_woken_but_the_schedule_goes_on(self):
        self.call('schedule', name='a', preset='alternate')
        self.fire(60)
        self.call('native_stop', name='a', state='paused')
        self.assertIn('paused', self.fire(60)['result'])
        self.assertEqual(self.data()['state'], 'active')

    def test_urgent_wake_between_check_ins_still_works(self):
        self.call('schedule', name='a', preset='overnight')
        with patch.object(duet, 'cli', return_value=['codex']), \
             patch.object(native.subprocess, 'run', return_value=type('R', (), dict(returncode=0, stdout='Queued', stderr=''))()):
            sent = self.call('send', sender='a', recipient='c', body='the run crashed, look now', wake=True)
        self.assertEqual(sent['wake']['status'], 'queued')

    def test_scheduled_wakes_share_the_rate_limit_and_cannot_be_forged(self):
        self.call('schedule', name='a', preset='quick')
        self.call('native_start', name='a', peer='c', max_wakes=2, max_hours=8)
        results = [self.fire(30)['result'] for _ in range(3)]
        self.assertEqual(results[:2], ['woken: queued', 'woken: attempted'])
        self.assertEqual(results[2], 'woken: limit_reached')
        with self.assertRaises(ValueError):
            duet.check_arguments('native_wake', self.base | {'name': 'a', 'message_id': 1, '_system': True})


class AdaptiveTests(Case):
    def test_the_checking_side_decides_the_next_check_and_a_forgotten_decision_has_a_fallback(self):
        plan = self.call('schedule', name='a', mode='adaptive', first_minutes=20)
        self.assertEqual((plan['next_check']['who'], plan['next_check']['in_minutes']), ('codex', 20))
        self.fire(20)
        self.assertIn('duet_schedule_next', self.mail('c')[0]['body'])
        pending = duet.dispatch('schedule_status', self.base)
        self.assertEqual(pending['next_check']['who'], 'claude')          # fallback: the other side, after the default interval
        decided = self.call('schedule_next', name='c', who='codex', minutes=180, reason='training runs for three more hours')
        self.assertEqual((decided['next_check']['who'], decided['next_check']['in_minutes']), ('codex', 180))
        self.assertIn('three more hours', decided['next_check']['reason'])
        for bad in (5, 5000):
            with self.assertRaises(ValueError):
                self.call('schedule_next', name='c', minutes=bad)

    def test_next_defaults_to_the_other_side(self):
        self.call('schedule', name='a', mode='adaptive')
        self.assertEqual(self.call('schedule_next', name='c', minutes=30)['next_check']['who'], 'claude')
        self.assertEqual(self.call('schedule_next', name='a', minutes=30)['next_check']['who'], 'codex')


class TimerTests(Case):
    def test_timer_comes_back_by_itself_after_it_died(self):
        self.call('schedule', name='a', preset='alternate')
        started = type('P', (), {'pid': 4242})()
        with patch.dict(os.environ, {'AGENT_DUET_NO_SCHEDULER': '0'}), patch.object(schedule, 'launch', return_value=started) as launch, \
             patch.object(duet, 'process_birth', return_value='birth'), patch.object(schedule, 'alive', return_value=False):
            self.call('inbox', name='a')                                  # any ordinary use of the bridge
        launch.assert_called_once()
        self.assertEqual(self.data()['pid'], 4242)

    def test_schedule_ends_at_its_time_and_tells_whoever_set_it(self):
        self.call('schedule', name='a', preset='alternate', hours=1)
        db = sqlite3.connect(self.root / '.agent-duet/state.sqlite3')
        data = json.loads(db.execute('SELECT data FROM schedule').fetchone()[0])
        data['until'] = time.time() - 1
        db.execute('UPDATE schedule SET data=?', (json.dumps(data),))
        db.commit()
        db.close()
        schedule.runner(str(self.root))                                   # returns at once instead of looping
        self.assertEqual(self.data()['state'], 'finished')
        self.assertIn('reached its end', self.mail('a')[0]['body'])

    def test_overdue_checks_after_sleep_do_not_pile_up(self):
        self.call('schedule', name='a', preset='quick')
        self.fire(advance_minutes=600)                                    # the computer slept for ten hours
        left = self.data()['next_due'] - self.clock[0]
        self.assertAlmostEqual(left, 30 * 60, delta=5)                    # one check, then counted from now
        self.assertEqual(self.data()['checks_done'], 1)


class FinishTests(Case):
    def test_finish_stops_everything_and_leaves_nothing_running(self):
        self.call('schedule', name='a', preset='alternate')
        self.fire(60)
        self.call('claim', name='a', resource='file:train.py')
        with patch.object(duet, 'popen', return_value=type('P', (), {'pid': 1})()), patch.object(duet, 'process_birth', return_value='b'):
            job = self.call('consult', requester='a', provider='codex', prompt='review')['job_id']
        done = self.call('finish', name='a', summary='Model trained, report written', evidence='eval 0.93')
        self.assertTrue(done['finished'])
        text = ' '.join(done['stopped'])
        for part in ('check-in schedule', 'wakeups', 'file claim'):
            self.assertIn(part, text)
        self.assertEqual(done['still_running'], [])
        self.assertEqual(self.data()['state'], 'stopped')
        status = duet.dispatch('status', {'root': str(self.root)})
        self.assertEqual(status['overview']['pairing']['state'], 'completed')
        self.assertEqual(status['claims'], [])
        self.assertTrue(status['latest_checkpoint']['summary'].startswith('Finished: Model trained'))
        self.assertIn(self.call('job', job_id=job)['status'], duet.TERMINAL)   # no consultation is left active
        with patch.object(native.subprocess, 'run') as transport:         # and nothing wakes anyone afterwards
            blocked = self.call('send', sender='a', recipient='c', body='x', wake=True)
        transport.assert_not_called()
        self.assertEqual(blocked['wake']['status'], 'not_sent')
        self.assertEqual(self.call('finish', name='a')['stopped'], ['nothing was running'])

    def test_pause_only_parks_the_project_and_a_new_schedule_resumes_it(self):
        self.call('schedule', name='a', preset='alternate')
        self.fire(60)
        parked = self.call('finish', name='a', pause_only=True)
        self.assertTrue(parked['parked'])
        self.assertEqual(duet.dispatch('status', {'root': str(self.root)})['overview']['pairing']['state'], 'paused')
        again = self.call('schedule', name='a', preset='relaxed')
        self.assertTrue(again['wakeups_resumed'])
        self.assertEqual(self.fire(120)['result'], 'woken: queued')

    def test_finish_needs_the_right_project_and_a_real_boolean(self):
        with self.assertRaises(ValueError):
            duet.dispatch('finish', {'root': str(self.root), 'project_id': 'wrong', 'name': 'a'})
        with self.assertRaises(ValueError):
            self.call('finish', name='a', pause_only='yes')


if __name__ == '__main__':
    unittest.main()
