import io
import json
import unittest
from unittest.mock import patch, MagicMock
from test_inventory import inv, collect, FakeClient, GIB


class AuthAndTotals(unittest.TestCase):
    def test_partial_total_visible_in_both_reports(self):
        r = collect(FakeClient({'/nodes/b/qemu/101/config': inv.ReadError('offline')}))
        inv.sort_report(r, 'configured')
        self.assertIn('Partial', inv.measured_total_label(r['storage_totals']))
        self.assertEqual(r['storage_totals'][1]['known_subtotal_bytes'], 20 * GIB)
        for markup in (inv.html_report(r), inv.management_summary_html(r)):
            self.assertIn('20.00 GiB · Partial', markup)
        markup = inv.html_report(r)
        self.assertNotIn('data-page="changes"', markup)
        self.assertIn('Needs Attention', markup)
        self.assertLess(markup.index('data-page-link="management"'), markup.index('data-page-link="downloads"'))

    def test_password_cookie_and_read_boundary(self):
        opener = MagicMock()
        opener.open.side_effect = [io.StringIO(json.dumps({'data': {'ticket': 'session-secret'}})), io.StringIO('{"data": {"version": "4"}}')]
        with patch.object(inv.request, 'build_opener', return_value=opener):
            client = inv.PBSClient('https://pbs:8007', auth='password', username='audit@pbs', password='hidden')
            self.assertEqual(client.get('/version')['version'], '4')
            self.assertEqual(opener.open.call_args.args[0].get_header('Cookie'), 'PBSAuthCookie=session-secret')
            with self.assertRaises(ValueError):
                client.get('/access/ticket')
            client.close()
            self.assertIsNone(client.cookie)

    def test_ssh_uses_only_get_and_closes(self):
        ssh = MagicMock()
        ssh.ssh_args.return_value = ['ssh']
        ssh.host, ssh.sudo = 'pbs', False
        with patch.object(inv, 'SSHClient', return_value=ssh), patch.object(inv.subprocess, 'run', return_value=MagicMock(returncode=0, stdout='[]')) as run:
            client = inv.PBSClient('https://pbs:8007', auth='ssh', ssh_options={})
            self.assertEqual(client.get('/admin/datastore'), [])
            self.assertIn('PROXMOX_DEBUG_API_CODE=0 proxmox-backup-debug api get', run.call_args.args[0][-1])
            with self.assertRaises(ValueError):
                client.get('/admin/datastore/x/prune')
            client.close()
            ssh.close.assert_called_once()

    def test_session_redaction(self):
        for name in ('PBSAuthCookie', 'ticket', 'CSRFPreventionToken', 'tfa-challenge'):
            self.assertNotIn('sensitive', inv.redact_diagnostics(name + '=sensitive'))

    def test_noninteractive_mfa_fails_without_prompt(self):
        opener = MagicMock()
        opener.open.return_value = io.StringIO(json.dumps({'data': {'ticket': 'challenge', 'NeedTFA': True}}))
        with patch.object(inv.request, 'build_opener', return_value=opener), patch.object(inv.getpass, 'getpass') as prompt:
            with self.assertRaises(inv.ReadError):
                inv.PBSClient('https://pbs:8007', auth='password', username='audit@pbs', password='hidden')
            prompt.assert_not_called()

    def test_password_renewal_uses_ticket(self):
        opener = MagicMock()
        opener.open.side_effect = [io.StringIO(json.dumps({'data': {'ticket': ticket}})) for ticket in ('first', 'renewed')]
        opener.open.side_effect = list(opener.open.side_effect) + [io.StringIO('{"data": []}')]
        with patch.object(inv.request, 'build_opener', return_value=opener):
            client = inv.PBSClient('https://pbs:8007', auth='password', username='audit@pbs', password='hidden')
            client.ticket_time -= 3601
            client.get('/admin/datastore')
            self.assertIn(b'password=first', opener.open.call_args_list[1].args[0].data)
            self.assertEqual(client.cookie, 'PBSAuthCookie=renewed')

    def test_unapproved_parameter_rejected(self):
        with patch.object(inv.request, 'build_opener'):
            client = inv.PBSClient('https://pbs:8007', 'audit@pbs!r', 'secret')
            with self.assertRaises(ValueError):
                client.get('/version', **{'output-format': 'text'})
