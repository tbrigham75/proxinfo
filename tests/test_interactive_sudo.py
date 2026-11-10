"""Interactive privilege transport tests; no SSH servers or credentials required."""
import contextlib
import base64
import io
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pve_inventory as inv


class InteractiveSudoSettingsTests(unittest.TestCase):
    def args(self, **overrides):
        values = dict(no_prompt=False, ssh_host='pve', ssh_user='audit', ssh_port=None,
                      ssh_key='/tmp/example-key', ssh_auth='key', timeout=20,
                      ssh_auth_timeout=120, ssh_sudo=False, ssh_sudo_mode=None)
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_legacy_and_explicit_modes(self):
        self.assertEqual(inv.SSHClient('pve').sudo_mode, 'none')
        self.assertEqual(inv.SSHClient('pve', sudo=True).sudo_mode, 'noninteractive')
        self.assertEqual(inv.SSHClient('pve', sudo_mode='interactive').sudo_mode, 'interactive')
        with self.assertRaises(ValueError):
            inv.SSHClient('pve', sudo_mode='unknown')

    def test_no_prompt_rejects_interactive_sudo_before_connection(self):
        with patch.object(inv.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                inv.SSHClient('pve', sudo_mode='interactive', batch=True, authentication='key')
            run.assert_not_called()

    def test_pve_explicit_mode_overrides_configuration(self):
        args = self.args(ssh_sudo_mode='interactive')
        with patch.object(inv.sys.stdin, 'isatty', return_value=True), patch('builtins.input', side_effect=AssertionError('Unexpected prompt')):
            settings = inv.ssh_settings(args, lambda key: 'noninteractive' if key == 'ssh_sudo_mode' else None)
        self.assertEqual(settings['sudo_mode'], 'interactive')
        self.assertNotIn('password', settings)

    def test_pve_configured_mode_is_used(self):
        with patch.object(inv.sys.stdin, 'isatty', return_value=True), patch('builtins.input', side_effect=AssertionError('Unexpected prompt')):
            settings = inv.ssh_settings(self.args(), lambda key: 'interactive' if key == 'ssh_sudo_mode' else None)
        self.assertEqual(settings['sudo_mode'], 'interactive')

    def test_pbs_has_separate_sudo_mode(self):
        args = SimpleNamespace(pbs=True, pbs_url=None, no_prompt=False)
        config = dict(pbs_auth='ssh', pbs_url='https://pbs:8007', pbs_ssh_host='pbs',
                      pbs_ssh_user='backup-audit', pbs_ssh_auth='key',
                      pbs_ssh_key='/tmp/pbs-key', pbs_ssh_sudo_mode='interactive')
        with patch.dict(inv.os.environ, {}, clear=True), patch.object(inv.sys.stdin, 'isatty', return_value=True), patch('builtins.input', side_effect=AssertionError('Unexpected prompt')):
            settings = inv.pbs_settings(args, config)
        self.assertEqual(settings['ssh_options']['sudo_mode'], 'interactive')
        self.assertEqual(settings['ssh_options']['user'], 'backup-audit')

    def test_unattended_configured_sudo_mode_does_not_prompt(self):
        with patch.object(inv.sys.stdin, 'isatty', return_value=False), patch('builtins.input', side_effect=AssertionError('Unexpected prompt')):
            with self.assertRaisesRegex(ValueError, 'cron/--no-prompt'):
                inv.ssh_settings(self.args(no_prompt=True), lambda key: 'interactive' if key == 'ssh_sudo_mode' else None)

    def test_root_and_explicit_noninteractive_modes_skip_sudo_question(self):
        with patch.object(inv, 'prompt_choice', side_effect=AssertionError('Unexpected prompt')):
            self.assertEqual(inv.choose_sudo_mode(None, 'root', True, False, 'PVE'), 'none')
            self.assertEqual(inv.choose_sudo_mode('noninteractive', 'audit', True, False, 'PVE'), 'noninteractive')

    def test_manual_nonroot_user_can_select_mfa_without_saving_response(self):
        with patch.object(inv, 'prompt_choice', return_value='interactive') as prompt, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(inv.choose_sudo_mode(None, 'audit', True, False, 'PVE'), 'interactive')
        self.assertEqual(prompt.call_args.args[-1], 'interactive')


class InteractiveSudoRoutingTests(unittest.TestCase):
    def client(self):
        client = inv.SSHClient('pve', user='audit', sudo_mode='interactive', authentication='key')
        client.control_path = '/tmp/proxinfo-test'
        client.interactive_session = MagicMock()
        client.interactive_session.run.return_value = subprocess.CompletedProcess([], 0, '[]', '')
        return client

    def test_read_commands_reuse_one_session(self):
        client = self.client()
        with patch.object(inv.subprocess, 'run') as run:
            self.assertEqual(client.get('/cluster/resources', type='vm'), [])
            self.assertEqual(client.backend_command(['hostname', '-s']), '[]')
            run.assert_not_called()
        self.assertEqual(client.interactive_session.run.call_count, 2)
        self.assertEqual(client.interactive_session.run.call_args_list[0].args[0][:3], ['pvesh', 'get', '/cluster/resources'])
        self.assertEqual(client.interactive_session.run.call_args_list[1].args[0], ['hostname', '-s'])

    def test_mutating_commands_never_reach_session(self):
        client = self.client()
        for command in (['lvchange', '-ay', 'disk'], ['dmsetup', 'create', 'bad']):
            with self.assertRaises(ValueError):
                client.backend_command(command)
        with self.assertRaises(ValueError):
            client.get('/nodes/pve/qemu/100/status/stop')
        with self.assertRaises(ValueError):
            client.run_pvesh(['pvesh', 'set', '/cluster/options'])
        client.interactive_session.run.assert_not_called()

    def test_closing_client_closes_only_its_session(self):
        client = self.client()
        session = client.interactive_session
        with patch.object(inv.subprocess, 'run') as run:
            client.close()
        session.close.assert_called_once()
        for call in run.call_args_list:
            self.assertNotIn('sudo', call.args[0])
        self.assertIsNone(client.control_path)


class InteractiveSudoProtocolTests(unittest.TestCase):
    def session(self):
        client = SimpleNamespace(host='pve', auth_timeout=30)
        session = inv.InteractiveSudoSession(client)
        session.terminal = 13
        session.process = MagicMock()
        session.process.stdin.fileno.return_value = 11
        session.process.stdout.fileno.return_value = 12
        session.ready = True
        return session

    def terminal_module(self):
        settings = lambda fd: [0, 0, 0, 3, 0, 0, [0] * 8]
        return SimpleNamespace(tcgetattr=MagicMock(side_effect=settings),
                               tcsetattr=MagicMock(), ECHO=1, ICANON=2,
                               VMIN=0, VTIME=1, TCSANOW=0, TCSAFLUSH=2)

    def test_fragmented_frames_keep_auth_text_separate(self):
        decoder = inv.SudoFrameDecoder(b'\x1ePROXINFO:nonce:')
        events = decoder.feed(b'[sudo] Password,Okta token: \x1ePROX')
        self.assertEqual(events, [('terminal', b'[sudo] Password,Okta token: ')])
        events = decoder.feed(b'INFO:nonce:{"event":"ready","uid":1000}\r\n')
        self.assertEqual(events, [('frame', {'event': 'ready', 'uid': 1000})])
        self.assertEqual(decoder.buffer, b'')

    def test_malformed_frame_error_does_not_echo_payload(self):
        decoder = inv.SudoFrameDecoder(b'nonce:')
        with self.assertRaises(inv.ReadError) as caught:
            decoder.feed(b'nonce:secret-password,otp\n')
        self.assertNotIn('secret-password', str(caught.exception))
        self.assertIn('framing', str(caught.exception))

    def test_auth_responses_are_forwarded_without_becoming_result_data(self):
        session = self.session()
        payload = dict(id='a' * 32, command=['pvesh', 'get', '/version'])
        frame = dict(event='result', id=payload['id'], code=0, stdout=base64.b64encode(b'{"version":"9"}').decode())
        secret = b'separate-password,123456\n'
        raw = [b'[sudo] Password and token: ', secret, session.decoder.prefix + json.dumps(frame).encode() + b'\n']
        terminal = self.terminal_module()
        selector = SimpleNamespace(select=MagicMock(side_effect=[([12], [], []), ([13], [], []), ([12], [], [])]))
        writes = []
        with patch.dict(inv.sys.modules, {'termios': terminal, 'select': selector}), \
             patch.object(inv.os, 'read', side_effect=raw), \
             patch.object(inv.os, 'write', side_effect=lambda fd, value: writes.append((fd, value)) or len(value)), \
             patch.object(inv.time, 'monotonic', return_value=0):
            result = session._exchange(payload, 10, True)
        self.assertEqual(result, frame)
        self.assertIn((11, secret), writes)
        self.assertNotIn((13, secret), writes)
        self.assertNotIn('separate-password', json.dumps(result))
        # The first write disables echo; the final write restores the original flags.
        self.assertEqual(terminal.tcsetattr.call_args_list[0].args[2][3], 0)
        self.assertEqual(terminal.tcsetattr.call_args_list[-1].args[2][3], 3)

    def test_auth_timeout_and_cancel_restore_terminal(self):
        for cancelled in (False, True):
            session = self.session()
            terminal = self.terminal_module()
            selector = SimpleNamespace(select=MagicMock(side_effect=KeyboardInterrupt))
            ticks = [0, 0] if cancelled else [0, 11]
            with self.subTest(cancelled=cancelled), \
                 patch.dict(inv.sys.modules, {'termios': terminal, 'select': selector}), \
                 patch.object(inv.time, 'monotonic', side_effect=ticks):
                with self.assertRaises(KeyboardInterrupt if cancelled else inv.ReadError):
                    session._exchange(None, 10, True)
            self.assertEqual(terminal.tcsetattr.call_args_list[-1].args[2][3], 3)

    def test_request_mismatch_is_fatal(self):
        session = self.session()
        frame = dict(event='result', id='b' * 32, code=0, stdout='')
        selector = SimpleNamespace(select=MagicMock(return_value=([12], [], [])))
        with patch.dict(inv.sys.modules, {'termios': self.terminal_module(), 'select': selector}), \
             patch.object(inv.os, 'write', side_effect=lambda fd, value: len(value)), \
             patch.object(inv.os, 'read', return_value=session.decoder.prefix + json.dumps(frame).encode() + b'\n'), \
             patch.object(inv.time, 'monotonic', return_value=0):
            with self.assertRaises(inv.ReadError):
                session._exchange(dict(id='a' * 32), 10, False)

    def test_failed_exchange_closes_without_replaying_credentials(self):
        session = self.session()
        with patch.object(session, '_exchange', side_effect=inv.ReadError('framing failed')) as exchange, \
             patch.object(session, 'close') as close, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(inv.ReadError):
                session.run(['pvesh', 'get', '/version'], 10)
        exchange.assert_called_once()
        close.assert_called_once()

    def test_run_cancellation_closes_the_owned_session(self):
        session = self.session()
        with patch.object(session, '_exchange', side_effect=KeyboardInterrupt), \
             patch.object(session, 'close') as close, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                session.run(['hostname', '-s'], 10)
        close.assert_called_once()

    def test_invalid_result_closes_session(self):
        for frame in (dict(code='zero', stdout=''), dict(code=0, stdout='not base64!')):
            session = self.session()
            with self.subTest(frame=frame), patch.object(session, '_exchange', return_value=frame), \
                 patch.object(session, 'close') as close, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises((inv.ReadError, ValueError)):
                    session.run(['hostname', '-s'], 10)
            close.assert_called_once()

    def test_timeout_budget_includes_fresh_mfa_time(self):
        session = self.session()
        with patch.object(session, '_exchange', return_value=dict(code=0, stdout='')) as exchange, \
             contextlib.redirect_stderr(io.StringIO()):
            session.run(['hostname', '-s'], 10)
        payload, timeout, terminal_input = exchange.call_args.args
        self.assertEqual(timeout, 40)
        self.assertEqual(payload['deadline'], 40)
        self.assertTrue(terminal_input)

    def test_startup_requires_verified_handshake_and_terminal(self):
        session = self.session()
        session.client.ssh_args = lambda: ['ssh', '-T', '-S', '/tmp/test']
        with patch.object(inv.os, 'name', 'posix'), patch.object(inv.sys.stdin, 'isatty', return_value=True), \
             patch.object(inv.os, 'open', return_value=13), patch.object(inv.subprocess, 'Popen') as popen, \
             patch.object(session, '_exchange', return_value=dict(event='result', uid=1000)), \
             patch.object(session, 'close') as close:
            with self.assertRaises(inv.ReadError):
                session.start()
        close.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn('-tt', command)
        self.assertNotIn('-T', command)
        self.assertNotIn('sudo -i', command[-1])
        self.assertIn('perl', command[-1])

    def test_query_error_returns_generic_stderr_only(self):
        session = self.session()
        with patch.object(session, '_exchange', return_value=dict(code=1, stdout='')), \
             patch.object(session, 'close') as close, \
             contextlib.redirect_stderr(io.StringIO()):
            result = session.run(['pvesh', 'get', '/version'], 10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('exit 1', result.stderr)
        close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
