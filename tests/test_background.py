import json,os,pathlib,subprocess,sys,tempfile,threading,time,unittest,uuid
from unittest.mock import patch
import test_backup as fixtures
from background import BackgroundClient,Worker,worker_active,safe_read
from engine import BackupError,Index,Paused,atomic_json,norm

def wait_for(callback,timeout=15):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        result=callback()
        if result:return result
        time.sleep(.1)
    raise AssertionError('Timed out waiting for background worker')

class SelectionTests(unittest.TestCase):
    setUp=fixtures.Tests.setUp;tearDown=fixtures.Tests.tearDown;engine=fixtures.Tests.engine
    def test_selected_only_is_staged_and_unselected_returns_next_scan(self):
        chosen=self.src/'chosen.txt';chosen.write_text('selected fixture')
        skipped=self.src/'skipped.txt';skipped.write_text('not uploaded')
        e=self.engine()
        def select(rows):
            self.assertEqual(list(e.runs.iterdir()),[])
            self.assertEqual(self.remote.uploads,0)
            return [r['selection_id'] for r in rows if r['source']==str(chosen)]
        e.select_files=select;result=e.run()
        self.assertEqual(result['runs'][-1]['unselected'],1)
        db=Index(self.state/'index.sqlite')
        try:self.assertIn(norm(chosen),db.all());self.assertNotIn(norm(skipped),db.all())
        finally:db.close()
        later=self.engine()
        try:self.assertEqual([r['source'] for r in later.scan()[0]],[str(skipped)])
        finally:later.index.close()
    def test_select_none_makes_no_package_and_no_network_calls(self):
        (self.src/'a.txt').write_text('fixture');e=self.engine();e.select_files=lambda rows:[]
        result=e.run();self.assertEqual(result['runs'][-1]['unselected'],1)
        self.assertEqual(list((self.state/'runs').iterdir()),[]);self.assertEqual(self.remote.uploads,0)
    def test_unknown_selection_rejected_before_staging(self):
        (self.src/'a.txt').write_text('fixture');e=self.engine();e.select_files=lambda rows:['unknown']
        with self.assertRaises(BackupError):e.run()
        self.assertEqual(self.remote.uploads,0)

class BackgroundTests(unittest.TestCase):
    setUp=fixtures.Tests.setUp
    def tearDown(self):
        if hasattr(self,'client'):
            if worker_active(self.state):self.client.pause();wait_for(lambda:not worker_active(self.state),timeout=15)
            if self.client.process:self.client.process.wait(timeout=10)
        fixtures.Tests.tearDown(self)
    def launch(self):
        self.path=self.base/'config.json';atomic_json(self.path,self.c)
        (self.src/'fixture.txt').write_text('no personal data')
        self.client=BackgroundClient(self.path,self.state);self.client.start()
        return wait_for(lambda:(s if (s:=self.client.status()).get('review_id') else None))
    def test_child_process_heartbeat_and_explicit_approval(self):
        value=self.launch();self.assertNotEqual(value['pid'],os.getpid())
        first=value['heartbeat'];wait_for(lambda:self.client.status().get('heartbeat',0)>first)
        self.assertEqual(list((self.state/'runs').iterdir()),[])
        self.client.approve(value['review_id'],[])
        result=wait_for(lambda:(s if (s:=self.client.status()).get('status')=='done' else None))
        self.assertEqual(result['result']['runs'][-1]['unselected'],1)
    def test_second_ui_attaches_without_starting_second_process(self):
        value=self.launch();other=BackgroundClient(self.path,self.state)
        self.assertFalse(other.start());self.assertEqual(other.status()['pid'],value['pid'])
    def test_pause_during_review_never_uploads(self):
        self.launch();self.client.pause()
        wait_for(lambda:self.client.status().get('status')=='paused')
        self.assertEqual(list((self.state/'runs').iterdir()),[])
    def test_old_selection_reply_cannot_approve_new_task(self):
        value=self.launch()
        atomic_json(self.state/'selection-response.json',{'run_id':'old','request_id':value['review_id'],'selected':[]})
        time.sleep(1.2);self.assertEqual(self.client.status()['status'],'running')
        self.assertTrue(worker_active(self.state))
    def test_worker_survives_launcher_process_exit(self):
        path=self.base/'config.json';atomic_json(path,self.c);(self.src/'fixture.txt').write_text('fixture')
        code='from background import BackgroundClient; import sys; c=BackgroundClient(sys.argv[1],sys.argv[2]); c.start(); print(c.run_id,flush=True)'
        output=subprocess.check_output([sys.executable,'-c',code,str(path),str(self.state)],cwd=pathlib.Path(__file__).resolve().parent.parent,text=True,timeout=15)
        self.client=BackgroundClient(path,self.state);self.client.run_id=output.strip()
        value=wait_for(lambda:(s if (s:=self.client.status()).get('review_id') else None))
        first=value['heartbeat'];wait_for(lambda:self.client.status().get('heartbeat',0)>first)
        self.assertTrue(worker_active(self.state))

if __name__=='__main__':unittest.main(verbosity=2)
