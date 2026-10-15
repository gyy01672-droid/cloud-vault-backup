import copy,hashlib,json,os,pathlib,shutil,tempfile,threading,unittest,zipfile
from unittest.mock import patch
from engine import Engine,BackupError,Paused,encrypt,digest,read_json
from recovery import RecoveryEngine,RecoveryRemote,original_target
from restore_incremental import mapped_target
from test_backup import FakeRemote

class DownloadRemote:
    def __init__(self):self.items=[];self.paths={};self.calls=0
    def private(self):return {'private':True}
    def releases(self):return [x[0] for x in self.items]
    def assets(self,release):return next(a for r,a in self.items if r['id']==release['id'])
    def download(self,asset,path,callback):
        self.calls+=1;shutil.copyfile(self.paths[asset['id']],path);callback(asset['size'],asset['size']);return path
    def add(self,tag,folder):
        rid=len(self.items)+1;assets={}
        for p in folder.glob('*.pcbackup'):
            aid=len(self.paths)+1;self.paths[aid]=p
            assets[p.name]={'id':aid,'name':p.name,'size':p.stat().st_size,'digest':'sha256:'+digest(p)}
        self.items.append(({'id':rid,'tag_name':tag},assets))

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).resolve().parent,prefix='fixture-');self.base=pathlib.Path(self.temp.name)
        self.src=self.base/'source';self.src.mkdir();self.key=self.base/'key.txt';self.key.write_text(os.urandom(32).hex())
        self.c={'state_dir':str(self.base/'state'),'key_file':str(self.key),'repo':'owner/repo','roots':[str(self.src)],'exclude_paths':[],'part_mib':1,'keep_local_cache':True,'keep_restore_cache':True}
        self.remote=DownloadRemote();self.upload=FakeRemote();self.keybytes=bytes.fromhex(self.key.read_text())
    def tearDown(self):self.temp.cleanup()
    def backup(self):
        result=Engine(self.c,remote=self.upload).run();tag=result['runs'][-1]['tag']
        self.remote.add(tag,pathlib.Path(self.c['state_dir'])/'runs'/tag/'assets');return tag
    def recovery(self):
        e=RecoveryEngine(self.c,remote=self.remote)
        e.protected=[self.c['state_dir'],self.c['key_file']]
        return e
    def test_original_restore_preserves_conflict_and_is_idempotent(self):
        f=self.src/'note.txt';f.write_text('cloud version');mtime=f.stat().st_mtime_ns;self.backup();f.write_text('local version')
        e=self.recovery()
        def approve(review):
            self.assertEqual(f.read_text(),'local version');self.assertEqual(review['conflicts'],1);return True
        e.confirm_restore=approve;result=e.run()
        self.assertEqual(f.read_text(),'cloud version');self.assertEqual(f.stat().st_mtime_ns,mtime)
        self.assertEqual(pathlib.Path(result['preserved'][0]['copy']).read_text(),'local version')
        calls=self.remote.calls;e=self.recovery();e.confirm_restore=lambda r:True;again=e.run()
        self.assertEqual(again['unchanged'],1);self.assertEqual(self.remote.calls,calls)
    def test_incremental_latest_and_historically_deleted_files_return(self):
        f=self.src/'a.txt';f.write_text('one');old=self.src/'deleted.txt';old.write_text('retain history');self.backup()
        f.write_text('version two');old.unlink();self.backup();f.unlink()
        e=self.recovery();e.confirm_restore=lambda r:True;result=e.run()
        self.assertEqual(result['files'],2);self.assertEqual(f.read_text(),'version two');self.assertEqual(old.read_text(),'retain history')
    def test_declining_review_writes_no_original_files(self):
        f=self.src/'a.txt';f.write_text('cloud');self.backup();f.unlink()
        e=self.recovery();e.confirm_restore=lambda r:False
        with self.assertRaises(Paused):e.run()
        self.assertFalse(f.exists())
    def test_missing_chain_blocks_all_original_writes(self):
        f=self.src/'a.txt';f.write_text('one');self.backup();f.write_text('two changed');self.backup();f.unlink()
        self.remote.items.pop(0);e=self.recovery();e.confirm_restore=lambda r:True
        with self.assertRaises(BackupError):e.run()
        self.assertFalse(f.exists())
    def test_tampered_package_blocks_original_writes(self):
        f=self.src/'a.txt';f.write_text('cloud');self.backup();f.unlink()
        asset=next(a for a in self.remote.items[0][1].values() if a['name'].startswith('part-'))
        path=self.remote.paths[asset['id']];raw=bytearray(path.read_bytes());raw[-1]^=1;path.write_bytes(raw)
        e=self.recovery();e.confirm_restore=lambda r:True
        with self.assertRaises(BackupError):e.run()
        self.assertFalse(f.exists())
    def test_original_paths_reject_traversal_links_and_protected_files(self):
        for source in ['C:/a/../b.txt','C:/a/file:stream','relative.txt','C:/CON.txt']:
            with self.assertRaises(BackupError):original_target(source)
        with self.assertRaises(BackupError):original_target(str(self.key),[self.key])
        link=self.src/'link'
        try:link.symlink_to(self.base,target_is_directory=True)
        except OSError:return
        with self.assertRaises(BackupError):original_target(str(link/'escape.txt'))
    def test_legacy_full_backup_original_paths(self):
        folder=self.base/'legacy';folder.mkdir();f=self.src/'legacy.txt';data=b'legacy data'
        row={'source':str(f),'archive_path':'data/X/test.txt','size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'mtime_ns':1234567890000000000}
        plain=folder/'plain.zip'
        with zipfile.ZipFile(plain,'w') as z:z.writestr(row['archive_path'],data);z.writestr('_manifest.json',json.dumps([row]))
        encrypt(plain,folder/'backup-001-of-001.pcbackup',self.keybytes);self.remote.add('snapshot-old',folder)
        e=self.recovery();e.confirm_restore=lambda r:True;result=e.run();self.assertEqual(result['files'],1);self.assertEqual(f.read_bytes(),data)
    def test_write_permission_failure_is_reported_and_retryable(self):
        f=self.src/'a.txt';f.write_text('cloud');self.backup();f.unlink();e=self.recovery();e.confirm_restore=lambda r:True
        with patch.object(e,'install_one',side_effect=PermissionError('fixture locked')):result=e.run()
        self.assertEqual(result['status'],'warning');self.assertEqual(len(result['issues']),1);self.assertFalse(f.exists())
        e=self.recovery();e.confirm_restore=lambda r:True;self.assertEqual(e.run()['files'],1)
    def test_legacy_missing_package_stops_before_original_write(self):
        self.remote.items=[({'id':1,'tag_name':'old'},{'backup-001-of-002.pcbackup':{'id':1,'name':'backup-001-of-002.pcbackup','size':100,'digest':'sha256:'+'a'*64}})]
        e=self.recovery();e.confirm_restore=lambda r:True
        with self.assertRaises(BackupError):e.run()
        self.assertEqual(list(self.src.iterdir()),[])
    def test_cached_plan_cannot_hide_removed_history(self):
        f=self.src/'a.txt';f.write_text('one');first=self.backup();f.write_text('version two');self.backup();f.write_text('version three');self.backup()
        e=self.recovery();e.prepare();self.remote.items.pop(0)
        with self.assertRaises(BackupError):self.recovery().prepare()
        self.assertEqual(f.read_text(),'version three')
    def test_manifest_has_original_path_and_complete_history_dependencies(self):
        from engine import decrypt
        f=self.src/'a.txt';f.write_text('one');first=self.backup();f.write_text('version two');second=self.backup();f.write_text('version three');third=self.backup()
        archive=pathlib.Path(self.c['state_dir'])/'runs'/third/'assets'/'manifest.pcbackup';plain=self.base/'plan.zip';decrypt(archive,plain,self.keybytes)
        with zipfile.ZipFile(plain) as z:plan=json.loads(z.read('snapshot.json'))
        self.assertEqual(plan['parent_tag'],second);self.assertEqual(plan['required_tags'],[first,second]);self.assertEqual(plan['files'][0]['source'],str(f))
        f.unlink();e=self.recovery();e.confirm_restore=lambda r:True;e.run();self.assertEqual(f.read_text(),'version three')

class Response:
    def __init__(self,status,data=b'',headers=None):self.status_code=status;self.data=data;self.headers=headers or {}
    def iter_content(self,_):yield self.data
    def close(self):pass
    def raise_for_status(self):
        if self.status_code>=400:raise RuntimeError('unexpected fixture HTTP status')

class DownloadTests(unittest.TestCase):
    def test_unsafe_redirect_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            remote=object.__new__(RecoveryRemote);remote.cancel=threading.Event();remote.prefix='/repos/owner/repo'
            class Session:
                def get(self,*a,**kw):return Response(302,headers={'Location':'https://attacker.invalid/file'})
            remote.session=Session()
            with patch('recovery.requests.get') as get:
                with self.assertRaises(BackupError):remote.download({'id':1,'size':100,'digest':'sha256:'+'0'*64},pathlib.Path(temp)/'part.pcbackup',lambda *a:None)
                get.assert_not_called()
    def test_range_resume_and_cached_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            data=b'x'*100;path=pathlib.Path(temp)/'part.pcbackup';path.with_suffix('.partial').write_bytes(data[:40])
            remote=object.__new__(RecoveryRemote);remote.cancel=threading.Event();remote.prefix='/repos/owner/repo'
            calls=[]
            class Session:
                def get(self,url,**kwargs):calls.append(kwargs);return Response(206,data[40:],{'Content-Range':'bytes 40-99/100'})
            remote.session=Session();asset={'id':1,'size':100,'digest':'sha256:'+hashlib.sha256(data).hexdigest()}
            remote.download(asset,path,lambda *a:None);self.assertEqual(path.read_bytes(),data);self.assertEqual(calls[0]['headers']['Range'],'bytes=40-')
            remote.download(asset,path,lambda *a:None);self.assertEqual(len(calls),1)
    def test_cross_host_redirect_does_not_use_authenticated_session(self):
        with tempfile.TemporaryDirectory() as temp:
            data=b'z'*100;remote=object.__new__(RecoveryRemote);remote.cancel=threading.Event();remote.prefix='/repos/owner/repo'
            class Session:
                def get(self,*a,**kw):return Response(302,headers={'Location':'https://release-assets.githubusercontent.com/fixture'})
            remote.session=Session();asset={'id':1,'size':100,'digest':'sha256:'+hashlib.sha256(data).hexdigest()}
            with patch('recovery.requests.get',return_value=Response(200,data)) as get:
                remote.download(asset,pathlib.Path(temp)/'part.pcbackup',lambda *a:None)
                self.assertNotIn('Authorization',get.call_args.kwargs['headers'])

if __name__=='__main__':unittest.main()
