import tempfile
import unittest
from types import SimpleNamespace
import pve_inventory as inv


class RoutingTests(unittest.TestCase):
    def client(self, address='192.0.2.9', answer='b'):
        calls = []
        def command(args):
            calls.append(args)
            self.assertTrue(inv.approved_backend_command(args))
            return answer if args[-1] == 'hostname -s' else 'counter'
        return SimpleNamespace(entry_node='a', timeout=20, backend_command=command,
            get=lambda path: [dict(type='node', name='b', ip=address)]), calls

    def test_wrong_node_never_reads_counters_and_failure_is_cached(self):
        client, calls = self.client(answer='wrong')
        for _ in range(2):
            with self.assertRaisesRegex(inv.ReadError, 'identity mismatch'):
                inv.node_command(client, 'b', ['dmsetup', 'status', '--target', 'thin', '--noflush'])
        self.assertEqual(len(calls), 1)

    def test_missing_discovery_can_use_explicit_ip(self):
        client, calls = self.client()
        client.get = lambda path: (_ for _ in ()).throw(inv.ReadError('403 denied'))
        with self.assertRaisesRegex(inv.ReadError, '403'):
            inv.node_command(client, 'b', ['hostname', '-s'])
        self.assertEqual(calls, [])
        fresh, calls = self.client()
        fresh.get = client.get
        fresh.node_address_overrides = {'b': '2001:db8::9'}
        inv.node_command(fresh, 'b', ['hostname', '-s'])
        self.assertEqual(calls[-1][-2], 'root@2001:db8::9')

    def test_deadline_stops_remote_command(self):
        client, calls = self.client()
        with tempfile.TemporaryDirectory() as tmp:
            tracker = inv.RunTracker(tmp, 'off', 1)
            try:
                tracker.started -= 2
                with self.assertRaises(inv.ReadError):
                    inv.node_command(inv.ObservedClient(client, tracker), 'b', ['hostname', '-s'])
                self.assertEqual(calls, [])
            finally:
                tracker.close()

    def test_routes_rebuilt_for_new_client(self):
        first, _ = self.client()
        inv.node_command(first, 'b', ['hostname', '-s'])
        second, calls = self.client('192.0.2.10')
        inv.node_command(second, 'b', ['hostname', '-s'])
        self.assertEqual(calls[-1][-2], 'root@192.0.2.10')
