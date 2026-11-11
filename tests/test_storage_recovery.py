"""Recovery regressions with synthetic commands only; no servers or WSL."""
import base64
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pve_inventory as inv
from test_inventory import FakeClient


class StorageRecoveryTests(unittest.TestCase):
    def client(self):
        client = inv.SSHClient('entry-host', user='audit', sudo_mode='interactive')
        client.control_path = '/synthetic/socket'
        session = inv.InteractiveSudoSession(client)
        session.ready = True
        client.interactive_session = session
        return client, session

    def frame(self, value=None, kind='', code=0):
        return dict(code=code, failure_kind=kind,
                    stdout=base64.b64encode(json.dumps(value).encode()).decode() if code == 0 else '')

    def test_missing_disk_and_guest_agent_do_not_prevent_later_vm_reads(self):
        client, session = self.client()
        fixture = FakeClient()
        for storage in fixture.data['/storage']:
            storage['type'] = 'zfspool'
        path = '/nodes/a/storage/shared/content/shared:100/vm-100-disk-0.raw'
        calls = []
        def exchange(payload, *unused):
            requested = payload['command'][2]
            calls.append(requested)
            if requested == path:
                return self.frame(kind='storage-object-missing', code=1)
            if requested.endswith('/agent/get-fsinfo'):
                return self.frame(kind='guest-agent-unavailable', code=255)
            # Convert the actual pvesh path to the fixture API path.
            for key, value in fixture.data.items():
                if inv.parse.unquote(key) == requested:
                    return self.frame(value)
            return self.frame({})
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()), \
             patch.object(session, '_exchange', side_effect=exchange):
            tracker = inv.RunTracker(directory, progress='off')
            try:
                report = inv.Collector(inv.ObservedClient(client, tracker)).collect()
                outcomes = tracker.metadata()['query_outcomes']
            finally:
                tracker.close()
        self.assertTrue(session.ready)
        self.assertTrue(all(vm['config_available'] for vm in report['vms']))
        self.assertIn('/nodes/b/qemu/101/config', calls)
        self.assertIn('/nodes/b/status', calls)
        self.assertEqual(outcomes['disk not found'], 1)
        self.assertEqual(outcomes['optional metric unavailable'], 1)
        self.assertNotIn('not collected', outcomes)
        first = next(v for v in report['vms'] if v['vmid'] == 100)
        self.assertIsNotNone(first['configured_bytes'])
        self.assertIsNone(first['actual_bytes'])
        self.assertTrue(any(d['measurement_status'] == 'disk not found' for d in first['disks']))

    def test_fatal_failure_records_one_incident_and_skips_without_remote_calls(self):
        client, session = self.client()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()), \
             patch.object(session, '_exchange', return_value=self.frame(code=1)) as exchange:
            tracker = inv.RunTracker(directory, progress='off')
            observed = inv.ObservedClient(client, tracker)
            try:
                with self.assertRaises(inv.ReadError):
                    observed.get('/nodes/a/qemu/161/config')
                for vmid in (162, 163, 164):
                    with self.assertRaises(inv.SessionUnavailable):
                        observed.get(f'/nodes/b/qemu/{vmid}/config')
                metadata = tracker.metadata()
                self.assertEqual(metadata['query_outcomes'], {'failed': 1, 'not collected': 3})
                self.assertEqual(metadata['skipped_query_count'], 3)
                self.assertEqual(len(tracker.skipped_reasons), 1)
                self.assertEqual(len(metadata['failures_by_node_storage']), 1)
                exchange.assert_called_once()
            finally:
                tracker.close()
            log = (Path(directory) / 'collection.log').read_text(encoding='utf-8')
            self.assertNotIn('Reading GET /nodes/b', log)
            self.assertIn('/nodes/a/qemu/161/config', log)

    def test_recovery_category_cannot_apply_to_wrong_endpoint_or_signal(self):
        for path, code in (('/version', 1), ('/nodes/a/qemu/161/config', 1),
                           ('/nodes/a/storage/local/content/local:vm-161-disk-0', 143)):
            client, session = self.client()
            with patch.object(session, '_exchange', return_value=self.frame(kind='storage-object-missing', code=code)), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(inv.ReadError):
                    client.get(path)
            self.assertFalse(session.ready)

    def test_auth_notice_only_once_for_multiple_reads(self):
        client, session = self.client()
        output = io.StringIO()
        with patch.object(session, '_exchange', return_value=self.frame({})), contextlib.redirect_stderr(output):
            client.get('/version')
            client.get('/nodes')
        self.assertEqual(output.getvalue().count('password/MFA'), 1)

    def test_skips_respect_runtime_limit_and_pbs_marks_not_collected(self):
        client, session = self.client()
        session.ready = False
        with tempfile.TemporaryDirectory() as directory:
            tracker = inv.RunTracker(directory, progress='off', max_runtime=1)
            tracker.started -= 2
            try:
                with self.assertRaises(inv.SessionUnavailable):
                    inv.ObservedClient(client, tracker).get('/version')
                self.assertTrue(tracker.expired)
            finally:
                tracker.close()
        collector = inv.PBSCollector(client)
        self.assertIsNone(collector.read('/version'))
        self.assertEqual(collector.checks[0]['status'], 'not collected')


PERL = shutil.which('perl')
if PERL is None and Path('C:/Program Files/Git/usr/bin/perl.exe').is_file():
    PERL = 'C:/Program Files/Git/usr/bin/perl.exe'


@unittest.skipUnless(PERL, 'Native Perl not installed; no WSL fallback')
class NativePerlClassificationTests(unittest.TestCase):
    def test_production_helper_compiles(self):
        result = subprocess.run([PERL, '-c', '-e', inv.SUDO_SESSION_HELPER], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exact_diagnostics_and_context_are_required(self):
        source = inv.SUDO_SESSION_HELPER
        function = source[source.index('sub failure_kind {'):source.index('$SIG{HUP}')]
        command = ['pvesh', 'get', '/nodes/another-host/storage/another-store/content/another-store:vm-999-disk-0']
        diagnostic = "proxy handler failed: zfs error: cannot open 'tank/data/vm-999-disk-0': dataset does not exist\n"
        cases = [
            [command, diagnostic, 0, 'storage-object-missing'],
            [command, diagnostic.removeprefix('proxy handler failed: '), 0, 'storage-object-missing'],
            [command, diagnostic, 1, ''],
            [command, 'sudo: authentication failure\n' + diagnostic, 0, ''],
            [command, diagnostic + 'Password token: synthetic\n', 0, ''],
            [command, 'x' * 5000, 0, ''],
            [['pvesh', 'get', '/version'], diagnostic, 0, ''],
            [command, 'QEMU guest agent is not running\n', 0, ''],
            [['pvesh', 'get', '/nodes/a/qemu/9/agent/get-fsinfo'], diagnostic, 0, ''],
            [['pvesh', 'get', '/nodes/a/qemu/9/agent/get-fsinfo'], 'QEMU guest agent is not running\n', 0, 'guest-agent-unavailable'],
            [['env', 'PROXMOX_DEBUG_API_CODE=0', 'proxmox-backup-debug'], diagnostic, 0, ''],
        ]
        program = ('use strict; use warnings; use JSON::PP; ' + function +
                   'my $cases = decode_json($ARGV[0]); print encode_json([map { failure_kind($_->[0], $_->[1], $_->[2]) } @$cases]);')
        result = subprocess.run([PERL, '-e', program, json.dumps(cases)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [case[3] for case in cases])


if __name__ == '__main__':
    unittest.main()
