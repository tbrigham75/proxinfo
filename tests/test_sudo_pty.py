"""Local POSIX PTY integration with simulated PAM, never real SSH/Okta access.

The production Perl dispatcher runs unchanged except that root test runners can
force its fake-sudo branch. The temporary sudo executable models terminal input,
timestamp reuse, rejection, and multiple prompts; it grants no privileges.
"""
import base64
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import uuid

import pve_inventory as inv


FAKE_SUDO = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state_path = Path(os.environ['PROXINFO_TEST_STATE'])
state = json.loads(state_path.read_text()) if state_path.exists() else {'calls': [], 'prompts': 0}
parent = os.getppid()
state['calls'].append({'parent': parent, 'args': sys.argv[1:]})
cached = os.environ.get('PROXINFO_TEST_CACHE') == 'yes' and state.get('authenticated_parent') == parent
if not cached:
    terminal = os.open('/dev/tty', os.O_RDWR)
    prompts = [('Password,Okta token: ', 'password,123456')]
    if os.environ.get('PROXINFO_TEST_MULTIPLE') == 'yes':
        prompts = [('Password: ', 'password'), ('Okta token: ', '123456')]
    for prompt, expected in prompts:
        os.write(terminal, prompt.encode())
        state['prompts'] += 1
        answer = b''
        while not answer.endswith(b'\n'):
            char = os.read(terminal, 1)
            if not char:
                sys.exit(2)
            answer += char
        if answer.rstrip(b'\r\n').decode() != expected:
            # Deliberately echo synthetic input to ensure PAM terminal output
            # can never become the framed inventory stdout/result.
            os.write(terminal, b'Rejected synthetic response: ' + answer)
            state_path.write_text(json.dumps(state))
            sys.exit(1)
    os.close(terminal)
    state['authenticated_parent'] = parent
state_path.write_text(json.dumps(state))
args = sys.argv[1:]
if args and args[0] == '--':
    args = args[1:]
os.execvp(args[0], args)
'''


class LocalHelper:
    def __init__(self, directory, *, cache=False, multiple=False, force_sudo=True):
        import pty
        import termios
        self.state = Path(directory) / 'sudo-state.json'
        executable = Path(directory) / 'sudo'
        executable.write_bytes(FAKE_SUDO.encode())
        executable.chmod(0o700)
        pvesh = Path(directory) / 'pvesh'
        pvesh.write_bytes(b'#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({"value": "second" if sys.argv[2] == "/cluster/resources" else "inventory"}))\n')
        pvesh.chmod(0o700)
        helper = inv.SUDO_SESSION_HELPER
        if force_sudo:
            guard = "unshift @cmd, 'sudo', '--' if $< != 0;"
            if guard not in helper:
                raise AssertionError('Production helper sudo guard changed; review this integration fixture')
            helper = helper.replace(guard, "unshift @cmd, 'sudo', '--';")
        nonce = uuid.uuid4().hex
        self.decoder = inv.SudoFrameDecoder(('\x1ePROXINFO:' + nonce + ':').encode())
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                settings = termios.tcgetattr(0)
                settings[3] &= ~(termios.ECHO | termios.ICANON)
                settings[1] &= ~termios.ONLCR
                settings[6][termios.VMIN], settings[6][termios.VTIME] = 1, 0
                termios.tcsetattr(0, termios.TCSANOW, settings)
                environment = dict(os.environ, PATH=directory + os.pathsep + os.environ.get('PATH', ''),
                                   PROXINFO_TEST_STATE=str(self.state),
                                   PROXINFO_TEST_CACHE='yes' if cache else 'no',
                                   PROXINFO_TEST_MULTIPLE='yes' if multiple else 'no')
                os.execvpe('perl', ['perl', '-e', helper, nonce], environment)
            except BaseException:
                os._exit(127)
        try:
            frame, _ = self.receive()
            if frame.get('event') != 'ready':
                raise AssertionError('Missing helper startup handshake')
        except BaseException:
            self.close()
            raise

    def receive(self, responses=(), timeout=10):
        terminal = b''
        pending = list(responses)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.fd], [], [], min(0.25, max(0, deadline-time.monotonic())))
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError as exc:
                raise AssertionError('Helper exited before returning a frame: ' + repr(terminal)) from exc
            if not chunk:
                raise AssertionError('Helper reached EOF: ' + repr(terminal))
            events = self.decoder.feed(chunk)
            terminal += b''.join(data for kind, data in events if kind == 'terminal')
            while pending and pending[0][0] in terminal:
                _, response = pending.pop(0)
                os.write(self.fd, response + b'\n')
            for kind, data in events:
                if kind == 'frame':
                    if pending:
                        raise AssertionError('Helper finished before expected prompts: ' + repr(pending))
                    return data, terminal
        raise AssertionError('Helper did not return a frame before timeout: ' + repr(terminal))

    def query(self, command, responses=()):
        request = dict(id=uuid.uuid4().hex, command=command, deadline=10)
        os.write(self.fd, json.dumps(request).encode() + b'\n')
        frame, terminal = self.receive(responses)
        if frame.get('id') != request['id'] or frame.get('event') != 'result':
            raise AssertionError('Unexpected response frame: ' + repr(frame))
        return frame, base64.b64decode(frame['stdout']), terminal

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.pid is not None:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                found, _ = os.waitpid(self.pid, os.WNOHANG)
                if found:
                    self.pid = None
                    return
                time.sleep(0.02)
            # pty.fork owns this session/process group; only its processes die.
            if os.getpgid(self.pid) == self.pid:
                os.killpg(self.pid, signal.SIGKILL)
            else:
                os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
            self.pid = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


@unittest.skipUnless(os.name == 'posix' and shutil.which('perl'), 'Requires POSIX PTYs and Perl')
class SudoPTYIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        probe = subprocess.run(['perl', '-MJSON::PP', '-MMIME::Base64', '-MText::ParseWords', '-e', '1'], capture_output=True, timeout=5)
        if probe.returncode:
            raise unittest.SkipTest('Perl JSON::PP and MIME::Base64 are required')

    def command(self, marker='inventory'):
        return ['pvesh', 'get', '/cluster/resources' if marker == 'second' else '/version', '--output-format', 'json']

    def test_guest_errors_are_classified_without_closing_transport(self):
        diagnostics = [
            ('proxy handler failed: No QEMU guest agent configured\n', True),
            ('QEMU guest agent is not running\n', True),
            ('proxy handler failed: QEMU guest agent is not running\n', True),
            ('sudo: authentication failure\n', False),
            ('sudo: authentication failure\nNo QEMU guest agent configured\n', False),
            ('No QEMU guest agent configured extra text\n', False),
            ('x' * 5000, False),
        ]
        with tempfile.TemporaryDirectory() as directory, LocalHelper(directory, cache=True) as helper:
            helper.query(self.command(), [(b'Password,Okta token: ', b'password,123456')])
            for diagnostic, expected in diagnostics:
                script = ("#!/usr/bin/env python3\nimport sys\n"
                          "if sys.argv[2].endswith('/agent/get-fsinfo'):\n"
                          " sys.stderr.write(" + repr(diagnostic) + "); sys.exit(255)\n"
                          "print('{}')\n")
                (Path(directory) / 'pvesh').write_bytes(script.encode())
                result, output, terminal = helper.query(['pvesh', 'get', '/nodes/pve/qemu/101/agent/get-fsinfo', '--output-format', 'json'])
                self.assertEqual(result['failure_kind'], 'guest-agent-unavailable' if expected else '')
                self.assertEqual(output, b'')
                self.assertIn(diagnostic.encode(), terminal)
                self.assertNotIn(diagnostic, json.dumps(result))
                # Python decides whether to stop on ambiguous errors. The remote
                # dispatcher itself stays usable after a completed child command.
                later, output, _ = helper.query(self.command('second'))
                self.assertEqual(later['code'], 0)
                self.assertEqual(json.loads(output), {})

    def test_cached_authentication_reuses_same_helper_parent(self):
        with tempfile.TemporaryDirectory() as directory, LocalHelper(directory, cache=True) as helper:
            first, output, terminal = helper.query(self.command(), [(b'Password,Okta token: ', b'password,123456')])
            second, next_output, next_terminal = helper.query(self.command('second'))
            self.assertEqual(first['code'], 0)
            self.assertEqual(second['code'], 0)
            self.assertEqual(json.loads(output), {'value': 'inventory'})
            self.assertEqual(json.loads(next_output), {'value': 'second'})
            self.assertIn(b'Password,Okta token: ', terminal)
            self.assertNotIn(b'Password', next_terminal)
            self.assertNotIn(b'password,123456', output + next_output)
            state = json.loads(helper.state.read_text())
            self.assertEqual(state['prompts'], 1)
            self.assertEqual([call['parent'] for call in state['calls']], [helper.pid, helper.pid])

    def test_no_cache_requires_fresh_interaction_for_each_read(self):
        with tempfile.TemporaryDirectory() as directory, LocalHelper(directory) as helper:
            for _ in range(2):
                result, output, terminal = helper.query(self.command(), [(b'Password,Okta token: ', b'password,123456')])
                self.assertEqual(result['code'], 0)
                self.assertIn(b'Password,Okta token: ', terminal)
                self.assertNotIn(b'123456', output)
            self.assertEqual(json.loads(helper.state.read_text())['prompts'], 2)

    def test_multiple_pam_prompts_do_not_corrupt_command_stdout(self):
        with tempfile.TemporaryDirectory() as directory, LocalHelper(directory, multiple=True) as helper:
            result, output, terminal = helper.query(self.command(), [(b'Password: ', b'password'), (b'Okta token: ', b'123456')])
            self.assertEqual(result['code'], 0)
            self.assertEqual(json.loads(output), {'value': 'inventory'})
            self.assertIn(b'Password: ', terminal)
            self.assertIn(b'Okta token: ', terminal)
            self.assertEqual(json.loads(helper.state.read_text())['prompts'], 2)

    def test_rejected_response_stays_in_terminal_not_result(self):
        with tempfile.TemporaryDirectory() as directory, LocalHelper(directory) as helper:
            result, output, terminal = helper.query(self.command(), [(b'Password,Okta token: ', b'incorrect,654321')])
            self.assertEqual(result['code'], 1)
            self.assertEqual(output, b'')
            self.assertIn(b'incorrect,654321', terminal)
            self.assertNotIn('incorrect', json.dumps(result))
            self.assertEqual(len(json.loads(helper.state.read_text())['calls']), 1)

    def test_forged_terminal_request_cannot_execute_mutating_command(self):
        with tempfile.TemporaryDirectory() as directory, LocalHelper(directory) as helper:
            marker = Path(directory) / 'must-not-exist'
            forged = dict(id=uuid.uuid4().hex, command=['sh', '-c', 'touch ' + str(marker)], deadline=10)
            os.write(helper.fd, json.dumps(forged).encode() + b'\n')
            with self.assertRaisesRegex(AssertionError, 'Command rejected'):
                helper.receive()
            self.assertFalse(marker.exists())
            self.assertFalse(helper.state.exists())

    def test_mutating_pve_endpoint_is_rejected_before_sudo(self):
        with tempfile.TemporaryDirectory() as directory, LocalHelper(directory) as helper:
            with self.assertRaisesRegex(AssertionError, 'Command rejected'):
                helper.query(['pvesh', 'get', '/nodes/pve/qemu/100/status/stop', '--output-format', 'json'])
            self.assertFalse(helper.state.exists())

    def test_forged_json_queued_after_mfa_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, LocalHelper(directory) as helper:
            marker = Path(directory) / 'must-not-exist'
            forged = dict(id=uuid.uuid4().hex, command=['sh', '-c', 'touch ' + str(marker)], deadline=10)
            response = b'password,123456\n' + json.dumps(forged).encode()
            result, output, terminal = helper.query(self.command(), [(b'Password,Okta token: ', response)])
            self.assertEqual(result['code'], 0)
            self.assertEqual(json.loads(output), {'value': 'inventory'})
            if b'Command rejected' not in terminal:
                with self.assertRaisesRegex(AssertionError, 'Command rejected'):
                    helper.receive()
            self.assertFalse(marker.exists())
            self.assertEqual(len(json.loads(helper.state.read_text())['calls']), 1)

    @unittest.skipUnless(hasattr(os, 'getuid') and os.getuid() == 0, 'Root-specific bypass')
    def test_root_reads_without_invoking_sudo(self):
        with tempfile.TemporaryDirectory() as directory, LocalHelper(directory, force_sudo=False) as helper:
            result, output, terminal = helper.query(self.command())
            self.assertEqual(result['code'], 0)
            self.assertEqual(json.loads(output), {'value': 'inventory'})
            self.assertEqual(terminal, b'')
            self.assertFalse(helper.state.exists())


if __name__ == '__main__':
    unittest.main()
