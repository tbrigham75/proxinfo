"""Capacity forecasts use synthetic observations, never production/WSL access."""
import copy
import json
import unittest

import pve_inventory as inv


GIB = 1024**3


class CapacityModelTests(unittest.TestCase):
    def report(self, **changes):
        store = dict(store='backup', total_bytes=1000*GIB, used_bytes=200*GIB,
                     available_bytes=800*GIB, gc={'last-run-state': 'OK'}, prune_status=[])
        store.update(changes)
        return dict(generated_at='2026-11-11T00:00:00+00:00', pbs={'datastores': [store]})

    def history(self, days_and_used):
        return [dict(time=f'2026-11-{day:02d}T00:00:00+00:00', used_bytes=used*GIB,
                     available_bytes=(1000-used)*GIB) for day, used in days_and_used]

    def test_first_run_has_reserve_gap_without_invented_growth(self):
        report = self.report(total_bytes=2*1024*GIB, used_bytes=2*1024*GIB, available_bytes=0)
        plan = inv.build_capacity_plan(report)
        store = plan['stores'][0]
        self.assertEqual(store['immediate_required_total_bytes'], 2560*GIB)
        self.assertEqual(store['immediate_additional_bytes'], 512*GIB)
        self.assertEqual(store['days_until_full'], 0)
        self.assertIsNone(store['observed_growth_bytes_per_day'])
        self.assertEqual(store['projections'], [])
        self.assertIsNone(store['primary_scenario'])
        self.assertIn('Insufficient evidence', store['management_text'])
        self.assertIn('suppress measured growth', ' '.join(store['assumptions']))

    def test_manual_assumption_accounts_for_reserved_unavailable_space(self):
        report = self.report(used_bytes=950*GIB, available_bytes=40*GIB)
        store = inv.build_capacity_plan(report, 20, {'backup': 10})['stores'][0]
        self.assertEqual(store['occupied_bytes'], 960*GIB)
        ninety = next(p for p in store['projections'] if p['days'] == 90)
        self.assertEqual(ninety['required_total_bytes'], 2325*GIB)
        self.assertEqual(ninety['additional_bytes'], 1325*GIB)
        self.assertEqual(ninety['source'], 'user assumption')
        self.assertEqual(store['days_until_full'], 4)
        self.assertEqual(store['days_until_reserve'], 0)

    def test_observed_growth_requires_daily_span_and_projects_net_trend(self):
        store = inv.build_capacity_plan(self.report(capacity_history=self.history([(1, 100), (5, 140), (11, 200)])))['stores'][0]
        self.assertEqual(store['history_count'], 3)
        self.assertEqual(store['history_days'], 10)
        self.assertEqual(store['observed_growth_bytes_per_day'], 10*GIB)
        self.assertEqual(store['days_until_full'], 80)
        self.assertEqual(store['days_until_reserve'], 60)
        self.assertEqual({p['source'] for p in store['projections']}, {'observed'})
        self.assertIn('Limited', store['confidence'])

    def test_observed_peak_is_sensitivity_not_statistical_bound(self):
        store = inv.build_capacity_plan(self.report(capacity_history=self.history([(1, 100), (5, 180), (11, 200)])))['stores'][0]
        self.assertEqual(store['observed_growth_bytes_per_day'], 10*GIB)
        rates = {p['growth_bytes_per_day'] for p in store['projections']}
        self.assertEqual(rates, {10*GIB, 20*GIB})
        self.assertIn('not a statistical upper bound', ' '.join(store['assumptions']))

    def test_declining_history_does_not_credit_future_deletions(self):
        store = inv.build_capacity_plan(self.report(capacity_history=self.history([(1, 400), (5, 300), (11, 200)])))['stores'][0]
        self.assertEqual(store['observed_growth_bytes_per_day'], 0)
        self.assertIsNone(store['days_until_full'])
        self.assertIsNone(store['days_until_reserve'])
        self.assertTrue(all(p['growth_bytes_per_day'] == 0 for p in store['projections']))
        self.assertIn('does not establish', ' '.join(store['assumptions']))

    def test_short_stale_or_duplicate_history_does_not_become_forecast(self):
        histories = [self.history([(1, 100), (11, 200)]),
                     self.history([(8, 100), (9, 150), (11, 200)]),
                     self.history([(1, 100), (2, 150), (3, 200)]),
                     self.history([(1, 100), (1, 100), (11, 200)])]
        for history in histories:
            with self.subTest(history=history):
                store = inv.build_capacity_plan(self.report(capacity_history=history))['stores'][0]
                self.assertIsNone(store['observed_growth_bytes_per_day'])
                self.assertEqual(store['projections'], [])

    def test_future_malformed_and_conflicting_readings_are_excluded(self):
        history = self.history([(1, 100), (5, 140), (11, 200), (12, 220)])
        history += [dict(time='invalid', used_bytes=0, available_bytes=1000*GIB),
                    dict(time='2026-11-11T00:00:00', used_bytes=200*GIB, available_bytes=800*GIB),
                    dict(history[1], used_bytes=160*GIB, available_bytes=840*GIB)]
        store = inv.build_capacity_plan(self.report(capacity_history=history))['stores'][0]
        self.assertEqual(store['history_count'], 2)
        self.assertIsNone(store['observed_growth_bytes_per_day'])

    def test_store_observation_timestamp_handles_pbs_collected_after_pve(self):
        history = self.history([(1, 100), (5, 140), (11, 200)])
        history[-1]['time'] = '2026-11-11T00:05:00+00:00'
        store = inv.build_capacity_plan(self.report(capacity_history=history,
                    capacity_observed_at='2026-11-11T00:05:00+00:00'))['stores'][0]
        self.assertIsNotNone(store['observed_growth_bytes_per_day'])

    def test_cleanup_failure_reduces_confidence_without_subtracting_gc_bytes(self):
        report = self.report(gc={'last-run-state': 'unable to get exclusive lock - ENOLCK', 'pending-bytes': 900*GIB})
        store = inv.build_capacity_plan(report, growth_overrides={'backup': 10})['stores'][0]
        self.assertIn('reduced reliability', store['confidence'])
        self.assertIn('No potential cleanup recovery is credited', store['management_text'])
        self.assertEqual(next(p for p in store['projections'] if p['days'] == 90)['required_total_bytes'], 1375*GIB)

    def test_missing_prune_visibility_reduces_confidence_even_if_gc_succeeded(self):
        store = inv.build_capacity_plan(self.report(prune_status=None), growth_overrides={'backup': 10})['stores'][0]
        self.assertIn('reduced reliability', store['confidence'])
        self.assertIn('unknown', ' '.join(store['assumptions']))

    def test_zero_growth_and_empty_datastore_are_distinct_from_unavailable(self):
        empty = inv.build_capacity_plan(self.report(used_bytes=0, available_bytes=1000*GIB), growth_overrides={'backup': 0})['stores'][0]
        self.assertEqual(empty['immediate_additional_bytes'], 0)
        self.assertIsNone(empty['days_until_full'])
        for changes in ({'used_bytes': None}, {'available_bytes': -1}, {'total_bytes': 0},
                        {'used_bytes': True}, {'total_bytes': float('inf')}, {'available_bytes': 1001*GIB}):
            with self.subTest(changes=changes):
                missing = inv.build_capacity_plan(self.report(**changes))['stores'][0]
                self.assertEqual(missing['status'], 'unavailable')
                self.assertIsNone(missing['immediate_additional_bytes'])
                self.assertEqual(missing['projections'], [])

    def test_invalid_settings_fail_clearly_and_tiny_rate_serializes_without_infinity(self):
        for reserve in (100, -1, float('nan'), True):
            with self.assertRaises(ValueError):
                inv.build_capacity_plan(self.report(), reserve)
        for rate in (-1, float('inf'), float('nan'), 1e308, True):
            with self.assertRaises(ValueError):
                inv.build_capacity_plan(self.report(), growth_overrides={'backup': rate})
        plan = inv.build_capacity_plan(self.report(), growth_overrides={'backup': '1e-320'})
        json.dumps(plan, allow_nan=False)

    def test_no_pbs_and_multiple_datastores_do_not_invent_shared_total(self):
        self.assertEqual(inv.build_capacity_plan({})['stores'], [])
        report = self.report()
        report['pbs']['datastores'].append(dict(copy.deepcopy(report['pbs']['datastores'][0]), store='same-filesystem'))
        plan = inv.build_capacity_plan(report, growth_overrides={'not-collected': 5})
        self.assertEqual(len(plan['stores']), 2)
        self.assertNotIn('total_bytes', plan)
        self.assertTrue(any('never add' in note for note in plan['notes']))
        self.assertTrue(any('not-collected' in note for note in plan['notes']))


if __name__ == '__main__':
    unittest.main()
