import json
import tempfile
import unittest
from pathlib import Path
import pve_inventory as inv
from test_inventory import collect, FakeClient


class EnvironmentReportTests(unittest.TestCase):
    def test_host_metrics_are_distinct_from_vm_assignments(self):
        client = FakeClient({'/nodes/a/status': {'memory': {'total': 64*1024**3, 'used': 8*1024**3}},
                             '/nodes/a/storage': [{'storage':'shared','total':100,'used':75,'avail':25}]})
        report = collect(client)
        host = report['hosts'][0]
        self.assertEqual(host['details']['memory']['total'], 64*1024**3)
        self.assertIn('75.0%', inv.hosts_page_html(report))
        self.assertEqual(report['vms'][0]['ram_bytes'], 4*1024**3)

    def test_comparison_missing_metrics_not_growth(self):
        old, current = collect(), collect()
        current['vms'][0]['actual_bytes'] = None
        current['vms'][0]['ram_bytes'] *= 2
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'prior.json'
            path.write_text(json.dumps(old))
            changes=inv.compare_inventory(current, path)
            self.assertEqual([item['change'] for item in changes['items']], ['ram_bytes'])
            current['scope']['targeted']=True
            self.assertEqual(inv.compare_inventory(current,path)['items'], [])

    def test_new_queries_are_get_allowlisted(self):
        client=inv.Client('local')
        from unittest.mock import patch
        import subprocess
        with patch.object(client,'run_pvesh',return_value=subprocess.CompletedProcess([],0,'{}','')) as run:
            client.get('/nodes/a/status')
            client.get('/nodes/a/storage')
            self.assertEqual(run.call_count,2)
        with self.assertRaises(ValueError):
            client.get('/nodes/a/reboot')

    def test_restore_notes_escape_html(self):
        html=inv.pbs_jobs_html({'restore_tests':[{'notes':'<script>bad</script>'}]})
        self.assertNotIn('<script>bad</script>',html)
        self.assertIn('&lt;script&gt;',html)

    def test_findmnt_allowlist(self):
        self.assertTrue(inv.approved_backend_command(['findmnt','--json','--target','/backup','--output','SOURCE,TARGET,FSTYPE,UUID','--first-only']))
        self.assertFalse(inv.approved_backend_command(['mount','/backup']))
