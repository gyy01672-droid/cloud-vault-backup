import copy,json,os,pathlib,shutil,sys,tempfile,threading,unittest
APP=pathlib.Path(__file__).resolve().parent.parent;sys.path.insert(0,str(APP))
from engine import Engine,Index,BackupError,Paused,digest,norm,read_json,atomic_json
from restore_incremental import restore,mapped_target

class FakeRemote:
    def __init__(self):self.sent={};self.uploads=0;self.fail_after=None;self.published=[];self.remote_assets={}
    def private(self):pass
    def release(self,s):return {'id':1,'tag_name':s['tag']}
    def upload(self,r,a,p,cb):
        key=(r['tag_name'],a['name'])
        if key in self.sent and self.sent[key]==a['sha256']:return {'digest':'sha256:'+a['sha256']}
        if self.fail_after is not None and self.uploads>=self.fail_after:raise BackupError('simulated connection loss')
        cb(a['size']);self.sent[key]=a['sha256'];self.uploads+=1
        self.remote_assets[key]={'state':'uploaded','size':a['size'],'digest':'sha256:'+a['sha256']}
        return {'digest':'sha256:'+a['sha256']}
    def assets(self,r):return {name:value for (tag,name),value in self.remote_assets.items() if tag==r['tag_name']}
    def api(self,method,path,payload=None):self.published.append(path);return {}

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).resolve().parent,prefix='fixture-');self.base=pathlib.Path(self.tmp.name);self.src=self.base/'source';self.src.mkdir()
        self.key=self.base/'key.txt';self.key.write_text(os.urandom(32).hex());self.state=self.base/'state'
        self.c={'state_dir':str(self.state),'key_file':str(self.key),'roots':[str(self.src)],'exclude_paths':[str(self.state)],'part_mib':1,'repo':'owner/repo'}
        self.remote=FakeRemote()
    def tearDown(self):self.tmp.cleanup()
    def engine(self,cancel=None):return Engine(self.c,cancel=cancel,remote=self.remote)
    def test_resume_after_upload_and_before_index_commit(self):
        f=self.src/'big.dat';f.write_bytes(os.urandom(2*1024*1024+123));self.remote.fail_after=1
        with self.assertRaises(BackupError):self.engine().run()
        db=Index(self.state/'index.sqlite');self.assertNotIn(norm(f),db.all());db.close()
        self.assertEqual(self.remote.uploads,1)
        self.remote.fail_after=None;result=self.engine().run();self.assertEqual(result['status'],'success')
        self.assertEqual(self.remote.uploads,4) # three data chunks + encrypted manifest, first chunk not re-uploaded
        runs=list((self.state/'runs').iterdir());self.assertEqual(len(runs),1)
        run=read_json(runs[0]/'run.json');self.assertEqual(run['status'],'completed')
        out=self.base/'restored';restored=restore(runs[0]/'assets',out,bytes.fromhex(self.key.read_text()))
        self.assertEqual(restored['files'],1)
        target=mapped_target(out,str(f));self.assertEqual(digest(target),digest(f))
        count=self.remote.uploads;self.engine().run();self.assertEqual(self.remote.uploads,count)
    def test_modify_delete_and_timestamp_only(self):
        f=self.src/'document.txt';f.write_text('version one');self.engine().run();first=self.remote.uploads
        os.utime(f,ns=(f.stat().st_atime_ns,f.stat().st_mtime_ns+10000000));self.engine().run();self.assertEqual(first,self.remote.uploads)
        f.write_text('version two changed');self.engine().run();self.assertGreater(self.remote.uploads,first)
        f.unlink();self.engine().run();db=Index(self.state/'index.sqlite');self.assertEqual(db.all()[norm(f)]['present'],0);db.close()
        self.assertGreater(len(self.remote.sent),first) # no historical assets deleted
    def test_missing_root_not_false_deletion(self):
        f=self.src/'file.txt';f.write_text('retain');self.engine().run();self.src.rename(self.base/'unavailable')
        e=self.engine();changed,deleted,issues,_=e.scan();e.index.close()
        self.assertTrue(issues);self.assertFalse(deleted)
    def test_deep_detects_same_size_same_timestamp(self):
        f=self.src/'file.txt';f.write_text('ABCD');self.engine().run();st=f.stat();f.write_text('EFGH');os.utime(f,ns=(st.st_atime_ns,st.st_mtime_ns))
        e=self.engine();self.assertEqual(e.scan()[0],[]);e.index.close()
        before=self.remote.uploads;self.engine().run(deep=True);self.assertGreater(self.remote.uploads,before)
    def test_cancel_does_not_advance_index(self):
        (self.src/'file.txt').write_text('data');cancel=threading.Event();cancel.set()
        with self.assertRaises(Paused):self.engine(cancel).run()
        db=Index(self.state/'index.sqlite');self.assertFalse(db.all());db.close()
    def test_credentials_excluded(self):
        (self.src/'.env').write_text('SECRET=placeholder');(self.src/'recovery-key.txt').write_text('not a key');(self.src/'paper.md').write_text('research')
        e=self.engine();rows,_,_,_=e.scan();e.index.close();self.assertEqual([pathlib.Path(r['source']).name for r in rows],['paper.md'])
    def test_corruption_blocks_restore(self):
        (self.src/'a.txt').write_text('hello');self.engine().run();run=next((self.state/'runs').iterdir())
        p=run/'assets'/'part-00001.pcbackup';b=bytearray(p.read_bytes());b[-1]^=1;p.write_bytes(b)
        with self.assertRaises(BackupError):restore(run/'assets',self.base/'out',bytes.fromhex(self.key.read_text()))
        self.assertFalse((self.base/'out').exists())

if __name__=='__main__':unittest.main(verbosity=2)
