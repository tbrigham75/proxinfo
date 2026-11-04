import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pve_inventory as inv
from test_inventory import FakeClient, collect


class OutputReuseTests(unittest.TestCase):
    def test_repeated_runs_replace_reports_and_logs_preserving_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'notes.txt').write_text('keep me')
            (directory / 'saved').mkdir()
            (directory / 'saved' / 'old.html').write_text('keep this too')
            client = FakeClient()
            ids = []
            with patch('pve_inventory.Client', return_value=client), \
                 patch('pve_inventory.shutil.which', return_value='/usr/bin/pvesh'), \
                 patch('builtins.input', side_effect=AssertionError('Unexpected prompt')), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                for name in ('first-run', 'second-run'):
                    client.data['/nodes/a/qemu/100/config']['name'] = name
                    self.assertEqual(inv.main(['--mode', 'local', '--environment', 'cluster',
                                              '--no-prompt', '--no-pbs', '--output-dir', tmp]), 0)
                    report = json.loads((directory / 'inventory.json').read_text(encoding='utf-8'))
                    ids.append(report['run']['run_id'])
            self.assertNotEqual(*ids)
            self.assertIn('second-run', (directory / 'inventory.html').read_text(encoding='utf-8'))
            self.assertNotIn('first-run', (directory / 'inventory.html').read_text(encoding='utf-8'))
            self.assertNotIn(ids[0], (directory / 'collection.log').read_text(encoding='utf-8'))
            with (directory / 'query_timings.csv').open(encoding='utf-8-sig', newline='') as stream:
                self.assertEqual({r['run_id'] for r in csv.DictReader(stream)}, {ids[1]})
            self.assertEqual(json.loads((directory / 'report-complete.json').read_text())['run_id'], ids[1])
            self.assertEqual((directory / 'notes.txt').read_text(), 'keep me')
            self.assertEqual((directory / 'saved' / 'old.html').read_text(), 'keep this too')

    def test_failed_publication_does_not_leave_old_completion_marker(self):
        report = collect()
        inv.sort_report(report, 'configured')
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inv.export_report(report, directory)
            with patch.object(Path, 'replace', side_effect=OSError('simulated publication failure')):
                with self.assertRaises(OSError):
                    inv.export_report(report, directory)
            self.assertFalse((directory / 'report-complete.json').exists())

    def test_failed_staging_preserves_previous_report(self):
        report = collect()
        inv.sort_report(report, 'configured')
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            inv.export_report(report, directory)
            original = (directory / 'inventory.html').read_bytes()
            marker = (directory / 'report-complete.json').read_bytes()
            with patch('pve_inventory.html_report', side_effect=ValueError('simulated render failure')):
                with self.assertRaises(ValueError):
                    inv.export_report(report, directory)
            self.assertEqual((directory / 'inventory.html').read_bytes(), original)
            self.assertEqual((directory / 'report-complete.json').read_bytes(), marker)
