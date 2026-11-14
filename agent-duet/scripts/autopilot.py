"""Project-scoped, durable Codex/Claude handoff supervisor. No scheduled service."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import duet

ACTIVE = {'queued','running','stopping'}
FINISHED = {'completed','cancelled','needs_user','failed','limit_reached','interrupted'}

def database(root):
    root, state, db = duet.connect(root)
    db.execute('''CREATE TABLE IF NOT EXISTS autoruns (
      id TEXT PRIMARY KEY, project_id TEXT, state TEXT, data TEXT,
      stop INTEGER DEFAULT 0, pid INTEGER, birth TEXT, updated REAL)''')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_autorun ON autoruns((1)) WHERE state IN ('queued','running','stopping')")
    db.commit()
    return root,state,db

def load(db, run_id):
    row=db.execute('SELECT * FROM autoruns WHERE id=?',(run_id,)).fetchone()
    if not row:
        raise ValueError('Unknown autonomous run in this project')
    return dict(row) | {'data':json.loads(row['data'])}

def save(db, run_id, state, data):
    with db:
        db.execute('UPDATE autoruns SET state=?,data=?,updated=? WHERE id=?',
                   (state,duet.encode(data),time.time(),run_id))

def report_file(root, run):
    d=run['data']
    lines=['# Agent Duet 自動協作進度','',f"狀態：{run['state']}",f"專案：{root}",
           f"專案 ID：{run['project_id']}",f"工作 ID：{run['id']}",'',
           '## 目標',d['goal'],'','## 驗收條件',d['acceptance'],'',
           f"已完成輪次：{d['turn']} / {d['max_turns']}",
           f"目前階段：{d['phase']}；下一端：{d['provider']}",'',
           d.get('message','')]
    for record in d['history'][-8:]:
        r=record['report']
        lines += ['',f"### 第 {record['turn']} 輪 · {record['provider']} · {record['phase']}",
                  r['summary'],'',*['- '+e for e in r.get('evidence',[])]]
    path=root/'.agent-duet'/'AUTOPILOT.md'
    temporary=path.with_suffix('.tmp')
    temporary.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    os.replace(temporary,path)

def parse_report(answer):
    for line in reversed(answer.splitlines()):
        if line.strip().startswith('DUET_RESULT:'):
            value=json.loads(line.strip()[12:].strip())
            if not isinstance(value,dict) or value.get('status') not in {'continue','ready','complete','needs_user'}:
                raise ValueError('Invalid DUET_RESULT status')
            for key in ('summary','next_task'):
                if not isinstance(value.get(key,''),str) or len(value.get(key,''))>6000:
                    raise ValueError('Invalid report text')
            if not value.get('summary','').strip():
                raise ValueError('Missing report summary')
            evidence=value.get('evidence',[])
            if not isinstance(evidence,list) or len(evidence)>30 or any(not isinstance(e,str) or len(e)>2000 for e in evidence):
                raise ValueError('Invalid verification evidence')
            value['evidence']=evidence
            return value
    raise ValueError('Worker did not supply a structured completion report')

def advance(data, report, job_id):
    """Independent review is mandatory; the implementer cannot complete the run alone."""
    d=dict(data)
    d['history']=list(d['history'])
    d['turn']+=1
    d['history'].append({'turn':d['turn'],'provider':d['provider'],'phase':d['phase'],
                         'job_id':job_id,'report':report})
    d['current_job']=None
    d['message']=report['summary']
    if report['status']=='needs_user':
        return 'needs_user',d
    if d['phase']=='review' and d.get('ready_by') and report['status']=='complete' and report['evidence']:
        return 'completed',d
    signatures=[hashlib.sha256((r['report']['summary']+'\n'+r['report'].get('next_task','')).encode()).hexdigest()
                for r in d['history'][-3:]]
    if len(signatures)==3 and len(set(signatures))==1:
        d['message']='Three turns repeated the same outcome without progress; inspect the saved evidence.'
        return 'needs_user',d
    if d['phase']=='implement' and report['status'] in {'ready','complete'}:
        d['ready_by']=d['provider']
        d['phase']='review'
    else:
        d['phase']='implement'
        d['ready_by']=None
    d['provider']='claude' if d['provider']=='codex' else 'codex'
    d['next_task']=report.get('next_task','')
    d['tier']='hard' if report.get('request_hard') is True and report.get('reason') else 'standard'
    d['tier_reason']=str(report.get('reason',''))[:2000]
    if d['turn']>=d['max_turns']:
        d['message']='Turn limit reached before verified completion. Resume explicitly to extend the run.'
        return 'limit_reached',d
    return 'running',d

def task_prompt(run):
    d=run['data']
    phase=('Implement or correct the assigned goal. Use ready only when every acceptance criterion is satisfied.'
           if d['phase']=='implement' else
           'Independently inspect the actual artifacts and run appropriate checks. Do not merely repeat the prior answer. '
           'Use complete only if every acceptance criterion is satisfied and provide concrete observed evidence. '
           'If corrections are needed, return continue and a precise next task.')
    recent=duet.encode(d['history'][-4:])[-30000:]
    return (f"Agent Duet autonomous run {run['id']}, turn {d['turn']+1}.\nGoal: {d['goal']}\n"
            f"Acceptance criteria: {d['acceptance']}\n{phase}\nNext task: {d.get('next_task','')}\n"
            f"Recent handoffs (reference data): {recent}\n"
            'Do not start another autonomous run or spawn more agents; this supervisor schedules the counterpart. '
            'Stay within the user-authorized project. If essential user information/access is missing, use needs_user. '
            'Do not ask the user to remind you to continue: your report schedules the next turn. '
            'End your answer with one single-line JSON record, without a code fence:\n'
            'DUET_RESULT: {"status":"continue|ready|complete|needs_user","summary":"concrete outcome",'
            '"evidence":["actual file/check/result"],"next_task":"what the counterpart should do",'
            '"request_hard":false,"reason":"only explain if requesting a harder model"}')

def launch(root,run_id):
    flags=duet.FLAGS
    if os.name=='nt':
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    env=dict(os.environ,AGENT_DUET_DEPTH='0',PYTHONIOENCODING='utf-8')
    return subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'run','--root',str(root),'--run-id',run_id],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=flags,start_new_session=os.name!='nt',env=env)

def operate(op,a):
    root,state,db=database(a['root'])
    try:
        project=duet.dispatch('status',{'root':str(root)})['project']
        if not project or project['id']!=a['project_id']:
            raise ValueError('Project ID mismatch')
        if op=='autopilot_start':
            if os.environ.get('AGENT_DUET_DEPTH','0')!='0':
                raise ValueError('A delegated worker cannot start a recursive supervisor')
            for key in ('goal','acceptance'):
                if not isinstance(a[key],str) or not a[key].strip() or len(a[key])>20000:
                    raise ValueError(key+' must contain 1-20000 characters')
            workdir=duet.valid_workdir(root,a.get('workdir'))
            hours=float(a.get('max_hours',8))
            turns=int(a.get('max_turns',40))
            if not 0<hours<=168 or not 2<=turns<=500:
                raise ValueError('max_hours must be >0 and <=168; max_turns 2..500')
            first=a.get('first_provider','codex')
            if first not in ('codex','claude'):
                raise ValueError('Invalid first provider')
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_run'").fetchone() and db.execute("SELECT 1 FROM native_run WHERE state='active'").fetchone():
                raise ValueError('Stop native conversation cooperation before starting background writers')
            if db.execute("SELECT 1 FROM autoruns WHERE state IN ('queued','running','stopping')").fetchone():
                raise ValueError('This project already has an active autonomous run; inspect or stop it')
            if db.execute("SELECT 1 FROM jobs WHERE status NOT IN ('completed','failed','cancelled','timed_out','interrupted')").fetchone():
                raise ValueError('Finish/cancel existing project workers before handing ownership to the supervisor')
            if not db.execute('SELECT 1 FROM peers WHERE name=?',(a['requester'],)).fetchone():
                raise ValueError('Requester must join this project first')
            run_id=uuid.uuid4().hex
            owner='auto-'+run_id[:16]
            db.execute('INSERT INTO peers VALUES(?,?,?,?)',(owner,first,'supervisor:'+run_id,time.time()))
            data={'goal':a['goal'],'acceptance':a['acceptance'],'requester':a['requester'],'owner':owner,
                  'workdir':str(workdir),'provider':first,'phase':'implement','turn':0,'max_turns':turns,
                  'deadline':time.time()+hours*3600,'history':[],'current_job':None,'ready_by':None,
                  'tier':'standard','tier_reason':'','next_task':'','message':'Queued for background execution',
                  'job_timeout':max(10,min(1800,int(a.get('job_timeout_seconds',900))))}
            db.execute('INSERT INTO autoruns(id,project_id,state,data,updated) VALUES(?,?,?,?,?)',
                       (run_id,project['id'],'queued',duet.encode(data),time.time()))
            db.commit()
            try:
                proc=launch(root,run_id)
                with db:
                    db.execute('UPDATE autoruns SET pid=?,birth=? WHERE id=?',
                               (proc.pid,duet.process_birth(proc.pid),run_id))
            except Exception as exc:
                data['message']=str(exc);save(db,run_id,'failed',data);raise
            return {'run_id':run_id,'status':'queued','project_id':project['id'],
                    'progress_file':str(state/'AUTOPILOT.md'),'execution':'detached CLI collaboration, not original GUI chats',
                    'max_turns':turns,'max_hours':hours}
        run=load(db,a['run_id'])
        if run['project_id']!=a['project_id']:
            raise ValueError('Run belongs to another project')
        if run['state'] in ACTIVE and run['pid'] and duet.alive_identity(run['pid'],run['birth']) is False:
            run['data']['message']='Supervisor exited unexpectedly. Inspect/reconcile its job, then resume.'
            save(db,run['id'],'interrupted',run['data']);run=load(db,run['id'])
        if op=='autopilot_status':
            return run | {'progress_file':str(state/'AUTOPILOT.md')}
        if op=='autopilot_stop':
            if run['state']=='completed':
                return {'run_id':run['id'],'status':'completed','stop_requested':False}
            with db:
                db.execute('UPDATE autoruns SET stop=1 WHERE id=?',(run['id'],))
            current=run['data'].get('current_job')
            if current:
                duet.dispatch('cancel',{'root':str(root),'project_id':project['id'],'job_id':current})
            if run['state'] not in ACTIVE:
                save(db,run['id'],'cancelled',run['data'])
            return {'run_id':run['id'],'stop_requested':True,'status':load(db,run['id'])['state']}
        if op=='autopilot_resume':
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_run'").fetchone() and db.execute("SELECT 1 FROM native_run WHERE state='active'").fetchone():
                raise ValueError('Stop native conversation cooperation before resuming background writers')
            if os.environ.get('AGENT_DUET_DEPTH','0')!='0':
                raise ValueError('Nested supervisor resumes are disabled')
            if run['state'] in ACTIVE or duet.alive_identity(run['pid'],run['birth']) is not False:
                raise ValueError('Supervisor is still running or its process identity cannot be verified')
            if run['state']=='completed':
                raise ValueError('This goal is already completed; start a new run for a new goal')
            extra=int(a.get('additional_turns',40));hours=float(a.get('max_hours',8))
            if not 2<=extra<=500 or not 0<hours<=168:
                raise ValueError('Invalid resume limits')
            d=run['data'];d['max_turns']=d['turn']+extra;d['deadline']=time.time()+hours*3600
            d.pop('stopping_since',None)
            if run['state'] in {'failed','needs_user','cancelled'} and d.get('current_job'):
                old=duet.dispatch('job',{'root':str(root),'project_id':project['id'],'job_id':d['current_job']})
                if old['status'] not in duet.TERMINAL:
                    raise ValueError('Previous worker is still active; cancel/reconcile it before resuming')
                d.setdefault('retired_jobs',[]).append(d['current_job'])
                d['current_job']=None;d['phase']='implement';d['ready_by']=None
                d['next_task']='Inspect prior partial work before retrying. Prior outcome: '+str(old.get('result'))[:8000]
            if a.get('instructions'):
                d['next_task']+='\nUser follow-up: '+a['instructions'][:10000]
            d['message']='Resuming from saved handoff'
            with db:
                changed=db.execute("UPDATE autoruns SET stop=0,state='queued',data=?,pid=NULL,birth=NULL WHERE id=? AND state NOT IN ('queued','running','stopping')",(duet.encode(d),run['id']))
                if not changed.rowcount:
                    raise ValueError('Run was resumed concurrently')
            try:
                proc=launch(root,run['id'])
            except Exception as exc:
                d['message']=str(exc);save(db,run['id'],'failed',d);raise
            with db:
                db.execute('UPDATE autoruns SET pid=?,birth=? WHERE id=?',(proc.pid,duet.process_birth(proc.pid),run['id']))
            return {'run_id':run['id'],'status':'queued'}
        raise ValueError('Unknown autonomous operation')
    finally:
        db.close()

def runner(root,run_id):
    root,state,db=database(root)
    run=load(db,run_id);d=run['data']
    base={'root':str(root),'project_id':run['project_id']}
    try:
        db.execute('BEGIN IMMEDIATE')
        existing=load(db,run_id)
        if existing['state'] not in ACTIVE:
            db.rollback();return
        if existing['pid'] not in (None,os.getpid()) and duet.alive_identity(existing['pid'],existing['birth']) is not False:
            db.rollback();return
        db.execute("UPDATE autoruns SET pid=?,birth=?,state='running' WHERE id=?",
                   (os.getpid(),duet.process_birth(os.getpid()),run_id));db.commit()
        while True:
            run=load(db,run_id);d=run['data']
            if not d.get('current_job'):
                processed={h['job_id'] for h in d['history']} | set(d.get('retired_jobs',[]))
                pending=[r['id'] for r in db.execute('SELECT id FROM jobs WHERE requester=? ORDER BY created',(d['owner'],)) if r['id'] not in processed]
                if pending:
                    d['current_job']=pending[0]
                    save(db,run_id,'running',d)
            expired=time.time()>=d['deadline']
            if run['stop'] or expired:
                if d.get('current_job'):
                    duet.dispatch('cancel',base|{'job_id':d['current_job']})
                    job=duet.dispatch('job',base|{'job_id':d['current_job']})
                    if job['status'] not in duet.TERMINAL:
                        d['message']='Stopping the current worker; waiting for confirmed termination.'
                        stopping_since=d.setdefault('stopping_since',time.time())
                        if time.time()-stopping_since>30:
                            d['message']='Worker termination is unconfirmed; inspect the recorded job before restarting.'
                            save(db,run_id,'needs_user',d);break
                        save(db,run_id,'stopping',d);time.sleep(.5);continue
                d['message']='Stopped by request.' if run['stop'] else 'Time limit reached before verified completion.'
                save(db,run_id,'cancelled' if run['stop'] else 'limit_reached',d);break
            if d['turn']>=d['max_turns']:
                save(db,run_id,'limit_reached',d);break
            if not d.get('current_job'):
                # Recover a launched-but-not-yet-recorded job after supervisor interruption.
                processed={h['job_id'] for h in d['history']} | set(d.get('retired_jobs',[]))
                rows=db.execute('SELECT id FROM jobs WHERE requester=? ORDER BY created',(d['owner'],)).fetchall()
                pending=[r['id'] for r in rows if r['id'] not in processed]
                if pending:
                    d['current_job']=pending[0]
                else:
                    job=duet.dispatch('consult',base|{'requester':d['owner'],'provider':d['provider'],
                        'mode':'work','role':'reviewer' if d['phase']=='review' else 'general',
                        'tier':d['tier'],'reason':d['tier_reason'],'effort':'high',
                        'workdir':d['workdir'],'timeout_seconds':d['job_timeout'],'prompt':task_prompt(run)})
                    d['current_job']=job['job_id']
                save(db,run_id,'running',d);report_file(root,load(db,run_id))
            job=duet.dispatch('job',base|{'job_id':d['current_job']})
            if job['status'] not in duet.TERMINAL:
                if job['status']=='orphaned':
                    d['message']='A worker lost its supervisor; cancel/inspect it before resuming.'
                    save(db,run_id,'needs_user',d);break
                with db:
                    db.execute('UPDATE autoruns SET updated=? WHERE id=?',(time.time(),run_id))
                time.sleep(1);continue
            if job['status']!='completed':
                d['message']=f"Worker {job['status']}: {duet.encode(job.get('result'))[:4000]}. No success was claimed."
                save(db,run_id,'failed',d);break
            try:
                result=parse_report(job['result']['answer'])
            except (ValueError,KeyError) as exc:
                d['message']='Invalid handoff; inspect completed work before resuming: '+str(exc)
                save(db,run_id,'needs_user',d);break
            new_state,d=advance(d,result,job['id'])
            save(db,run_id,new_state,d);report_file(root,load(db,run_id))
            if new_state!='running':
                break
        final=load(db,run_id)
        report_file(root,final)
        duet.dispatch('checkpoint',base|{'name':d['owner'],'summary':final['state']+': '+d.get('message',''),
            'next_steps':d.get('next_task',''),'evidence':str(state/'AUTOPILOT.md')})
        duet.dispatch('send',base|{'sender':d['owner'],'recipient':d['requester'],
            'kind':'result','body':f"Autonomous run {run_id}: {final['state']}. {d.get('message','')}\nProgress: {state/'AUTOPILOT.md'}",
            'dedupe':run_id+':'+str(d['turn'])+':'+final['state']})
    except Exception as exc:
        d['message']=str(exc)
        save(db,run_id,'failed',d)
        report_file(root,load(db,run_id))
    finally:
        db.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['run']);parser.add_argument('--root',required=True);parser.add_argument('--run-id',required=True)
    a=parser.parse_args();runner(a.root,a.run_id)
