"""Presentation tests for capacity planning; no servers or WSL required."""
import copy
import unittest

import pve_inventory as inv


GIB = 1024 ** 3


class CapacityRenderingTests(unittest.TestCase):
    def report(self):
        return {'capacity_plan': {
            'reserve_percent': 20,
            'horizons_days': [30, 90, 180],
            'notes': ['Shared filesystems must not be added together.'],
            'jobs': [{'id': 'nightly', 'storage': 'pbs', 'datastore': 'archive',
                      'namespace': '', 'enabled': True, 'schedule': 'daily',
                      'selected_vm_count': 12, 'selection_complete': False,
                      'known_configured_bytes': 300 * GIB, 'excluded_disk_count': 2,
                      'retention': {'keep-last': 7}, 'notes': ['Disk configuration is incomplete.']}],
            'stores': [{
                'store': 'archive', 'total_bytes': 1000 * GIB, 'used_bytes': 1000 * GIB,
                'available_bytes': 0, 'occupied_bytes': 1000 * GIB,
                'status': 'Planning scenarios only', 'confidence': 'Assumption',
                'basis': 'No usable growth history', 'history_count': 1, 'history_days': 0,
                'observed_growth_bytes_per_day': None, 'days_until_full': 0,
                'days_until_reserve': 0, 'immediate_required_total_bytes': 1250 * GIB,
                'immediate_additional_bytes': 250 * GIB,
                'projections': [{'scenario': '5 GiB/day example', 'source': 'illustrative',
                                 'growth_bytes_per_day': 5 * GIB, 'days': 90,
                                 'required_total_bytes': 1813 * GIB, 'additional_bytes': 813 * GIB}],
                'assumptions': ['Cleanup failure may distort growth.'],
                'management_text': 'Restore a 20% reserve by adding 250 GiB before future growth.',
            }],
        }}

    def test_no_pbs_explains_why_vm_sizes_cannot_supply_estimate(self):
        page = inv.capacity_page_html({})
        self.assertIn('Capacity evidence is unavailable', page)
        self.assertIn('VM disk sizes alone cannot', page)
        self.assertIn('PBS-only collection does not read PVE jobs', page)
        self.assertIn('measurements are needed', inv.capacity_management_html({}))
        self.assertIn('N/A:', inv.capacity_terminal_text({}))

    def test_scenario_is_clearly_separate_from_measurement(self):
        report = self.report()
        page = inv.capacity_page_html(report)
        self.assertIn('Illustrative scenarios — not a measured forecast.', page)
        self.assertIn('Additional capacity for reserve now', page)
        self.assertIn('0.00 B', page)
        self.assertIn('Already reached', page)
        self.assertIn('250.00 GiB', page)
        self.assertIn('selection incomplete', page)
        self.assertIn('Logical; known included disks only', page)
        self.assertIn('not multiplied by VM sizes', page)
        self.assertIn('÷ (1 − reserve percentage / 100)', page)
        self.assertIn('[illustrative]', inv.capacity_terminal_text(report))

    def test_observed_source_and_assumption_are_not_relabeled(self):
        report = self.report()
        store = report['capacity_plan']['stores'][0]
        store['projections'][0]['source'] = 'observed'
        store['projections'].append(dict(store['projections'][0], source='user assumption'))
        page = inv.capacity_page_html(report)
        self.assertNotIn('Illustrative scenarios — not a measured forecast.', page)
        self.assertIn('<td>observed</td>', page)
        self.assertIn('<td>user assumption</td>', page)

    def test_html_escapes_remote_and_operator_text(self):
        report = self.report()
        bad = '<script>alert("x")</script>'
        store = report['capacity_plan']['stores'][0]
        for key in ('store', 'status', 'confidence', 'basis', 'management_text'):
            store[key] = bad
        store['assumptions'] = [bad]
        store['projections'][0]['scenario'] = bad
        store['projections'][0]['source'] = bad
        job = report['capacity_plan']['jobs'][0]
        for key in ('id', 'storage', 'datastore', 'namespace', 'schedule'):
            job[key] = bad
        job['retention'] = {bad: bad}
        job['notes'] = [bad]
        report['capacity_plan']['notes'] = [bad]
        for output in (inv.capacity_page_html(report), inv.capacity_management_html(report)):
            self.assertNotIn('<script>', output)
            self.assertIn('&lt;script&gt;', output)

    def test_unknown_and_zero_remain_distinct_without_mutating_report(self):
        report = self.report()
        before = copy.deepcopy(report)
        self.assertEqual(inv.capacity_days_label(None), 'N/A — no reliable growth rate')
        self.assertEqual(inv.capacity_days_label(0), 'Already reached')
        self.assertEqual(inv.capacity_days_label(0.25), 'Less than 1 day')
        inv.capacity_page_html(report)
        inv.capacity_management_html(report)
        inv.capacity_terminal_text(report)
        self.assertEqual(report, before)
        store = report['capacity_plan']['stores'][0]
        for key in ('total_bytes', 'used_bytes', 'available_bytes', 'occupied_bytes',
                    'immediate_required_total_bytes', 'immediate_additional_bytes'):
            store[key] = None
        store['projections'] = []
        self.assertIn('N/A — capacity or planning inputs', inv.capacity_page_html(report))


if __name__ == '__main__':
    unittest.main()
