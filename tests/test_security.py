import copy,json,os,pathlib,tempfile,threading,unittest,zipfile
from unittest.mock import Mock
import test_backup as fixtures
from engine import Engine,Index,BackupError,RunLock,GitHub,atomic_json,decrypt,encrypt,digest,exclusion
from configuration import parse_repo,validate_config,write_key
from restore_incremental import restore,mapped_target
from cryptography.exceptions import InvalidTag

class SafetyTests(unittest.TestCase):
    setUp=fixtures.Tests.setUp
    tearDown=fixtures.Tests.tearDown
    engine=fixtures.Tests.engine
    def test_reject_foreign_repository_with_existing_index(self):
        (self.src/'a.txt').write_text('original');self.engine().run()
        self.c['repo']='another/private-repo'
        with self.assertRaises(BackupError):self.engine().run()
    def test_reject_changed_recovery_key(self):
        (self.src/'a.txt').write_text('original');self.engine().run()
        self.key.write_text(os.urandom(32).hex())
        with self.assertRaises(BackupError):self.engine().run()
    def test_excluded_root_cannot_bypass_filter(self):
        secret=self.src/'.ssh';secret.mkdir();(secret/'id_rsa').write_text('fixture')
        self.c['roots']=[str(secret)]
        e=self.engine()
        try:self.assertEqual(e.scan()[0],[])
        finally:e.index.close()
    def test_second_process_cannot_enter_same_state(self):
        self.state.mkdir();one=RunLock(self.state/'engine.lock');two=RunLock(self.state/'engine.lock')
        one.acquire()
        try:
            with self.assertRaises(BackupError):two.acquire()
        finally:one.close();two.close()
    def test_authentication_failure_never_exposes_plaintext_or_overwrites_output(self):
        plain=self.base/'plain';sealed=self.base/'sealed';out=self.base/'out'
        plain.write_text('private fixture');out.write_text('existing file')
        encrypt(plain,sealed,bytes.fromhex(self.key.read_text()))
        data=bytearray(sealed.read_bytes());data[-1]^=1;sealed.write_bytes(data)
        with self.assertRaises(InvalidTag):decrypt(sealed,out,bytes.fromhex(self.key.read_text()))
        self.assertEqual(out.read_text(),'existing file');self.assertFalse(list(self.base.glob('.decrypt-*')))
    def test_final_remote_asset_loss_blocks_index_commit(self):
        (self.src/'a.txt').write_text('fixture');self.remote.assets=lambda r:{}
        with self.assertRaises(BackupError):self.engine().run()
        db=Index(self.state/'index.sqlite')
        try:self.assertEqual(db.all(),{})
        finally:db.close()
        self.assertEqual(self.remote.published,[])
    def test_corrupt_pack_rebuilt_manifest_matches_new_hash(self):
        (self.src/'a.txt').write_text('fixture');e=self.engine()
        try:
            (folder,snapshot),_=e.make_snapshot();e.stage(folder,snapshot);e.package(folder,snapshot)
            part=folder/'assets'/'part-00001.pcbackup';part.write_bytes(b'corrupt')
            snapshot['status']='packing';e.package(folder,snapshot)
            result=restore(folder/'assets',self.base/'restored',e.key)
            self.assertEqual(result['files'],1)
        finally:e.index.close()
    def test_empty_file_restores(self):
        (self.src/'empty.txt').touch();self.engine().run();folder=next((self.state/'runs').iterdir())
        self.assertEqual(restore(folder/'assets',self.base/'restored',bytes.fromhex(self.key.read_text()))['files'],1)
    def test_manifest_tag_path_traversal_is_rejected_before_output(self):
        key=bytes.fromhex(self.key.read_text());archives=self.base/'archives';archives.mkdir();plain=self.base/'manifest.zip'
        plan={'version':2,'tag':'../../escape','files':[],'assets':[],'deleted':[]}
        with zipfile.ZipFile(plain,'w') as z:z.writestr('snapshot.json',json.dumps(plan))
        encrypt(plain,archives/'manifest.pcbackup',key)
        with self.assertRaises(BackupError):restore(archives,self.base/'out',key)
        self.assertFalse((self.base/'out').exists())
    def test_wrong_key_refuses_restore(self):
        (self.src/'a.txt').write_text('fixture');self.engine().run();folder=next((self.state/'runs').iterdir())
        with self.assertRaises(InvalidTag):restore(folder/'assets',self.base/'out',os.urandom(32))
        self.assertFalse((self.base/'out').exists())
    def test_recovery_key_export_never_overwrites(self):
        with self.assertRaises(FileExistsError):write_key(self.key,os.urandom(32))
    def test_restore_chain_requires_parent_and_applies_in_order(self):
        f=self.src/'a.txt';f.write_text('first');self.engine().run()
        first=next((self.state/'runs').iterdir())
        f.write_text('second version');self.engine().run()
        second=next(p for p in (self.state/'runs').iterdir() if p!=first)
        key=bytes.fromhex(self.key.read_text());out=self.base/'restored'
        with self.assertRaises(BackupError):restore(second/'assets',out,key)
        restore(first/'assets',out,key)
        restore(second/'assets',out,key,apply=True)
        self.assertEqual(mapped_target(out,str(f)).read_text(),'second version')

class ValidationTests(unittest.TestCase):
    def test_windows_reparse_directory_is_excluded_on_python_311(self):
        self.assertIsNotNone(exclusion(pathlib.Path.cwd()/'linked-folder',{'exclude_paths':[]},0x400))
    def test_repo_url_and_short_name(self):
        self.assertEqual(parse_repo('https://github.com/example/private-backup.git'),'example/private-backup')
        self.assertEqual(parse_repo('example/private-backup'),'example/private-backup')
    def test_bad_repository_inputs(self):
        for value in ['https://other.test/a/b','https://github.com@other.test/a/b','https://github.com/a/b?token=fixture','a/..','a/b/../../x','git@github.com:a/b','https://github.com/a/b/releases','https://github.com:443/a/b']:
            with self.subTest(value=value),self.assertRaises(ValueError):parse_repo(value)
    def test_restore_paths(self):
        dest=pathlib.Path.cwd().resolve()/'restored-fixture'
        for source in ['C:relative.txt',r'C:\safe\..\escape.txt',r'C:\safe\file:stream',r'C:\safe\NUL.txt','../escape','/home/../escape']:
            with self.subTest(source=source),self.assertRaises(BackupError):mapped_target(dest,source)
        self.assertEqual(mapped_target(dest,r'C:\Documents\hello.txt'),dest/'data'/'C'/'Documents'/'hello.txt')
        self.assertEqual(mapped_target(dest,r'\\server\share\hello.txt'),dest/'data'/'UNC'/'server'/'share'/'hello.txt')
        self.assertEqual(mapped_target(dest,'/home/example/hello.txt'),dest/'data'/'posix'/'home'/'example'/'hello.txt')
    def test_private_repo_identity_and_default_branch(self):
        gh=GitHub.__new__(GitHub);gh.config={'repo_id':123};gh.api=Mock(return_value={'id':123,'private':True,'default_branch':'trunk'})
        gh.private();self.assertEqual(gh.default_branch,'trunk')
        gh.api.return_value={'id':456,'private':True}
        with self.assertRaises(BackupError):gh.private()
        gh.api.return_value={'id':123,'private':False}
        with self.assertRaises(BackupError):gh.private()

if __name__=='__main__':unittest.main(verbosity=2)
