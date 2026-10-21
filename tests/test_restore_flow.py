import gc,json,os,pathlib,runpy,sys,tracemalloc,unittest
from unittest.mock import Mock,patch
import test_backup as fixtures
from configuration import read_recovery_key
from background import BackgroundClient,main
from engine import BackupError,GitHub,Index,Paused
from recovery import RecoveryEngine
import test_recovery as recovery_fixtures

class FlowTests(unittest.TestCase):
    setUp=fixtures.Tests.setUp;tearDown=fixtures.Tests.tearDown;engine=fixtures.Tests.engine
    def test_repeated_deep_and_quick_scans_never_upload_identical_content(self):
        f=self.src/'same.txt';f.write_text('unchanged');self.c['roots'].append(str(self.src))
        self.engine().run();count=self.remote.uploads
        for deep in [False,True,True,False]:self.engine().run(deep=deep)
        self.assertEqual(count,self.remote.uploads)
    def test_staging_does_not_copy_full_index(self):
        (self.src/'a.txt').write_text('content');e=self.engine()
        try:
            pending,_=e.make_snapshot(False)
            with patch.object(e.index,'all',side_effect=AssertionError('full index copied')):e.stage(*pending)
        finally:e.index.close()
    def test_key_format_is_bounded_and_validated(self):
        self.assertEqual(len(read_recovery_key(self.key)),32)
        for data in [b'',b'g'*64,b'a'*65,b'a'*1000000,b'\xff'*64]:
            self.key.write_bytes(data)
            with self.assertRaises(ValueError):read_recovery_key(self.key)
    def test_task_key_override_never_changes_backup_config(self):
        c=dict(self.c);selected=self.base/'selected.txt';selected.write_text(os.urandom(32).hex())
        argv=['background.py','--config',str(self.base/'config.json'),'--run-id','test','--operation','restore','--recovery-key',str(selected)]
        with patch('sys.argv',argv),patch('background.load_config',return_value=c),patch('background.Worker') as worker:
            main();actual=worker.call_args.args[0]
        self.assertEqual(actual['key_file'],str(selected.resolve()));self.assertEqual(c,self.c)
    def test_child_command_contains_key_path_not_key_contents(self):
        client=BackgroundClient(self.base/'config.json',self.state)
        with patch('background.worker_active',return_value=False),patch('background.subprocess.Popen') as launch:
            client.start(operation='restore',recovery_key=self.key)
        args=launch.call_args.args[0];self.assertIn(str(self.key.resolve()),args);self.assertNotIn(self.key.read_text(),args)
    def test_completed_process_reference_is_released(self):
        client=BackgroundClient(self.base/'config.json',self.state);client.process=Mock();client.process.poll.return_value=None
        self.assertFalse(client.reap_finished());client.process.poll.return_value=0
        self.assertTrue(client.reap_finished());self.assertIsNone(client.process)
    def test_restore_failure_closes_session_and_drops_catalog(self):
        e=RecoveryEngine(self.c,remote=self.remote);remote=object.__new__(GitHub);remote.session=Mock();e.remote=remote
        e.catalog={'fixture':{'source':'fixture'}};e.confirm_restore=lambda r:True
        with patch.object(e,'_run',side_effect=Paused()):
            with self.assertRaises(Paused):e.run()
        remote.session.close.assert_called_once();self.assertEqual(e.catalog,{});self.assertEqual(e.key,b'');self.assertIsNone(e.confirm_restore)

class KeyRecoveryTests(unittest.TestCase):
    setUp=recovery_fixtures.RecoveryTests.setUp;tearDown=recovery_fixtures.RecoveryTests.tearDown;backup=recovery_fixtures.RecoveryTests.backup;recovery=recovery_fixtures.RecoveryTests.recovery
    def test_wrong_key_never_writes_originals(self):
        f=self.src/'a.txt';f.write_text('cloud');self.backup();f.unlink();self.key.write_text(os.urandom(32).hex())
        e=self.recovery();e.confirm_restore=Mock(return_value=True)
        with self.assertRaisesRegex(BackupError,'密钥'):e.run()
        self.assertFalse(f.exists())

class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        cls.tk=tk
        try:cls.root=tk.Tk();cls.root.withdraw()
        except tk.TclError:raise unittest.SkipTest('Tk display unavailable')
        cls.module=runpy.run_path(str(pathlib.Path(__file__).resolve().parent.parent/'app.pyw'))
    @classmethod
    def tearDownClass(cls):cls.root.destroy()
    def request(self,n=1000):
        return {'request_id':'fixture','count':n,'bytes':n,'releases':1,'conflicts':1,'identical':0,'issues':[],'volumes':{},'files':[{'selection_id':str(i),'source':f'C:/Documents/fixture-{i}.txt','action':'create','size':1,'kind':'new'} for i in range(n)]}
    def test_final_confirmation_no_then_yes(self):
        from recovery_ui import RestoreReview
        approve=Mock();w=RestoreReview(self.root,self.request(),approve,Mock())
        try:
            with patch('recovery_ui.messagebox.askyesno',return_value=False):w.submit()
            approve.assert_not_called();self.assertTrue(w.window.winfo_exists())
            with patch('recovery_ui.messagebox.askyesno',return_value=True):w.submit()
            approve.assert_called_once_with('fixture');self.assertEqual(w.files,[]);self.assertEqual(w.request,{})
        finally:
            if w.window.winfo_exists():w.window.destroy()
    def test_restore_rows_are_paged(self):
        from recovery_ui import RestoreReview
        w=RestoreReview(self.root,self.request(2000),Mock(),Mock())
        try:
            self.assertNotIn('fixture-500.txt',w.text.get('1.0','end'));w.change_page(1)
            self.assertIn('fixture-500.txt',w.text.get('1.0','end'));self.assertNotIn('fixture-1000.txt',w.text.get('1.0','end'))
        finally:w.window.destroy()
    def test_repeated_dialogs_release_large_lists(self):
        from selection import FileSelection
        from recovery_ui import RestoreReview
        def cycle():
            for kind in (FileSelection,RestoreReview):
                w=kind(self.root,self.request(10000),Mock(),Mock());w.window.destroy()
                self.assertEqual(w.files,[]);self.assertEqual(w.request,{})
            self.root.update_idletasks();gc.collect()
        tracemalloc.start()
        try:
            cycle();baseline=tracemalloc.get_traced_memory()[0]
            for _ in range(12):cycle()
            growth=tracemalloc.get_traced_memory()[0]-baseline
            type(self).memory_growth_bytes=growth
            self.assertLess(growth,2*1024*1024);self.assertEqual(self.root.winfo_children(),[])
        finally:tracemalloc.stop()
    def test_log_keeps_at_most_600_lines(self):
        obj=Mock();obj.logbox=self.tk.Text(self.root)
        try:
            for i in range(1500):self.module['Window'].log(obj,'fixture '+str(i))
            self.assertLessEqual(int(obj.logbox.index('end-1c').split('.')[0]),601)
            self.assertNotIn('fixture 0\n',obj.logbox.get('1.0','end'))
        finally:obj.logbox.destroy()
    def test_picker_cancel_does_not_start_restore(self):
        obj=Mock(running=False,admin_busy=False,config={'key_file':'key.txt','repo':'owner/repo'},root=self.root)
        with patch('tkinter.filedialog.askopenfilename',return_value=''):
            self.module['Window'].start_restore(obj)
        obj.start.assert_not_called()
    def test_picker_validates_then_confirms_task_key(self):
        obj=Mock(running=False,admin_busy=False,config={'key_file':'key.txt','repo':'owner/repo'},root=self.root)
        with patch('tkinter.filedialog.askopenfilename',return_value='selected.txt'),patch.dict(self.module['Window'].start_restore.__globals__,read_recovery_key=Mock()),patch('tkinter.messagebox.askyesno',return_value=True):
            self.module['Window'].start_restore(obj)
        obj.start.assert_called_once_with('restore',recovery_key='selected.txt');self.assertEqual(obj.config['key_file'],'key.txt')
