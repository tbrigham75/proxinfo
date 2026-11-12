"""PBS task outcomes and incident ordering; all data is synthetic."""
import copy
import unittest

import pve_inventory as inv
from test_inventory import collect
from test_extensions import collect_pbs


LOCK_ERROR = 'unable to get exclusive lock - ENOLCK: No record locks available'


class CleanupAlertTests(unittest.TestCase):
    def report(self):
        report = collect()
        report['pbs'] = collect_pbs()
        store = report['pbs']['datastores'][0]
        store['available_bytes'] = 0
        store['used_bytes'] = store['total_bytes']
        store['prune_status'] = [{'last-run-state': 'OK'}]
        store['gc'] = {'last-run-state': LOCK_ERROR, 'last-run-endtime': 1700000000,
                       'upid': 'older-statistics-task', 'removed-bytes': 0, 'pending-bytes': 0}
        inv.sort_report(report, 'configured')
        return report

    def test_pbs_task_states_do_not_depend_on_error_keywords(self):
        cases = [({'last-run-state': 'OK'}, 'success'),
                 ({'last-run-state': 'WARNINGS: 2'}, 'warnings'),
                 ({'last-run-state': LOCK_ERROR}, 'failed'),
                 ({'last-run-state': 'aborted by user'}, 'failed'),
                 ({'last-run-state': 'unknown'}, 'unknown'),
                 ({'last-run-state': 'running'}, 'running'),
                 ({'last-run-state': None, 'last-run-upid': 'active-task'}, 'running'),
                 ({'upid': 'old-statistics-task'}, 'unknown'),
                 ({'last-run-state': {'bad': 'shape'}}, 'unknown'),
                 ({}, 'unknown')]
        for job, expected in cases:
            with self.subTest(job=job):
                self.assertEqual(inv.cleanup_state(job), expected)
        self.assertIn('Failure', inv.cleanup_result({'last-run-state': LOCK_ERROR}))
        self.assertIn('warnings', inv.cleanup_result({'last-run-state': 'WARNINGS: 2'}))
        self.assertIn('Running', inv.cleanup_result({'last-run-upid': 'active-task'}))

    def test_full_datastore_and_lock_failure_are_prioritized(self):
        report = self.report()
        items = report['review_items']
        self.assertEqual([item['rule'] for item in items[:2]], ['pbs-capacity-full', 'pbs-cleanup-failure'])
        self.assertIn('0 bytes available', items[0]['evidence'])
        self.assertIn(LOCK_ERROR, items[1]['evidence'])
        # The statistics UPID can identify an older GC; never attach it to a newer failure.
        self.assertNotIn('older-statistics-task', items[1]['evidence'])
        self.assertIn('2023', items[1]['evidence'])
        page = inv.management_body(report)
        self.assertIn('Backup storage has no available space', page)
        self.assertIn('Garbage collection reported a failure', page)
        self.assertLess(page.index('Backup storage has no available space'), page.index('Garbage collection reported a failure'))

    def test_running_and_unknown_are_not_failed_cleanup_alerts(self):
        report = self.report()
        store = report['pbs']['datastores'][0]
        for job in ({'last-run-upid': 'active-task'}, {'last-run-state': 'unknown'}, {}):
            store['gc'] = job
            self.assertNotIn('pbs-cleanup-failure', [item['rule'] for item in inv.review_items(report)])
        store['gc'] = {'last-run-state': 'WARNINGS: 3'}
        rules = [item['rule'] for item in inv.review_items(report)]
        self.assertIn('pbs-cleanup-warning', rules)
        self.assertNotIn('pbs-cleanup-failure', rules)

    def test_pbs_only_summary_keeps_priority_alerts(self):
        report = self.report()
        report['scope']['pbs_only'] = True
        report['vms'] = []
        report['review_items'] = inv.review_items(report)
        page = inv.management_body(report)
        self.assertIn('PVE inventory was not requested', page)
        self.assertIn('Backup storage has no available space', page)
        self.assertIn('Garbage collection reported a failure', page)
        self.assertLess(page.index('Backup storage has no available space'), page.index('Garbage collection reported a failure'))

    def test_management_assigned_capacity_preserves_partial_subtotal(self):
        report = self.report()
        report['storage_totals'][0]['total_bytes'] = None
        report['storage_totals'][0]['known_subtotal_bytes'] = 150 * 1024**3
        page = inv.management_body(report)
        self.assertIn('150.00 GiB · Partial</strong><span>assigned disk space', page)

    def test_closed_session_vm_gaps_share_one_incident(self):
        report = self.report()
        cause = ('Not Collected — Connection Unavailable; the shared interactive session on pve-entry '
                 'closed after /nodes/pve-other/storage/local-zfs/content/local-zfs%3Avm-161-disk-0. Reconnect to continue')
        report['vms'] = []
        template = collect()['vms'][0]
        for vmid in (162, 163):
            vm = copy.deepcopy(template)
            vm.update(vmid=vmid, config_available=False, configured_bytes=None, actual_bytes=None,
                      disks=[], backup='No', notes=[f'GET /nodes/pve/qemu/{vmid}/config: ' + cause,
                                                   f'GET /nodes/pve/qemu/{vmid}/status/current: ' + cause])
            report['vms'].append(vm)
        items = inv.review_items(report)
        incidents = [item for item in items if item['rule'] == 'inventory-interrupted']
        self.assertEqual(len(incidents), 1)
        self.assertIn('2 VM configurations', incidents[0]['evidence'])
        self.assertIn('162, 163', incidents[0]['evidence'])
        self.assertEqual([item['rule'] for item in items[:3]],
                         ['pbs-capacity-full', 'pbs-cleanup-failure', 'inventory-interrupted'])
        self.assertFalse(any(item['rule'] in ('missing-measurement', 'unreadable-settings') for item in items))
        self.assertEqual(sum(item['rule'] == 'no-backup-job' for item in items), 0)


if __name__ == '__main__':
    unittest.main()
