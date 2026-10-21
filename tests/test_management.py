import copy,hashlib,json,os,pathlib,shutil,threading,time,unittest
from types import SimpleNamespace
from unittest.mock import patch
from test_backup import Tests as BackupFixture,FakeRemote
from test_recovery import RecoveryTests as RecoveryFixture,DownloadRemote
from engine import Engine,Index,BackupError,Paused,atomic_json,read_json,norm
from capacity import enforce,measure,GIB
from repositories import switch_repository
from history_remote import HistoryRemote
from task_cleanup import discard_tasks
from background import WorkerLock,Worker

class CapacityTests(unittest.TestCase):
    def test_pending_bytes_excludes_verified_parts(self):
        remote=SimpleNamespace(api=lambda *a:[{'tag_name':'a'}],assets=lambda r:{'part':{'size':10,'digest':'sha256:x','state':'uploaded'}})
        self.assertEqual(measure(remote,{'tag':'a','assets':[{'name':'part','size':10,'sha256':'x'},{'name':'next','size':7,'sha256':'y'}]}),(10,17))
    def test_soft_once_hard_always_and_decline(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            calls=[];engine=SimpleNamespace(c={'repo':'owner/repo'},state=pathlib.Path(tmp),remote=None,emit=lambda *a:None,confirm_capacity=lambda r:calls.append(r) or True)
            snapshot={'assets':[]}
            with patch('capacity.measure',return_value=(20*GIB,21*GIB)):
                enforce(engine,snapshot);enforce(engine,snapshot);self.assertEqual(len(calls),1)
            with patch('capacity.measure',return_value=(29*GIB,30*GIB)):
                with self.assertRaises(BackupError):enforce(engine,snapshot)
            engine.c['repo']='owner/another';engine.confirm_capacity=lambda r:False
            with patch('capacity.measure',return_value=(20*GIB,21*GIB)):
                with self.assertRaises(Paused):enforce(engine,snapshot)
    def test_asset_limits(self):
        with self.assertRaises(BackupError):enforce(None,{'assets':[{'size':2*GIB}]})
        with self.assertRaises(BackupError):enforce(None,{'assets':[{'size':1}]*1001})
    def test_capacity_review_can_be_cancelled(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            worker=Worker({'state_dir':tmp},'test');worker.cancel.set()
            with self.assertRaises(Paused):worker.confirm_capacity({'used':20})
            self.assertIsNone(worker.data['review_id'])

class ManagementTests(unittest.TestCase):
    setUp=BackupFixture.setUp;tearDown=BackupFixture.tearDown;engine=BackupFixture.engine
    def test_discard_preserves_original_index_completed_and_unknown(self):
        f=self.src/'first.txt';f.write_text('saved');self.engine().run()
        index_bytes=(self.state/'index.sqlite').read_bytes();completed=next((self.state/'runs').iterdir())
        f.write_text('changed');engine=self.engine();pending,_=engine.make_snapshot();folder,snapshot=pending;engine.stage(folder,snapshot);engine.index.close()
        extra=folder/'assets';extra.mkdir();(extra/'unrecognized.keep').write_text('retain')
        result=discard_tasks(self.c);self.assertGreater(result['freed_bytes'],0)
        self.assertEqual(read_json(folder/'run.json')['status'],'cancelled');self.assertEqual(f.read_text(),'changed')
        self.assertEqual(index_bytes,(self.state/'index.sqlite').read_bytes());self.assertTrue((completed/'assets'/'manifest.pcbackup').exists())
        self.assertTrue((extra/'unrecognized.keep').exists());self.assertTrue(self.key.exists())
        count=self.remote.uploads;self.engine().run();self.assertGreater(self.remote.uploads,count)
        self.assertEqual(read_json(folder/'run.json')['status'],'cancelled')
    def test_active_worker_blocks_discard(self):
        lock=WorkerLock(self.state/'background.lock');self.assertTrue(lock.acquire())
        try:
            with self.assertRaises(BackupError):discard_tasks(self.c)
        finally:lock.close()
    def test_lost_publication_response_reconciled_before_discard(self):
        f=self.src/'saved.txt';f.write_text('cloud data');engine=self.engine();(folder,snapshot),_=engine.make_snapshot()
        engine.stage(folder,snapshot);engine.package(folder,snapshot);engine.index.close()
        snapshot['release_id']=1;atomic_json(folder/'run.json',snapshot)
        class Remote:
            def __init__(self,*a):pass
            def private(self):pass
            def api(self,*a,**kw):return {'tag_name':snapshot['tag'],'draft':False}
            def assets(self,r):return {a['name']:{'state':'uploaded','size':a['size'],'digest':'sha256:'+a['sha256']} for a in snapshot['assets']}
        discard_tasks(self.c,Remote)
        self.assertEqual(read_json(folder/'run.json')['status'],'completed')
        index=Index(self.state/'index.sqlite')
        try:self.assertEqual(index.get(f)['sha256'],hashlib.sha256(b'cloud data').hexdigest())
        finally:index.close()
    def test_switch_copies_index_preserves_key_and_rejects_reuse(self):
        f=self.src/'saved.txt';f.write_text('one');self.engine().run();config=self.base/'config.json';atomic_json(config,self.c)
        class Remote:
            def __init__(self,c,*a):self.c=c
            def private(self):return {'id':1 if self.c['repo']=='owner/repo' else 2,'default_branch':'main'}
            def api(self,method,path):return [] if path.startswith('/releases') else [{'sha':'initial'}]
        updated=switch_repository(config,'https://github.com/owner/new.git',Remote)
        self.assertEqual(updated['key_file'],str(self.key));self.assertEqual(updated['repository_history'][0]['repo'],'owner/repo')
        engine=Engine(updated,remote=FakeRemote());self.assertEqual(engine.scan()[0],[]);self.assertTrue(engine.index.history());engine.index.close()
        with self.assertRaises(BackupError):switch_repository(config,'OWNER/REPO',Remote)
        with self.assertRaises(BackupError):switch_repository(config,'owner/renamed',Remote)
    def test_parallel_workers_are_bounded_and_join_on_cancel(self):
        for n in range(20):(self.src/f'{n}.txt').write_text('x'*100)
        engine=self.engine();(folder,snapshot),_=engine.make_snapshot();real_stat=pathlib.Path.stat
        active=0;peak=0;guard=threading.Lock()
        def slow_stat(path,*a,**kw):
            nonlocal active,peak
            if threading.current_thread().name.startswith('backup-reader') and path.parent==self.src:
                with guard:active+=1;peak=max(peak,active)
                time.sleep(.01)
                with guard:active-=1
            return real_stat(path,*a,**kw)
        saves=[];original=engine.save
        with patch.object(pathlib.Path,'stat',slow_stat),patch.object(engine,'save',side_effect=lambda *a:(saves.append(1),original(*a))):engine.stage(folder,snapshot)
        self.assertGreater(peak,1);self.assertLessEqual(peak,4);self.assertLess(len(saves),10)
        self.assertFalse(any(t.name.startswith('backup-reader') for t in threading.enumerate()));engine.index.close()
    def test_cancellation_joins_threads_without_committing(self):
        for n in range(30):(self.src/f'{n}.txt').write_text('data')
        engine=self.engine();(folder,snapshot),_=engine.make_snapshot()
        engine.cancel.set()
        with self.assertRaises(Paused):engine.stage(folder,snapshot)
        self.assertEqual(engine.index.all(),{});engine.index.close()
        self.assertFalse(any(t.name.startswith('backup-reader') for t in threading.enumerate()))
    def test_same_content_does_not_copy_plaintext(self):
        f=self.src/'same.txt';f.write_text('data');self.engine().run();engine=self.engine();pending,_=engine.make_snapshot(deep=True)
        engine.stage(*pending);self.assertEqual(pending[1]['files'],[]);self.assertEqual(list((pending[0]/'spool').iterdir()),[]);engine.index.close()

class HistoryTests(unittest.TestCase):
    setUp=RecoveryFixture.setUp;tearDown=RecoveryFixture.tearDown;backup=RecoveryFixture.backup
    def test_fresh_machine_discovers_old_repository_and_restores_chain(self):
        f=self.src/'changed.txt';f.write_text('one');untouched=self.src/'old.txt';untouched.write_text('old data');self.backup()
        old=copy.deepcopy(self.remote);old_repo=self.c['repo'];self.remote=DownloadRemote()
        self.c=dict(self.c,repo='owner/new',repository_history=[{'repo':old_repo}])
        # This fixture intentionally shares the copied index but removes the old identity and runs.
        shutil.move(self.c['state_dir']+'/runs',str(self.base/'old-runs'))
        for aid,p in old.paths.items():old.paths[aid]=self.base/'old-runs'/p.relative_to(pathlib.Path(self.c['state_dir'])/'runs')
        (pathlib.Path(self.c['state_dir'])/'identity.json').unlink()
        f.write_text('new version');self.backup();new=copy.deepcopy(self.remote)
        # GitHub release/asset IDs are globally unique.
        new.items[0][0]['id']=200
        for asset in new.items[0][1].values():asset['id']+=200
        new.paths={k+200:v for k,v in new.paths.items()}
        f.unlink();untouched.unlink()
        def factory(c,*a):return old if c['repo']==old_repo else new
        fresh=dict(self.c,repository_history=[],state_dir=str(self.base/'fresh-state'))
        from recovery import RecoveryEngine
        with patch('recovery.RecoveryRemote',side_effect=factory):
            engine=RecoveryEngine(fresh);engine.protected=[fresh['state_dir'],fresh['key_file']];engine.confirm_restore=lambda r:True
            result=engine.run()
        self.assertEqual(result['files'],2);self.assertEqual(f.read_text(),'new version');self.assertEqual(untouched.read_text(),'old data')
    def test_history_cannot_remove_or_duplicate_known_repository(self):
        remote=HistoryRemote(dict(self.c,repository_history=[{'repo':'owner/old'}]),lambda *a:None,threading.Event())
        with self.assertRaises(BackupError):remote.adopt([{'repo':self.c['repo']}])
        with self.assertRaises(BackupError):remote.adopt([{'repo':'owner/old'},{'repo':'OWNER/OLD'},{'repo':self.c['repo']}])
    def test_discovered_identity_checked_even_if_client_already_exists(self):
        client=SimpleNamespace(private=lambda:{'id':9,'private':True},releases=lambda:[])
        remote=HistoryRemote(self.c,lambda *a:None,threading.Event(),factory=lambda *a:client)
        remote.current_releases();remote.adopt([{'repo':self.c['repo'],'repo_id':10}])
        with self.assertRaises(BackupError):remote.private()

del BackupFixture,RecoveryFixture
if __name__=='__main__':unittest.main(verbosity=2)
