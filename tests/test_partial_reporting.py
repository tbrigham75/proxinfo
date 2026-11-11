"""Coverage and compatible subtotals stay visible after partial collection."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

import pve_inventory as inv
from test_inventory import collect, FakeClient, GIB


class PartialReportingTests(unittest.TestCase):
    def disk(self, name, used, method='ZFS usedbydataset', capacity=10):
        return dict(identity=['node', 'local', name], actual_bytes=used,
                    actual_method=method, capacity_bytes=capacity)

    def test_bucket_keeps_partial_known_values_without_counting_duplicate(self):
        disk = self.disk('a', 0)
        row = inv.disk_bucket_totals([disk, dict(disk), self.disk('b', None)], 2)
        self.assertEqual(row['unique_disk_count'], 2)
        self.assertEqual(row['known_actual_bytes'], 0)
        self.assertEqual(row['known_configured_bytes'], 20)
        self.assertIsNone(row['actual_bytes'])
        self.assertIn('0.00 B', inv.bucket_usage_label(row))
        self.assertIn('Partial', inv.bucket_usage_label(row))
        self.assertIn('their disk counts are unknown', inv.bucket_coverage_label(row))

    def test_missing_measurement_is_distinct_from_zero(self):
        row = inv.disk_bucket_totals([self.disk('a', None)])
        self.assertIsNone(row['known_actual_bytes'])
        self.assertEqual(row['measured_subtotals'], {})
        self.assertEqual(inv.bucket_usage_label(row), 'No Measurements Collected')

    def test_different_metric_families_are_not_combined(self):
        row = inv.disk_bucket_totals([self.disk('a', 4), self.disk('b', 8, 'file allocated bytes')])
        self.assertIsNone(row['actual_bytes'])
        self.assertIsNone(row['known_actual_bytes'])
        self.assertEqual(row['measured_subtotals'], {'ZFS usedbydataset': 4, 'file allocated bytes': 8})
        self.assertIn('Separate Subtotals', inv.bucket_usage_label(row))
        self.assertNotIn('12.00 B', inv.bucket_usage_label(row))

    def test_conflicting_duplicate_stays_excluded_after_later_match(self):
        row = inv.disk_bucket_totals([self.disk('a', 4), self.disk('a', 5), self.disk('a', 4), self.disk('b', 6)])
        self.assertEqual(row['known_actual_bytes'], 6)
        self.assertEqual(row['measured_disk_count'], 1)
        self.assertIsNone(row['actual_bytes'])

    def partial_report(self):
        report = collect(FakeClient({'/nodes/b/qemu/101/config': inv.ReadError('Not Collected — Connection Unavailable')}))
        inv.sort_report(report, 'configured')
        return report

    def test_terminal_headline_uses_partial_total_and_unknown_disk_caveat(self):
        text = inv.terminal_report(self.partial_report())
        self.assertIn('Measured disk usage: 20.00 GiB · Partial', text)
        self.assertIn('Configured VM disks (logical capacity): 150.00 GiB · Partial', text)
        self.assertIn('Disk counts for the 1 unread configurations are unknown', text)
        self.assertNotIn('Measured disk usage: N/A', text)

    def test_shared_breakdown_html_and_csv_preserve_known_subtotal(self):
        report = self.partial_report()
        shared = next(row for row in report['storage_breakdowns'] if row['name'] == 'Shared storage')
        self.assertEqual(shared['known_actual_bytes'], 20 * GIB)
        self.assertIsNone(shared['actual_bytes'])
        rendered = inv.breakdown_page_html(report)
        self.assertIn('20.00 GiB · Partial', rendered)
        self.assertIn('their disk counts are unknown', rendered)
        rendered_full = inv.html_report(report)
        self.assertIn('id="table-assigned-total">150.00 GiB · Partial', rendered_full)
        with tempfile.TemporaryDirectory() as folder:
            inv.export_report(report, Path(folder))
            with (Path(folder) / 'storage_breakdowns.csv').open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            row = next(row for row in rows if row['name'] == 'Shared storage')
            self.assertEqual(int(row['known_actual_bytes']), 20 * GIB)
            self.assertEqual(row['actual_bytes'], 'N/A')
            self.assertEqual(json.loads(row['measured_subtotals']), {'file allocated bytes': 20 * GIB})
            with (Path(folder) / 'summary.csv').open(encoding='utf-8-sig', newline='') as stream:
                summary = {row['metric']: row['value'] for row in csv.DictReader(stream)}
            self.assertEqual(summary['Total measured VM disk allocation'], '20.00 GiB · Partial')

    def test_closed_session_reason_does_not_blame_individual_host(self):
        vm = next(vm for vm in self.partial_report()['vms'] if not vm['config_available'])
        reason = ' '.join(inv.allocation_vm_reason(vm))
        self.assertIn('shared inventory connection closed', reason)
        self.assertNotIn('Ask IT to check access to its host server', reason)


if __name__ == '__main__':
    unittest.main()
