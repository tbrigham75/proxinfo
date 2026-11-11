"""Local capacity planning and CLI checks; no network or WSL."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pve_inventory as inv
from test_extensions import FakePBS, collect_pbs
from test_inventory import collect, GIB


class CapacityIntegrationTests(unittest.TestCase):
    def test_cli_exports_scenarios_and_local_history(self):
        with tempfile.TemporaryDirectory() as tmp, patch('pve_inventory.Client', side_effect=AssertionError('PVE used')), \
             patch('pve_inventory.PBSClient', return_value=FakePBS()), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            code = inv.main(['--pbs-only', '--no-prompt', '--pbs-url', 'https://pbs:8007',
                             '--output-root', tmp, '--pbs-growth-gib-per-day', 'store=10'])
            self.assertEqual(code, 0)
            folder = next(Path(tmp).glob('pve-report-*'))
            report = json.loads((folder / 'inventory.json').read_text(encoding='utf-8'))
            self.assertTrue((Path(tmp) / 'pbs-history.sqlite3').exists())
            store = report['capacity_plan']['stores'][0]
            self.assertTrue(any(p['source'] == 'user assumption' and p['days'] == 90 for p in store['projections']))
            for name in ('capacity_planning.csv', 'backup_job_scope.csv', 'capacity-plan.html', 'capacity-justification.txt'):
                self.assertTrue((folder / name).exists(), name)
            self.assertIn('Capacity Planning', (folder / 'inventory.html').read_text(encoding='utf-8'))
            self.assertIn('Capacity Planning', (folder / 'management-summary.html').read_text(encoding='utf-8'))
            plan_html = (folder / 'capacity-plan.html').read_text(encoding='utf-8')
            self.assertIn('Current Retention And Cleanup', plan_html)
            self.assertIn('Keep the 7 most recent backups', plan_html)
            self.assertIn('Prune result daily', (folder / 'capacity-justification.txt').read_text(encoding='utf-8'))

    def test_history_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp, patch('pve_inventory.PBSClient', return_value=FakePBS()), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(inv.main(['--pbs-only', '--no-prompt', '--pbs-url', 'https://pbs:8007',
                                      '--output-root', tmp, '--no-pbs-history']), 0)
            self.assertFalse((Path(tmp) / 'pbs-history.sqlite3').exists())

    def test_job_scope_excludes_backup_disabled_and_keeps_uncertainty(self):
        report = collect()
        report['pbs'] = {'server': 'pbs'}
        report['pbs_destinations'] = [{'storage': 'pbs', 'server': 'pbs', 'datastore': 'store'}]
        report['jobs'][0]['storage'] = 'pbs'
        row = inv.capacity_job_scope(report)[0]
        self.assertEqual(row['selected_vm_count'], 1)
        self.assertEqual(row['excluded_disk_count'], 1)
        self.assertEqual(row['known_configured_bytes'], 150 * GIB)
        report['vms'][0]['config_available'] = False
        self.assertFalse(inv.capacity_job_scope(report)[0]['selection_complete'])
        report['backup_job_members']['nightly'] = None
        self.assertFalse(inv.capacity_job_scope(report)[0]['selection_complete'])

    def test_history_separates_retention_changes_and_uses_daily_readings(self):
        report = {'pbs': collect_pbs(), 'run': {'run_id': 'one'}, 'generated_at': '2026-11-01T00:00:00+00:00'}
        store = report['pbs']['datastores'][0]
        with tempfile.TemporaryDirectory() as tmp:
            for run, stamp in [('one', '2026-11-01T00:00:00+00:00'), ('two', '2026-11-01T02:00:00+00:00'),
                               ('three', '2026-11-08T00:00:00+00:00')]:
                report['run']['run_id'] = run
                store['capacity_observed_at'] = stamp
                inv.pbs_history(report, Path(tmp), 'https://pbs')
            self.assertEqual(len(store['capacity_history']), 2)
            store['prune_config'][0]['keep-last'] = 14
            report['run']['run_id'] = 'four'
            inv.pbs_history(report, Path(tmp), 'https://pbs')
            self.assertEqual(len(store['capacity_history']), 1)

    def test_growth_inputs_reject_malformed_duplicates_and_nonfinite_values(self):
        for values in (['store=nan'], ['store=inf'], ['store=-1'], ['store=1', 'store=2'], ['=2'], ['store']):
            with self.subTest(values=values), self.assertRaises(ValueError):
                inv.parse_growth_overrides(values)
        self.assertEqual(inv.parse_growth_overrides(['store=0']), {'store': 0})


if __name__ == '__main__':
    unittest.main()
