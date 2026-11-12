"""Selected backup scope uses synthetic data; no server connections."""
import copy
import json
import re
import tempfile
from pathlib import Path
import unittest

import pve_inventory as inv
from test_inventory import collect, GIB


class SelectedWorkloadTests(unittest.TestCase):
    def test_selected_scope_ignores_unselected_and_pbs_failures(self):
        report = collect()
        data = inv.selected_backup_workload(report)
        self.assertEqual([v['vmid'] for v in data['vms']], [100])
        self.assertEqual(data['known_configured_bytes'], 150*GIB)
        self.assertTrue(data['complete'])
        report['pbs'] = {'status': 'failed', 'groups': []}
        self.assertEqual(inv.selected_backup_workload(report), data)
        self.assertTrue(all(r['required_bytes'] is None for r in data['requirements']))
        self.assertNotIn('no-backup-job', {r['rule'] for r in inv.review_items(report)})

    def test_shared_disks_and_multiple_jobs_do_not_multiply_total(self):
        report = collect()
        report['jobs'].append(dict(id='second', enabled=1, storage='other'))
        report['backup_job_members']['second'] = ['100', '101']
        for vm in report['vms']:
            vm['enabled_jobs'].append('second')
        data = inv.selected_backup_workload(report)
        self.assertEqual(data['vm_count'], 2)
        self.assertEqual(data['known_configured_bytes'], 150*GIB)
        self.assertEqual(len(data['destinations']), 2)
        self.assertTrue(data['complete'])

    def test_unknowns_do_not_become_zero(self):
        report = collect()
        vm = report['vms'][0]
        vm['config_available'] = False
        vm['disks'] = []
        data = inv.selected_backup_workload(report)
        self.assertIsNone(data['known_configured_bytes'])
        self.assertIsNone(data['vms'][0]['known_configured_bytes'])
        self.assertFalse(data['complete'])
        report['jobs'] = []
        data = inv.selected_backup_workload(report)
        self.assertEqual(data['known_configured_bytes'], 0)
        self.assertTrue(data['complete'])
        self.assertIsNone(inv.selected_backup_workload({})['known_configured_bytes'])

    def test_missing_members_and_unknown_backup_flags_are_partial(self):
        report = collect()
        report['backup_job_members']['nightly'].append('999')
        self.assertFalse(inv.selected_backup_workload(report)['complete'])
        report = collect()
        next(d for d in report['vms'][0]['disks'] if d['slot']=='scsi2')['backup_disk'] = None
        data = inv.selected_backup_workload(report)
        self.assertFalse(data['complete'])
        self.assertEqual(data['known_configured_bytes'], 100*GIB)

    def test_conflicting_shared_capacity_is_not_arbitrarily_selected(self):
        rows = [dict(complete=True, disks=[dict(identity=['shared','one'], capacity_bytes=n)]) for n in (100,200,100)]
        self.assertEqual(inv.selected_workload_totals(rows), dict(vm_count=3, complete=False, known_configured_bytes=None))

    def test_destination_namespace_and_task_ownership_are_not_assumed(self):
        report = collect()
        report['pbs_destinations'] = [dict(storage='pbs', server='pbs.example', datastore='main', namespace='prod')]
        report['pbs'] = dict(server='pbs.example', groups=[dict(backup_type='vm', backup_id='100', store='main', namespace='other')], datastores=[dict(store='main', backup_attempts=[dict(worker_id='main:vm/100', endtime=123, status='OK')])])
        row = inv.selected_backup_workload(report)['vms'][0]
        self.assertNotIn('Matching backup group visible', row['coverage'])
        self.assertFalse(row['backup_evidence'][0]['ownership_confirmed'])
        self.assertEqual(row['backup_evidence'][0]['state'], 'success')
        report['pbs']['groups'][0]['namespace'] = 'prod'
        self.assertIn('ownership unconfirmed', inv.selected_backup_workload(report)['vms'][0]['coverage'])
        report['pbs']['server'] = 'other.example'
        self.assertEqual(inv.selected_backup_workload(report)['vms'][0]['backup_evidence'], [])

    def test_json_and_html_escape_script_breakout_without_mutating_baseline(self):
        report = collect()
        bad = '</script><script>alert(1)</script>&'
        report['vms'][0]['name'] = bad
        original = copy.deepcopy(report)
        page = inv.selected_workload_html(report)
        self.assertNotIn(bad, page)
        payload = re.search(r'id="backup-selection-data">(.*?)</script>', page, re.S)[1]
        self.assertEqual(json.loads(payload)['vms'][0]['name'], bad)
        self.assertEqual(report, original)

    def test_exports_include_scope_totals_and_explicit_unknown_requirements(self):
        report = collect()
        inv.sort_report(report, 'configured')
        with tempfile.TemporaryDirectory() as folder:
            inv.export_report(report, Path(folder))
            for name in ('selected_backup_vms.csv', 'selected_backup_totals.csv', 'selected_backup_requirements.csv'):
                self.assertTrue((Path(folder)/name).is_file())
            self.assertIn('150.00 GiB', (Path(folder)/'capacity-justification.txt').read_text(encoding='utf-8'))
