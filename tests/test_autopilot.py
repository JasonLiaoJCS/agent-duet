import isolate  # noqa: F401  (must come first)
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent-duet/scripts'))
import duet
import autopilot as auto
from channels import ChannelWatch

def result(status,evidence=None,summary='Done',next_task=''):
    return {'status':status,'summary':summary,'next_task':next_task,'evidence':evidence or []}

class AutonomousTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        joined=duet.dispatch('join',{'root':str(self.root),'name':'owner','provider':'codex','session':'test','project_name':'Test','goal':'Write and verify'})
        self.base={'root':str(self.root),'project_id':joined['project']['id']}
    def tearDown(self):
        self.temp.cleanup()
    def start(self,**kwargs):
        with patch.object(auto,'launch',return_value=SimpleNamespace(pid=os.getpid())):
            return auto.operate('autopilot_start',self.base|{'requester':'owner','goal':'Write and verify','acceptance':'Contents equal 55','max_turns':4}|kwargs)
    def test_start_scopes_and_duplicate(self):
        run=self.start()
        self.assertEqual(run['project_id'],self.base['project_id'])
        with self.assertRaises(ValueError):self.start()
        with self.assertRaises(ValueError):
            auto.operate('autopilot_status',self.base|{'project_id':'wrong','run_id':run['run_id']})
    def test_nested_start_rejected(self):
        with patch.dict(os.environ,{'AGENT_DUET_DEPTH':'1'}):
            with self.assertRaises(ValueError):self.start()
    def test_implementer_cannot_self_complete(self):
        started=self.start();_,_,db=auto.database(self.root)
        d=auto.load(db,started['run_id'])['data'];db.close()
        state,d=auto.advance(d,result('complete',['file created']),'job1')
        self.assertEqual(state,'running');self.assertEqual(d['phase'],'review');self.assertEqual(d['provider'],'claude')
        state,d=auto.advance(d,result('complete',['Read file and ran test; passes']),'job2')
        self.assertEqual(state,'completed')
    def test_reviewer_must_have_evidence(self):
        started=self.start();_,_,db=auto.database(self.root)
        d=auto.load(db,started['run_id'])['data'];db.close()
        _,d=auto.advance(d,result('ready',['created']),'a')
        state,d=auto.advance(d,result('complete'),'b')
        self.assertNotEqual(state,'completed')
    def test_needs_user_stops(self):
        started=self.start();_,_,db=auto.database(self.root)
        d=auto.load(db,started['run_id'])['data'];db.close()
        state,_=auto.advance(d,result('needs_user',summary='Missing API login'),'j')
        self.assertEqual(state,'needs_user')
    def test_parse_rejects_unstructured_success(self):
        with self.assertRaises(ValueError):auto.parse_report('All done!')
        self.assertEqual(auto.parse_report('text\nDUET_RESULT: '+json.dumps(result('ready')))['status'],'ready')
    def test_full_handoff_loop_and_checkpoint(self):
        started=self.start()
        real=duet.dispatch;calls=[]
        def fake(op,args):
            if op=='consult':
                calls.append(args);return {'job_id':'job'+str(len(calls))}
            if op=='job':
                report=result('ready' if len(calls)==1 else 'complete',['fixture verified'])
                return {'id':args['job_id'],'status':'completed','result':{'answer':'DUET_RESULT: '+json.dumps(report)}}
            return real(op,args)
        with patch.object(duet,'dispatch',side_effect=fake):auto.runner(self.root,started['run_id'])
        final=auto.operate('autopilot_status',self.base|{'run_id':started['run_id']})
        self.assertEqual(final['state'],'completed',final['data'])
        self.assertEqual([c['provider'] for c in calls],['codex','claude'])
        self.assertTrue((self.root/'.agent-duet/AUTOPILOT.md').exists())
        self.assertEqual(len(real('inbox',self.base|{'name':'owner'})['messages']),1)
    def test_stop_before_first_inference(self):
        started=self.start();auto.operate('autopilot_stop',self.base|{'run_id':started['run_id']})
        auto.runner(self.root,started['run_id'])
        self.assertEqual(auto.operate('autopilot_status',self.base|{'run_id':started['run_id']})['state'],'cancelled')
    def test_expired_limit_not_completion(self):
        started=self.start();_,_,db=auto.database(self.root)
        run=auto.load(db,started['run_id']);run['data']['deadline']=time.time()-1
        auto.save(db,run['id'],'queued',run['data']);db.close()
        auto.runner(self.root,started['run_id'])
        self.assertEqual(auto.operate('autopilot_status',self.base|{'run_id':started['run_id']})['state'],'limit_reached')
    def test_stop_does_not_erase_completion(self):
        started=self.start();_,_,db=auto.database(self.root)
        row=auto.load(db,started['run_id']);auto.save(db,row['id'],'completed',row['data']);db.close()
        stopped=auto.operate('autopilot_stop',self.base|{'run_id':row['id']})
        self.assertEqual(stopped['status'],'completed');self.assertFalse(stopped['stop_requested'])
    def test_resume_retires_failed_job_and_keeps_partial_context(self):
        started=self.start();_,_,db=auto.database(self.root)
        row=auto.load(db,started['run_id']);d=row['data'];d['current_job']='failed-one'
        with db:
            db.execute("INSERT INTO jobs(id,requester,provider,status,result,created,updated,timeout) VALUES(?,?,?,?,?,?,?,?)",
                       ('failed-one',d['owner'],'codex','failed',json.dumps({'error':'partial write'}),time.time(),time.time(),10))
            db.execute('UPDATE autoruns SET pid=NULL,birth=NULL WHERE id=?',(row['id'],))
        auto.save(db,row['id'],'failed',d);db.close()
        with patch.object(auto,'launch',return_value=SimpleNamespace(pid=os.getpid())):
            auto.operate('autopilot_resume',self.base|{'run_id':row['id'],'instructions':'Check partial artifact before retry'})
        _,_,db=auto.database(self.root);new=auto.load(db,row['id']);db.close()
        self.assertIsNone(new['data']['current_job'])
        self.assertIn('failed-one',new['data']['retired_jobs'])
        self.assertIn('partial write',new['data']['next_task'])
    def test_channel_push_ack_is_separate(self):
        duet.dispatch('join',{'root':str(self.root),'name':'peer','provider':'claude','session':'channel-test','project_name':'Test','goal':'Write and verify'})
        outputs=[];watch=ChannelWatch(duet.dispatch,outputs.append,interval=.02)
        try:
            watch.bind(self.base|{'name':'peer'})
            duet.dispatch('send',self.base|{'sender':'owner','recipient':'peer','body':'wake and verify'})
            end=time.time()+2
            while not outputs and time.time()<end:time.sleep(.02)
            self.assertEqual(outputs[0]['method'],'notifications/claude/channel')
            self.assertEqual(outputs[0]['params']['meta']['project_id'],self.base['project_id'])
            self.assertEqual(len(duet.dispatch('inbox',self.base|{'name':'peer'})['messages']),1)
            with self.assertRaises(ValueError):watch.bind(self.base|{'name':'owner'})
        finally:watch.close()

if __name__=='__main__':unittest.main()
