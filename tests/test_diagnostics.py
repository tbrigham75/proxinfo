import json
import tempfile
import unittest
from pathlib import Path
import pve_inventory as inv
from test_inventory import collect


class DiagnosticsTests(unittest.TestCase):
    def test_bundle_preserves_failures_and_redacts_secrets(self):
        report = collect()
        report['vms'][0]['disks'][0]['actual_reason'] = 'failure private-value PVEAPIToken=user!token=unknown-value'
        with tempfile.TemporaryDirectory() as tmp:
            tracker = inv.RunTracker(Path(tmp) / 'report', 'off')
            try:
                report['run'] = tracker.metadata()
                tracker.message('password=never-share')
                folder = inv.write_diagnostics(report, tracker, Path(tmp) / 'logs', ['private-value'])
                text = '\n'.join(path.read_text(encoding='utf-8-sig') for path in folder.iterdir())
                for secret in ('private-value', 'unknown-value', 'never-share'):
                    self.assertNotIn(secret, text)
                self.assertIn('[REDACTED', text)
                data = json.loads((folder / 'storage-diagnostics.json').read_text())
                self.assertEqual(len(data['vms']), 2)
                self.assertNotIn('filesystems', data['vms'][0])
                self.assertNotIn('name', data['vms'][0])
                self.assertTrue((folder / 'query_timings.csv').exists())
            finally:
                tracker.close()

    def test_redaction_handles_headers_private_keys_and_url_credentials(self):
        text = 'Authorization: Bearer abc\nhttps://user:pass@host/path\n-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----'
        cleaned = inv.redact_diagnostics(text)
        self.assertNotIn('abc', cleaned)
        self.assertNotIn('user:pass', cleaned)
