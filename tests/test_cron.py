import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import pve_inventory as inv
from test_inventory import FakeClient

class CronTests(unittest.TestCase):
    def run_cron(self, root, client=None, extra=()):
        with patch('pve_inventory.CronLock') as lock, patch('pve_inventory.Client', return_value=client or FakeClient()), patch('pve_inventory.shutil.which', return_value='/usr/bin/pvesh'), patch('builtins.input', side_effect=AssertionError('Unexpected prompt')), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = inv.main(['--cron','--mode','local','--environment','cluster','--output-root',str(root),*extra])
            lock.return_value.close.assert_called_once()
            return code

    def test_repeated_runs_are_unique_and_noninteractive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for _ in range(2):
                self.assertEqual(self.run_cron(root),0)
            folders=list(root.glob('pve-report-*'))
            self.assertEqual(len(folders),2)
            for folder in folders:
                report=json.loads((folder/'inventory.json').read_text(encoding='utf-8'))
                self.assertFalse(report['pbs']['requested'])
            self.assertEqual(json.loads((root/'last-run.json').read_text())['status'],'complete')
            self.assertTrue((root/'index.html').exists())

    def test_partial_and_failed_status_leave_last_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.assertEqual(self.run_cron(root,FakeClient({'/nodes/a/qemu/100/config':inv.ReadError('denied')})),3)
            old=(root/'index.html').read_text()
            self.assertEqual(self.run_cron(root,FakeClient({'/cluster/resources':inv.ReadError('unreachable')})),1)
            self.assertEqual((root/'index.html').read_text(),old)
            self.assertEqual(json.loads((root/'last-run.json').read_text())['status'],'failed')

    def test_overlap_does_not_replace_active_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'last-run.json').write_text('{"status":"running"}')
            with patch('pve_inventory.CronLock',side_effect=inv.CronBusy('busy')), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(inv.main(['--cron','--mode','local','--output-root',tmp]),4)
            self.assertEqual(json.loads((root/'last-run.json').read_text())['status'],'running')
            self.assertFalse(list(root.glob('pve-report-*')))

    def test_password_rejected_and_required_options_checked(self):
        for options in (['--cron'], ['--cron','--mode','ssh','--output-root','relative'], ['--cron','--mode','ssh','--output-root',str(Path.cwd()),'--ssh-auth','password']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                inv.main(options)
            self.assertEqual(caught.exception.code,2)

    def test_missing_local_command_leaves_startup_log(self):
        with tempfile.TemporaryDirectory() as tmp, patch('pve_inventory.CronLock'), patch('pve_inventory.shutil.which',return_value=None), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(inv.main(['--cron','--mode','local','--output-root',tmp]),1)
            folder=next(Path(tmp).glob('pve-report-*'))
            self.assertIn('Collection failed',(folder/'collection.log').read_text())

    def test_kernel_lock_contract(self):
        fake=MagicMock();fake.LOCK_EX=2;fake.LOCK_NB=4
        with tempfile.TemporaryDirectory() as tmp, patch.dict('sys.modules',{'fcntl':fake}):
            lock=inv.CronLock(Path(tmp)/'job.lock')
            self.assertEqual(fake.flock.call_args.args[1],6)
            lock.close()
            self.assertTrue(lock.stream.closed)
            fake.flock.side_effect=BlockingIOError()
            with self.assertRaises(inv.CronBusy):
                inv.CronLock(Path(tmp)/'job.lock')
