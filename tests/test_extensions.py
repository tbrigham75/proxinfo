import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pve_inventory as inv
from test_inventory import FakeClient, collect, GIB


class FakePBS:
    def __init__(self, overrides=None):
        self.calls = []
        base = '/admin/datastore/store'
        self.data = {
            '/version': {'version': '4.0'}, '/admin/datastore': [{'store': 'store'}],
            '/config/prune': [{'id': 'daily', 'store': 'store', 'schedule': 'daily', 'keep-last': 7}],
            '/admin/prune': [{'id': 'daily', 'store': 'store', 'last-run-state': 'error: test failure', 'last-run-endtime': 300}],
            '/config/datastore/store': {'name': 'store', 'gc-schedule': 'daily', 'keep-last': 10, 'path': '/not/exported'},
            base+'/status': {'total': 1000*GIB, 'used': 950*GIB, 'avail': 40*GIB},
            base+'/gc': {'upid': 'recorded-gc-id', 'last-run-state': 'OK', 'last-run-endtime': 400,
                         'removed-bytes': 2*GIB, 'pending-bytes': 0},
            base+'/namespace': [{'ns': ''}, {'ns': 'team'}],
            (base+'/groups', ''): [{'backup-type': 'vm', 'backup-id': '100', 'backup-count': 2, 'last-backup': 200}],
            (base+'/groups', 'team'): [{'backup-type': 'vm', 'backup-id': '100', 'backup-count': 1, 'last-backup': 300}],
            (base+'/snapshots', ''): [{'backup-type': 'vm', 'backup-id': '100', 'backup-time': 100, 'size': 100*GIB},
                                     {'backup-type': 'vm', 'backup-id': '100', 'backup-time': 200, 'size': 110*GIB}],
            (base+'/snapshots', 'team'): [{'backup-type': 'vm', 'backup-id': '100', 'backup-time': 300, 'size': 120*GIB}],
            '/nodes/localhost/tasks': [{'worker_type': 'prunejob', 'status': 'OK', 'endtime': 200}],
        }
        self.data.update(overrides or {})

    def get(self, path, **params):
        self.calls.append((path, params))
        key = (path, params.get('ns', '')) if path.endswith(('/groups', '/snapshots')) else path
        value = self.data.get(key, inv.ReadError('fixture endpoint unavailable'))
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def collect_pbs(client=None):
    with patch('sys.stderr', new=io.StringIO()):
        return inv.PBSCollector(client or FakePBS()).collect()


class BackendTests(unittest.TestCase):
    def test_exact_thin_sectors_zero_and_failed_targets(self):
        parsed = inv.parse_thin_status('pve-vm--100--disk--0: 0 2097152 thin 8192 2097151\n'
                                       'pve-vm--101--disk--0: 0 2097152 thin 0 -\n'
                                       'pve-vm--102--disk--0: 0 2097152 thin Fail\n')
        self.assertEqual(parsed['pve-vm--100--disk--0'], 8192*512)
        self.assertEqual(parsed['pve-vm--101--disk--0'], 0)
        self.assertNotIn('pve-vm--102--disk--0', parsed)

    def test_thin_active_metrics_and_inactive_gap_without_volume_api_placeholder(self):
        client = FakeClient({'/storage': [{'storage': 'thin', 'type': 'lvmthin', 'vgname': 'pve-vg'}],
            '/nodes/a/qemu/100/config': {'scsi0': 'thin:vm-100-disk-0,size=100G'},
            '/nodes/b/qemu/101/config': {'scsi0': 'thin:vm-101-disk-0,size=20G'}})
        client.thin_status = lambda node: {'pve--vg-vm--100--disk--0': 8192*512} if node == 'a' else {}
        report = collect(client)
        self.assertEqual(report['vms'][0]['actual_bytes'], 8192*512)
        self.assertIn('mapped sectors', report['vms'][0]['disks'][0]['actual_reason'])
        self.assertIsNone(report['vms'][1]['actual_bytes'])
        self.assertIn('not active', report['vms'][1]['disks'][0]['actual_reason'])
        self.assertFalse(any('/content/' in path for path, _ in client.calls))

    def test_node_routing_uses_only_read_command_and_existing_trust(self):
        calls = []
        def command(args):
            calls.append(args)
            return 'a\n' if args == ['hostname', '-s'] else 'pve-vm--100--disk--0: 0 100 thin 10 99\n'
        client = SimpleNamespace(backend_command=command)
        self.assertEqual(inv.read_thin_status(client, 'b')['pve-vm--100--disk--0'], 5120)
        routed = calls[-1]
        self.assertEqual(routed[:10], inv.cluster_ssh_prefix())
        self.assertEqual(routed[10:], ['root@b', 'dmsetup status --target thin --noflush'])
        self.assertTrue(inv.approved_backend_command(routed))
        self.assertFalse(inv.approved_backend_command(routed[:-1]+['dmsetup create bad']))
        self.assertFalse(inv.approved_backend_command(['lvchange', '-ay', 'disk']))

    def test_api_token_does_not_execute_backend_commands(self):
        client = inv.Client('api', url='https://pve:8006', token_id='a@pve!r', token_secret='secret')
        with patch('pve_inventory.subprocess.run') as run:
            with self.assertRaises(inv.ReadError):
                client.thin_status('a')
            run.assert_not_called()

    def test_rbd_uses_validated_api_semantics_and_preserves_zero(self):
        client = FakeClient({'/version': {'version': '8.4.0'}, '/storage': [{'storage': 'ceph', 'type': 'rbd'}],
            '/nodes/a/qemu/100/config': {'scsi0': 'ceph:vm-100-disk-0,size=100G'},
            '/nodes/a/storage/ceph/content/ceph%3Avm-100-disk-0': {'used': 0}})
        report = collect(client)
        self.assertEqual(report['vms'][0]['actual_bytes'], 0)
        self.assertEqual(report['vms'][0]['actual_method'], 'Ceph RBD image allocated bytes')
        client.data['/version'] = {'version': '7.0'}
        self.assertIsNone(collect(client)['vms'][0]['actual_bytes'])


class PBSReportingTests(unittest.TestCase):
    def test_pbs_is_off_by_default_even_when_credentials_are_configured(self):
        args = SimpleNamespace(no_prompt=True, pbs=None, pbs_url='https://pbs:8007', timeout=20)
        with patch.dict(os.environ, {'PBS_URL': 'https://pbs:8007', 'PBS_TOKEN_SECRET': 'secret'}), patch('builtins.input') as ask:
            self.assertIsNone(inv.pbs_settings(args, {}))
            ask.assert_not_called()
        args.no_prompt = False
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='') as ask, patch('pve_inventory.getpass.getpass') as secret:
            self.assertIsNone(inv.pbs_settings(args, {}))
            self.assertIn('[skip]', ask.call_args.args[0])
            secret.assert_not_called()

    def test_skip_flag_never_asks_for_credentials(self):
        args = SimpleNamespace(no_prompt=False, pbs=False, pbs_url=None, timeout=20)
        with patch('sys.stdin.isatty', return_value=True), patch('builtins.input') as ask, patch('pve_inventory.getpass.getpass') as secret:
            self.assertIsNone(inv.pbs_settings(args, {}))
            ask.assert_not_called(); secret.assert_not_called()

    def test_namespaces_keep_same_vm_ids_separate_and_sizes_are_logical(self):
        report = collect_pbs()
        self.assertEqual(report['status'], 'collected')
        self.assertEqual(len(report['groups']), 2)
        self.assertEqual([g['snapshot_count'] for g in report['groups']], [2, 1])
        self.assertEqual([g['latest_logical_bytes'] for g in report['groups']], [110*GIB, 120*GIB])
        self.assertEqual(report['datastores'][0]['listed_snapshot_count'], 3)
        self.assertEqual(report['datastores'][0]['available_bytes'], 40*GIB)  # not estimated as total-used
        self.assertNotIn('path', report['datastores'][0]['datastore_retention'])
        self.assertFalse(any('physical_bytes' in g for g in report['groups']))

    def test_incomplete_namespace_or_snapshot_access_is_not_zero(self):
        pbs = collect_pbs(FakePBS({'/admin/datastore/store/namespace': inv.ReadError('HTTP 403'),
            ('/admin/datastore/store/snapshots', ''): inv.ReadError('unreachable')}))
        self.assertEqual(pbs['status'], 'partial')
        self.assertFalse(pbs['datastores'][0]['inventory_complete'])
        self.assertEqual(pbs['groups'][0]['snapshot_count'], 2)
        self.assertIsNone(pbs['groups'][0]['latest_logical_bytes'])
        self.assertTrue(pbs['warnings'])

    def test_placeholder_gc_zero_without_recorded_run_is_unavailable(self):
        self.assertIsNone(inv.gc_measurement({'removed-bytes': 0, 'pending-bytes': 0}, 'removed-bytes'))
        self.assertEqual(inv.gc_measurement({'upid': 'old-run', 'pending-bytes': 0}, 'pending-bytes'), 0)

    def test_unavailable_inventory_counts_are_not_reported_as_zero(self):
        overrides = {}
        for ns in ('', 'team'):
            for endpoint in ('groups', 'snapshots'):
                overrides[('/admin/datastore/store/'+endpoint, ns)] = inv.ReadError('HTTP 403')
        result = collect_pbs(FakePBS(overrides))
        self.assertIsNone(result['datastores'][0]['visible_group_count'])
        self.assertIsNone(result['datastores'][0]['listed_snapshot_count'])
        self.assertFalse(result['datastores'][0]['inventory_complete'])

    def test_tls_token_scheme_and_get_only_boundary(self):
        with patch('pve_inventory.ssl.create_default_context') as tls, patch('pve_inventory.request.build_opener') as build:
            client = inv.PBSClient('https://pbs:8007', 'audit@pbs!r', 'private-secret', ca_file='ca.pem')
            tls.assert_called_once_with(cafile=str(Path('ca.pem').expanduser()))
            build.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"data":{"total":100}}'
            client.get('/admin/datastore/store/gc')
            req = build.return_value.open.call_args.args[0]
            self.assertEqual(req.get_method(), 'GET')
            self.assertEqual(req.headers['Authorization'], 'PBSAPIToken=audit@pbs!r:private-secret')
            for path in ('/admin/datastore/store/prune', '/admin/prune/job/run', '/access/ticket'):
                with self.assertRaises(ValueError):
                    client.get(path)

    def test_failed_pbs_does_not_prevent_pve_report(self):
        with tempfile.TemporaryDirectory() as tmp, patch('pve_inventory.Client', return_value=FakeClient()), \
             patch('pve_inventory.PBSClient', side_effect=inv.ReadError('PBS unavailable')), \
             patch('sys.stdout', new=io.StringIO()), patch('sys.stderr', new=io.StringIO()):
            code = inv.main(['--mode', 'local', '--environment', 'cluster', '--pbs', '--no-prompt', '--output-dir', tmp])
            self.assertEqual(code, 0)
            report = json.loads((Path(tmp)/'inventory.json').read_text(encoding='utf-8'))
            self.assertEqual(report['pbs']['status'], 'unavailable')
            self.assertTrue((Path(tmp)/'management-summary.html').exists())


class ReviewBreakdownAndSummaryTests(unittest.TestCase):
    def report(self):
        report = collect()
        report['pbs'] = collect_pbs()
        report['large_vm_bytes'] = 100*GIB
        inv.sort_report(report, 'configured')
        return report

    def test_review_findings_have_evidence_and_no_deletion_actions(self):
        report = self.report()
        rules = {r['rule'] for r in report['review_items']}
        self.assertTrue({'large-vm', 'large-stopped-vm', 'no-backup-job', 'excluded-disks', 'pbs-capacity', 'pbs-cleanup-failure'} <= rules)
        self.assertTrue(all(r['evidence'] and r['follow_up'] for r in report['review_items']))
        self.assertNotIn('large-stopped-vm', {r['rule'] for r in inv.review_items(dict(report, vms=[dict(report['vms'][1], template=True)]))})

    def test_host_and_location_breakdowns_deduplicate_shared_disks(self):
        report = self.report()
        host = [r for r in report['storage_breakdowns'] if r['grouping'] == 'host']
        location = [r for r in report['storage_breakdowns'] if r['grouping'] == 'storage']
        self.assertEqual(sum(r['configured_bytes'] for r in host), 150*GIB)
        self.assertEqual(sum(r['configured_bytes'] for r in location), 150*GIB)
        shared = next(r for r in host if r['name'] == 'Shared storage')
        self.assertEqual(shared['vm_count'], 2)
        self.assertEqual(shared['unique_disk_count'], 1)
        self.assertEqual(shared['configured_bytes'], 100*GIB)

    def test_breakdowns_keep_missing_identity_unknown(self):
        report = collect(FakeClient({'/storage': inv.ReadError('denied')}))
        inv.sort_report(report, 'configured')
        self.assertTrue(all(r['configured_bytes'] is None for r in report['storage_breakdowns']))

    def test_printable_and_pbs_exports_preserve_scope_and_escape_text(self):
        report = self.report()
        report['pbs']['datastores'][0]['store'] = '<script>bad</script>'
        with tempfile.TemporaryDirectory() as tmp:
            inv.export_report(report, Path(tmp))
            for name in ('management-summary.html', 'review_items.csv', 'storage_breakdowns.csv', 'pbs_groups.csv', 'pbs_snapshots.csv', 'pbs_datastores.csv'):
                self.assertTrue((Path(tmp)/name).exists())
            summary = (Path(tmp)/'management-summary.html').read_text(encoding='utf-8')
            self.assertIn('Print / Save as PDF', summary)
            self.assertIn('N/A means unavailable', summary)
            self.assertNotIn('<script>bad</script>', summary)
            self.assertIn('may overlap', summary)
            markup = (Path(tmp)/'inventory.html').read_text(encoding='utf-8')
            for page in ('review', 'pbs', 'management'):
                self.assertIn('data-page="'+page+'"', markup)


if __name__ == '__main__':
    unittest.main()
