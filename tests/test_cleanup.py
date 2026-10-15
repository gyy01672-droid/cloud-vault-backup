import copy,pathlib,unittest
from unittest.mock import patch
import test_backup as fixtures
import test_recovery as recovery_fixtures
from engine import BackupError,Index,read_json,atomic_json
from cache_cleanup import clean_backup

class CleanupTests(unittest.TestCase):
    setUp=fixtures.Tests.setUp;tearDown=fixtures.Tests.tearDown;engine=fixtures.Tests.engine
    def test_default_cleans_only_after_remote_and_index_commit(self):
        self.c.pop('keep_local_cache');f=self.src/'file.txt';f.write_text('cloud data')
        self.engine().run();folder=next((self.state/'runs').iterdir());plan=read_json(folder/'run.json')
        self.assertTrue(plan['cache_cleaned']);self.assertEqual(list((folder/'assets').iterdir()),[])
        self.assertEqual(list((folder/'spool').iterdir()),[]);self.assertEqual(f.read_text(),'cloud data');self.assertTrue(self.key.exists())
        index=Index(self.state/'index.sqlite')
        try:self.assertEqual(len(index.all()),1)
        finally:index.close()
    def test_remote_loss_preserves_all_resumable_files(self):
        self.c.pop('keep_local_cache');(self.src/'a').write_text('fixture');self.remote.assets=lambda r:{}
        with self.assertRaises(BackupError):self.engine().run()
        folder=next((self.state/'runs').iterdir());self.assertTrue(list((folder/'assets').glob('*.pcbackup')));self.assertTrue(list((folder/'spool').iterdir()))
    def test_publication_not_confirmed_preserves_cache_and_index(self):
        self.c.pop('keep_local_cache');(self.src/'a').write_text('fixture');original=self.remote.api
        self.remote.api=lambda method,path,payload=None: {'draft':True,'tag_name':self.remote.current_tag} if method=='GET' else original(method,path,payload)
        with self.assertRaises(BackupError):self.engine().run()
        folder=next((self.state/'runs').iterdir());self.assertTrue(list((folder/'assets').iterdir()))
        index=Index(self.state/'index.sqlite')
        try:self.assertFalse(index.all())
        finally:index.close()
    def test_index_commit_failure_never_cleans(self):
        self.c.pop('keep_local_cache');(self.src/'a').write_text('fixture')
        with patch.object(Index,'commit',side_effect=OSError('fixture disk error')):
            with self.assertRaises(OSError):self.engine().run()
        folder=next((self.state/'runs').iterdir());self.assertTrue(list((folder/'assets').iterdir()))
    def test_cleanup_failure_is_retried_on_next_start(self):
        self.c.pop('keep_local_cache');(self.src/'a').write_text('fixture')
        with patch('engine.clean_backup',side_effect=PermissionError('fixture lock')):self.engine().run()
        folder=next((self.state/'runs').iterdir());self.assertIn('cleanup_error',read_json(folder/'run.json'))
        self.engine().run();self.assertTrue(read_json(folder/'run.json')['cache_cleaned'])
    def test_cleanup_rejects_wrong_root_and_links(self):
        with self.assertRaises(ValueError):clean_backup(self.src,self.state,{'tag':self.src.name,'status':'completed'})
        folder=self.state/'runs'/'test';folder.mkdir(parents=True);link=folder/'assets'
        try:link.symlink_to(self.src,target_is_directory=True)
        except OSError:return
        with self.assertRaises(ValueError):clean_backup(folder,self.state,{'tag':'test','status':'completed'})

class RestoreCleanupTests(unittest.TestCase):
    setUp=recovery_fixtures.RecoveryTests.setUp
    tearDown=recovery_fixtures.RecoveryTests.tearDown
    backup=recovery_fixtures.RecoveryTests.backup
    recovery=recovery_fixtures.RecoveryTests.recovery

def clean_success(self):
    f=self.src/'a';f.write_text('cloud');self.backup();f.write_text('local')
    self.c.pop('keep_restore_cache');e=self.recovery();e.confirm_restore=lambda r:True;result=e.run()
    self.assertTrue(result['cache_cleaned']);self.assertFalse(e.base.exists());self.assertEqual(f.read_text(),'cloud')
    self.assertEqual(pathlib.Path(result['preserved'][0]['copy']).read_text(),'local')
def keep_on_error(self):
    f=self.src/'a';f.write_text('cloud');self.backup();f.unlink();self.c.pop('keep_restore_cache')
    e=self.recovery();e.confirm_restore=lambda r:True
    with patch.object(e,'install_one',side_effect=PermissionError('fixture locked')):result=e.run()
    self.assertTrue(e.base.exists());self.assertNotIn('cache_cleaned',result)
RestoreCleanupTests.test_success_clears_cache_preserves_original_and_conflict=clean_success
RestoreCleanupTests.test_incomplete_restore_preserves_cache=keep_on_error
