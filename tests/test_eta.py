import pathlib,runpy,sys,unittest
APP=pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0,str(APP))
UploadEstimate=runpy.run_path(str(APP/'app.pyw'),run_name='eta_tests')['UploadEstimate']

class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.t=0;self.meter=UploadEstimate(lambda:self.t);self.meter.observe(0)
    def sample(self,t,wire):
        self.t=t;self.meter.observe(wire)
    def test_constant_rate_and_remaining_bytes(self):
        self.sample(5,500)
        rate,eta,status=self.meter.read(2000,0,500)
        self.assertEqual((rate,eta,status),(100,15,None))
        self.sample(6,600)
        self.assertEqual(self.meter.read(2000,0,600)[1],14)
    def test_display_speed_updates_once_per_second(self):
        self.sample(.5,50);self.assertEqual(self.meter.speed(),0)
        self.sample(1,100);self.assertEqual(self.meter.speed(),100)
        self.sample(1.5,250);self.assertEqual(self.meter.speed(),100)
        self.sample(2,400);self.assertEqual(self.meter.speed(),300)
        self.t=3;self.assertEqual(self.meter.speed(),0)
    def test_speed_uses_actual_elapsed_time_after_ui_delay(self):
        self.sample(2,600);self.assertEqual(self.meter.speed(),300)
        self.sample(4,1000);self.assertEqual(self.meter.speed(),200)
    def test_newer_speed_replaces_old_speed(self):
        for t in range(1,21):self.sample(t,100*t)
        slow=self.meter.read(10000,0,2000)[1]
        for t in range(21,41):self.sample(t,2000+200*(t-20))
        rate,eta,status=self.meter.read(10000,0,6000)
        self.assertEqual(rate,200);self.assertLess(eta,slow)
    def test_timer_recalculates_without_new_events(self):
        self.sample(5,500);before=self.meter.read(2000,0,500)[1]
        self.t=8
        self.assertGreater(self.meter.read(2000,0,500)[1],before)
        self.t=15
        self.assertEqual(self.meter.read(2000,0,500),(0,None,'等待响应'))
    def test_retry_resets_remaining_but_not_wire_count(self):
        self.sample(5,500)
        self.assertEqual(self.meter.read(2000,0,0,True)[2],'等待重试')
        self.sample(10,700)
        rate,eta,status=self.meter.read(2000,0,200)
        self.assertEqual(rate,70);self.assertAlmostEqual(eta,1800/70);self.assertIsNone(status)
    def test_skipped_packages_do_not_inflate_rate(self):
        self.sample(5,500);self.sample(5,500)
        self.assertEqual(self.meter.read(10000,9000,0)[0],100)
    def test_last_packet_does_not_claim_finished(self):
        self.sample(5,500)
        self.assertEqual(self.meter.read(500,0,500)[2],'校验中')
        self.assertEqual(self.meter.read(500,500,0)[2],'校验中')
    def test_initial_and_paused_transfer_have_no_fake_eta(self):
        self.assertEqual(self.meter.read(500,0,0)[2],'测算中')
        self.sample(.1,100)
        self.assertEqual(self.meter.read(500,0,100)[2],'测算中')

if __name__=='__main__':unittest.main(verbosity=2)
