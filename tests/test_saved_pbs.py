import json
import tempfile
import unittest
from pathlib import Path
import pve_inventory as inv
from test_inventory import collect
from test_extensions import collect_pbs

class SavedPBSTests(unittest.TestCase):
    def saved(self, root, name='old', server='pbs'):
        report = collect()
        report['pbs'] = collect_pbs()
        report['pbs']['server'] = server
        report['generated_at'] = '2026-01-01T00:00:00+00:00'
        directory = root/name
        directory.mkdir()
        (directory/'inventory.json').write_text(json.dumps(report), encoding='utf-8')
        (directory/'report-complete.json').write_text('{}')
        return directory

    def fresh(self):
        report = collect()
        report['pbs'] = dict(requested=False, datastores=[])
        return report

    def test_reuse_preserves_original_time_and_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); self.saved(root)
            report=self.fresh(); inv.saved_pbs_fallback(report,root)
            self.assertEqual(report['pbs']['status'],'historical')
            self.assertEqual(report['pbs']['provenance']['collected_at'],'2026-01-01T00:00:00+00:00')
            self.assertIn('Previous PBS Data',inv.pbs_history_notice(report))
            self.assertEqual(report['vms'][0]['vmid'],100)

    def test_ambiguous_and_wrong_server_are_not_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); self.saved(root); self.saved(root,'other','other')
            for expected in (None,'missing'):
                report=self.fresh(); inv.saved_pbs_fallback(report,root,expected)
                self.assertEqual(report['pbs']['datastores'],[])

    def test_partial_live_collection_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); self.saved(root)
            report=self.fresh(); report['pbs']['datastores']=[{'store':'live'}]
            inv.saved_pbs_fallback(report,root)
            self.assertEqual(report['pbs']['datastores'],[{'store':'live'}])

    def test_index_ignores_corrupt_and_unfinished_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); self.saved(root)
            unfinished=self.saved(root,'unfinished'); (unfinished/'report-complete.json').unlink()
            broken=self.saved(root,'broken'); (broken/'inventory.json').write_text('{')
            inv.update_report_index(root)
            text=(root/'index.html').read_text()
            self.assertIn('old/inventory.html',text)
            self.assertNotIn('unfinished/inventory.html',text)
            self.assertNotIn('broken/inventory.html',text)
