import unittest
from types import SimpleNamespace
from unittest.mock import patch
import pve_inventory as inv
from test_inventory import FakeClient

class StorageBudgetTests(unittest.TestCase):
    def test_budget_restored_after_timeout_without_duplicate_query(self):
        client = FakeClient()
        client.timeout = 20
        client.pvesh = SimpleNamespace(timeout=20)
        collector = inv.Collector(client, storage_timeout=90)
        calls = []
        def operation():
            calls.append((client.timeout, client.pvesh.timeout))
            raise inv.ReadError('timed out')
        with self.assertRaisesRegex(inv.ReadError, 'Node pve01; storage query budget 90s'):
            collector.storage_read(operation, 'Node pve01')
        self.assertEqual(calls, [(90, 90)])
        self.assertEqual((client.timeout, client.pvesh.timeout), (20, 20))

    def test_storage_is_independent_of_guest_agent(self):
        with patch('sys.stderr'):
            one = inv.Collector(FakeClient(), guest_agent=True).collect()
            client = FakeClient()
            two = inv.Collector(client, guest_agent=False).collect()
        self.assertEqual([v['actual_bytes'] for v in one['vms']], [v['actual_bytes'] for v in two['vms']])
        self.assertFalse(any('/agent/' in path for path, params in client.calls))

class RunTrackingTests(unittest.TestCase):
    def test_targeted_inventory_does_not_query_other_vm(self):
        client = FakeClient()
        with patch('sys.stderr'):
            report = inv.Collector(client, vmids=[101]).collect()
        self.assertEqual([v['vmid'] for v in report['vms']], [101])
        self.assertTrue(report['scope']['targeted'])
        self.assertFalse(any('/qemu/100/' in path for path, _ in client.calls))

    def test_unmatched_selection_never_falls_back(self):
        with self.assertRaises(inv.ReadError):
            inv.Collector(FakeClient(), vmids=[999]).collect()

    def test_cancel_keeps_discovered_vm_placeholders(self):
        import tempfile
        from pathlib import Path
        client = FakeClient()
        client.data['/nodes/a/qemu/100/config'] = KeyboardInterrupt()
        original = client.get
        def get(path, **params):
            if path.endswith('/qemu/100/config'):
                raise KeyboardInterrupt()
            return original(path, **params)
        client.get = get
        with tempfile.TemporaryDirectory() as tmp:
            run = inv.RunTracker(tmp, 'off')
            try:
                report = inv.Collector(inv.ObservedClient(client, run)).collect()
                self.assertTrue(run.cancelled)
                self.assertEqual(len(report['vms']), 2)
                self.assertTrue(all(v['configured_bytes'] is None for v in report['vms']))
                self.assertIn('cancelled', (Path(tmp)/'query_timings.csv').read_text())
            finally:
                run.close()

    def test_expired_budget_issues_no_query(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            run = inv.RunTracker(tmp, 'off', 1)
            try:
                run.started -= 2
                with patch('builtins.print') as call:
                    with self.assertRaises(inv.ReadError):
                        run.query('test', call)
                    call.assert_not_called()
                self.assertTrue(run.expired)
            finally:
                run.close()

    def test_log_does_not_copy_exception_secrets(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            run = inv.RunTracker(tmp, 'off')
            try:
                with self.assertRaises(inv.ReadError):
                    run.query('GET /version', lambda: (_ for _ in ()).throw(inv.ReadError('private-token-123')))
                self.assertNotIn('private-token-123', (Path(tmp)/'collection.log').read_text())
            finally:
                run.close()
