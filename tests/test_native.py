import json,sys,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent-duet/scripts'))
import duet,native

class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.base={'root':self.tmp.name}
        self.ids={n:str(uuid.uuid4()) for n in ('c','a')}
        for n,p in [('c','codex'),('a','claude')]:
            r=duet.dispatch('join',self.base|dict(name=n,provider=p,session=self.ids[n],project_name='Exact project',goal='Prove native routing'))
            self.base['project_id']=r['project']['id']
            self.call('native_bind',name=n,session=self.ids[n],address=self.ids[n] if n=='c' else 'claude-peer [abc123]')
        self.run=self.call('native_start',name='c',peer='a')['run_id']
    def tearDown(self): self.tmp.cleanup()
    def call(self,op,**kw): return duet.dispatch(op,self.base|kw)
    def send(self,s='a',r='c'):
        return self.call('send',sender=s,recipient=r,body='Task with `literal` $(text)')['message_id']
    def wake(self,m,name='a'):
        return self.call('native_wake',name=name,run_id=self.run,message_id=m)
    def test_uuid_and_identity(self):
        with self.assertRaises(ValueError): self.call('native_bind',name='c',session=str(uuid.uuid4()))
        with self.assertRaises(ValueError): self.call('native_bind',name='c',session=self.ids['c'],address='other')
    def test_project_guard(self):
        with self.assertRaises(ValueError): duet.dispatch('native_status',self.base|{'project_id':'wrong'})
    def test_exact_queue_and_idempotence(self):
        m=self.send()
        with patch.object(native.subprocess,'run') as run:
            run.return_value=type('R',(),dict(returncode=0,stdout='Queued',stderr=''))()
            r=self.wake(m); self.assertEqual(r['status'],'queued'); self.assertFalse(r['processed_by_recipient'])
            self.wake(m); self.assertEqual(run.call_count,2)
            args=run.call_args.args[0]
            self.assertEqual(args[args.index('--thread')+1],self.ids['c'])
            self.assertIn(self.base['project_id'],args[-1])
    def test_timeout_never_resends(self):
        m=self.send()
        with patch.object(native.subprocess,'run',side_effect=TimeoutError('uncertain')) as run:
            self.assertEqual(self.wake(m)['status'],'unknown'); self.wake(m)
            self.assertEqual(run.call_count,1)
    def test_stop_and_wrong_sender(self):
        m=self.send()
        with self.assertRaises(ValueError): self.wake(m,name='c')
        self.call('native_stop',name='c',run_id=self.run)
        with self.assertRaises(ValueError): self.wake(m)
    def test_ack_skips_transport(self):
        m=self.send(); self.call('ack',name='c',ids=[m])
        with patch.object(native.subprocess,'run') as run:
            self.assertEqual(self.wake(m)['status'],'already_acknowledged'); run.assert_not_called()
    def test_claude_only_courier(self):
        m=self.send('c','a')
        with patch.object(native,'relay_claude',return_value={'status':'attempted'}) as relay:
            self.wake(m,name='c')
            self.assertEqual(relay.call_args.args[2],'claude-peer [abc123]')
            self.assertEqual(relay.call_args.kwargs['session'],self.ids['a'])
    def test_claude_uses_current_address_for_same_uuid(self):
        from subprocess import CompletedProcess
        rows=[dict(sessionId=self.ids['a'],cwd=self.tmp.name,name='original Claude')]
        with patch.object(duet,'cli',return_value=['claude']), patch.object(native.subprocess,'run') as run:
            run.side_effect=[CompletedProcess([],0,json.dumps(rows),''),CompletedProcess([],0,'','')]
            self.wake(self.send('c','a'),name='c')
        self.assertEqual(run.call_args_list[0].args[0],['claude','agents','--json'])
        payload=json.loads(run.call_args_list[1].kwargs['input'].split('\n',1)[1])
        self.assertIsNone(payload['exact_address'])
        self.assertEqual(payload['expected_title'],'original Claude')
        self.assertEqual(payload['uuid_verified_in_live_local_inventory'],self.ids['a'])
    def test_claude_refuses_stale_wrong_project_or_ambiguous_identity(self):
        from subprocess import CompletedProcess
        original=dict(sessionId=self.ids['a'],cwd=self.tmp.name,name='original Claude')
        unrelated=original|dict(sessionId=str(uuid.uuid4()))
        for rows in ([unrelated], [original|dict(cwd=str(Path(self.tmp.name).parent))], [original,unrelated]):
            with self.subTest(rows=rows), patch.object(duet,'cli',return_value=['claude']), patch.object(native.subprocess,'run') as run:
                run.return_value=CompletedProcess([],0,json.dumps(rows),'')
                result=native.relay_claude(duet,Path(self.tmp.name).resolve(),'old [abc123]','old','probe',session=self.ids['a'])
                self.assertEqual(result['status'],'not_sent')
                self.assertEqual(result['reported_cost_usd'],0)
                run.assert_called_once()
    def test_limit(self):
        import sqlite3
        with sqlite3.connect(Path(self.tmp.name)/'.agent-duet/state.sqlite3') as db:
            db.execute('UPDATE native_run SET deadline=0')
        db.close()
        self.assertEqual(self.wake(self.send())['status'],'limit_reached')
    def test_no_rebinding_live_identity(self):
        with self.assertRaises(ValueError): self.call('native_bind',name='a',session=self.ids['a'],address='different')
    def test_claude_self_address_without_exposed_uuid(self):
        address='another-peer [abcdef]'
        self.call('join',name='a2',provider='claude',session=address,project_name='Exact project',goal='Prove native routing')
        self.assertEqual(self.call('native_bind',name='a2',session=address,address=address)['session'],address)
    def test_reject_background_start_during_native_pair(self):
        with self.assertRaisesRegex(ValueError,'native conversation'):
            self.call('autopilot_start',requester='c',goal='Test',acceptance='Test')
    def test_duplicate_start_attaches_same_pair(self):
        result=self.call('native_start',name='a',peer='c')
        self.assertTrue(result['attached']);self.assertEqual(result['run_id'],self.run)
    def test_resume_flag_when_installed_cli_supports_it(self):
        with patch.object(native.subprocess,'run') as run:
            run.side_effect=[type('R',(),dict(stdout='--resume'))(),type('R',(),dict(returncode=0,stdout='Queued',stderr='Warning: resume failed'))()]
            result=self.wake(self.send())
            self.assertIn('--resume',run.call_args.args[0])
            self.assertIn('Warning',result['stderr'])
            self.assertFalse(result['processed_by_recipient'])
    def test_burst_coalescing_does_not_consume_an_attempt(self):
        with patch.object(native.subprocess,'run') as run:
            run.return_value=type('R',(),dict(returncode=0,stdout='Queued',stderr=''))()
            self.wake(self.send())
            result=self.wake(self.send())
            self.assertEqual(result['status'],'cooldown')
            self.assertEqual(run.call_count,2)
    def test_courier_budget_and_compact_status(self):
        import time
        later=time.time()+25
        with patch.object(native,'relay_claude',return_value={'status':'attempted','reported_cost_usd':2,'response':'x'*10000}):
            result=self.wake(self.send('c','a'),name='c')
            self.assertLessEqual(len(result['response']),1000)
        with patch.object(native.time,'time',return_value=later):
            result=self.wake(self.send('c','a'),name='c')
        self.assertEqual(result['status'],'courier_budget_reached')
        self.assertLess(len(json.dumps(self.call('native_status'))),3000)

if __name__=='__main__': unittest.main()
