#!/usr/bin/env python3
"""Read-only, standard-library PVE cluster VM inventory. Python 3.9+."""

import ipaddress
import argparse
import csv
import getpass
import html
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import threading
import uuid
import sqlite3
from contextlib import closing
from collections import Counter
from datetime import datetime, timezone
from urllib import error, parse, request


VERSION = "1.4.0"


class CronBusy(Exception):
    pass


class CronLock:
    """Kernel releases the lock on process exit; the lock file is never unlinked."""
    def __init__(self, path):
        try:
            import fcntl
        except ImportError:
            raise ValueError("Cron mode requires Linux/POSIX; use WSL instead of native Windows") from None
        self.fcntl = fcntl
        self.stream = os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "a+")
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise CronBusy("Another run is active for this output root; skipped") from None
        except BaseException:
            self.stream.close()
            raise

    def close(self):
        self.stream.close()


def atomic_text(path, text):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class RunTracker:
    """Local observations only; heartbeat never issues a remote request."""
    def __init__(self, directory, progress="auto", max_runtime=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started = time.monotonic()
        self.progress = progress
        self.max_runtime = max_runtime
        self.cancelled = False
        self.expired = False
        self.queries = []
        self.log = (self.directory / "collection.log").open("w", encoding="utf-8")
        self.stream = (self.directory / "query_timings.csv").open("w", newline="", encoding="utf-8-sig")
        self.writer = csv.DictWriter(self.stream, fieldnames=["run_id", "operation", "started_at", "elapsed_seconds", "outcome"])
        self.writer.writeheader()
        self.stream.flush()
        self.message("ProxInfo " + VERSION + " | run " + self.run_id)

    def message(self, text):
        text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", str(text))
        self.log.write(datetime.now(timezone.utc).isoformat() + " " + text + "\n")
        self.log.flush()
        if self.progress != "off":
            print(text, file=sys.stderr, flush=True)

    def query(self, operation, call, timeout=None):
        if self.max_runtime is not None and time.monotonic() - self.started >= self.max_runtime:
            self.expired = True
        if self.cancelled or self.expired:
            raise ReadError("Not queried: collection cancelled or overall time limit reached")
        start = time.monotonic()
        stamp = datetime.now(timezone.utc).isoformat()
        stop = threading.Event()
        self.message("Reading " + operation + (f" | timeout {timeout:g}s" if timeout is not None else ""))
        def heartbeat():
            while not stop.wait(5):
                self.message(f"Waiting: {operation} | {time.monotonic()-start:.0f}s elapsed" + (f" | timeout {timeout:g}s" if timeout is not None else ""))
        worker = threading.Thread(target=heartbeat, daemon=True)
        worker.start()
        outcome = "success"
        try:
            return call()
        except KeyboardInterrupt:
            self.cancelled = True
            outcome = "cancelled"
            raise ReadError("Cancelled during query; remaining measurements were not requested") from None
        except Exception as exc:
            reason = str(exc).lower()
            outcome = ("timeout" if "timeout" in reason or "timed out" in reason else
                       "access denied" if "403" in reason or "permission denied" in reason else "failed")
            raise
        finally:
            stop.set()
            worker.join()
            row = dict(run_id=self.run_id, operation=operation, started_at=stamp,
                       elapsed_seconds=round(time.monotonic()-start, 3), outcome=outcome)
            self.queries.append(row)
            self.writer.writerow({k: csv_cell(v) for k,v in row.items()})
            self.stream.flush()
            self.message(f"{outcome}: {operation} | {row['elapsed_seconds']}s")

    def metadata(self):
        groups = Counter()
        for query in self.queries:
            if query['outcome'] != 'success':
                node = re.search(r"/nodes/([^/]+)", query['operation'])
                storage = re.search(r"/storage/([^/]+)", query['operation'])
                groups[(node[1] if node else 'general') + '/' + (storage[1] if storage else 'general') + ': ' + query['outcome']] += 1
        return dict(failures_by_node_storage=dict(groups), version=VERSION, run_id=self.run_id, started_at=self.started_at,
                    finished_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=round(time.monotonic()-self.started, 3),
                    cancelled=self.cancelled, time_limit_reached=self.expired,
                    query_outcomes=dict(Counter(q['outcome'] for q in self.queries)),
                    slowest_queries=sorted(self.queries, key=lambda q:q['elapsed_seconds'], reverse=True)[:10])

    def close(self):
        self.log.close()
        self.stream.close()


class ObservedClient:
    def __init__(self, client, tracker):
        object.__setattr__(self, "client", client)
        object.__setattr__(self, "tracker", tracker)

    def __getattr__(self, name):
        return getattr(self.client, name)

    def __setattr__(self, name, value):
        setattr(self.client, name, value)

    def observed(self, name, call):
        clients = [self.client] + ([self.client.pvesh] if hasattr(self.client, "pvesh") else [])
        previous = [(c,c.timeout) for c in clients if hasattr(c,"timeout")]
        if self.tracker.max_runtime is not None:
            remaining = max(0.01, self.tracker.max_runtime - (time.monotonic()-self.tracker.started))
            for c, timeout in previous:
                c.timeout = min(timeout, remaining)
        try:
            return self.tracker.query(name, call, getattr(self.client, "timeout", None))
        finally:
            for c, timeout in previous:
                c.timeout = timeout

    def get(self, path, **params):
        return self.observed("GET " + path, lambda: self.client.get(path, **params))

    def thin_status(self, node):
        return read_thin_status(self, node) if hasattr(self.client, "backend_command") else self.observed("LVM-thin counters on " + node, lambda: self.client.thin_status(node))

    def backend_command(self, command):
        node = next((arg.split('=', 1)[1] for arg in command if arg.startswith('HostKeyAlias=')), None)
        label = ('/nodes/' + node + '/storage/host ') if node else ''
        return self.observed(label + 'Storage metadata ' + shlex.join(command), lambda: self.client.backend_command(command))


NA = "N/A"
DISK = re.compile(r"^(?:(?:ide|sata|scsi|virtio)\d+|efidisk0|tpmstate0|unused\d+)$")
SHARED_TYPES = {"nfs", "cifs", "rbd", "cephfs", "iscsi", "iscsidirect", "zfs", "glusterfs"}
ACTUAL_METHODS = {
    "dir": "file allocated bytes",
    "nfs": "file allocated bytes",
    "cifs": "file allocated bytes",
    "cephfs": "file allocated bytes",
    "glusterfs": "file allocated bytes",
    "zfspool": "ZFS usedbydataset",
    "rbd": "Ceph RBD image allocated bytes",
    "lvmthin": "LVM-thin mapped bytes (active volumes)",
}
LIMITATIONS = (
    "PBS reporting is separate and optional; its collection status is shown on the PBS page.",
    "Configured capacity is logical provisioning, not host allocation, guest usage or PBS consumption.",
    "Actual means backend-reported disk allocation, with the measurement method recorded for each disk. "
    "It is not exclusive, reclaimable space or complete pool usage; snapshots, clones, backing chains, "
    "compression, deduplication and filesystem overhead affect accounting. No cluster physical-use total is inferred.",
    "Guest usage is shown per filesystem exactly as reported by QEMU guest agent. Free = reported total minus used, "
    "not necessarily space available to an unprivileged user. Mounts may overlap; they are never summed.",
    "Disk totals include attached writable disks, EFI and TPM state; exclude CD-ROMs, cloud-init media and unused disks. "
    "Duplicate references to the same volume count once. Unknown sizes make a VM total N/A, not zero.",
    "Cluster logical totals deduplicate shared volume IDs across nodes and local volume IDs within each node. "
    "Storage aliases pointing at the same backing volume cannot be detected; correct those aliases before interpreting totals.",
    "Backup inclusion means selection by a currently enabled PVE scheduled job, not successful backup or disk coverage. "
    "Disabled-job matches are listed separately. Manual/external backups and PBS-only jobs are not discovered.",
    "PBS needs depend on backed-up disks, changed chunks over time, compression, shared deduplication, "
    "snapshot retention and prune/GC completion. VM disk capacity cannot establish a VM's PBS footprint. "
    "Pruning removes snapshot references; garbage collection later reclaims eligible unreferenced chunks.",
    "Results are a sequence of observations, not an atomic cluster snapshot. Migration/configuration changes can cause gaps. "
    "API ACLs can silently hide VMs, so a successful API call does not prove cluster-wide visibility.",
)


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
        return result if result >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def size_bytes(value):
    if value is None:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTPE]?)(?:i?B)?", str(value), re.I)
    if not match:
        return None
    from decimal import Decimal
    return int(Decimal(match[1]) * (1024 ** (" KMGTPE".index(match[2].upper()) if match[2] else 0)))


def human(value):
    if value is None:
        return NA
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
        if abs(amount) < 1024 or unit == "EiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024


def enabled(value):
    return str(value).lower() not in ("0", "false", "no", "off", "none", "")


def segment(value):
    return parse.quote(str(value), safe="")


class ReadError(Exception):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an API credential to a redirect target.
        return None


class Client:
    def __init__(self, mode="local", timeout=20, url=None, token_id=None,
                 token_secret=None, ca_file=None):
        self.mode, self.timeout = mode, timeout
        self.secret = token_secret
        if mode == "api":
            parsed = parse.urlsplit(url or "")
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment
                    or parsed.path.rstrip("/") not in ("", "/api2/json")):
                raise ValueError("PVE_URL must be https://host:8006 (no credentials or query string)")
            if not token_id or not token_secret:
                raise ValueError("API mode requires PVE_TOKEN_ID and PVE_TOKEN_SECRET (or config entries)")
            self.base = f"https://{parsed.netloc}/api2/json"
            self.auth = f"PVEAPIToken={token_id}={token_secret}"
            context = ssl.create_default_context(cafile=ca_file)
            self.opener = request.build_opener(NoRedirect(), request.HTTPSHandler(context=context))

    def run_pvesh(self, command):
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=self.timeout, check=False, shell=False)

    def backend_command(self, command):
        if self.mode != "local":
            raise ReadError("Exact LVM-thin measurements require local or SSH access, not an API token")
        if not approved_backend_command(command):
            raise ValueError("Not an approved backend read command")
        result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout, check=False, shell=False)
        if result.returncode:
            raise ReadError(result.stderr.strip()[:600] or "Backend read failed")
        return result.stdout

    def thin_status(self, node):
        return read_thin_status(self, node)

    def get(self, path, **params):
        # Fixed GET-only surface, including exactly one read-only guest-agent command.
        allowed = (
            r"/version", r"/nodes", r"/cluster/status", r"/cluster/resources", r"/cluster/backup",
            r"/nodes/[^/]+/(?:status|storage)", r"/storage", r"/cluster/backup/[^/]+/included_volumes",
            r"/nodes/[^/]+/qemu/\d+/(?:config|status/current|agent/get-fsinfo)",
            r"/nodes/[^/]+/storage/[^/]+/content/[^/]+",
        )
        if not any(re.fullmatch(pattern, path) for pattern in allowed):
            raise ValueError(f"Not an approved inventory GET path: {path}")
        try:
            if self.mode == "local":
                # Unlike HTTPS routing, pvesh's direct RESTHandler lookup does
                # not URL-decode path components. Content routes consume the
                # remaining raw volume ID (including its slash) as one fragment.
                command = ["pvesh", "get", parse.unquote(path), "--output-format", "json"]
                for key, value in params.items():
                    command.extend([f"--{key}", str(value)])
                result = self.run_pvesh(command)
                if result.returncode:
                    raise ReadError("pvesh: " + result.stderr.strip()[:600])
                return json.loads(result.stdout)
            url = self.base + path
            if params:
                url += "?" + parse.urlencode(params)
            req = request.Request(url, headers={"Authorization": self.auth}, method="GET")
            with self.opener.open(req, timeout=self.timeout) as response:
                return json.load(response)["data"]
        except error.HTTPError as exc:
            raise ReadError(f"HTTP {exc.code}: endpoint unavailable, permission denied or request rejected") from None
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            message = str(exc)
            if self.secret:
                message = message.replace(self.secret, "[REDACTED]")
            raise ReadError(message[:600]) from None


class SSHClient(Client):
    """Run allowlisted PVE GETs and exact thin-status reads through an OpenSSH master.

    Python and report generation stay local. No code or report is uploaded.
    """
    def __init__(self, host, user="root", port=None, identity=None, timeout=20,
                 auth_timeout=120, sudo=False, batch=False, authentication="auto"):
        super().__init__("local", timeout)
        if not host or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", host):
            raise ValueError("SSH host must be a hostname, IP address or SSH config alias, without user@ or a URL")
        if not user or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", user):
            raise ValueError("Invalid SSH username")
        if port is not None and not 1 <= int(port) <= 65535:
            raise ValueError("SSH port must be between 1 and 65535")
        self.host, self.user, self.port = host, user, port
        self.identity = str(Path(identity).expanduser().resolve()) if identity else None
        self.auth_timeout, self.sudo, self.batch = auth_timeout, sudo, batch
        if authentication not in ("auto", "password", "key"):
            raise ValueError("SSH authentication must be auto, password or key")
        if batch and authentication == "password":
            raise ValueError("Password SSH login requires an interactive terminal. Omit --no-prompt or use key authentication")
        self.authentication = authentication
        self.directory = None
        self.control_path = None

    def ssh_args(self):
        args = ["ssh", "-a", "-T", "-S", self.control_path,
                "-o", "ClearAllForwardings=yes", "-o", "ForwardX11=no",
                "-o", f"ConnectTimeout={max(1, int(self.timeout))}",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
                "-l", self.user]
        if self.port is not None:
            args += ["-p", str(self.port)]
        if self.identity:
            args += ["-i", self.identity]
        if self.authentication == "password":
            args += ["-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=keyboard-interactive,password"]
        elif self.authentication == "key":
            args += ["-o", "PreferredAuthentications=publickey"]
        return args

    def connect(self):
        if os.name != "posix":
            raise ValueError("SSH mode requires WSL/Linux or another POSIX system with OpenSSH multiplexing. Run from your WSL terminal")
        if not shutil.which("ssh"):
            raise ValueError("OpenSSH client not found. Install openssh-client in WSL")
        self.directory = tempfile.TemporaryDirectory(prefix="pve-ssh-")
        self.control_path = str(Path(self.directory.name) / "s")
        if len(os.fsencode(self.control_path)) > 100:
            self.close()
            raise ValueError("SSH socket path is too long; set TMPDIR=/tmp before running")
        # Authenticate in the user's terminal first. OpenSSH owns all password,
        # key-passphrase and host-key prompts; the script never reads secrets.
        command = self.ssh_args() + ["-M", "-N", "-f", "-o", "ControlMaster=yes",
                                    "-o", "ControlPersist=60", "-o", "PermitLocalCommand=no",
                                    "-o", "StrictHostKeyChecking=" + ("yes" if self.batch else "ask"),
                                    "-o", "BatchMode=" + ("yes" if self.batch else "no"), "--", self.host]
        try:
            print(f"Connecting to {self.user}@{self.host}; SSH will handle authentication...", file=sys.stderr)
            result = subprocess.run(command, timeout=self.auth_timeout, check=False, shell=False)
            if result.returncode:
                raise ReadError("SSH connection failed. See the SSH message above for authentication, host-key or network details")
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.close()
            raise ReadError(f"SSH connection failed: {exc}") from None
        except BaseException:
            self.close()
            raise

    def run_pvesh(self, command):
        if not self.control_path:
            raise ReadError("SSH session is not connected")
        # get() already checked the endpoint. Remote shells still need quoting,
        # even though subprocess itself uses shell=False locally.
        if command[:2] != ["pvesh", "get"]:
            raise ValueError("SSH mode accepts only pvesh get commands")
        remote = (["sudo", "-n", "--"] if self.sudo else []) + command
        return subprocess.run(self.ssh_args() + ["-o", "ControlMaster=no", "-o", "BatchMode=yes",
                              "-o", "StrictHostKeyChecking=yes", "-o", "PermitLocalCommand=no",
                              "--", self.host, shlex.join(remote)], capture_output=True, text=True,
                              timeout=self.timeout, check=False, shell=False, stdin=subprocess.DEVNULL)

    def backend_command(self, command):
        if not approved_backend_command(command):
            raise ValueError("Not an approved backend read command")
        if not self.control_path:
            raise ReadError("SSH session is not connected")
        remote = (["sudo", "-n", "--"] if self.sudo else []) + command
        try:
            result = subprocess.run(self.ssh_args() + ["-o", "ControlMaster=no", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "PermitLocalCommand=no", "--", self.host,
                shlex.join(remote)], capture_output=True, text=True, timeout=self.timeout, check=False,
                shell=False, stdin=subprocess.DEVNULL)
            if result.returncode:
                raise ReadError(result.stderr.strip()[:600] or "Backend read failed")
            return result.stdout
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReadError(str(exc)[:600]) from None

    def close(self):
        if self.control_path:
            try:
                subprocess.run(self.ssh_args() + ["-O", "exit", "--", self.host],
                               capture_output=True, timeout=5, check=False, shell=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.control_path = None
        if self.directory:
            self.directory.cleanup()
            self.directory = None


def ssh_settings(args, setting):
    interactive = not args.no_prompt and sys.stdin.isatty()
    host = args.ssh_host or setting("ssh_host")
    user = args.ssh_user or setting("ssh_user") or "root"
    port = args.ssh_port or setting("ssh_port")
    identity = args.ssh_key or setting("ssh_key")
    authentication = getattr(args, "ssh_auth", None) or setting("ssh_auth") or ("key" if identity else "auto")
    if interactive:
        if authentication == "auto":
            print("Choose how to sign in. Password uses your PVE SSH account password; key uses an existing SSH key file.", file=sys.stderr)
            authentication = prompt_choice("SSH sign-in method", ("password", "key", "auto"), "password")
        if not host:
            host = input("PVE SSH hostname or IP address: ").strip()
        if not args.ssh_user and not setting("ssh_user"):
            user = input(f"SSH username [{user}]: ").strip() or user
        if authentication == "key" and not identity:
            identity = input("SSH key file on this computer [Enter to use your existing default SSH keys]: ").strip() or None
        if authentication == "password":
            print("SSH will ask for your account password when it connects. Nothing is displayed while you type it.", file=sys.stderr)
    if not host:
        raise ValueError("SSH mode requires --ssh-host, PVE_SSH_HOST, a config ssh_host, or an interactive hostname prompt")
    return dict(host=host, user=user, port=int(port) if port is not None else None, identity=identity,
                timeout=args.timeout, auth_timeout=args.ssh_auth_timeout, sudo=args.ssh_sudo,
                batch=args.no_prompt, authentication=authentication)


def api_settings(args, setting):
    values = {key: setting(key) for key in ("url", "token_id", "token_secret", "ca_file")}
    if not args.no_prompt and sys.stdin.isatty():
        print("API login uses a token created in PVE: Datacenter → Permissions → API Tokens. It does not use your SSH password.", file=sys.stderr)
        if not values["url"]:
            host = input("PVE API hostname or IP address (or full HTTPS URL): ").strip()
            values["url"] = host if host.startswith("https://") else "https://" + host + ("" if ":" in host else ":8006")
        if not values["token_id"]:
            values["token_id"] = input("API Token ID (e.g. inventory@pve!report): ").strip()
        if not values["token_secret"]:
            values["token_secret"] = getpass.getpass("API Token Secret (paste the Value shown when the token was created; input is hidden): ")
        if not values["ca_file"]:
            print("HTTPS certificates are checked. For a private PVE CA, supply its trusted CA PEM file on this computer.", file=sys.stderr)
            values["ca_file"] = input("Trusted CA file path [Enter if the certificate is already trusted]: ").strip() or None
    return values


class PBSClient:
    """GET-only PBS API token client. Separate credentials and authorization scheme."""
    def __init__(self, url, token_id=None, token_secret=None, ca_file=None, timeout=20,
                 auth='token', username=None, password=None, interactive=False, ssh_options=None):
        parsed = parse.urlsplit(url or "")
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path.rstrip("/") not in ("", "/api2/json")):
            raise ValueError("PBS URL must be https://hostname:8007 without credentials or query parameters")
        if auth == 'token' and (not token_id or not token_secret):
            raise ValueError("PBS reporting requires a PBS Token ID and Secret, separate from the PVE token")
        self.base, self.timeout, self.secret = "https://" + parsed.netloc + "/api2/json", timeout, token_secret or password or '[unset]'
        self.authorization = f"PBSAPIToken={token_id}:{token_secret}"
        context = ssl.create_default_context(cafile=str(Path(ca_file).expanduser()) if ca_file else None)
        self.opener = request.build_opener(NoRedirect(), request.HTTPSHandler(context=context))
        self.ssh, self.cookie = None, None
        if auth == 'ssh':
            self.ssh = SSHClient(**ssh_options)
            self.ssh.connect()
        elif auth == 'password':
            if not username or '@' not in username or not password:
                raise ValueError('PBS password login requires username@realm and a password')
            def login(fields):
                try:
                    req = request.Request(self.base + '/access/ticket', data=parse.urlencode(fields).encode(), method='POST')
                    with self.opener.open(req, timeout=self.timeout) as response:
                        return json.load(response)['data']
                except (OSError, ValueError, KeyError):
                    raise ReadError('PBS password login failed. Check credentials, certificate trust and authentication requirements') from None
            data = login(dict(username=username, password=password))
            if data.get('NeedTFA') or str(data.get('ticket', '')).startswith('PBS:!tfa!'):
                if not interactive:
                    raise ReadError('PBS requires interactive two-factor authentication; use an API token for scheduled reports')
                code = getpass.getpass('PBS authenticator code (TOTP; hidden): ')
                data = login(dict(username=username, password='totp:' + code, **{'tfa-challenge': data['ticket']}))
            ticket = data.get('ticket')
            if not ticket or data.get('NeedTFA'):
                raise ReadError('PBS did not return a usable session. Use an API token for browser/security-key login accounts')
            self.cookie = 'PBSAuthCookie=' + ticket
            self.renew = lambda: login(dict(username=username, password=self.cookie.split('=', 1)[1]))
            self.ticket_time = time.monotonic()

    def close(self):
        if self.ssh:
            self.ssh.close()
        self.cookie = None
        self.renew = None

    def get(self, path, **params):
        paths = (r"/version", r"/admin/datastore", r"/config/datastore/[^/]+", r"/config/prune",
                 r"/admin/sync", r"/config/sync", r"/admin/prune", r"/config/verify", r"/admin/verify", r"/nodes/localhost/tasks",
                 r"/admin/datastore/[^/]+/(?:status|namespace|groups|snapshots|gc)")
        if not any(re.fullmatch(pattern, path) for pattern in paths):
            raise ValueError("Not an approved PBS inventory GET path: " + path)
        allowed = ({'store', 'start', 'limit', 'since'} if path == '/nodes/localhost/tasks' else
                   {'max-depth', 'ns'} if path.endswith('/namespace') else
                   {'ns'} if path.endswith(('/groups', '/snapshots')) else set())
        if set(params) - allowed:
            raise ValueError('Not an approved PBS inventory query parameter')
        if self.ssh:
            command = ['env', 'PROXMOX_DEBUG_API_CODE=0', 'proxmox-backup-debug', 'api', 'get', path, '--output-format', 'json']
            for key, value in params.items():
                if not re.fullmatch(r'[a-z][a-z0-9-]*', key):
                    raise ValueError('Invalid PBS query parameter')
                command += ['--' + key, str(value).lower() if isinstance(value, bool) else str(value)]
            if self.ssh.sudo:
                command = ['sudo', '-n', '--'] + command
            try:
                result = subprocess.run(self.ssh.ssh_args() + ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'PermitLocalCommand=no', '--', self.ssh.host, shlex.join(command)], capture_output=True, text=True, timeout=self.timeout, stdin=subprocess.DEVNULL)
                if result.returncode:
                    raise ReadError('PBS SSH read failed; check root/restricted sudo access and proxmox-backup-debug availability')
                return json.loads(result.stdout)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                raise ReadError('PBS SSH query failed or timed out') from None
        try:
            if self.cookie and time.monotonic() - self.ticket_time > 3600:
                renewed = self.renew()
                if not renewed.get('ticket') or renewed.get('NeedTFA'):
                    raise ReadError('PBS session renewal failed; rerun interactively or use an API token')
                self.cookie = 'PBSAuthCookie=' + renewed['ticket']
                self.ticket_time = time.monotonic()
            url = self.base + path + ("?" + parse.urlencode(params) if params else "")
            req = request.Request(url, headers={"Cookie": self.cookie} if self.cookie else {"Authorization": self.authorization}, method="GET")
            with self.opener.open(req, timeout=self.timeout) as response:
                data = json.load(response)
                if not isinstance(data, dict) or "data" not in data:
                    raise ReadError("PBS returned an unrecognized response")
                return data["data"]
        except error.HTTPError as exc:
            raise ReadError(f"PBS HTTP {exc.code}: insufficient permission, unavailable endpoint or rejected request") from None
        except (OSError, ValueError) as exc:
            raise ReadError(str(exc).replace(self.secret, "[REDACTED]")[:600]) from None


def pbs_settings(args, config):
    def setting(key):
        explicit = getattr(args, 'pbs_' + key, None)
        return explicit if explicit is not None else os.environ.get("PBS_" + key.upper(), config.get("pbs_" + key))
    interactive = not args.no_prompt and sys.stdin.isatty()
    requested = args.pbs
    if requested is None:
        requested = False
        if interactive:
            requested = prompt_choice("Optional PBS backup-server report", ("include", "skip"), "skip") == "include"
    if not requested:
        return None
    values = dict(url=args.pbs_url or setting("url"), token_id=setting("token_id"),
                  token_secret=setting("token_secret"), ca_file=setting("ca_file"), timeout=getattr(args, "pbs_timeout", 60))
    auth = getattr(args, 'pbs_auth', None) or setting('auth')
    if not auth:
        auth = 'token' if values['token_id'] else (prompt_choice('PBS connection: password = web account; token = API token; ssh = system login', ('password', 'token', 'ssh'), 'password') if interactive else 'token')
    if auth not in ('token', 'password', 'ssh'):
        raise ValueError('PBS auth must be token, password or ssh')
    if auth != 'token':
        values.update(auth=auth, interactive=interactive)
        if auth == 'ssh':
            method = setting('ssh_auth') or (prompt_choice('PBS SSH sign-in method', ('password', 'key', 'auto'), 'password') if interactive else 'key')
            host = setting('ssh_host') or (input('PBS SSH hostname or IP: ').strip() if interactive else None)
            user = setting('ssh_user') or (input('PBS SSH system username [root]: ').strip() or 'root' if interactive else 'root')
            identity = setting('ssh_key')
            if method == 'key' and interactive and not identity:
                identity = input('PBS SSH private key file [Enter for configured keys/agent]: ').strip() or None
            values['ssh_options'] = dict(host=host, user=user, identity=identity, port=setting('ssh_port'), authentication=method, batch=not interactive, timeout=values['timeout'], sudo=bool(config.get('pbs_ssh_sudo', False)))
            if not values['url'] and host:
                values['url'] = 'https://' + ('[' + host + ']' if ':' in host else host) + ':8007'
        else:
            values['username'] = setting('username') or (input('PBS web account including realm (e.g. inventory@pbs): ').strip() if interactive else None)
            values['password'] = setting('password') or (getpass.getpass('PBS web-account password (hidden): ') if interactive else None)
        if not values['url'] and interactive:
            host = input('PBS HTTPS hostname/IP or URL: ').strip()
            values['url'] = host if host.startswith('https://') else 'https://' + ('[' + host + ']' if ':' in host else host) + ':8007'
        return values
    if interactive:
        print("PBS is a separate backup server. Use a read-only API token created in its web interface; no backups will be changed.", file=sys.stderr)
        if not values["url"]:
            host = input("PBS hostname or IP address (or full HTTPS URL): ").strip()
            values["url"] = host if host.startswith("https://") else "https://" + host + ("" if ":" in host else ":8007")
        if not values["token_id"]:
            values["token_id"] = input("PBS API Token ID (e.g. inventory@pbs!report): ").strip()
        if not values["token_secret"]:
            values["token_secret"] = getpass.getpass("PBS API Token Secret (paste the token Value; hidden input): ")
        if not values["ca_file"]:
            values["ca_file"] = input("Trusted PBS CA certificate file [Enter if already trusted by this computer]: ").strip() or None
    return values


def verification_state(value):
    if not isinstance(value, dict):
        return "unavailable"
    state = value.get('state')
    return {'ok': 'passed', 'failed': 'failed', 'none': 'not verified'}.get(state, 'unavailable')


def pbs_relationships(report, instance, overrides):
    pbs=report['pbs']
    hostname=parse.urlsplit(instance).hostname
    destinations=[d for d in report.get('pbs_destinations',[]) if d.get('server')==hostname]
    vmids={str(v['vmid']):v for v in report['vms']}
    for group in pbs['groups']:
        group['matching_pve']='PVE matching not requested' if report.get('scope',{}).get('pbs_only') else 'No confirmed match in this PVE inventory'
        for rule in overrides:
            if not isinstance(rule,dict) or not isinstance(rule.get('hours'),(float,int)) or not 0 < rule['hours'] < float('inf'):
                raise ValueError('pbs_freshness_overrides entries need positive finite hours')
            if all(rule.get(k,group[k])==group[k] for k in ('store','namespace','backup_type','backup_id')):
                group['freshness']='unavailable' if group['age_hours'] is None else 'older than expected' if group['age_hours']>rule['hours'] else 'within expectation'
        if group['backup_type']=='vm' and group['backup_id'] in vmids and any(d['datastore']==group['store'] and (d.get('namespace') or '')==group['namespace'] for d in destinations):
            group['matching_pve']='Destination and VM ID match current inventory; cluster ownership remains unconfirmed'
        group['review_reasons']=[]
        if group['freshness']=='older than expected':
            group['review_reasons'].append('Latest visible backup is older than the configured expectation')
        if group['verification_state']=='failed':
            group['review_reasons'].append('Latest snapshot has a recorded verification failure')
    pbs['backup_coverage']=[]
    jobs={str(j['id']):j for j in report.get('jobs') or []}
    for vm in report['vms']:
        for jobid in vm['enabled_jobs']:
            job=jobs.get(jobid,{})
            for dest in destinations:
                if job.get('storage') != dest['storage']:
                    continue
                store=next((d for d in pbs['datastores'] if d['store']==dest['datastore']),None)
                namespace=dest.get('namespace') or ''
                checked=bool(store and store['inventory_complete'] and namespace in store['namespaces'])
                found=any(g['store']==dest['datastore'] and g['namespace']==namespace and g['backup_type']=='vm' and g['backup_id']==str(vm['vmid']) for g in pbs['groups'])
                pbs['backup_coverage'].append(dict(vmid=vm['vmid'], job=jobid, datastore=dest['datastore'], namespace=namespace,
                    status='Matching group visible; ownership unconfirmed' if found else 'No matching group visible in checked namespace; permissions may hide backups' if checked else 'Destination coverage unavailable'))


def pbs_history(report, root, instance):
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / 'pbs-history.sqlite3', timeout=10)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS readings (run TEXT, scope TEXT, stamp TEXT, used INTEGER, available INTEGER, PRIMARY KEY(run,scope))')
        for store in report['pbs']['datastores']:
            scope = json.dumps([instance,store['store'],store.get('backend_type'),store.get('backing_scope'),store['total_bytes']])
            if store['used_bytes'] is not None and store['available_bytes'] is not None:
                db.execute('INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?)', (report['run']['run_id'],scope,report['generated_at'],store['used_bytes'],store['available_bytes']))
            rows = db.execute('SELECT stamp,used,available FROM readings WHERE scope=? ORDER BY stamp DESC LIMIT 90',(scope,)).fetchall()[::-1]
            store['capacity_history'] = [dict(time=r[0],used_bytes=r[1],available_bytes=r[2]) for r in rows]
            store['change_since_previous_bytes'] = rows[-1][1]-rows[-2][1] if len(rows)>1 and rows[-1][0]==report['generated_at'] else None

        db.commit()

class PBSCollector:
    def __init__(self, client, stores=None, namespaces=None, task_days=14, task_limit=1000, fresh_hours=None):
        self.client, self.stores, self.warnings = client, stores, []
        self.selected_namespaces = namespaces
        self.task_days, self.task_limit, self.fresh_hours = task_days, task_limit, fresh_hours
        self.checks = []

    def read(self, path, expected=None, **params):
        try:
            result = self.client.get(path, **params)
            if expected and not isinstance(result, expected):
                raise ReadError("Unexpected response format")
            if expected is list and any(not isinstance(item, dict) for item in result):
                raise ReadError("Unexpected non-object record in PBS response")
            self.checks.append(dict(path=path, status="available"))
            return result
        except ReadError as exc:
            detail = str(exc)
            state = ("permission denied" if "403" in detail else "authentication failed" if "401" in detail else
                     "unsupported or unavailable endpoint" if "404" in detail else "timed out" if "timed out" in detail or "timeout" in detail.lower() else "unavailable")
            self.checks.append(dict(path=path, status=state))
            self.warnings.append(f"GET {path}: {exc}")
            return None

    def collect(self):
        version = self.read("/version", dict)
        visible = self.read("/admin/datastore", list)
        prune_config = self.read("/config/prune", list)
        prune_status = self.read("/admin/prune", list)
        verify_config = self.read("/config/verify", list)
        verify_status = self.read("/admin/verify", list)
        sync_jobs = self.read("/admin/sync", list)
        names = self.stores if self.stores else [s["store"] for s in visible or [] if isinstance(s, dict) and s.get("store")]
        results, all_groups, all_snapshots = [], [], []
        for store in dict.fromkeys(names):
            print(f"Reading PBS datastore {store}...", file=sys.stderr)
            base = "/admin/datastore/" + segment(store)
            capacity = self.read(base + "/status", dict)
            config = self.read("/config/datastore/" + segment(store), dict)
            gc = self.read(base + "/gc", dict)
            tasks, task_complete = [], False
            since = int(time.time() - self.task_days * 86400)
            while len(tasks) < self.task_limit:
                batch = self.read("/nodes/localhost/tasks", list, store=store, start=len(tasks), limit=min(100, self.task_limit-len(tasks)), since=since)
                if batch is None:
                    break
                remaining = min(100, self.task_limit-len(tasks))
                tasks.extend(batch[:remaining])
                if len(batch) < remaining:
                    task_complete = True
                    break
            if not task_complete:
                self.warnings.append(f"PBS {store}: task history limited or unavailable; no absence-of-cleanup conclusion is possible")
            namespaces = self.read(base + "/namespace", list, **{"max-depth": 7})
            scope = {""}
            for ns in namespaces or []:
                if isinstance(ns, dict) and isinstance(ns.get("ns"), str):
                    scope.add(ns["ns"])
            group_count, snapshot_count = 0, 0
            any_groups_read, any_snapshots_read = False, False
            inventory_complete = namespaces is not None
            if namespaces is not None and any(not isinstance(ns.get("ns"), str) for ns in namespaces):
                inventory_complete = False
                self.warnings.append(f"PBS {store}: unrecognized namespace records; inventory scope is incomplete")
            if self.selected_namespaces is not None:
                requested = {"" if n == "/" else n for n in self.selected_namespaces}
                for missing in requested - scope:
                    self.warnings.append(f"PBS {store}: namespace {missing or '/'} not visible")
                scope &= requested
            for ns in sorted(scope):
                params = {"ns": ns} if ns else {}
                groups = self.read(base + "/groups", list, **params)
                snapshots = self.read(base + "/snapshots", list, **params)
                any_groups_read = any_groups_read or groups is not None or snapshots is not None
                any_snapshots_read = any_snapshots_read or snapshots is not None
                inventory_complete = inventory_complete and groups is not None and snapshots is not None
                snap_map = {}
                for snap in snapshots or []:
                    if not isinstance(snap, dict) or not all(key in snap for key in ("backup-type", "backup-id", "backup-time")):
                        self.warnings.append(f"PBS {store}/{ns}: malformed snapshot record was skipped")
                        inventory_complete = False
                        continue
                    key = (str(snap["backup-type"]), str(snap["backup-id"]))
                    snapshot_time = number(snap.get("backup-time"))
                    if snapshot_time is None:
                        inventory_complete = False
                        continue
                    normalized = dict(store=store, namespace=ns, backup_type=key[0], backup_id=key[1],
                                      backup_time=snapshot_time, logical_bytes=number(snap.get("size")),
                                      protected=snap.get("protected"), verification=snap.get("verification"))
                    if snapshot_time not in snap_map.setdefault(key, {}):
                        snap_map[key][snapshot_time] = normalized
                        all_snapshots.append(normalized)
                        snapshot_count += 1
                group_map = {(str(g["backup-type"]), str(g["backup-id"])): g for g in groups or []
                             if isinstance(g, dict) and "backup-type" in g and "backup-id" in g}
                if groups is not None and len(group_map) != len(groups):
                    inventory_complete = False
                    self.warnings.append(f"PBS {store}/{ns}: malformed or duplicate group records were skipped")
                for key in sorted(set(group_map) | set(snap_map)):
                    group, snaps = group_map.get(key, {}), list(snap_map.get(key, {}).values())
                    latest = max(snaps, key=lambda s: s["backup_time"]) if snaps else None
                    count = number(group.get("backup-count"))
                    if count is None and snapshots is not None:
                        count = len(snaps)
                    all_groups.append(dict(store=store, namespace=ns, backup_type=key[0], backup_id=key[1],
                        snapshot_count=count, count_source="PBS group backup-count" if number(group.get("backup-count")) is not None else "listed snapshots" if snapshots is not None else "unavailable",
                        latest_backup_time=latest["backup_time"] if latest else number(group.get("last-backup")),
                        latest_logical_bytes=latest["logical_bytes"] if latest else None,
                        oldest_backup_time=min((s['backup_time'] for s in snaps), default=None),
                        protected_count=sum(s['protected'] is True or s['protected'] == 1 for s in snaps) if snapshots is not None and all(s['protected'] is not None for s in snaps) else None,
                        age_hours=max(0, (time.time()-latest['backup_time'])/3600) if latest else None,
                        freshness=("older than expected" if latest and self.fresh_hours is not None and time.time()-latest['backup_time'] > self.fresh_hours*3600 else "within expectation" if latest and self.fresh_hours is not None else "no expectation configured"),
                        verification_state=verification_state(latest.get('verification') if latest else None),
                        older_verification_failures=sum(verification_state(s.get('verification')) == 'failed' for s in snaps if s is not latest),
                        matching_pve="Not confirmed; VM ID alone is insufficient"))
                    group_count += 1
            for group in [g for g in all_groups if g['store'] == store]:
                policies=[]
                for job in prune_config or []:
                    parent=job.get('ns','')
                    namespace=group['namespace']
                    if job.get('store') != store or not (namespace==parent or namespace.startswith(parent+'/') or not parent):
                        continue
                    depth=len(namespace.split('/'))-len(parent.split('/')) if parent else len(namespace.split('/')) if namespace else 0
                    if job.get('max-depth') is not None and depth > int(job['max-depth']):
                        continue
                    policies.append(dict(id=job.get('id'), disabled=job.get('disable',False), namespace=parent,
                        max_depth=job.get('max-depth'), schedule=job.get('schedule'),
                        rules={k:v for k,v in job.items() if k.startswith('keep-')}))
                group['pbs_prune_policies'] = policies if prune_config is not None else None
            safe_config = {key: value for key, value in (config or {}).items()
                           if key.startswith("keep-") or key in ("gc-schedule", "prune-schedule")}
            recent_cleanup = [task for task in tasks or [] if isinstance(task, dict) and str(task.get("worker_type", task.get("worker-type", ""))).replace("-", "_") in ("prune", "prunejob", "garbage_collection")]
            backend = (config or {}).get('backend', {'type': 'local'})
            backend_type = backend.get('type', 'unknown') if isinstance(backend, dict) else str(backend)
            local_capacity = config is not None and backend_type == 'local'
            if not local_capacity:
                self.warnings.append(f"PBS {store}: backend capacity semantics not validated or configuration unreadable; full capacity omitted")
            latest_cleanup = {}
            for kind in ('prune', 'prunejob', 'garbage_collection'):
                matching = [t for t in recent_cleanup if str(t.get('worker_type',t.get('worker-type',''))).replace('-','_') == kind]
                matching.sort(key=lambda t:number(t.get('starttime')) or number(t.get('endtime')) or 0, reverse=True)
                latest_cleanup[kind] = dict(latest_attempt=matching[0] if matching else None,
                    latest_success=next((t for t in matching if t.get('status') == 'OK' and t.get('endtime') is not None), None))
            if not local_capacity:
                capacity = None
            results.append(dict(backup_attempts=[t for t in tasks or [] if t.get("worker_type", t.get("worker-type")) == "backup"], sync_jobs=[j for j in sync_jobs or [] if j.get("store") == store] if sync_jobs is not None else None, backend_type=backend_type, backing_scope=(config or {}).get('path'),
                task_history=dict(since=since, records=len(tasks), complete_within_visible_scope=task_complete, limit=self.task_limit),
                cleanup_history=latest_cleanup,
                verification_jobs=[j for j in verify_status or [] if j.get("store")==store] if verify_status is not None else None,
                verification_config=[j for j in verify_config or [] if j.get("store")==store] if verify_config is not None else None,
                store=store, total_bytes=number((capacity or {}).get("total")),
                used_bytes=number((capacity or {}).get("used")), available_bytes=number((capacity or {}).get("avail")),
                capacity_scope="Underlying filesystem reported by PBS; may include other data or be shared with another datastore. Do not sum datastore capacities.",
                namespaces=sorted(scope), namespace_enumeration_available=namespaces is not None,
                inventory_complete=inventory_complete, visible_group_count=group_count if any_groups_read else None,
                listed_snapshot_count=snapshot_count if any_snapshots_read else None, datastore_retention=safe_config if config is not None else None,
                prune_config=[j for j in prune_config or [] if j.get("store") == store] if prune_config is not None else None,
                prune_status=[j for j in prune_status or [] if j.get("store") == store] if prune_status is not None else None,
                gc=gc, recent_cleanup_tasks=recent_cleanup if tasks is not None else None))
        return dict(requested=True, status="collected" if results and not self.warnings else "partial" if results else "unavailable",
                    version=version, checks=self.checks, datastores=results, groups=all_groups, snapshots=all_snapshots, warnings=self.warnings,
                    scope_note="Visible PBS datastores/namespaces only. Permissions can hide objects. VM IDs can be reused by other clusters; a matching ID is not proof of ownership. Snapshot archive sizes are logical referenced data, not unique physical PBS usage.")


def key_value_output(text):
    """Top-level qm config/status properties only; skip nested verbose blocks."""
    return dict(match.groups() for line in text.splitlines()
                if (match := re.match(r"^([^\s:#]+):\s*(.*)$", line)))


def cluster_ssh_prefix():
    return ["ssh", "-T", "-a", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "--"]


def approved_backend_command(command):
    if (len(command) == 7 and command[:3] == ['findmnt', '--json', '--target']
            and command[3].startswith('/') and command[4:] == ['--output', 'SOURCE,TARGET,FSTYPE,UUID', '--first-only']):
        return True
    if len(command) == 16 and command[:1] == ['ssh']:
        node = command[10].removeprefix('HostKeyAlias=')
        address = command[14].removeprefix('root@')
        try:
            ipaddress.ip_address(address)
            return (re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', node) is not None
                    and command[:-1] == routed_ssh_prefix(node, address)
                    and shlex.split(command[-1])[:1] != ['ssh']
                    and approved_backend_command(shlex.split(command[-1])))
        except ValueError:
            return False

    if (len(command) == 3 and command[:2] == ['pvesm', 'path']
            and re.fullmatch(r'[A-Za-z0-9_.-]+:[A-Za-z0-9_./-]+', command[2])):
        return True
    if (len(command) == 5 and command[:4] == ['stat', '-L', '--printf=%f|%b|%B', '--']
            and command[4].startswith('/') and '\n' not in command[4]):
        return True
    if (len(command) == 12 and command[:10] == cluster_ssh_prefix()
            and re.fullmatch(r'root@[A-Za-z0-9][A-Za-z0-9_.-]*', command[10])):
        nested = shlex.split(command[11])
        return nested[:1] != ['ssh'] and approved_backend_command(nested)
    reads = (["hostname", "-s"], ["dmsetup", "status", "--target", "thin", "--noflush"])
    return command in reads or (len(command) == 12 and command[:10] == cluster_ssh_prefix()
        and re.fullmatch(r"root@[A-Za-z0-9][A-Za-z0-9_.-]*", command[10]) is not None
        and command[11] == shlex.join(reads[1]))


def parse_thin_status(output):
    """Exact mapped sectors from active thin targets. Never derive bytes from rounded Data%."""
    result = ThinCounters()
    for line in output.splitlines():
        match = re.fullmatch(r"([^:]+):\s+0\s+\d+\s+thin\s+(\d+)\s+\S+\s*", line.strip())
        if match:
            name, sectors = match.groups()
            value = int(sectors) * 512
            if name in result:  # ambiguous/multiple segments are not guessed
                result[name] = None
            else:
                result[name] = value
        elif ':' in line:
            name, detail = line.split(':', 1)
            result.errors[name.strip()] = ('Storage target reported failure' if 'Fail' in detail else
                                           'Storage target response was not understood')
    return result


class ThinCounters(dict):
    def __init__(self):
        super().__init__()
        self.errors = {}


def node_command(client, node, command):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', node):
        raise ReadError('Invalid cluster node name')
    if not hasattr(client, 'entry_node'):
        client.entry_node = client.backend_command(['hostname', '-s']).strip()
    if node == client.entry_node:
        return client.backend_command(command)
    if not hasattr(client, 'node_routes'):
        client.node_routes = {}
    if node not in client.node_routes:
        route = dict(source=client.entry_node, node=node, address=None, verified=False, error=None, connection_timeout_seconds=10, port_source='OpenSSH configuration/default (normally 22)')
        client.node_routes[node] = route
        try:
            if not hasattr(client, 'cluster_addresses'):
                client.cluster_addresses = {}
                try:
                    entries = client.get('/cluster/status')
                    client.cluster_addresses = {entry['name']: entry.get('ip') for entry in entries
                                                if entry.get('type') == 'node'}
                    client.address_error = None
                except ReadError as exc:
                    client.address_error = str(exc)
            address = getattr(client, 'node_address_overrides', {}).get(node) or client.cluster_addresses.get(node)
            if not address:
                raise ReadError('Cluster IP unavailable: ' + (getattr(client, 'address_error', None) or node))
            route['address'] = str(ipaddress.ip_address(address))
            prefix = routed_ssh_prefix(node, route['address'])
            actual = client.backend_command(prefix + [shlex.join(['hostname', '-s'])]).strip()
            if actual != node:
                raise ReadError(f'Node identity mismatch: expected {node}, received {actual}')
            route['verified'] = True
        except (ReadError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            route['error'] = str(exc)
    route = client.node_routes[node]
    if route['error']:
        raise ReadError(f"Storage connection {route['source']} -> {node} ({route['address']}): {route['error']}")
    return client.backend_command(routed_ssh_prefix(node, route['address']) + [shlex.join(command)])


def routed_ssh_prefix(node, address):
    return ['ssh', '-T', '-a', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=10', '-o', 'HostKeyAlias=' + node,
            '-o', 'UserKnownHostsFile=/etc/pve/nodes/' + node + '/ssh_known_hosts /root/.ssh/known_hosts',
            '--', 'root@' + address]


def read_thin_status(client, node):
    return parse_thin_status(node_command(client, node, ['dmsetup', 'status', '--target', 'thin', '--noflush']))


def raw_file_allocation(client, node, volume):
    """Read allocation of one raw regular file on its owning host, without opening its image."""
    def run(command):
        return node_command(client, node, command)
    path = run(['pvesm', 'path', volume]).strip()
    if not path.startswith('/') or '\n' in path:
        raise ReadError('Storage returned an invalid file path')
    fields = run(['stat', '-L', '--printf=%f|%b|%B', '--', path]).strip().split('|')
    if len(fields) != 3 or not re.fullmatch(r'[0-9a-fA-F]+', fields[0]) or not stat.S_ISREG(int(fields[0], 16)):
        raise ReadError('Allocation path is not a regular file')
    if not all(value.isdigit() for value in fields[1:]):
        raise ReadError('File allocation counters were not understood')
    return int(fields[1]) * int(fields[2])


def parse_qm_list(text, node):
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] == "VMID":
            continue
        if len(fields) < 6 or not fields[0].isdigit():
            raise ReadError("Unrecognized qm list output; refusing a partial VM list")
        rows.append(dict(vmid=int(fields[0]), name=fields[1], status=fields[2], node=node, type="qemu"))
    return rows


def storage_file_metadata(text):
    """Only keep non-secret identity fields from PVE's storage.cfg format."""
    result, current = {}, None
    for line in text.splitlines():
        section = re.match(r"^(\S+):\s+(\S+)\s*$", line)
        if section:
            kind, storage = section.groups()
            current = result.setdefault(storage, {"storage": storage, "type": kind})
        elif current is not None:
            option = re.match(r"^\s+(shared|path|pool|vgname|thinpool)\s+(.+?)\s*$", line)
            if option:
                current[option[1]] = option[2]
    return result


class CLIClient:
    """Standalone adapter: fixed read commands, with optional pvesh job queries."""
    mode = "cli"
    container_count_unknown = True

    def __init__(self, timeout=20):
        self.timeout = timeout
        self.node = socket.gethostname().split(".")[0]
        self.storages = {}
        self.pvesh = Client("local", timeout)

    def thin_status(self, node):
        return self.pvesh.thin_status(node)

    def run(self, command):
        safe = (command == ["qm", "list"] or command == ["pvesm", "status"] or
                command == ["pveversion"] or
                len(command) == 5 and command[:2] == ["qm", "config"] and command[2].isdigit() and command[3:] == ["--current", "1"] or
                len(command) == 5 and command[:2] == ["qm", "status"] and command[2].isdigit() and command[3:] == ["--verbose", "1"] or
                len(command) == 5 and command[:3] == ["qm", "guest", "cmd"] and command[3].isdigit() and command[4] == "get-fsinfo" or
                len(command) == 3 and command[:2] == ["pvesm", "path"] and not command[2].startswith("-"))
        # Separate exact property query so no arbitrary zfs commands are accepted.
        safe = safe or (len(command) == 8 and command[:7] == ["zfs", "get", "-H", "-p", "-o", "value", "usedbydataset"] and not command[7].startswith("-"))
        if not safe:
            raise ValueError("Not an approved read-only CLI command")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout,
                                    check=False, shell=False, env=dict(os.environ, LC_ALL="C"))
            if result.returncode:
                raise ReadError(f"{command[0]} {command[1] if len(command) > 1 else ''}: {result.stderr.strip()[:600]}")
            return result.stdout
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReadError(str(exc)[:600]) from None

    def get(self, path, **params):
        if path == "/version":
            return {"version": self.run(["pveversion"]).strip(), "source": "pveversion"}
        if path == "/nodes":
            return [{"node": self.node, "status": "online"}]
        if path == "/cluster/resources":
            return parse_qm_list(self.run(["qm", "list"]), self.node)
        if path == "/storage":
            output = self.run(["pvesm", "status"])
            try:
                metadata = storage_file_metadata(Path("/etc/pve/storage.cfg").read_text(encoding="utf-8"))
            except OSError:
                metadata = {}
            for line in output.splitlines():
                fields = line.split()
                if not fields or fields[0].lower() == "name":
                    continue
                if len(fields) < 3:
                    raise ReadError("Unrecognized pvesm status output")
                self.storages[fields[0]] = dict(metadata.get(fields[0], {}), storage=fields[0], type=fields[1])
            return list(self.storages.values())
        if path == "/cluster/backup" or re.fullmatch(r"/cluster/backup/[^/]+/included_volumes", path):
            # There is no equivalent qm/pvesm command for complete job selection.
            # Optional: unavailable pvesh yields N/A rather than guessed membership.
            return self.pvesh.get(path, **params)
        match = re.fullmatch(r"/nodes/([^/]+)/qemu/(\d+)/(config|status/current|agent/get-fsinfo)", path)
        if match:
            node, vmid, kind = match.groups()
            if parse.unquote(node) != self.node:
                raise ReadError("Standalone CLI mode cannot query another node")
            if kind == "config":
                return key_value_output(self.run(["qm", "config", vmid, "--current", "1"]))
            if kind == "status/current":
                return key_value_output(self.run(["qm", "status", vmid, "--verbose", "1"]))
            try:
                return json.loads(self.run(["qm", "guest", "cmd", vmid, "get-fsinfo"]))
            except ValueError:
                raise ReadError("qm guest cmd get-fsinfo returned no parseable JSON") from None
        match = re.fullmatch(r"/nodes/([^/]+)/storage/([^/]+)/content/([^/]+)", path)
        if match:
            node, storage, volume = map(parse.unquote, match.groups())
            if node != self.node:
                raise ReadError("Standalone CLI mode cannot query another node")
            kind = self.storages.get(storage, {}).get("type")
            if kind == "rbd":
                return self.pvesh.get(path)
            filename = self.run(["pvesm", "path", volume]).strip()
            try:
                if kind == "zfspool":
                    if not filename.startswith("/dev/zvol/"):
                        raise ReadError("ZFS volume path is not a zvol; allocation unavailable")
                    dataset = filename[len("/dev/zvol/"):]
                    used = number(self.run(["zfs", "get", "-H", "-p", "-o", "value", "usedbydataset", dataset]).strip())
                    if used is None:
                        raise ReadError("ZFS returned no numeric usedbydataset value")
                    return {"used": used, "_allocation_source": "zfs get usedbydataset (via pvesm path)"}
                if kind in ACTUAL_METHODS:
                    if not volume.endswith('.raw'):
                        if self.pvesh:
                            return self.pvesh.get(path, **params)
                        raise ReadError('Image may span multiple data files; primary-file allocation is not a complete image measurement')
                    info = os.stat(filename)
                    if not stat.S_ISREG(info.st_mode) or not hasattr(info, "st_blocks"):
                        raise ReadError("Not a regular file with allocated-block counters")
                    return {"used": info.st_blocks * 512,
                            "_allocation_source": "file stat st_blocks * 512 (via pvesm path)"}
            except OSError as exc:
                raise ReadError(str(exc)[:600]) from None
            raise ReadError("Backend allocation is not supported in CLI mode")
        raise ValueError(f"Not an approved standalone inventory path: {path}")


def detect_environment():
    if Path("/etc/pve/corosync.conf").exists():
        return "cluster"
    if Path("/etc/pve").is_dir():
        return "standalone"
    return None


def prompt_choice(prompt, choices, default):
    while True:
        try:
            answer = input(f"{prompt} ({'/'.join(choices)}) [{default}]: ").strip().lower()
        except EOFError:
            return default
        answer = answer or default
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            answer = choices[int(answer)-1]
        if answer in choices:
            return answer
        print("Choose one of: " + ", ".join(choices), file=sys.stderr)


def startup_options(args):
    detected = detect_environment() if args.mode not in ("api", "ssh") else None
    interactive = not args.no_prompt and sys.stdin.isatty()
    environment = args.environment
    mode = args.mode
    if mode is not None and interactive:
        print(f"Connection method: {mode} — selected by command-line option. Omit --mode for guided selection.", file=sys.stderr)
    if mode is None:
        default = "local" if shutil.which("pvesh") else "cli" if detected == "standalone" else "ssh"
        if interactive:
            print("How would you like to connect?\n"
                  "  1. ssh   — Log in to PVE using your account password or an SSH key (from WSL/Ubuntu).\n"
                  "  2. api   — Paste a Token ID and Secret created in the PVE web interface.\n"
                  "  3. local — Run on PVE itself using pvesh.\n"
                  "  4. cli   — Run on a standalone PVE host using qm/pvesm.", file=sys.stderr)
            mode = prompt_choice("Connection method", ("ssh", "api", "local", "cli"), default)
        else:
            mode = default
    if mode in ("ssh", "api"):
        detected = None  # Local cluster files say nothing about a remote target.
    if environment == "auto":
        if interactive:
            print("Read-only PVE inventory setup.", file=sys.stderr)
            if mode in ("local", "cli"):
                print(f"Local environment detection: {detected or 'unavailable'}", file=sys.stderr)
            environment = prompt_choice("Environment", ("cluster", "standalone"), detected or "cluster")
        else:
            environment = detected or "cluster"
    if detected == "cluster" and environment == "standalone":
        raise ValueError("Local cluster configuration was detected. Use --environment cluster to avoid an incomplete local-only inventory")
    if mode == "cli" and environment != "standalone":
        raise ValueError("Regular CLI mode is for standalone servers. Use --mode local or api to inventory all cluster nodes")
    return environment, mode


class Collector:
    def __init__(self, client, guest_agent=True, storage_timeout=60, vmids=None, selected_nodes=None):
        self.client = client
        self.vmids = set(vmids or [])
        self.selected_nodes = set(selected_nodes or [])
        self.storage_timeout = storage_timeout
        self.guest_agent = guest_agent
        self.warnings = []
        self.volume_cache = {}
        self.thin_cache = {}
        self.pve_major = None

    def read(self, path, **params):
        try:
            return self.client.get(path, **params), None
        except ReadError as exc:
            message = f"GET {path}: {exc}"
            self.warnings.append(message)
            return None, message

    def storage_read(self, operation, context):
        """Allow slow storage measurements a separate budget; never retry a timed-out remote process."""
        clients = [self.client] + ([self.client.pvesh] if hasattr(self.client, "pvesh") else [])
        previous = [(client, client.timeout) for client in clients if hasattr(client, "timeout")]
        for client, _ in previous:
            client.timeout = self.storage_timeout
        try:
            return operation()
        except (ReadError, OSError, subprocess.TimeoutExpired) as exc:
            raise ReadError(f"{context}; storage query budget {self.storage_timeout:g}s: {exc}") from None
        finally:
            for client, timeout in previous:
                client.timeout = timeout

    def volume(self, node, storage, volume, config):
        # Identity is known only when storage configuration is visible.
        if config is None:
            return None, None, "N/A: storage configuration unavailable; identity and backend unknown"
        shared = enabled(config.get("shared", 0)) or config.get("type") in SHARED_TYPES
        identity = ("shared" if shared else node, storage, volume)
        method = ACTUAL_METHODS.get(config.get("type"))
        if not method:
            return identity, None, f"N/A: backend {config.get('type', NA)} has no validated allocation metric in this version"
        if identity not in self.volume_cache:
            if (config.get('type') in ('dir', 'nfs', 'cifs', 'cephfs', 'glusterfs')
                    and volume.endswith('.raw') and getattr(self.client, 'mode', None) in ('local', 'ssh')):
                try:
                    used = self.storage_read(lambda: raw_file_allocation(self.client, node, volume),
                                             f'Node {node}, raw file {volume}')
                    self.volume_cache[identity] = (used, method + '; source regular-file stat allocated blocks; owning node ' + node)
                except (ReadError, OSError, subprocess.TimeoutExpired) as exc:
                    self.volume_cache[identity] = (None, 'N/A: ' + str(exc))
                return (identity,) + self.volume_cache[identity]
            if config.get("type") == "lvmthin":
                try:
                    if node not in self.thin_cache:
                        try:
                            self.thin_cache[node] = (self.storage_read(lambda: self.client.thin_status(node), f"Node {node}, LVM-thin counters"), None)
                        except (ReadError, AttributeError, OSError, subprocess.TimeoutExpired) as exc:
                            self.thin_cache[node] = ({}, str(exc))
                    mappings, failure = self.thin_cache[node]
                    vg, lv = config.get("vgname"), volume.split(":", 1)[1]
                    if not vg:
                        raise ReadError("LVM volume-group name was not available in storage settings")
                    name = vg.replace("-", "--") + "-" + lv.replace("-", "--")
                    used = mappings.get(name)
                    if used is None:
                        raise ReadError(failure or getattr(mappings, 'errors', {}).get(name) or "Thin volume is not active or has no exact mapped-sector counter; no volume was activated")
                    self.volume_cache[identity] = (used, method + "; source dmsetup status --target thin --noflush; mapped sectors × 512; shared blocks may be counted by multiple volumes")
                except (ReadError, ValueError, IndexError) as exc:
                    self.volume_cache[identity] = (None, "N/A: " + str(exc))
                return (identity,) + self.volume_cache[identity]
            if config.get("type") == "rbd" and (self.pve_major is None or self.pve_major < 8):
                return identity, None, "N/A: Ceph RBD API allocation semantics are validated for PVE 8+ only; version unavailable or older"
            path = f"/nodes/{segment(node)}/storage/{segment(storage)}/content/{segment(volume)}"
            try:
                info = self.storage_read(lambda: self.client.get(path), f"Node {node}, storage {storage}, volume {volume}, GET {path}")
                err = None
            except ReadError as exc:
                info, err = None, str(exc)
                self.warnings.append(err)
            used = number((info or {}).get("used"))
            source = (info or {}).get("_allocation_source", path + " used")
            reason = f"{method}; source {source}" if used is not None else f"N/A: {err or 'volume API did not return used bytes'}"
            self.volume_cache[identity] = (used, reason)
        return (identity,) + self.volume_cache[identity]

    def disks(self, node, config, storages):
        disks = []
        for slot, raw in config.items():
            if not DISK.fullmatch(slot):
                continue
            parts = str(raw).split(",")
            volume = parts[0].removeprefix("file=")
            options = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
            storage = volume.split(":", 1)[0] if ":" in volume else None
            excluded = ("unused disk" if slot.startswith("unused") else
                        "CD-ROM" if options.get("media") == "cdrom" or volume in ("none", "cdrom") else
                        "cloud-init media" if "cloudinit" in volume.lower() else None)
            capacity = size_bytes(options.get("size"))
            reason = "N/A: " + excluded if excluded else None
            identity, actual = None, None
            started = time.monotonic()
            cached_before = set(self.volume_cache)
            if not excluded:
                if storage:
                    identity, actual, reason = self.volume(node, storage, volume, storages.get(storage))
                else:
                    reason = "N/A: passthrough/unmanaged disk has no storage API allocation metric"
            disks.append(dict(slot=slot, volume=volume, storage=storage, capacity_bytes=capacity,
                              capacity_reason=("configuration size=" + options["size"] if capacity is not None else
                                               "N/A: " + (excluded or "no parseable configured size; volume size is not substituted")),
                              actual_bytes=actual, actual_reason=reason,
                              storage_backend=storages.get(storage, {}).get('type'),
                              storage_vg=storages.get(storage, {}).get('vgname'),
                              storage_shared=storages.get(storage, {}).get('shared'),
                              measurement_status=disk_outcome(actual, reason, excluded is not None),
                              measurement_seconds=round(time.monotonic() - started, 3),
                              cached_measurement=identity in cached_before if identity else False,
                              actual_method=ACTUAL_METHODS.get(storages.get(storage, {}).get("type")) if actual is not None else None,
                              identity=list(identity) if identity else None,
                              counted=excluded is None, excluded_reason=excluded,
                              backup_disk=(enabled(options.get("backup", 1)) if not excluded else None)))
        return sorted(disks, key=lambda item: item["slot"])

    def filesystems(self, path, status):
        if not self.guest_agent:
            return [], "N/A: guest-agent collection disabled with --no-guest-agent"
        if status != "running":
            return [], "N/A: VM is not confirmed running; guest agent was not queried"
        data, err = self.read(path + "/agent/get-fsinfo")
        if err:
            return [], "N/A: guest agent unavailable, unsupported or inaccessible; " + err
        entries = data.get("result") if isinstance(data, dict) else data
        if not isinstance(entries, list) or not entries:
            return [], "N/A: guest agent returned no filesystem list"
        result = []
        for entry in entries:
            used, total = number(entry.get("used-bytes")), number(entry.get("total-bytes"))
            valid = used is not None and total is not None and used <= total
            result.append(dict(mountpoint=entry.get("mountpoint"), name=entry.get("name"),
                               type=entry.get("type"), used_bytes=used, total_bytes=total,
                               free_bytes=total-used if valid else None,
                               reason="guest agent; free=total-used" if valid else "N/A: missing or inconsistent guest counters"))
        return result, "Per-filesystem counters only; overlapping mounts are not summed"

    def collect(self):
        version, _ = self.read("/version")
        match = re.search(r"(?:pve-manager/)?(\d+)\.", str((version or {}).get("version", "")))
        self.pve_major = int(match[1]) if match else None
        resources, err = self.read("/cluster/resources", type="vm")
        if err or not isinstance(resources, list):
            raise ReadError(err or "Cluster resources response was not a list; inventory cannot proceed")
        visible = [r for r in resources if r.get("type") == "qemu"]
        selected = [r for r in visible if (not self.vmids or r["vmid"] in self.vmids)
                    and (not self.selected_nodes or r["node"] in self.selected_nodes)]
        unmatched = self.vmids - {r["vmid"] for r in selected}
        unknown_nodes = self.selected_nodes - {r["node"] for r in selected}
        if unmatched or unknown_nodes:
            self.warnings.append(f"Selection not visible or does not match: VM IDs {sorted(unmatched)}, nodes {sorted(unknown_nodes)}")
        if (self.vmids or self.selected_nodes) and not selected:
            raise ReadError("No visible VMs match the selection; full inventory was not substituted")
        resources = selected + [r for r in resources if r.get("type") != "qemu"]
        nodes, _ = self.read("/nodes")
        address_rows, _ = (None, None) if getattr(self.client, "container_count_unknown", False) else self.read("/cluster/status")
        host_addresses = {item.get("name"): item.get("ip") for item in address_rows or [] if item.get("type") == "node"}
        node_status = {item["node"]: item.get("status") for item in nodes or []}
        storage_list, _ = self.read("/storage")
        storages = {item["storage"]: item for item in storage_list or []}
        jobs, jobs_err = self.read("/cluster/backup")
        job_members = {}
        for job in jobs or []:
            job_id = str(job["id"])
            data, member_err = self.read(f"/cluster/backup/{segment(job_id)}/included_volumes")
            # Use PVE's own selection logic (pools, all, exclusions, node restrictions).
            if not member_err and (not isinstance(data, dict) or not isinstance(data.get("children"), list)):
                member_err = f"GET /cluster/backup/{job_id}/included_volumes: unrecognized response; membership unknown"
                self.warnings.append(member_err)
            job_members[job_id] = (None if member_err else
                                   {str(child["id"]) for child in data["children"]})
        vms = []
        processed = 0
        for resource in resources:
            if resource.get("type") != "qemu":
                continue
            vmid, node = resource["vmid"], resource["node"]
            processed += 1
            tracker = getattr(self.client, "tracker", None)
            if tracker:
                tracker.message(f"VM {processed}/{len(selected)}: {vmid} on {node}")
            else:
                print(f"Reading VM {vmid} on {node}...", file=sys.stderr)
            path = f"/nodes/{segment(node)}/qemu/{vmid}"
            notes = []
            if node_status.get(node) == "offline":
                config, status = None, None
                notes.append("N/A: node reported offline; configuration, status and allocation queries skipped")
            else:
                config, conf_err = self.read(path + "/config", current=1)
                status, status_err = self.read(path + "/status/current")
                notes.extend(e for e in (conf_err, status_err) if e)
            config_ok = config is not None
            config, status = config or {}, status or {}
            disks = self.disks(node, config, storages) if config_ok else []
            counted = unique_disks(disks)
            capacity = complete_sum([d["capacity_bytes"] for d in counted]) if config_ok else None
            actual = complete_sum([d["actual_bytes"] for d in counted]) if config_ok else None
            # A diskless VM has zero configured capacity, but no measured allocation.
            if not counted:
                actual = None
            methods = {d["actual_method"] for d in counted if d["actual_method"]}
            if len(methods) > 1:
                actual = None
                notes.append("N/A actual total: disks have different allocation metric meanings")
            if any(d["counted"] and d["backup_disk"] is False for d in disks):
                notes.append("One or more attached disks have backup=0; VM job membership does not imply disk coverage")
            fs, fs_reason = self.filesystems(path, status.get("status"))
            match, disabled, unknown = [], [], bool(jobs_err)
            for job in jobs or []:
                members = job_members[str(job["id"])]
                active = enabled(job.get("enabled", 1))
                if members is None:
                    unknown = unknown or active
                elif str(vmid) in members:
                    (match if active else disabled).append(str(job["id"]))
            backup = "Yes" if match else NA if unknown else "No"
            memory = config.get("memory")
            if isinstance(memory, str) and "=" in memory:
                memory = dict(x.split("=", 1) for x in memory.split(",") if "=" in x).get("max")
            mem_mib = number(memory)
            if mem_mib is None and config_ok:
                mem_mib = 512  # Documented QEMU configuration default, not a measured value.
                notes.append("RAM allocation uses PVE's 512 MiB configuration default")
            cpu = number(config.get("vcpus"))
            if cpu is None and config_ok:
                cpu = (number(config.get("cores", 1)) or 1) * (number(config.get("sockets", 1)) or 1)
            if not config_ok or "tags" not in config:
                tags = resource.get("tags")
            else:
                tags = config["tags"]
            if tags is None:
                tags = "" if config_ok else None
            notes.extend([
                "N/A configured total: configuration or at least one disk size unavailable" if capacity is None else "",
                "N/A actual total: incomplete/unsupported allocation data or no disks" if actual is None else "",
                "N/A memory use: current status did not return mem" if number(status.get("mem")) is None else "",
                "N/A current VM status: status API unavailable or missing status; cached cluster status=" + display(resource.get("status")) if status.get("status") is None else "",
                "N/A backup membership: job selection endpoint unavailable or permission denied" if backup == NA else "",
                "N/A tags: config and resource tags unavailable" if tags is None else "",
            ])
            vms.append(dict(vmid=vmid, name=config.get("name", resource.get("name")), node=node,
                            status=status.get("status"), cached_status=resource.get("status"), tags=tags,
                            template=enabled(config.get("template", resource.get("template", 0))),
                            configured_bytes=capacity, actual_bytes=actual,
                            actual_method=next(iter(methods)) if len(methods) == 1 and actual is not None else None,
                            ram_bytes=mem_mib * 1024**2 if mem_mib is not None else None,
                            memory_used_bytes=number(status.get("mem")), vcpus=cpu,
                            backup=backup, enabled_jobs=match, disabled_jobs=disabled,
                            disks=disks, filesystems=fs, filesystem_reason=fs_reason,
                            config_available=config_ok, notes=[n for n in notes if n]))
        final_resources, final_error = self.read('/cluster/resources', type='vm')
        if final_error:
            self.warnings.append('Could not verify VM placement at collection end; measurements reflect earlier observations')
        else:
            final_nodes = {int(r['vmid']): r.get('node') for r in final_resources or [] if r.get('type') == 'qemu'}
            for vm in vms:
                if final_nodes.get(vm['vmid']) != vm['node']:
                    reason = 'VM moved or disappeared during collection; storage measurements withheld; rerun after migration completes'
                    vm['actual_bytes'] = None
                    vm['actual_method'] = None
                    vm['notes'].append(reason)
                    self.warnings.append(f"VM {vm['vmid']}: {reason}")
                    for disk in vm['disks']:
                        if disk['counted']:
                            disk.update(actual_bytes=None, actual_method=None, actual_reason=reason, measurement_status='placement changed')
        return dict(generated_at=datetime.now(timezone.utc).isoformat(), pve_version=version,
                    vms=vms, nodes=nodes, jobs=jobs, warnings=self.warnings,
                    hosts=[dict(node=n["node"], address=host_addresses.get(n["node"]), status=n.get("status"), details=self.read("/nodes/" + segment(n["node"]) + "/status")[0] if n.get("status") != "offline" else None, storage=self.read("/nodes/" + segment(n["node"]) + "/storage")[0] if n.get("status") != "offline" else None) for n in nodes or [] if (not self.selected_nodes or n["node"] in self.selected_nodes) and not getattr(self.client, "container_count_unknown", False)],
                    pbs_destinations=[{k:st.get(k) for k in ("storage","server","datastore","namespace")} for st in storages.values() if st.get("type")=="pbs"],
                    scope=dict(targeted=bool(self.vmids or self.selected_nodes), vmids=sorted(self.vmids), nodes=sorted(self.selected_nodes), discovered=len(visible), selected=len(selected)),
                    skipped_containers=(None if getattr(self.client, "container_count_unknown", False) else
                                        sum(r.get("type") == "lxc" for r in resources)),
                    limitations=list(LIMITATIONS))


def unique_disks(disks):
    seen, result = {}, []
    for disk in disks:
        if not disk["counted"]:
            continue
        key = tuple(disk["identity"]) if disk["identity"] else (disk["volume"],)
        if key not in seen:
            item = dict(disk)
            result.append(item)
            seen[key] = item
        else:
            # Duplicate attachments with contradictory/missing measurements
            # cannot establish a complete volume total.
            for metric in ("capacity_bytes", "actual_bytes"):
                if seen[key][metric] != disk[metric]:
                    seen[key][metric] = None
    return result


def complete_sum(values):
    return None if any(value is None for value in values) else sum(values)


def measurement_details(vm):
    disks = unique_disks(vm['disks'])
    measured = [d for d in disks if d['actual_bytes'] is not None]
    groups = {}
    for disk in measured:
        method = disk['actual_method']
        groups[method] = groups.get(method, 0) + disk['actual_bytes']
    state = ('complete' if vm['actual_bytes'] is not None else
             'partial' if measured else 'unavailable')
    return dict(measurement_status=state, measured_disk_count=len(measured),
                counted_disk_count=len(disks), measured_subtotals=groups,
                known_actual_bytes=next(iter(groups.values())) if len(groups) == 1 else None)


def disk_outcome(value, reason, excluded=False):
    if excluded:
        return 'excluded'
    if value is not None:
        return 'measured'
    reason = (reason or '').lower()
    for category, fragments in (
        ('not collected', ('not queried', 'cancelled', 'time limit reached', 'runtime budget')),
        ('timeout', ('timed out', 'timeout')),
        ('access denied', ('permission denied', '403')),
        ('backend failure', ('target reported failure',)),
        ('unrecognized response', ('not understood',)),
        ('counter unavailable', ('not active',)),
        ('unsupported', ('no validated', 'not supported', 'unmanaged'))):
        if any(fragment in reason for fragment in fragments):
            return category
    return 'unavailable'


def measurement_label(vm):
    detail = measurement_details(vm)
    if detail['measurement_status'] == 'complete':
        return human(vm['actual_bytes']) + ' (complete)'
    amounts = '; '.join(human(value) + ' ' + method for method, value in detail['measured_subtotals'].items())
    return ((amounts + ' measured; partial; ') if amounts else 'N/A; ') + f"{detail['measured_disk_count']} of {detail['counted_disk_count']} disks measured"


def sort_report(report, metric):
    for vm in report['vms']:
        vm.update(measurement_details(vm))
    key = metric + "_bytes"
    if metric == "actual":
        vms = report["vms"]
        methods = {v["actual_method"] for v in vms}
        if not vms or any(v[key] is None for v in vms) or len(methods) != 1:
            raise ValueError("Actual sorting requires complete allocation data for every VM and one metric meaning. "
                             "Use --sort configured to retain incomplete VMs in the report.")
    report["vms"].sort(key=lambda v: (v[key] is None, -(v[key] or 0), int(v["vmid"])))
    report["sort_metric"] = metric
    top = [v["vmid"] for v in report["vms"] if v[key] is not None][:10]
    report["top_vmids"] = top
    report["summary"] = summary(report)
    report["storage_breakdowns"] = storage_breakdowns(report)
    report["review_items"] = review_items(report)


def disk_bucket_totals(disks, missing_configs=0):
    records, unidentified = {}, 0
    for disk in disks:
        if not disk["identity"]:
            unidentified += 1
            continue
        key = tuple(disk["identity"])
        candidate = dict(capacity=disk["capacity_bytes"], used=disk["actual_bytes"], method=disk["actual_method"])
        if key not in records:
            records[key] = candidate
        else:
            old = records[key]
            if old["capacity"] != candidate["capacity"]:
                old["capacity"] = None
            if old["used"] != candidate["used"] or old["method"] != candidate["method"]:
                old["used"], old["method"] = None, None
    sizes = [r["capacity"] for r in records.values()]
    measured = [r for r in records.values() if r["used"] is not None and r["method"]]
    methods = {r["method"] for r in measured}
    known_size = sum(size for size in sizes if size is not None)
    full_size = not missing_configs and not unidentified and all(size is not None for size in sizes)
    full_used = bool(records) and not missing_configs and not unidentified and len(measured) == len(records) and len(methods) == 1
    return dict(unique_disk_count=len(records), configured_bytes=known_size if full_size else None,
                known_configured_bytes=known_size if any(size is not None for size in sizes) or full_size else None,
                actual_bytes=sum(r["used"] for r in measured) if full_used else None,
                measured_disk_count=len(measured), actual_methods=sorted(methods), unidentified_disk_references=unidentified,
                missing_vm_configs=missing_configs)


def storage_breakdowns(report):
    vms, rows, shared = report["vms"], [], []
    locations = {}
    for node in sorted({v["node"] for v in vms}):
        local = []
        node_vms = [v for v in vms if v["node"] == node]
        for vm in node_vms:
            for disk in unique_disks(vm["disks"]):
                scope = disk["identity"][0] if disk["identity"] else node
                if scope == "shared":
                    shared.append((vm, disk))
                else:
                    local.append(disk)
                locations.setdefault((disk["storage"] or "Unmanaged", scope), []).append((vm, disk))
        rows.append(dict(grouping="host", name=node, host=node, scope="Local disks only", vm_count=len(node_vms),
                         **disk_bucket_totals(local, sum(not v["config_available"] for v in node_vms))))
    if shared:
        rows.append(dict(grouping="host", name="Shared storage", host="Across cluster", scope="Shared disks counted once",
                         vm_count=len({v["vmid"] for v, _ in shared}),
                         **disk_bucket_totals([d for _, d in shared], sum(not v["config_available"] for v in vms))))
    for (name, scope), pairs in sorted(locations.items()):
        missing = sum(not v["config_available"] for v in vms if scope == "shared" or v["node"] == scope)
        rows.append(dict(grouping="storage", name=name, host="Across cluster" if scope == "shared" else scope,
                         scope="Shared disks counted once" if scope == "shared" else "Local storage on this host",
                         vm_count=len({v["vmid"] for v, _ in pairs}), **disk_bucket_totals([d for _, d in pairs], missing)))
    return rows


def review_items(report):
    threshold = report.get("large_vm_bytes", 500 * 1024**3)
    result = []
    def add(rule, vm, title, evidence, follow_up, category="Review"):
        result.append(dict(rule=rule, category=category, vmid=vm.get("vmid"), name=vm.get("name"),
                           node=vm.get("node"), title=title, evidence=evidence, follow_up=follow_up))
    for vm in report["vms"]:
        large = vm["configured_bytes"] is not None and vm["configured_bytes"] >= threshold
        if large:
            add("large-vm", vm, "Large assigned disk space", human(vm["configured_bytes"]) + " assigned; review threshold is " + human(threshold),
                "Ask the owner whether the disk size and backup needs still match the machine's purpose.")
        if large and vm["status"] == "stopped" and not vm["template"]:
            add("large-stopped-vm", vm, "Large machine currently off", "Stopped at collection time with " + human(vm["configured_bytes"]) + " assigned",
                "Confirm whether this machine is still needed. Being off does not prove it is unused or safe to remove.")
        if vm["backup"] == "No":
            add("no-backup-job", vm, "No active backup schedule found", "No enabled PVE job checked here selects this machine",
                "Confirm its backup arrangements with IT. Manual or external backups may still exist.")
        excluded = [d["slot"] for d in vm["disks"] if d["counted"] and d["backup_disk"] is False]
        if excluded:
            add("excluded-disks", vm, "Disk marked excluded from backups", "backup=0 on " + ", ".join(excluded),
                "Confirm with the owner that these exclusions are intentional. A VM being scheduled does not mean every disk is included.")
        if vm["actual_bytes"] is None:
            add("missing-measurement", vm, "Storage usage could not be fully measured", " ".join(allocation_vm_reason(vm)),
                "Review data availability before using a full storage-used total.", "Data gap")
        if not vm["config_available"] or vm["backup"] == NA:
            add("unreadable-settings", vm, "Some settings could not be read", "VM configuration or backup selection is unavailable",
                "Ask IT to check account permissions and host connectivity.", "Data gap")
    for store in report.get("pbs", {}).get("datastores", []):
        target = dict(name=store["store"], node="PBS")
        total, used = store["total_bytes"], store["used_bytes"]
        warning = report.get("pbs_warning_percent", 90)
        if total and used is not None and 100 * used / total >= warning:
            add("pbs-capacity", target, "Backup storage near its review threshold",
                f"{100*used/total:.1f}% used on the filesystem reported for {store['store']}; review threshold {warning}%",
                "Check retention, backup scope and cleanup results. Other data on the same filesystem can also contribute.", "Capacity")
        for kind, jobs in (("Prune", store.get("prune_status") or []), ("Garbage collection", [store.get("gc") or {}])):
            for job in jobs:
                state = str(job.get("last-run-state", ""))
                if state and ("error" in state.lower() or "fail" in state.lower()):
                    add("pbs-cleanup-failure", target, kind + " reported a failure", state,
                        "Ask IT to inspect the task log and resolve the cause before considering cleanup changes.", "Cleanup")
    for group in report.get('pbs',{}).get('groups',[]):
        for reason in group.get('review_reasons',[]):
            add('pbs-backup-review', dict(name=group['store']+'/'+group['namespace']+'/'+group['backup_type']+'/'+group['backup_id'],node='PBS'), reason,
                'Based on visible snapshot metadata', 'Review the group in PBS. No verification or restore was started.', 'Backup')
    return result


def summary(report):
    vms = report["vms"]
    volumes, allocations, missing = {}, {}, 0
    capacity_conflicts, allocation_conflicts = set(), set()
    identity_unknown = False
    unidentified_disks = 0
    for vm in vms:
        if not vm["config_available"]:
            missing += 1
        for disk in unique_disks(vm["disks"]):
            if not disk["identity"]:
                identity_unknown = True
                unidentified_disks += 1
                continue
            key = tuple(disk["identity"])
            capacity = disk["capacity_bytes"]
            if key in volumes and volumes[key] != capacity:
                capacity_conflicts.add(key)
                volumes[key] = None  # conflicting observations stay excluded
            else:
                volumes[key] = None if key in capacity_conflicts else capacity
            allocation = (disk["actual_bytes"], disk["actual_method"])
            if key in allocations and allocations[key] != allocation:
                allocation_conflicts.add(key)
                allocations[key] = (None, None)
            else:
                allocations[key] = (None, None) if key in allocation_conflicts else allocation
    unknown_sizes = sum(v is None for v in volumes.values())
    known = sum(v for v in volumes.values() if v is not None)
    full = missing == 0 and unknown_sizes == 0 and not identity_unknown
    measured = [(used, method) for used, method in allocations.values()
                if used is not None and method is not None]
    methods = {method for _, method in measured}
    allocation_complete = (bool(volumes) and missing == 0 and not identity_unknown
                           and len(measured) == len(volumes) and len(methods) == 1)
    allocation_total = sum(used for used, _ in measured) if allocation_complete else None
    subtotals = {method: sum(used for used, kind in measured if kind == method) for method in sorted(methods)}
    reasons = []
    if missing:
        reasons.append(f"Could not read the disk configuration for {missing} VM(s).")
    if unidentified_disks:
        reasons.append(f"Could not identify the storage for {unidentified_disks} disk reference(s), so they cannot be counted safely.")
    if len(measured) < len(volumes):
        reasons.append(f"Storage usage is available for {len(measured)} of {len(volumes)} identified disks. "
                       f"The remaining {len(volumes)-len(measured)} have no usable measurement.")
    if len(methods) > 1:
        reasons.append("The storage systems report usage in different ways (" + ", ".join(sorted(methods)) + "). "
                       "Their subtotals are shown separately instead of adding unlike measurements.")
    if not volumes and not missing and not unidentified_disks:
        reasons.append("No attached disks with a measurable storage identity were found.")
    unavailable_disks = [dict(vmid=v["vmid"], name=v["name"], node=v["node"], slot=d["slot"],
                              storage=d["storage"], reason=d["actual_reason"])
                         for v in vms for d in unique_disks(v["disks"]) if d["actual_bytes"] is None]
    report["allocation_diagnostics"] = dict(
        complete=allocation_complete, measured_disks=len(measured), identified_disks=len(volumes),
        missing_config_vmids=[v["vmid"] for v in vms if not v["config_available"]],
        unidentified_disk_references=unidentified_disks, methods=sorted(methods),
        reasons=reasons, unavailable_disks=unavailable_disks)
    accounting_note = ("Distinct attached volume IDs counted once. Allocation counters are not exclusive physical "
                       "storage, reclaimable space or PBS usage; shared extents and storage aliases may affect accounting.")
    report["storage_totals"] = [
        dict(metric="Configured logical capacity", total_bytes=known if full else None,
             known_subtotal_bytes=known if len(volumes) > unknown_sizes or full else None,
             measured_volumes=len(volumes)-unknown_sizes, known_identity_volumes=len(volumes),
             complete_coverage=full, notes=accounting_note),
        dict(metric="Measured VM disk allocation", total_bytes=allocation_total,
             known_subtotal_bytes=next(iter(subtotals.values())) if len(subtotals) == 1 else None, measured_volumes=len(measured),
             known_identity_volumes=len(volumes), complete_coverage=allocation_complete,
             notes="A combined total requires complete coverage and one allocation metric meaning. " + accounting_note),
    ] + [dict(metric="Allocation subtotal: " + method, total_bytes=None, known_subtotal_bytes=subtotal,
              measured_volumes=sum(kind == method for _, kind in measured), known_identity_volumes=len(volumes),
              complete_coverage=False, notes="Known volumes with this metric only; not a full inventory total. " + accounting_note)
         for method, subtotal in subtotals.items()]
    values = [
        ("Unique configured logical capacity", human(known) if full else "N/A (incomplete identity/configuration/size coverage)"),
        ("Total measured VM disk allocation", human(allocation_total) if allocation_complete else
         "N/A — " + " ".join(reasons)),
        ("Allocation volume coverage", f"{len(measured)}/{len(volumes)} volumes with known identity; "
         f"{missing} missing VM configurations; unidentified volumes={'yes' if identity_unknown else 'no'}"),
        ("Storage accounting", accounting_note),
    ] + [("Known allocation subtotal: " + method, human(subtotal)) for method, subtotal in subtotals.items()] + [
        ("Visible QEMU VMs (templates included)", str(len(vms))),
        ("Containers omitted", display(report["skipped_containers"])),
        ("VMs with complete configured capacity", f"{sum(v['configured_bytes'] is not None for v in vms)}/{len(vms)}"),
        ("VMs with complete actual allocation", f"{sum(v['actual_bytes'] is not None for v in vms)}/{len(vms)}"),
        ("Known unique logical-capacity subtotal", human(known) if len(volumes) > unknown_sizes or full else NA),
        ("Visible distinct volumes with known identity", str(len(volumes))),
        ("VMs missing configurations", str(missing)),
        ("Cluster physical storage consumed", "N/A (per-volume counters cannot establish a unique physical footprint)"),
        ("Guest filesystem usage total", "N/A (filesystems can overlap; see individual mounts)"),
        ("Known allocated RAM subtotal", human(sum(v["ram_bytes"] for v in vms if v["ram_bytes"] is not None)) if any(v["ram_bytes"] is not None for v in vms) else NA),
        ("Known current PVE memory-use subtotal", human(sum(v["memory_used_bytes"] for v in vms if v["memory_used_bytes"] is not None)) if any(v["memory_used_bytes"] is not None for v in vms) else NA),
        ("RAM allocation / use coverage", f"{sum(v['ram_bytes'] is not None for v in vms)}/{len(vms)} allocated; {sum(v['memory_used_bytes'] is not None for v in vms)}/{len(vms)} use"),
        ("Known allocated vCPU subtotal", str(sum(v["vcpus"] for v in vms if v["vcpus"] is not None)) if any(v["vcpus"] is not None for v in vms) else NA),
        ("vCPU coverage", f"{sum(v['vcpus'] is not None for v in vms)}/{len(vms)}"),
        ("Enabled backup job membership", "; ".join(f"{label}: {sum(v['backup'] == label for v in vms)}" for label in ("Yes", "No", NA))),
    ]
    return dict(values)


def display(value):
    return NA if value is None else str(value)


def disk_text(disk):
    return (f"{disk['slot']} {disk['volume']} | storage={display(disk['storage'])} | "
            f"configured={human(disk['capacity_bytes'])} ({disk['capacity_reason']}) | "
            f"actual={human(disk['actual_bytes'])} ({disk['actual_reason']}) | "
            f"disk backup flag={display(disk['backup_disk'])}" +
            (f" | excluded: {disk['excluded_reason']}" if not disk["counted"] else ""))


def fs_text(fs):
    return (f"{display(fs['mountpoint'])} [{display(fs['name'])}, {display(fs['type'])}] "
            f"used={human(fs['used_bytes'])}; free={human(fs['free_bytes'])}; "
            f"total={human(fs['total_bytes'])} ({fs['reason']})")


def job_text(job):
    return (f"{job['id']} | enabled={enabled(job.get('enabled', 1))} | storage={job.get('storage', NA)} | "
            f"schedule={job.get('schedule', job.get('starttime', NA))} | "
            f"job prune override={json.dumps(job.get('prune-backups', NA), ensure_ascii=False)} | "
            f"maxfiles={job.get('maxfiles', NA)} (legacy; effective PBS retention not determined)")


def terminal_report(report):
    lines = ["PBS INVENTORY" if report.get("scope",{}).get("pbs_only") else "PVE VM INVENTORY", run_description(report), f"Generated: {report['generated_at']}",
             f"Environment: {report.get('environment', 'cluster')}; access: {report.get('access_mode', 'local')}",
             f"PVE version: {json.dumps(report['pve_version'], ensure_ascii=False)}",
             f"Largest first by {report['sort_metric']} bytes; * = top 10 measured VMs", ""]
    for label, key in (("VMs inventoried", "Visible QEMU VMs (templates included)"),
                       ("Configured VM disks (logical capacity)", "Unique configured logical capacity"),
                       ("Measured disk usage", "Total measured VM disk allocation"),
                       ("Disk measurement coverage", "Allocation volume coverage"),
                       ("Backup job selection", "Enabled backup job membership"),
                       ("Known RAM assigned", "Known allocated RAM subtotal"),
                       ("Known RAM in use", "Known current PVE memory-use subtotal"),
                       ("Known vCPUs assigned", "Known allocated vCPU subtotal")):
        lines.append(f"{label}: {report['summary'][key]}")
    lines.extend(f"{key}: {value}" for key, value in report["summary"].items() if key.startswith("Known allocation subtotal:"))
    lines.append("Configured disks, measured usage and guest filesystem space describe different things. PBS capacity is reported separately.")
    lines.extend(["", "  VMID  NODE / NAME / STATUS | CONFIGURED | ACTUAL | RAM allocated / PVE used | vCPU | BACKUP"])
    for vm in report["vms"]:
        star = "*" if vm["vmid"] in report["top_vmids"] else " "
        lines.append(f"{star} {vm['vmid']}  {vm['node']} / {display(vm['name'])} / {display(vm['status'])} "
                     f"| {human(vm['configured_bytes'])} | {measurement_label(vm)} "
                     f"| {human(vm['ram_bytes'])} / {human(vm['memory_used_bytes'])} "
                     f"| {display(vm['vcpus'])} | {vm['backup']}")
        lines.append(f"    Tags: {display(vm['tags']) or '(none)'}; template={vm['template']}; "
                     f"enabled jobs={','.join(vm['enabled_jobs']) or '(none)'}; "
                     f"disabled jobs={','.join(vm['disabled_jobs']) or '(none)'}")
        lines.extend("    Disk: " + disk_text(disk) for disk in vm["disks"])
        lines.append("    Guest: " + vm["filesystem_reason"])
        lines.extend("      " + fs_text(fs) for fs in vm["filesystems"])
        lines.extend("    Note: " + note for note in vm["notes"])
    lines.extend(["", "PVE BACKUP JOBS (configuration only)"])
    lines.extend(job_text(job) for job in report["jobs"] or [])
    if report["jobs"] is None:
        lines.append("N/A: backup-job API unavailable; see collection warnings")
    elif not report["jobs"]:
        lines.append("No visible PVE scheduled jobs")
    lines.extend(["", "INTERPRETATION / LIMITATIONS"])
    lines.extend("- " + note for note in report["limitations"])
    lines.extend(["", "COLLECTION WARNINGS"] + (report["warnings"] or ["None"]))
    lines.extend(["", "ITEMS TO REVIEW (no automatic changes)"])
    lines.extend(f"- {r['name']} / {r['title']}: {r['evidence']} Next: {r['follow_up']}" for r in report.get("review_items", []))
    if not report.get("review_items"):
        lines.append("No items met the configured review rules. This does not confirm backup health.")
    lines.extend(["", "STORAGE BY HOST AND LOCATION"])
    lines.extend(f"- {r['grouping']} / {r['name']} / {r['host']}: assigned={human(r['configured_bytes'])}; measured={human(r['actual_bytes'])}; {r['scope']}" for r in report.get("storage_breakdowns", []))
    pbs = report.get("pbs", {})
    lines.extend(["", "PBS BACKUP STORAGE", "Collection status: " + pbs.get("status", "not requested")])
    for store in pbs.get("datastores", []):
        lines.append(f"- {store['store']}: total={human(store['total_bytes'])}; used={human(store['used_bytes'])}; available={human(store['available_bytes'])}; filesystem capacity, not per-VM usage")
        lines.append(f"  Visible groups={display(store['visible_group_count'])}; listed snapshots={display(store['listed_snapshot_count'])}; inventory complete within visible scope={store['inventory_complete']}")
        lines.append("  Retention configuration: " + json.dumps(store["datastore_retention"]) + "; prune jobs: " + json.dumps(store["prune_config"]))
        lines.append("  Prune status: " + json.dumps(store["prune_status"]) + "; GC status: " + json.dumps(store["gc"]))
    lines.extend("- " + w for w in pbs.get("warnings", []))
    # Remote labels must not inject terminal control sequences.
    return "\n".join(re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", line) for line in lines) + "\n"


VM_COLUMNS = ["vmid", "name", "node", "status", "cached_status", "tags", "template",
              "configured_bytes", "actual_bytes", "actual_method", "ram_bytes", "memory_used_bytes",
              "vcpus", "backup", "enabled_jobs", "disabled_jobs", "filesystem_reason", "notes"]


def csv_cell(value):
    if value is None:
        return NA
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    # Preserve numeric cells; neutralize untrusted spreadsheet formulas in text cells.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_cell(row.get(key)) for key in columns})


HTML_STYLE = r'''
:root{color-scheme:light;--bg:#f3f6fa;--panel:#fff;--ink:#18273b;--muted:#5d6c80;--line:#dce4ed;--accent:#146c60;--soft:#e5f3ee;--amber:#8a5814;--amber-bg:#fff4df;--header:#edf2f7;--shadow:0 5px 24px #18273b08}
:root[data-theme=dark]{color-scheme:dark;--bg:#10171f;--panel:#19232e;--ink:#e7eef7;--muted:#a6b7c9;--line:#31404f;--accent:#80d9c0;--soft:#203c37;--amber:#f0c779;--amber-bg:#3b3020;--header:#202c39;--shadow:none}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:20px}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,select{font:inherit}button,a,input,select,summary{outline-offset:4px}button,summary{cursor:pointer}a{color:var(--accent)}.shell{max-width:1600px;margin:auto;padding:36px clamp(18px,4vw,60px)}.masthead,.section-head,.toolbar,.brand{display:flex;align-items:center;gap:16px}.masthead,.section-head{justify-content:space-between}.brand-mark{background:var(--ink);color:var(--panel);font-size:13px;font-weight:800;letter-spacing:1px;border-radius:12px;padding:12px}.eyebrow{font-size:11px;font-weight:750;text-transform:uppercase;letter-spacing:1.8px;color:var(--accent);margin:0 0 5px}h1{font-size:clamp(27px,3vw,38px);letter-spacing:-1.3px;line-height:1.2;margin:0}h2{font-size:20px;letter-spacing:-.5px;margin:0}h3{font-size:15px;margin:0 0 6px}p{margin:8px 0}.muted,.meta,.hint{color:var(--muted)}.meta{font-size:12px}.nav{display:flex;gap:24px;border-bottom:1px solid var(--line);padding:22px 0 14px;margin-bottom:26px;flex-wrap:wrap}.nav a{text-decoration:none;font-size:13px;color:var(--muted);font-weight:650}.nav a:hover{color:var(--accent)}.button{border:1px solid var(--line);border-radius:9px;padding:8px 13px;background:var(--panel);color:var(--ink);white-space:nowrap}.button:hover{border-color:var(--accent)}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}.card,.panel{background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow);border-radius:15px}.card{padding:22px}.card .label{font-size:12px;color:var(--muted);font-weight:650}.card .value{font-size:30px;font-weight:720;letter-spacing:-1px;line-height:1.3;margin:9px 0}.card p{font-size:12px;color:var(--muted)}.card.primary{border-top:3px solid var(--accent)}.badge{display:inline-flex;align-items:center;gap:5px;border-radius:5px;padding:2px 7px;font-size:11px;font-weight:650;background:var(--header);color:var(--muted)}.badge.good{background:var(--soft);color:var(--accent)}.badge.warn{background:var(--amber-bg);color:var(--amber)}.panel{padding:24px;margin-top:20px}.two-col{display:grid;grid-template-columns:1.2fr 1fr;gap:20px}.two-col .panel{min-width:0}.section-head{align-items:flex-start;margin-bottom:15px}.coverage{height:8px;background:var(--header);border-radius:9px;overflow:hidden;margin:14px 0}.coverage span{display:block;height:100%;background:var(--accent)}.reason-list{padding-left:20px;color:var(--muted);font-size:13px}.reason-list li{margin-bottom:9px}.callout{border-left:3px solid var(--accent);background:var(--soft);padding:10px 14px;border-radius:0 8px 8px 0;font-size:13px;margin-top:15px}.subtotal{display:flex;justify-content:space-between;gap:16px;padding:10px 0;border-top:1px solid var(--line);font-size:13px}.subtotal span{color:var(--muted)}.top-list{list-style:none;margin:0;padding:0}.top-list li{padding:10px 0;border-bottom:1px solid var(--line)}.top-label{display:flex;justify-content:space-between;gap:12px;font-size:12px}.rank{color:var(--muted);display:inline-block;min-width:23px}.bar{height:5px;margin:7px 0 0 25px;border-radius:5px;background:var(--header)}.bar span{display:block;height:100%;border-radius:5px;background:var(--accent);opacity:.8}.toolbar{flex-wrap:wrap;margin:15px 0}.scope-control select{border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--ink);padding:8px 12px}.toolbar input,.toolbar select{border:1px solid var(--line);border-radius:9px;background:var(--bg);color:var(--ink);padding:9px 12px}.toolbar input[type=search]{flex:1;min-width:220px}.toolbar label{display:flex;gap:7px;align-items:center;font-size:12px;color:var(--muted)}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:10px}table{border-collapse:collapse;width:100%;font-size:12px}#vms{min-width:1200px}#inventory .table-wrap{max-height:70vh}@media print{#inventory .table-wrap{max-height:none}}.toolbar input[type=checkbox]{width:auto;min-width:0;flex:none;margin:0}th,td{text-align:left;border-bottom:1px solid var(--line);padding:12px 11px;vertical-align:top}th{background:var(--header);white-space:nowrap;font-size:11px;color:var(--muted);position:sticky;top:0;z-index:1}th button{background:none;border:0;font:inherit;color:inherit;font-weight:700;padding:0;text-align:left}th[aria-sort=ascending] button::after{content:' ↑'}th[aria-sort=descending] button::after{content:' ↓'}td{font-variant-numeric:tabular-nums}tr:last-child td{border-bottom:0}tr.top td:first-child{box-shadow:inset 3px 0 var(--accent)}td:nth-child(6),td:nth-child(7),td:nth-child(8),td:nth-child(9){white-space:nowrap}td small{display:block;color:var(--muted);font-size:10px}details>summary{font-size:12px;color:var(--accent);font-weight:600}details[open]>summary{margin-bottom:12px}.metric-reason{white-space:normal;min-width:110px;max-width:330px}.metric-reason p{font-size:12px;color:var(--muted)}.vm-details{min-width:190px}.vm-details[open]{min-width:190px}.vm-drawer{margin:0 0 0 auto;max-height:100vh;height:100vh;max-width:680px;border-radius:0;padding:24px}.disk-detail{padding:12px;background:var(--bg);border:1px solid var(--line);border-radius:8px;margin-top:10px;overflow-wrap:anywhere}.disk-detail p{font-size:12px}.guide-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:22px}.guide-grid p{font-size:13px;color:var(--muted)}.subsection{margin-top:20px;padding-top:18px;border-top:1px solid var(--line)}.technical dl{display:grid;grid-template-columns:minmax(180px,1fr) 2fr;gap:10px 24px;font-size:12px}.technical dt{font-weight:600}.technical dd{margin:0;color:var(--muted)}.warning-list{font-size:12px;overflow-wrap:anywhere;padding-left:20px}.footer{display:flex;justify-content:space-between;gap:18px;color:var(--muted);font-size:11px;padding:25px 0}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}[hidden]{display:none!important}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#10171f;--panel:#19232e;--ink:#e7eef7;--muted:#a6b7c9;--line:#31404f;--accent:#80d9c0;--soft:#203c37;--amber:#f0c779;--amber-bg:#3b3020;--header:#202c39;--shadow:none}}
@media(max-width:1100px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.two-col{grid-template-columns:1fr}.guide-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){.shell{padding:22px 14px}.brand-mark{display:none}.cards,.guide-grid{grid-template-columns:1fr}.panel{padding:18px}.technical dl{grid-template-columns:1fr}.masthead{align-items:flex-start}.footer{display:block}.toolbar input[type=search]{min-width:160px}}
@media print{.nav,.toolbar,#theme-toggle,.footer button{display:none}.shell{padding:0;max-width:none}.panel,.card{box-shadow:none;break-inside:avoid}.table-wrap{overflow:visible}.two-col{display:block}body{background:white;color:black}}
'''

HTML_STYLE += r'''
.nav{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px;gap:5px;border:1px solid var(--line);border-radius:12px;margin:22px 0 28px;box-shadow:var(--shadow)}.nav a{padding:9px 13px;border-radius:8px}.nav a[aria-current=page]{background:var(--panel);color:var(--accent);box-shadow:var(--shadow)}.page-intro{color:var(--muted);max-width:950px;margin:8px 0 22px;font-size:14px}.help-link{font-size:12px;text-decoration:underline;text-underline-offset:3px;font-weight:600}.card .help-link{display:inline-block;margin-top:4px}th .help-link{display:inline-flex;align-items:center;justify-content:center;width:21px;height:21px;border:1px solid var(--line);border-radius:50%;text-decoration:none;margin-left:3px}.vm-details .subsection{margin-top:12px;padding-top:12px}.glossary-entry{scroll-margin-top:100px}.glossary-entry:target{outline:2px solid var(--accent);outline-offset:8px;border-radius:6px}.glossary-entry p{color:var(--muted);font-size:14px}.guide-grid{margin-top:16px}tfoot td,tfoot th{background:var(--soft);color:var(--ink);font-weight:650;position:static;white-space:normal!important}tfoot small{display:block;color:var(--muted);font-weight:400}#table-total-note{font-size:12px;font-weight:400}dialog{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:16px;padding:28px;max-width:620px;width:calc(100% - 32px);max-height:85vh;box-shadow:0 20px 90px #0005}dialog::backdrop{background:#07111db3}dialog form{display:flex;justify-content:flex-end;margin-bottom:12px}dialog h2{margin-bottom:14px}dialog p{font-size:16px;line-height:1.8;color:var(--muted)}.report-page h2:focus{outline:none}
@media(max-width:600px){.nav{position:static;gap:2px}.nav a{font-size:12px;padding:8px}.vm-details[open]{min-width:350px}}
#vms[data-resources=false]{min-width:1050px}#vms[data-resources=false] thead th:nth-child(n+8):nth-child(-n+10),#vms[data-resources=false] tbody td:nth-child(n+8):nth-child(-n+10){display:none}#vms tbody td:nth-child(2){min-width:130px}#table-total-note{display:block;max-width:380px}tfoot a{white-space:normal}
@media print{.nav,dialog{display:none}.report-page[hidden]{display:none!important}}
.management-sheet{max-width:900px;margin:20px auto;padding:28px;background:white;color:#18273b;border:1px solid #dce4ed;border-radius:12px;font-size:13px;line-height:1.45}.management-sheet h1{font-size:27px}.management-sheet h2{font-size:17px;margin:18px 0 8px}.management-sheet table{font-size:12px}.management-sheet th{background:#edf2f7;color:#18273b}.management-sheet th,.management-sheet td{padding:7px;border-color:#dce4ed}.management-sheet .meta,.management-note{font-size:11px;color:#526377}.management-sheet li{margin:7px 0}.management-metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}.management-metrics div{border:1px solid #dce4ed;border-radius:8px;padding:12px}.management-metrics strong,.management-metrics span{display:block}.management-metrics strong{font-size:23px}.management-metrics span{font-size:11px;color:#526377}.print-toolbar{margin:18px 0}pre{max-width:100%;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}code{overflow-wrap:anywhere}
@media(max-width:600px){.management-metrics{grid-template-columns:repeat(2,1fr)}.management-sheet{padding:16px}}
@page{size:auto;margin:12mm}
.selected-vm{outline:2px solid var(--accent);outline-offset:-2px}
@media print{.management-sheet h2{break-after:avoid}.management-sheet tr,.management-sheet li{break-inside:avoid}.management-sheet thead{display:table-header-group}.management-sheet th{position:static}.management-sheet td{white-space:normal!important;overflow-wrap:anywhere}.management-sheet p{orphans:3;widows:3}}
@media print{.print-toolbar{display:none}.management-sheet{max-width:none;border:0;border-radius:0;padding:0;margin:0;font-size:10pt;line-height:1.25;break-inside:auto}.management-sheet h1{font-size:18pt}.management-sheet h2{font-size:12pt;margin:12px 0 6px}.management-sheet table{font-size:9pt}.management-sheet .meta,.management-note{font-size:8.5pt}.management-sheet li{margin:5px 0}.management-metrics{gap:8px;margin:12px 0}.management-metrics div{padding:8px}.management-metrics strong{font-size:20px}.management-sheet th,.management-sheet td{padding:5px}}
'''

HTML_SCRIPT = r'''
const root=document.documentElement, themeButton=document.querySelector('#theme-toggle');
function applyTheme(theme){root.dataset.theme=theme;themeButton.textContent=theme==='dark'?'Light Mode':'Dark Mode';themeButton.setAttribute('aria-pressed',String(theme==='dark'));}
let preferred=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
try{const stored=localStorage.getItem('pve-inventory-theme');if(stored==='light'||stored==='dark')preferred=stored;}catch{}
applyTheme(preferred);
themeButton.addEventListener('click',()=>{const next=root.dataset.theme==='dark'?'light':'dark';applyTheme(next);try{localStorage.setItem('pve-inventory-theme',next);}catch{}});
const table=document.querySelector('#vms'), body=table.tBodies[0], numeric=new Set([0,5,6,7,8,9]);
document.querySelectorAll('button[data-col]').forEach(button=>{
 button.addEventListener('click',()=>{
  const col=Number(button.dataset.col),asc=button.dataset.direction!=='asc',rows=Array.from(body.rows);
  rows.sort((a,b)=>{const ac=a.cells[col],bc=b.cells[col],am=ac.dataset.missing==='true',bm=bc.dataset.missing==='true';
   if(am!==bm)return am?1:-1;if(am){if(col===6){return Number(bc.dataset.partial==='true')-Number(ac.dataset.partial==='true');}return 0;}
   const x=ac.dataset.value,y=bc.dataset.value,diff=numeric.has(col)?Number(x)-Number(y):x.localeCompare(y,undefined,{numeric:true,sensitivity:"base"});return asc?diff:-diff;});
  table.querySelectorAll('th').forEach(h=>h.removeAttribute('aria-sort'));button.closest('th').setAttribute('aria-sort',asc?'ascending':'descending');button.dataset.direction=asc?'asc':'desc';body.append(...rows);
 });
});
const pages=Array.from(document.querySelectorAll('[data-page]')),pageLinks=Array.from(document.querySelectorAll('[data-page-link]'));
function showPage(name,focus=false){if(!pages.some(p=>p.dataset.page===name))name='overview';pages.forEach(p=>p.hidden=p.dataset.page!==name);pageLinks.forEach(a=>{if(a.dataset.pageLink===name)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});if(focus){document.querySelector(`[data-page="${name}"] h2`).focus({preventScroll:true});window.scrollTo(0,0);}}
const glossary=document.querySelector('#glossary-details');glossary.open=false;
function route(){const name=location.hash.slice(1);if(name.startsWith('help-')){showPage('help');glossary.open=true;document.getElementById(name)?.scrollIntoView();}else showPage(name||'overview',Boolean(name));}
window.addEventListener('hashchange',route);route();
const dialog=document.querySelector('#explanation-dialog');
document.querySelectorAll('[data-help]').forEach(link=>link.addEventListener('click',event=>{const entry=document.getElementById('help-'+link.dataset.help);if(!entry||typeof dialog.showModal!=='function')return;event.preventDefault();const content=entry.cloneNode(true);content.removeAttribute('id');const heading=content.querySelector('h3');document.querySelector('#explanation-title').textContent=heading.textContent;heading.remove();document.querySelector('#explanation-content').replaceChildren(content);dialog.showModal();}));
dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}});
const vmDialog=document.createElement('dialog');vmDialog.className='vm-drawer';vmDialog.innerHTML='<form method="dialog"><button class="button">Close VM Information</button></form><div class="vm-drawer-body"></div>';document.body.append(vmDialog);
document.querySelectorAll('.vm-details').forEach(details=>details.addEventListener('toggle',()=>{if(details.open){vmDialog.querySelector('.vm-drawer-body').innerHTML=details.innerHTML;vmDialog.querySelector('.vm-drawer-body summary')?.remove();details.open=false;vmDialog.showModal();}}));
const pbsPage=document.querySelector('[data-page="pbs"]');if(pbsPage&&pbsPage.querySelector('#pbs-inventory')){Array.from(pbsPage.children).filter(e=>e.querySelector('h2')?.textContent.startsWith('Datastore:')).forEach(panel=>{const retention=panel.querySelector(':scope > .subsection');if(retention){const section=document.createElement('section');section.className='panel';const title=document.createElement('h2');title.textContent='Retention & Cleanup · '+panel.querySelector('h2').textContent;section.append(title,retention);pbsPage.append(section);}});const panels=Array.from(pbsPage.children).filter(e=>e.tagName==='SECTION');const controls=document.createElement('div');controls.className='toolbar';const groups=['Overview','Backups','Activity','Retention & Cleanup','Evidence'];panels.forEach(panel=>{const heading=panel.querySelector('h2')?.textContent||'';panel.dataset.pbsSection=heading.startsWith('Retention & Cleanup')?'Retention & Cleanup':heading==='Backup inventory'?'Backups':heading.includes('attempts and secondary')?'Activity':heading.includes('evidence')||heading.includes('visibility')||heading.includes('checks')?'Evidence':'Overview';});groups.forEach(name=>{const button=document.createElement('button');button.className='button';button.textContent=name;button.type='button';button.onclick=()=>{panels.forEach(p=>p.hidden=p.dataset.pbsSection!==name);controls.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));};controls.append(button);});pbsPage.insertBefore(controls,panels[0]);controls.firstElementChild.click();}
function humanBytes(value){let amount=Number(value);for(const unit of ['B','KiB','MiB','GiB','TiB','PiB','EiB']){if(amount<1024||unit==='EiB')return amount.toFixed(2)+' '+unit;amount/=1024;}}
function updateStorageTotals(rows){const label=document.querySelector('#table-total-label'),assigned=document.querySelector('#table-assigned-total'),used=document.querySelector('#table-used-total'),note=document.querySelector('#table-total-note');const ram=document.querySelector('#table-ram-total');const values=rows.map(r=>r.cells[7]).filter(c=>c.dataset.missing!=='true').map(c=>BigInt(c.dataset.value));ram.textContent=!rows.length?'—':values.length===rows.length?humanBytes(values.reduce((a,b)=>a+b,0n)):(values.length?humanBytes(values.reduce((a,b)=>a+b,0n))+' known · '+values.length+' of '+rows.length+' VMs':'N/A');label.textContent=`Totals for ${rows.length} shown VMs`;if(!rows.length){assigned.textContent='—';used.textContent='—';note.textContent='No machines match the filters.';return;}
 const disks=new Map();let missingConfig=false,unknownIdentity=false;
 rows.forEach(row=>{if(row.dataset.configAvailable!=='true')missingConfig=true;JSON.parse(row.dataset.disks).forEach(d=>{if(!d.identity){unknownIdentity=true;return;}const key=JSON.stringify(d.identity);if(disks.has(key)){const old=disks.get(key);if(old.capacity!==d.capacity)old.capacity=null;if(old.used!==d.used||old.method!==d.method){old.used=null;old.method=null;}}else disks.set(key,{...d});});});
 let assignedKnown=0n,usedKnown=0n,sizes=0,measurements=0;const methods=new Set();disks.forEach(d=>{if(d.capacity!==null){assignedKnown+=BigInt(d.capacity);sizes++;}if(d.used!==null&&d.method){usedKnown+=BigInt(d.used);measurements++;methods.add(d.method);}});
 const fullSizes=!missingConfig&&!unknownIdentity&&sizes===disks.size,fullUsage=disks.size>0&&!missingConfig&&!unknownIdentity&&measurements===disks.size&&methods.size===1;
 assigned.textContent=fullSizes?humanBytes(assignedKnown):'N/A';used.textContent=fullUsage?humanBytes(usedKnown)+' · Complete':measurements&&methods.size===1?humanBytes(usedKnown)+' · Partial':measurements?'Separate Measurement Totals':'No Measurements Collected';
 const messages=[];if(!fullSizes&&sizes)messages.push('Known assigned subtotal: '+humanBytes(assignedKnown)+'.');const partial=new Map();disks.forEach(d=>{if(d.used!==null&&d.method)partial.set(d.method,(partial.get(d.method)||0n)+BigInt(d.used));});if(methods.size>1)used.textContent=Array.from(partial,([method,amount])=>method+': '+humanBytes(amount)).join('; ');if(!fullUsage)partial.forEach((amount,method)=>messages.push('Measured subtotal ('+method+'): '+humanBytes(amount)+'.'));messages.push(`Storage used measured for ${measurements} of ${disks.size} identified disks.`);if(missingConfig||unknownIdentity)messages.push('Some disk settings or storage locations are unavailable.');if(methods.size>1)messages.push('The storage systems count space differently; a combined usage total is unavailable.');messages.push('Shared disk IDs count once. Missing values are not zero.');note.textContent=messages.join(' ');
}
const search=document.querySelector('#search'),nodeFilter=document.querySelector('#node-filter'),missingOnly=document.querySelector('#missing-only'),resultCount=document.querySelector('#result-count');
function filterRows(){const shown=[];Array.from(body.rows).forEach(row=>{const match=row.dataset.search.includes(search.value.toLowerCase().trim())&&(!nodeFilter.value||row.dataset.node===nodeFilter.value)&&(!missingOnly.checked||row.dataset.missingAllocation==='true');row.hidden=!match;if(match)shown.push(row);});const active=[search.value.trim()?'Search: '+search.value.trim():'',nodeFilter.value?'PVE node: '+nodeFilter.value:'',missingOnly.checked?'Usage unavailable':''].filter(Boolean);resultCount.textContent=`${shown.length} of ${body.rows.length} machines shown`+(active.length?' — '+active.join(' · '):' — All VMs');document.querySelector('#no-results').hidden=shown.length!==0;updateStorageTotals(shown);}
search.addEventListener('input',filterRows);nodeFilter.addEventListener('change',filterRows);missingOnly.addEventListener('change',filterRows);
document.querySelector('#show-resources').addEventListener('change',event=>{table.dataset.resources=String(event.target.checked);document.querySelector('#table-total-note').closest('td').colSpan=event.target.checked?4:2;document.querySelector('#table-ram-total').hidden=!event.target.checked;});
const globalNode=document.querySelector('#global-node-filter');const nodeViews=JSON.parse(document.querySelector('#node-view-data')?.textContent||'{}');const fullViews={};['overview','hosts','review','storage'].forEach(key=>{fullViews[key]=document.querySelector(`[data-page="${key}"]`).innerHTML;});
function applyNodeView(value){if(globalNode)globalNode.value=value;nodeFilter.value=value;filterRows();for(const key of Object.keys(fullViews)){document.querySelector(`[data-page="${key}"]`).innerHTML=value&&nodeViews[value]?nodeViews[value][key]:fullViews[key];}const note=document.querySelector('#global-scope-note');if(note)note.textContent=(value?'Viewing PVE node: '+value:'Viewing all collected PVE nodes')+'. Management Summary, print, PBS, backup-job definitions and exports retain their full collected scope.';document.querySelectorAll('[data-review-vmid]').forEach(link=>{link.onclick=event=>{event.preventDefault();applyNodeView('');search.value='';missingOnly.checked=false;filterRows();showPage('machines');location.hash='machines';const target=Array.from(body.rows).find(row=>row.cells[0].dataset.value===link.dataset.reviewVmid);body.querySelectorAll('.selected-vm').forEach(r=>r.classList.remove('selected-vm'));if(target){target.classList.add('selected-vm');target.scrollIntoView({block:'center'});}};});const missingButton=document.querySelector('#show-missing');if(missingButton)missingButton.onclick=()=>{missingOnly.checked=true;filterRows();showPage('machines');location.hash='machines';};}
if(globalNode){globalNode.onchange=()=>applyNodeView(globalNode.value);nodeFilter.addEventListener('change',()=>applyNodeView(nodeFilter.value));}
function clearFilters(){search.value='';nodeFilter.value='';missingOnly.checked=false;applyNodeView('');}
document.querySelector('#clear-filters').addEventListener('click',clearFilters);
document.querySelectorAll('[data-review-vmid]').forEach(link=>link.addEventListener('click',event=>{event.preventDefault();clearFilters();location.hash='machines';showPage('machines');Array.from(body.rows).forEach(row=>{const selected=row.cells[0].dataset.value===link.dataset.reviewVmid;row.classList.toggle('selected-vm',selected);if(selected){row.tabIndex=-1;requestAnimationFrame(()=>{row.scrollIntoView({block:'center'});row.focus({preventScroll:true});});}});}));
document.querySelector('#show-missing').addEventListener('click',()=>{missingOnly.checked=true;filterRows();location.hash='machines';showPage('machines',true);});
filterRows();
const pbsPanel=document.querySelector('#pbs-inventory');
if(pbsPanel){const pbsBody=pbsPanel.querySelector('tbody'),allRows=Array.from(pbsBody.rows),input=document.querySelector('#pbs-search');let pageIndex=0,filtered=allRows;
function renderPBS(){filtered=allRows.filter(r=>r.textContent.toLowerCase().includes(input.value.trim().toLowerCase()));pageIndex=Math.max(0,Math.min(pageIndex,Math.ceil(filtered.length/50)-1));allRows.forEach(r=>r.hidden=true);filtered.slice(pageIndex*50,(pageIndex+1)*50).forEach(r=>r.hidden=false);document.querySelector('#pbs-count').textContent=`${filtered.length} of ${allRows.length} groups match; page ${pageIndex+1} of ${Math.max(1,Math.ceil(filtered.length/50))}`;document.querySelector('#pbs-prev').disabled=pageIndex===0;document.querySelector('#pbs-next').disabled=(pageIndex+1)*50>=filtered.length;}
input.addEventListener('input',()=>{pageIndex=0;renderPBS();});document.querySelector('#pbs-prev').onclick=()=>{pageIndex--;renderPBS();};document.querySelector('#pbs-next').onclick=()=>{pageIndex++;renderPBS();};
pbsPanel.querySelectorAll('[data-pbs-col]').forEach(button=>button.onclick=()=>{const col=Number(button.dataset.pbsCol),asc=button.dataset.direction!=='asc';button.dataset.direction=asc?'asc':'desc';allRows.sort((a,b)=>{const x=a.cells[col],y=b.cells[col];if(x.dataset.unknown!==y.dataset.unknown)return x.dataset.unknown==='true'?1:-1;const left=x.dataset.pbsValue,right=y.dataset.pbsValue;return (asc?1:-1)*([4,5,6,7,8,11,12].includes(col)?Number(left)-Number(right):left.localeCompare(right,undefined,{numeric:true}));});allRows.forEach(r=>pbsBody.append(r));pageIndex=0;renderPBS();});renderPBS();}

'''


REPORT_HELP = {
    "machines": ("Virtual Machines and templates", "A virtual machine (VM) is a computer created in software. It runs on a physical server and has its own applications, disk space, memory and processing capacity. A template is a saved setup used to create new VMs; templates are included because their disks can take up storage. This report includes only machines the connected account can see."),
    "hosts": ("Host servers and clusters", "A host is the physical Proxmox server that runs a VM. Proxmox calls it a node. A cluster is a group of these servers managed together. The Host server column identifies where the VM was assigned when this report was collected."),
    "power": ("Power state", "On means Proxmox reports that the VM is running. Off means it is stopped. Unknown means the current state could not be confirmed. A machine can be On while an application inside it is unhealthy; this report does not test application availability."),
    "labels": ("Labels", "Labels are the tags your team has assigned to a VM, such as production or finance. They help identify its purpose or owner. A blank field means there are no labels. N/A means the labels could not be read."),
    "assigned": ("Assigned disk space", "This is the size of the disks made available to a VM, whether or not it has filled them. For example, a 500 GiB disk counts as 500 GiB even if it contains only 20 GiB of files. The overall total counts repeated references to the same identified disk once. It excludes installation media and disks that are no longer attached. It is not a measurement of backup storage used."),
    "measured": ("Measured storage used", "This is the disk space that a supported storage system reports as allocated to VM disks. It differs from assigned disk size and from files used inside a VM. Storage systems can count shared data, snapshots and compressed data differently. Known measured amounts are shown with Complete or Partial coverage. Different measurement types have separate subtotals. It does not establish unique physical space used, space recoverable by deletion, or backup storage used."),
    "missing": ("Why a number may be unavailable", "N/A means no reliable number is available; it does not mean zero. The storage system may not provide a supported measurement, the account may lack access, or a server may not respond. A missing VM configuration can also prevent a full total. Known subtotals cover only the disks that could be read. Open the VM's Data availability section for a plain-language explanation, or its IT Reference for the exact request details."),
    "memory": ("RAM assigned and memory in use", "Memory (RAM) is the short-term workspace a computer uses while running applications. It is different from disk space, which stores files. Assigned memory is the maximum configured for the VM. RAM in use is the current value reported by Proxmox; it can be affected by memory-sharing and reporting settings. This report does not measure the memory used by each application."),
    "cpu": ("Virtual CPUs", "Virtual CPUs are the processing capacity assigned to a VM. A larger number lets it use more processing work at once, subject to what the host can supply. They are not a count of physical processor cores owned by the VM, and this number does not show current processor usage."),
    "backups": ("Scheduled for backup", "Yes means at least one currently enabled Proxmox backup schedule selects this VM. No means no enabled schedule selects it among the schedules successfully checked. Unknown means the selection could not be determined. This does not prove that a backup ran or succeeded. Manual backups, older backups and jobs outside PVE are not discovered here. A schedule can select a VM while excluding some of its disks."),
    "inside": ("Space inside the virtual machine", "These numbers describe the drives or mounted folders reported by a small helper service inside the VM, called the guest agent. Used means space occupied within that reported filesystem; free is reported total minus used. A mount point is the drive letter or folder where that filesystem is accessed. The same storage can appear in more than one place, so these entries are not added together. Missing data often means the VM is off or the helper service is unavailable."),
    "units": ("Storage and memory units", "GiB and TiB are measures of size: 1 GiB is 1,024 MiB, and 1 TiB is 1,024 GiB. Storage vendors often use decimal GB and TB instead. A drive sold as 2 TB is about 1.82 TiB before filesystem overhead. All human-readable sizes in this report use binary units consistently. CSV fields ending in _bytes contain the original byte counts."),
    "retention": ("How many backups are kept", "Retention rules decide which older backups should remain. A job can have its own rules or use settings from its storage destination. Several rules can apply together; their counts should not simply be added. Rules for days, weeks or months use periods that contain backups. A job's listed rules do not establish the effective PBS policy, and this report does not confirm that cleanup ran."),
    "schedule": ("Backup schedules", "The schedule is when PVE intends to start a backup job. It does not show when a backup last succeeded. Simple daily times are shown directly. Custom schedules may use Proxmox calendar notation, such as mon..fri 02:00 for weekday mornings at 02:00. Times follow the PVE schedule's time zone, not necessarily your browser's local time. Ask IT to confirm complex schedules."),
    "pbs": ("What this report says about backup storage", "Proxmox Backup Server (PBS) stores backups separately from the running VMs. It can compress data and share identical pieces across VMs and backup versions, so adding VM disk sizes does not tell you how much PBS space they use. PBS reporting is optional and off by default. When enabled, its page shows filesystem capacity, visible backup groups and saved versions, retention settings, and recent cleanup results. Filesystem usage may include other data. Logical snapshot sizes are not unique space assigned to an individual VM."),
}


def report_help_link(topic, label="Explain", *, title=None):
    text = html.escape(label, quote=True)
    accessible = html.escape(title or ("Explain " + REPORT_HELP[topic][0].lower()), quote=True)
    return f'<a class="help-link" href="#help-{topic}" data-help="{topic}" aria-label="{accessible}">{text}</a>'


def retention_description(job):
    rules = job.get("prune-backups")
    if rules is None:
        return "Not set in this job; storage settings may apply."
    if isinstance(rules, str):
        rules = dict(part.split("=", 1) for part in rules.split(",") if "=" in part)
    if not isinstance(rules, dict):
        return "Custom settings; ask IT to review the job."
    if enabled(rules.get("keep-all", 0)):
        return "Keep every backup (job setting)."
    descriptions = []
    for key, period in (("last", None), ("hourly", "hours"), ("daily", "days"), ("weekly", "weeks"), ("monthly", "months"), ("yearly", "years")):
        amount = number(rules.get("keep-" + key))
        if amount:
            descriptions.append(f"Keep the {amount} most recent backups" if period is None else
                                f"Keep backups from the {amount} most recent {period} with backups")
    return "; ".join(descriptions) + "." if descriptions else "No readable retention rules listed in this job."


def schedule_description(job):
    value = job.get("schedule", job.get("starttime"))
    names = {"hourly": "Every hour", "daily": "Every day", "weekly": "Every week", "monthly": "Every month", "yearly": "Every year"}
    if value in names:
        return names[value]
    if isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        return "Every day at " + value
    return "Custom schedule" if value else "Schedule not available"


def allocation_vm_reason(vm):
    reasons = []
    if not vm["config_available"]:
        reasons.append("This machine's disk settings could not be read. Ask IT to check access to its host server.")
    for index, disk in enumerate(unique_disks(vm["disks"]), 1):
        if disk["actual_bytes"] is None:
            raw = disk["actual_reason"] or "No measurement was returned"
            backend = re.search(r"backend (\S+) has no validated", raw)
            if backend:
                reason = f"This tool cannot yet measure storage used on the {backend[1]} storage system. The disk's assigned size is shown separately."
            elif "403" in raw or "permission denied" in raw.lower():
                reason = "Access to this storage was denied. Ask IT to check the account's permissions."
            elif "storage configuration unavailable" in raw:
                reason = "Storage settings could not be read, so this disk could not be identified safely."
            elif "volume-group name" in raw:
                reason = "Storage settings were incomplete, so the exact thin-volume usage could not be matched to this disk."
            elif "did not return used" in raw:
                reason = "The storage system answered but did not provide a storage-used measurement for this disk."
            elif "timed out" in raw.lower() or "timeout" in raw.lower():
                reason = "The storage measurement exceeded its time limit. This does not establish that storage is offline. Check the node, storage and query details in IT Reference; guest tools are not required for this measurement."
            elif "passthrough" in raw.lower() or "unmanaged" in raw.lower():
                reason = "This disk is attached directly rather than managed through a supported storage system. Its storage usage is unavailable."
            elif "not active" in raw.lower():
                reason = "This thin-provisioned disk is not active or has no readable exact usage counter. The tool does not activate disks just to measure them."
            elif "require" in raw.lower() and "SSH" in raw:
                reason = "This storage measurement needs SSH or local host access. A PVE API token alone cannot read the exact thin-volume counter."
            else:
                reason = "A reliable measurement could not be collected. IT can review the exact request in this machine's IT Reference."
            reasons.append(f"{disk['slot']} ({disk['volume']}): {reason} Exact result: {raw}")
    if len({d["actual_method"] for d in unique_disks(vm["disks"]) if d["actual_method"]}) > 1:
        reasons.append("The storage systems count space differently. Their measurements cannot safely be added into one total.")
    if not reasons:
        reasons.append("This machine has no complete set of comparable disk usage measurements. See its Disks and storage section.")
    return reasons


def power_state(status):
    return {"running": "On", "stopped": "Off"}.get(status, "Unknown")


def vm_information_html(vm):
    esc = lambda value: html.escape(display(value), quote=True)
    disk_parts = []
    for i, disk in enumerate(vm["disks"], 1):
        label = ("Startup settings disk" if disk["slot"] == "efidisk0" else
                 "Security settings disk" if disk["slot"] == "tpmstate0" else
                 "Unattached disk" if disk["slot"].startswith("unused") else
                 "Setup or installation media" if not disk["counted"] else f"Disk {i}")
        disk_parts.append('<article class="disk-detail"><h3>' + esc(label) + '</h3><p>Storage location: <strong>' + esc(disk["storage"] or "Directly attached") + '</strong></p><p>Assigned space: <strong>' + human(disk["capacity_bytes"]) + '</strong> · Storage used: <strong>' + human(disk["actual_bytes"]) + '</strong></p>' +
                          ('<p class="muted">Not included in the storage total because this is setup media or an unattached disk.</p>' if not disk["counted"] else '') +
                          ('<p class="muted">Usage could not be measured. See Data availability below.</p>' if disk["actual_bytes"] is None and disk["counted"] else '') +
                          ('<span class="badge warn">This disk is excluded from backups</span>' if disk["backup_disk"] is False and disk["counted"] else '') + '</article>')
    disk_html = ''.join(disk_parts) or '<p class="muted">No disk details were available for this machine.</p>'
    if vm["filesystems"]:
        inside_html = ''.join('<article class="disk-detail"><h3>Drive or folder: ' + esc(f["mountpoint"]) + '</h3><p>Space used: <strong>' + human(f["used_bytes"]) + '</strong><br>Space free: <strong>' + human(f["free_bytes"]) + '</strong><br>Total size: <strong>' + human(f["total_bytes"]) + '</strong></p></article>' for f in vm["filesystems"])
    else:
        inside_html = '<p class="muted">Drive-level space information was not available. The machine may be off, its reporting helper may be unavailable, or this check may have been skipped. The exact collection message is in IT Reference below.</p>'
    availability = []
    if vm["actual_bytes"] is None:
        availability.extend(allocation_vm_reason(vm))
    for key, text in (("configured_bytes", "A full assigned-disk-space total could not be calculated."),
                      ("ram_bytes", "Assigned memory could not be read."),
                      ("memory_used_bytes", "Current memory use could not be read."),
                      ("vcpus", "The number of assigned virtual CPUs could not be read.")):
        if vm[key] is None:
            availability.append(text)
    if power_state(vm["status"]) == "Unknown":
        availability.append("The machine's current power state could not be confirmed.")
    if vm["backup"] == NA:
        availability.append("It was not possible to confirm whether this machine is selected by a backup schedule.")
    if vm["tags"] is None:
        availability.append("Machine labels could not be read.")
    available_html = '<ul class="reason-list">' + ''.join('<li>' + esc(a) + '</li>' for a in availability) + '</ul>' if availability else '<p>No gaps were found in the main table values for this machine. Drive-level information is described separately above.</p>'
    raw = ['PVE status: ' + display(vm["status"]), vm["filesystem_reason"]] + [disk_text(d) for d in vm["disks"]] + [fs_text(f) for f in vm["filesystems"]] + vm["notes"]
    return '<p><strong>Power:</strong> ' + power_state(vm["status"]) + '</p>' + \
        '<details class="subsection"><summary>Disks and storage</summary><p class="hint">Assigned space is the size given to the machine. Storage used is a separate measurement. ' + report_help_link("assigned", "Explain the difference") + '</p>' + disk_html + '</details>' + \
        '<details class="subsection"><summary>Space inside the VM</summary><p class="hint">Space used and free on its drives or folders. Entries can overlap and are not added together. ' + report_help_link("inside", "How is this measured?") + '</p>' + inside_html + '</details>' + \
        '<details class="subsection"><summary>Backup schedules</summary><p>' + ('Selection could not be confirmed.' if vm["backup"] == NA else 'An active schedule selects this machine.' if vm["backup"] == 'Yes' else 'No active schedule was found selecting this machine.') + '</p><p><strong>Active schedules:</strong> ' + esc(', '.join(vm["enabled_jobs"]) or "None confirmed") + '<br><strong>Inactive schedules:</strong> ' + esc(', '.join(vm["disabled_jobs"]) or "None confirmed") + '</p><p class="hint">Selection does not prove that a backup succeeded. ' + report_help_link("backups", "What does this tell me?") + '</p></details>' + \
        '<details class="subsection"><summary>Memory and processing</summary><p>RAM assigned: <strong>' + human(vm["ram_bytes"]) + '</strong><br>RAM in use: <strong>' + human(vm["memory_used_bytes"]) + '</strong><br>Virtual CPUs assigned: <strong>' + esc(vm["vcpus"]) + '</strong></p>' + report_help_link("memory", "Explain memory") + ' · ' + report_help_link("cpu", "Explain virtual CPUs") + '</details>' + \
        '<details class="subsection"><summary>Data availability</summary>' + available_html + '</details>' + \
        '<details class="subsection"><summary>IT Reference for this VM</summary><p class="hint">Original disk identifiers, measurement sources and collection messages for troubleshooting.</p><ul class="warning-list">' + ''.join('<li>' + esc(item) + '</li>' for item in raw) + '</ul></details>'


def utc_time(value):
    value = number(value)
    if value is None:
        return NA
    try:
        return datetime.fromtimestamp(value, timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return NA


def review_page_html(report):
    esc = lambda value: html.escape(display(value), quote=True)
    items = report.get("review_items", [])
    gaps = [r for r in items if r["category"] == "Data gap"]
    primary = [r for r in items if r["category"] != "Data gap"]
    rows = ''.join('<tr><td><span class="badge">' + esc(r["category"]) + '</span></td><td><strong>' + esc(r["name"]) + '</strong>' +
        ('<br><a href="#machines" data-review-vmid="' + esc(r["vmid"]) + '">Find VM ' + esc(r["vmid"]) + '</a>' if r["vmid"] is not None else '<br>PBS datastore') +
        '</td><td><strong>' + esc(r["title"]) + '</strong><p>' + esc(r["evidence"]) + '</p></td><td>' + esc(r["follow_up"]) + '</td></tr>' for r in primary)
    table = '<div class="table-wrap"><table><thead><tr><th>Type</th><th>Machine or datastore</th><th>What was found</th><th>Suggested follow-up</th></tr></thead><tbody>' + rows + '</tbody></table></div>' if rows else '<p>No capacity or backup-scope findings met the review rules. Measurement gaps, if present, are listed below.</p>'
    if gaps:
        table += '<details class="subsection"><summary>Measurement and access gaps — ' + str(len(gaps)) + ' findings</summary><p>These are limits in the collected information, not proof of a storage failure. Expand a machine for details.</p>' + ''.join('<details class="disk-detail"><summary>' + esc(r['name']) + ' — ' + esc(r['title']) + '</summary><p>' + esc(r['evidence']) + '</p><a href="#machines" data-review-vmid="' + esc(r['vmid']) + '">Find VM ' + esc(r['vmid']) + '</a></details>' for r in gaps) + '</details>'
    return '<p class="eyebrow">Suggested follow-up</p><h2 tabindex="-1">Items to review</h2><p class="page-intro">' + str(len(items)) + ' findings based on the data collected. These are questions to investigate, not instructions to delete machines or backups. Large-machine threshold: ' + human(report.get("large_vm_bytes", 500*1024**3)) + ' assigned disk space. Backup filesystem review threshold: ' + esc(report.get("pbs_warning_percent", 90)) + '% used.</p><section class="panel">' + table + '</section>'


def breakdown_page_html(report):
    esc = lambda value: html.escape(display(value), quote=True)
    tables = []
    for grouping, title, note in (("host", "Storage by host server", "Host rows include local disks only. Shared disks have their own row so they are not counted on every host. A host with no local disks may still use shared storage. Machine counts can repeat in the shared row."),
                                  ("storage", "Storage by location", "Local storage with the same name on different hosts is listed separately. Shared disks are counted once. A machine with disks in several locations can appear in more than one machine count.")):
        rows = ''.join('<tr><td><strong>' + esc(r["name"]) + '</strong><small>' + esc(r["scope"]) + '</small></td><td>' + esc(r["host"]) + '</td><td>' + str(r["vm_count"]) + '</td><td>' + human(r["configured_bytes"]) + ('<small>Known subtotal: ' + human(r["known_configured_bytes"]) + '</small>' if r["configured_bytes"] is None else '') + '</td><td>' + human(r["actual_bytes"]) + '</td><td>' + str(r["measured_disk_count"]) + ' of ' + str(r["unique_disk_count"]) + ' identified disks</td></tr>' for r in report.get("storage_breakdowns", []) if r["grouping"] == grouping)
        tables.append('<section class="panel"><h2>' + title + '</h2><p class="hint">' + note + '</p><div class="table-wrap"><table><thead><tr><th>Host or storage location</th><th>Host scope</th><th>Machines</th><th>Assigned space</th><th>Measured usage</th><th>Measurements available</th></tr></thead><tbody>' + rows + '</tbody></table></div></section>')
    return ''.join(tables) + '<p class="hint">These are attached VM disk totals, not whole-pool capacity or PBS usage. N/A totals can reflect missing settings or different measurement methods. Storage aliases pointing at the same underlying disks cannot be detected automatically.</p>'


def cleanup_result(job):
    state = (job or {}).get("last-run-state")
    if not state:
        return "Not available"
    state = str(state)
    if state == "OK":
        return "Succeeded"
    if "fail" in state.lower() or "error" in state.lower():
        return "Failure reported; ask IT to inspect the task"
    return str(state)


def gc_measurement(gc, key):
    # PBS can return a default all-zero struct before any GC statistics exist.
    return number((gc or {}).get(key)) if (gc or {}).get('upid') else None


def pbs_inventory_html(report):
    esc = lambda x: html.escape(display(x), quote=True)
    groups = report.get('pbs', {}).get('groups', [])
    columns = [('store','Datastore'),('namespace','Namespace'),('backup_type','Type'),('backup_id','ID'),('snapshot_count','Snapshots'),('protected_count','Protected'),('oldest_backup_time','Oldest backup'),('latest_backup_time','Latest backup'),('age_hours','Age (hours)'),('freshness','Freshness'),('verification_state','Latest verification'),('older_verification_failures','Older verification failures'),('latest_logical_bytes','Latest logical size'),('matching_pve','PVE relationship')]
    rows=[]
    for g in groups:
        cells=[]
        for key,label in columns:
            value=g.get(key)
            text = 'Root' if key=='namespace' and value=='' else utc_time(value) if key.endswith('_time') else human(value) if key.endswith('_bytes') else f'{value:.1f}' if key=='age_hours' and value is not None else display(value)
            cells.append('<td data-pbs-value="'+esc(value)+'" data-unknown="'+str(value is None).lower()+'">'+esc(text)+'</td>')
        cells.append('<td><details><summary>Retention rules</summary><p>PBS policies are shown separately; disabled policies do not run. PVE job rules, if collected, are on PVE Backup Jobs. This is not a deletion prediction.</p><pre>'+esc(json.dumps(g.get('pbs_prune_policies'),indent=2))+'</pre></details></td>')
        rows.append('<tr>'+''.join(cells)+'</tr>')
    return '<section class="panel" id="pbs-inventory"><h2>Backup inventory</h2><p>Logical backup sizes are not physical PBS consumption. PVE ownership is unconfirmed; matching an ID alone is insufficient. Verification is not a restore test.</p><label>Search datastore, namespace, type, ID or status <input id="pbs-search" type="search"></label><p id="pbs-count" aria-live="polite"></p><div class="table-wrap"><table><thead><tr>'+''.join('<th><button type="button" data-pbs-col="'+str(i)+'">'+label+'</button></th>' for i,(key,label) in enumerate(columns))+'<th>Retention</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div><button type="button" id="pbs-prev">Previous</button> <button type="button" id="pbs-next">Next</button><p>50 groups per page. All collected records are in CSV/JSON. Search includes freshness and verification status.</p></section>'


def pbs_extra_html(report):
    esc=lambda x:html.escape(display(x),quote=True)
    output=[]
    for store in report.get('pbs',{}).get('datastores',[]):
        output.append('<section class="panel"><h2>'+esc(store['store'])+' — evidence and history</h2><p>Backend: '+esc(store.get('backend_type'))+'</p>')
        for kind, records in store.get('cleanup_history', {}).items():
            latest=records.get('latest_attempt')
            success=records.get('latest_success')
            status=('Running or completion unavailable' if latest and latest.get('endtime') is None else latest.get('status','Unavailable') if latest else 'No matching task found in checked history')
            output.append('<p><strong>'+esc(kind.replace('_',' '))+'</strong>: '+esc(status)+'; latest confirmed success: '+utc_time(success.get('endtime') if success else None)+'</p>')
        for title,key in [('Cleanup attempts and confirmed successes','cleanup_history'),('Task-history coverage','task_history'),('Verification jobs and recorded results','verification_jobs')]:
            output.append('<details><summary>'+title+'</summary><pre>'+esc(json.dumps(store.get(key),indent=2))+'</pre></details>')
        history=store.get('capacity_history',[])
        output.append('<h3>Capacity history</h3><p>Change in reported used space since previous reading: '+human(store.get('change_since_previous_bytes'))+'. Readings describe the same capacity scope, not per-VM usage.</p>')
        if len(history)>1:
            maximum=max(h['used_bytes'] for h in history) or 1
            points=' '.join(f'{i*600/(len(history)-1):.1f},{100-h["used_bytes"]*95/maximum:.1f}' for i,h in enumerate(history))
            output.append('<svg viewBox="0 0 600 110" role="img" aria-label="Reported used bytes at successive valid readings">'+''.join('<circle fill="currentColor" r="3" cx="'+point.split(',')[0]+'" cy="'+point.split(',')[1]+'"/>' for point in points.split())+'</svg><p>Points are successive valid readings, not a continuous timeline. See dates below for collection gaps.</p>')
        else:
            output.append('<p>History is disabled or more comparable readings are needed.</p>')
        output.append('<details><summary>History values and dates</summary><pre>'+esc(json.dumps(history,indent=2))+'</pre></details></section>')
    output.append('<section class="panel"><h2>Scheduled VM backup visibility</h2>'+record_table(report.get('pbs',{}).get('backup_coverage',[]), [('vmid','VM'),('job','Backup job'),('datastore','Destination'),('namespace','Namespace'),('status','Evidence')])+'<p>ID matches alone do not prove ownership.</p></section>')
    output.append('<section class="panel"><h2>Connection and visibility checks</h2><pre>'+esc(json.dumps(report.get('pbs',{}).get('checks',[]),indent=2))+'</pre></section>')
    return ''.join(output)


def pbs_page_html(report):
    esc = lambda value: html.escape(display(value), quote=True)
    pbs = report.get("pbs", {})
    status = pbs.get("status", "not requested")
    intro = '<p class="eyebrow">Separate backup server</p><h2 tabindex="-1">PBS Backup Storage</h2>'
    if not pbs.get("requested"):
        return intro + '<section class="panel"><h2>PBS reporting was skipped</h2><p>It is off by default. Choose include at the optional PBS prompt or add <code>--pbs</code> next time to collect backup-server information. No PBS credentials are needed when skipped.</p><p>This report still includes the PVE inventory and review findings. PBS capacity and cleanup results are unavailable.</p></section>'
    out = [intro, '<p>PBS server: ' + esc(pbs.get('server')) + '</p>', '<p class="page-intro">Collection status: <strong>' + esc(status) + '</strong>. Capacity comes from PBS itself, not from adding VM disk sizes. ' + report_help_link("pbs", "Explain backup storage") + '</p>']
    for store in pbs.get("datastores", []):
        total, used = store["total_bytes"], store["used_bytes"]
        percent = f"{100*used/total:.1f}%" if total and used is not None else NA
        gc = store.get("gc") or {}
        location = store.get('location_evidence', {})
        if location:
            out.append('<section class="panel"><h3>Backing storage observed on ' + esc(location.get('ssh_host')) + '</h3>' + record_table(location.get('filesystems', []), [('target','Mount point'),('source','Device, dataset or share'),('fstype','Filesystem'),('uuid','Filesystem ID')]) + '</section>')
        if store.get('location_error'):
            out.append('<p>' + esc(store['location_error']) + '</p>')
        cards = [('Total filesystem capacity', human(total)), ('Used on that filesystem', human(used)), ('Available on that filesystem', human(store['available_bytes'])), ('Used / total', percent)]
        out.append('<section class="panel"><h2>Datastore: ' + esc(store["store"]) + '</h2><p>Configured folder on PBS: <strong>' + esc(store.get('backing_scope')) + '</strong></p><p>Backend: ' + esc(store.get('backend_type')) + '. The backing filesystem or device has not been independently identified.</p><div class="cards">' + ''.join('<div class="card"><div class="label">' + label + '</div><div class="value">' + value + '</div></div>' for label, value in cards) + '</div><p class="hint">PBS reports the underlying filesystem. Other data or another datastore may share it, so do not add datastore capacities or interpret all used bytes as this datastore alone.</p>')
        out.append('<div class="two-col"><article><h3>Backup inventory</h3><p>' + display(store["visible_group_count"]) + ' visible backup groups · ' + display(store["listed_snapshot_count"]) + ' snapshot records read.</p><p>' + ('Inventory queries completed within visible scope.' if store['inventory_complete'] else 'Inventory is incomplete; some namespaces, groups or snapshots could not be read.') + '</p><p class="hint">A backup group represents a machine or another backup source. A snapshot is one saved backup version. Namespaces are folders used to organize groups.</p></article><article><h3>Space cleanup</h3><p>Last garbage collection: <strong>' + esc(cleanup_result(gc)) + '</strong><br>Last completion: ' + utc_time(gc.get('last-run-endtime')) + '<br>Bytes removed in recorded GC statistics: ' + human(gc_measurement(gc, 'removed-bytes')) + '<br>Bytes waiting under safety rules: ' + human(gc_measurement(gc, 'pending-bytes')) + '</p><p class="hint">Pruning removes old backup references. Garbage collection later reclaims eligible unreferenced data. Recorded GC statistics may be older than the latest job result; pending bytes are not space that can necessarily be freed immediately.</p></article></div>')
        out.append('<div class="subsection"><h3>Retention and prune jobs</h3><p>Datastore-level rules: ' + esc(retention_description({"prune-backups": {k: v for k, v in (store.get('datastore_retention') or {}).items() if k.startswith('keep-')}}) if store.get('datastore_retention') is not None else 'Not available') + '</p>')
        configs = {j.get('id'): j for j in store.get('prune_config') or []}
        statuses = {j.get('id'): j for j in store.get('prune_status') or []}
        if not configs and not statuses:
            out.append('<p>No prune jobs were returned, or access was unavailable. This does not establish that no other retention rules apply.</p>')
        else:
            rows = []
            for key in sorted(set(configs) | set(statuses), key=str):
                config, state = configs.get(key, {}), statuses.get(key, {})
                combined = dict(state, **config)
                rules = {k: v for k, v in combined.items() if k.startswith('keep-')}
                out_enabled = 'No' if str(combined.get('disable', 0)).lower() in ('1', 'true') else 'Yes'
                rows.append('<tr><td>' + esc(key) + '</td><td>' + out_enabled + '</td><td>' + esc(combined.get('ns') or 'Root') + '</td><td>' + esc(combined.get('schedule')) + '</td><td>' + esc(retention_description({'prune-backups': rules})) + '</td><td>' + esc(cleanup_result(state)) + '<small>' + utc_time(state.get('last-run-endtime')) + '</small></td></tr>')
            out.append('<div class="table-wrap"><table><thead><tr><th>Prune job</th><th>Enabled</th><th>Namespace</th><th>Schedule</th><th>Keep rules</th><th>Last result</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')
        out.append('<p class="hint">Job rules can be limited to a namespace or depth, and snapshots may be protected. PVE jobs may also apply retention. Review all settings together rather than treating one rule as the full policy.</p></div><details class="subsection"><summary>IT Reference for this datastore</summary><pre>' + esc(json.dumps({k: store.get(k) for k in ('datastore_retention', 'prune_config', 'prune_status', 'gc', 'recent_cleanup_tasks')}, indent=2)) + '</pre></details></section>')
    pve_ids = {str(v['vmid']) for v in report['vms']}
    out.append(pbs_inventory_html(report))
    out.append(pbs_extra_html(report))
    out.append(pbs_jobs_html(report))
    out.append('<section class="panel"><details><summary>PBS collection messages · ' + str(len(pbs.get('warnings', []))) + '</summary><ul class="warning-list">' + (''.join('<li>' + esc(w) + '</li>' for w in pbs.get('warnings', [])) or '<li>No request failures were recorded. Permissions may still limit visible data.</li>') + '</ul></details></section>')
    return ''.join(out)


def measured_total_label(totals):
    value = totals[1]
    if value['complete_coverage']:
        return human(value['total_bytes']) + ' · Complete'
    groups = [r for r in totals[2:] if r['known_subtotal_bytes'] is not None]
    if len(groups) == 1:
        return human(groups[0]['known_subtotal_bytes']) + ' · Partial'
    if groups:
        return '; '.join(r['metric'].replace('Allocation subtotal: ', '') + ': ' + human(r['known_subtotal_bytes']) for r in groups)
    return 'No Measurements Collected'


def title_controls(markup):
    def title(match):
        label = re.sub(r'(^|\s)([a-z])', lambda m: m[1] + m[2].upper(), match[2])
        return match[1] + label + match[3]
    return re.sub(r'(<button\b[^>]*>|<a\b[^>]*class="button"[^>]*>)([^<]+)(</button>|</a>)', title, markup)


def management_body(report):
    markup = _management_body(report)
    if report.get('scope', {}).get('pbs_only'):
        return markup
    counts = report['allocation_diagnostics']
    note = ('<p class="management-note">Full collected scope. ' + str(counts['measured_disks']) +
            ' of ' + str(counts['identified_disks']) + ' identified disks measured. ' +
            html.escape(' '.join(counts['reasons'])) +
            ' <a href="inventory.html#storage">View Missing Measurements</a></p>')
    return markup.replace('<h2>Priority questions</h2>', note + '<h2>Priority questions</h2>')


def _management_body(report):
    esc = lambda value: html.escape(display(value), quote=True)
    totals = report['storage_totals']
    if report.get('scope', {}).get('pbs_only'):
        return '<article class="management-sheet"><h1>PBS backup review</h1><p>PVE inventory was not requested. Collected '+esc(report['generated_at'])+'</p>'+''.join('<h2>'+esc(d['store'])+'</h2><p>Reported used: '+human(d['used_bytes'])+'; available: '+human(d['available_bytes'])+'</p><p>Visible groups: '+esc(d['visible_group_count'])+'; snapshot records: '+esc(d['listed_snapshot_count'])+'</p>' for d in report.get('pbs',{}).get('datastores',[]))+'<p>Filesystem capacities may overlap. See the full report for cleanup, protection, verification and data gaps.</p></article>'
    try:
        collected = datetime.fromisoformat(report['generated_at']).astimezone(timezone.utc).strftime('%b %d, %Y at %H:%M UTC')
    except (ValueError, TypeError):
        collected = report['generated_at']
    pbs = report.get('pbs', {})
    vms = report['vms']
    # Keep a concise overview, allowing readable pagination for long content.
    findings = sorted(report.get('review_items', []), key=lambda r: {'Cleanup': 0, 'Capacity': 1, 'Review': 2, 'Data gap': 3}.get(r['category'], 4))
    findings_html = ''.join('<li><strong>' + esc(r['name']) + ': ' + esc(r['title']) + '.</strong> ' + esc(r['follow_up']) + '</li>' for r in findings[:5]) or '<li>No findings met the review rules. This does not confirm backup health.</li>'
    metric = report['sort_metric'] + '_bytes'
    top = [v for v in vms if v[metric] is not None][:5]
    top_rows = ''.join('<tr><td>' + esc(v['name']) + ' (#' + esc(v['vmid']) + ')</td><td>' + human(v[metric]) + '</td><td>' + power_state(v['status']) + '</td><td>' + ('Unknown' if v['backup'] == NA else v['backup']) + '</td></tr>' for v in top)
    capacity = '; '.join(s['store'] + ': ' + human(s['used_bytes']) + ' used, ' + human(s['available_bytes']) + ' available' for s in pbs.get('datastores', [])[:3]) or 'No PBS capacity data collected.'
    return '<article class="management-sheet"><p class="meta">' + esc(('SELECTED VMS ONLY' if report.get('scope', {}).get('targeted') else 'All visible VMs') + ' | Run ' + report.get('run', {}).get('run_id', 'N/A') + ' | ' + report.get('run', {}).get('status', '')) + '</p><p class="eyebrow">Management Summary · ProxInfo</p><h1>VM storage and backup review</h1><p class="meta">Collected ' + esc(collected) + '</p><div class="management-metrics"><div><strong>' + str(len(vms)) + '</strong><span>machines reviewed</span></div><div><strong>' + human(totals[0]['total_bytes']) + '</strong><span>assigned disk space</span></div><div><strong>' + esc(measured_total_label(totals)) + '</strong><span>measured VM disk usage</span></div><div><strong>' + str(sum(v['backup'] == 'Yes' for v in vms)) + ' of ' + str(len(vms)) + '</strong><span>scheduled for backup</span></div></div><p class="management-note">Assigned space is disk capacity given to VMs, including empty space. Measured usage includes available disk readings. Partial totals exclude unreadable disks. N/A means unavailable, not zero. Scheduled does not mean the backup succeeded.</p><h2>Priority questions</h2><ol>' + findings_html + '</ol><p class="management-note">Showing up to 5 of ' + str(len(findings)) + ' findings. Large-machine review threshold: ' + human(report.get('large_vm_bytes', 500*1024**3)) + ' assigned.</p><h2>Five largest machines</h2><table><thead><tr><th>Machine</th><th>' + ('Assigned space' if metric == 'configured_bytes' else 'Measured usage') + '</th><th>Power</th><th>Scheduled backup</th></tr></thead><tbody>' + top_rows + '</tbody></table><p>Partial storage measurements: ' + esc('; '.join(str(v['vmid']) + ': ' + measurement_label(v) for v in vms if measurement_details(v)['measurement_status'] == 'partial') or 'None') + '</p><h2>Backup server</h2><p>PBS collection: <strong>' + esc(pbs.get('status', 'not requested')) + '</strong>. ' + esc(capacity) + '</p><p class="management-note">Up to 3 datastores shown. Capacities describe underlying filesystems and may overlap. Do not add datastore figures together. VM disk sizes and logical backup sizes do not establish physical PBS consumption.</p><p class="management-note">Next step: confirm backup scope, retention rules, and recent prune/garbage-collection results with IT. No configurations or backups were changed. See inventory.html for the full report and measurement gaps.</p></article>'


def management_summary_html(report):
    return title_controls(_management_summary_html(report))


def _management_summary_html(report):
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ProxInfo management summary</title><style>' + HTML_STYLE + '</style></head><body><main class="shell"><div class="print-toolbar"><button class="button" onclick="window.print()">Print / Save As PDF</button> <a href="inventory.html">Open Full Report</a></div>' + management_body(report) + '</main></body></html>'


def compare_inventory(report, previous):
    if previous is None:
        return dict(status='No previous report selected', items=[])
    old = json.loads(Path(previous).read_text(encoding='utf-8'))
    selection = lambda r: {k:r.get('scope', {}).get(k) for k in ('targeted','vmids','nodes','pbs_only')}
    if selection(old) != selection(report) or {n.get('node') for n in old.get('nodes') or []} != {n.get('node') for n in report.get('nodes') or []}:
        return dict(status='Collection scopes differ; comparison withheld', items=[])
    before = {v['vmid']:v for v in old.get('vms', [])}
    after = {v['vmid']:v for v in report.get('vms', [])}
    items = []
    for vmid in sorted(before.keys() | after.keys()):
        if vmid not in before:
            items.append(dict(vmid=vmid, change='Newly visible in this report'))
        elif vmid not in after:
            items.append(dict(vmid=vmid, change='Not visible this run; deletion is unconfirmed'))
        else:
            a,b = before[vmid],after[vmid]
            for key in ('node','configured_bytes','ram_bytes','vcpus','actual_bytes'):
                if a.get(key) is None or b.get(key) is None:
                    continue
                if key == 'actual_bytes' and ({json.dumps(d.get('identity')) for d in unique_disks(a.get('disks', []))} != {json.dumps(d.get('identity')) for d in unique_disks(b.get('disks', []))} or a.get('actual_method') != b.get('actual_method') or not b.get('actual_method')):
                    continue
                if a[key] != b[key]:
                    items.append(dict(vmid=vmid, change=key, previous=a[key], current=b[key]))
    return dict(status='Compared selected reports; VM ID reuse and access changes can affect interpretation', previous_date=old.get('generated_at'), items=items)


def record_table(records, columns):
    esc = lambda x: html.escape(display(x), quote=True)
    if not records:
        return '<p>No records were returned in the checked scope.</p>'
    return '<div class="table-wrap"><table><thead><tr>' + ''.join('<th>'+esc(label)+'</th>' for key,label in columns) + '</tr></thead><tbody>' + ''.join('<tr>'+''.join('<td>'+esc(r.get(key))+'</td>' for key,label in columns)+'</tr>' for r in records) + '</tbody></table></div>'


def changes_html(report):
    changes = report.get('changes') or dict(status='No previous report selected', items=[])
    return '<h2>Changes between reports</h2><p>'+html.escape(changes['status'])+'</p><p>Compare reports from the same environment. Byte fields are exact byte counts. Missing measurements are never treated as zero.</p>'+record_table(changes['items'], [('vmid','VM ID'),('change','Changed field'),('previous','Previous'),('current','Current')])


def pbs_jobs_html(report):
    parts = ['<section class="panel"><h2>Backup attempts and secondary copies</h2><p>Results describe visible tasks. Successful synchronization is not a restore test or proof that every backup has a second copy.</p>']
    for store in report.get('pbs', {}).get('datastores', []):
        parts.append('<h3>'+html.escape(store['store'])+'</h3>')
        attempts = sorted(store.get('backup_attempts', []), key=lambda t:t.get('starttime',0), reverse=True)
        rows = [dict(source=t.get('worker_id'), started=utc_time(t.get('starttime')), result=t.get('status', 'Running or completion unavailable'), duration=t['endtime']-t['starttime'] if t.get('endtime') is not None and t.get('starttime') is not None else None) for t in attempts]
        success = next((t for t in attempts if t.get('status') == 'OK' and t.get('endtime') is not None), None)
        parts.append('<p>Most recent confirmed successful backup task in checked history: ' + utc_time(success.get('endtime') if success else None) + '. This date does not establish coverage for every VM.</p>')
        parts.append(record_table(rows,[('source','Backup source'),('started','Attempt started'),('result','Result'),('duration','Duration (seconds)')]))
        parts.append('<h3>Sync jobs</h3>'+ ('<p>Sync-job visibility unavailable.</p>' if store.get('sync_jobs') is None else record_table(store['sync_jobs'],[('id','Job'),('remote','Remote'),('remote-store','Remote datastore'),('schedule','Schedule'),('last-run-state','Last result')])) )
    groups = report.get('pbs', {}).get('groups', [])
    candidates = [dict(store=g['store'], group=g['backup_type']+'/'+g['backup_id'], namespace=g['namespace'], snapshots=g.get('snapshot_count'), protected=g.get('protected_count'), policies='No visible enabled PBS prune job' if g.get('pbs_prune_policies') is not None and not any(not j.get('disabled') for j in g['pbs_prune_policies']) else 'Review applicable policies') for g in groups if g.get('protected_count') or g.get('pbs_prune_policies') == []]
    parts.append('<h3>Retention review candidates</h3><p>Protected versions and absent visible prune policies deserve review. Other retention mechanisms may apply. These are not deletion recommendations or estimates of recoverable space.</p>'+record_table(candidates, [('store','Datastore'),('namespace','Namespace'),('group','Group'),('snapshots','Versions'),('protected','Protected'),('policies','Policy visibility')]))
    parts.append('<h3>Operator-recorded restore tests</h3><p>Supplied locally by an operator; ProxInfo has not performed or independently verified these tests.</p>'+record_table(report.get('restore_tests', []), [('server','PBS server'),('datastore','Datastore'),('source','Source'),('date','Test date'),('result','Result'),('notes','Notes')])+'</section>')
    return ''.join(parts)


def pbs_location_details(report, config, args, tracker, existing=None):
    host = existing.host if existing else config.get('pbs_ssh_host')
    if not host or not report.get('pbs', {}).get('requested'):
        return
    client = existing or SSHClient(host, user=config.get('pbs_ssh_user', 'root'),
        port=config.get('pbs_ssh_port'), identity=config.get('pbs_ssh_key'),
        timeout=args.pbs_timeout, batch=args.no_prompt, authentication='auto')
    try:
        if not existing:
            client.connect()
        client = ObservedClient(client, tracker)
        for store in report['pbs'].get('datastores', []):
            path = store.get('backing_scope')
            if path and path.startswith('/'):
                try:
                    result = json.loads(client.backend_command(['findmnt', '--json', '--target', path,
                        '--output', 'SOURCE,TARGET,FSTYPE,UUID', '--first-only']))
                    store['location_evidence'] = dict(ssh_host=host, filesystems=result.get('filesystems', []))
                except (ReadError, ValueError, OSError) as exc:
                    store['location_error'] = str(exc)
    except (ReadError, ValueError, OSError) as exc:
        report['pbs']['warnings'].append('Optional PBS filesystem lookup unavailable: ' + str(exc))
    finally:
        if not existing:
            client.close()


def downloads_html():
    files = [('Host inventory', 'hosts.csv'), ('VM spreadsheet', 'vms.csv'), ('Disk spreadsheet', 'disks.csv'),
             ('Full inventory', 'inventory.json'), ('Printable summary', 'management-summary.html'),
             ('PBS backup groups', 'pbs_groups.csv'), ('PBS snapshots', 'pbs_snapshots.csv')]
    return '<h2>Downloads</h2><p>These files cover the entire collected report, regardless of browser filters. Keep them alongside this HTML file.</p><section class="panel">' + ''.join('<p><a href="' + path + '">' + label + '</a></p>' for label, path in files) + '</section>'


def hosts_page_html(report):
    esc = lambda value: html.escape(display(value), quote=True)
    parts = ['<h2>PVE Hosts</h2><p>Physical host readings at collection time. Assigned VM resources are separate from physical capacity. Missing readings do not establish that a host is offline.</p>']
    for host in report.get('hosts', []):
        info = host.get('details') or {}
        ram = info.get('memory') or {}
        cpu = info.get('cpuinfo') or {}
        parts.append('<section class="panel"><h3>' + esc(host['node']) + '</h3><p>IP: ' + esc(host.get('address')) + ' · Status: ' + esc(host.get('status')) + ' · PVE: ' + esc(info.get('pveversion')) + ' · Uptime (seconds): ' + esc(info.get('uptime')) + '</p><p>CPU: ' + esc(cpu.get('model')) + ' · Logical processors: ' + esc(cpu.get('cpus')) + '</p><p>Physical RAM: ' + human(number(ram.get('total'))) + ' · Used: ' + human(number(ram.get('used'))) + ' · Available: ' + human(number(ram.get('available'))) + '</p><h3>Storage capacity reported on this host</h3><p>These are storage-location capacities, not attached VM disk totals. Shared or overlapping storage may appear on several hosts; these rows are not summed.</p><div class="table-wrap"><table><thead><tr><th>Location</th><th>Type</th><th>Total</th><th>Used</th><th>Available</th><th>Used %</th></tr></thead><tbody>')
        for store in host.get('storage') or []:
            total, used = number(store.get('total')), number(store.get('used'))
            percent = f'{100*used/total:.1f}%' if total and used is not None else NA
            parts.append('<tr>' + ''.join('<td>' + esc(value) + '</td>' for value in (store.get('storage'), store.get('type'), human(total), human(used), human(number(store.get('avail'))), percent)) + '</tr>')
        parts.append('</tbody></table></div>' + ('<p>Storage information unavailable.</p>' if host.get('storage') is None else '') + '</section>')
    if not report.get('hosts'):
        parts.append('<p>Host details were not collected in this report or connection mode.</p>')
    return ''.join(parts)


def html_report(report, include_scopes=True):
    return title_controls(_html_report(report, include_scopes))


def _html_report(report, include_scopes=True):
    esc = lambda value: html.escape(display(value), quote=True)
    vms, totals, diagnostic = report["vms"], report["storage_totals"], report["allocation_diagnostics"]
    count = len(vms)
    measured, identified = diagnostic["measured_disks"], diagnostic["identified_disks"]
    percent = round(100 * measured / identified) if identified else 0
    nodes = sorted({v["node"] for v in vms} | {h["node"] for h in report.get("hosts", [])})
    yes, no, unknown = (sum(v["backup"] == state for v in vms) for state in ("Yes", "No", NA))
    try:
        date = datetime.fromisoformat(report["generated_at"]).astimezone(timezone.utc).strftime("%b %d, %Y at %H:%M UTC")
    except (ValueError, TypeError):
        date = report["generated_at"]
    method_label = lambda method: {"file allocated bytes": "Disk-file measurements", "ZFS usedbydataset": "ZFS storage measurements", "LVM-thin mapped bytes (active volumes)": "Active LVM-thin mapped data", "Ceph RBD image allocated bytes": "Ceph RBD disk allocation"}.get(method, "Other storage measurements")
    card_data = [
        ("Virtual Machines reviewed", str(count), f"Across {len(nodes)} host servers", "Includes " + str(sum(v["template"] for v in vms)) + " saved machine templates. Only machines this account can view are included.", "machines"),
        ("Assigned disk space", human(totals[0]["total_bytes"]), "All disk sizes available" if totals[0]["complete_coverage"] else "Partial data; known space: " + human(totals[0]["known_subtotal_bytes"]), "Disk space made available to the machines, including space they have not filled.", "assigned"),
        ("Measured storage used", esc(measured_total_label(totals)), f"{measured} of {identified} identified disks measured", "Space reported as used by VM disks. Partial totals include the disks successfully measured; see Storage for missing disks and separate measurement types. Guest tools are not required.", "measured"),
        ("Scheduled for backup", f"{yes} of {count}", f"{no} not selected · {unknown} unknown", "Machines selected by an active PVE backup schedule. Successful backups have not been checked.", "backups"),
    ]
    cards = ''.join('<article class="card' + (' primary' if i == 1 else '') + '"><div class="label">' + esc(label) + '</div><div class="value">' + esc(value) + '</div><span class="badge">' + esc(caption) + '</span><p>' + esc(note) + '</p>' + report_help_link(topic, "What does this mean?") + '</article>' for i, (label, value, caption, note, topic) in enumerate(card_data))
    metric = report["sort_metric"] + "_bytes"
    rank_label = "assigned disk space" if report["sort_metric"] == "configured" else "measured storage used"
    top = [v for v in vms if v["vmid"] in report["top_vmids"]]
    largest = max((v[metric] or 0 for v in top), default=0)
    bars = ''.join('<li><div class="top-label"><span><span class="rank">' + str(i+1) + '</span><strong>' + esc(v["name"]) + '</strong> <span class="muted">#' + esc(v["vmid"]) + '</span></span><strong>' + human(v[metric]) + '</strong></div><div class="bar"><span style="width:' + str(round(100*(v[metric] or 0)/largest) if largest else 0) + '%"></span></div></li>' for i, v in enumerate(top)) or '<li>No reliable sizes were available for this ranking.</li>'
    review = ['Start with the largest machines. Ask the owner whether their assigned disk space and backup needs still match their use.']
    if not diagnostic["complete"]:
        review.append("Some storage-used measurements are missing or cannot be added safely. Use Storage measurements to see what is known.")
    if no:
        review.append(f"{no} machines are not selected by the active backup schedules checked here. Confirm their backup arrangements with IT; they may have manual or other backups.")
    if unknown:
        review.append(f"Backup scheduling could not be confirmed for {unknown} machines. Ask IT to check access to the schedule settings.")
    overview = '<p class="callout">' + esc(('SELECTED VMS ONLY' if report.get('scope', {}).get('targeted') else 'All visible VMs') + ' | ProxInfo ' + VERSION + ' | Run ' + report.get('run', {}).get('run_id', 'N/A') + ' | ' + report.get('run', {}).get('status', '')) + '</p><div class="section-head"><div><p class="eyebrow">Summary</p><h2 tabindex="-1">Storage and backup review</h2></div></div><p class="page-intro">A virtual machine is a computer created in software and run on a physical server. This report helps you decide which machines deserve a closer look. ' + report_help_link("machines", "New to virtual machines?") + '</p><div class="cards">' + cards + '</div><div class="two-col"><section class="panel"><p class="eyebrow">Where to start</p><h2>Items to review</h2><ul class="reason-list">' + ''.join('<li>' + esc(item) + '</li>' for item in review) + '</ul><p><a href="#storage">See storage measurements</a> · <a href="#machines">Review the machines</a></p><div class="callout">PBS Backup Storage was not measured. ' + report_help_link("pbs", "How does this relate to the full backup server?") + '</div></section><section class="panel"><p class="eyebrow">Largest first</p><h2>10 largest virtual machines</h2><p class="hint">Ranked by ' + rank_label + '. This is a starting point for review, not a recommendation to remove backups.</p><ol class="top-list">' + bars + '</ol></section></div>'
    gaps = []
    if diagnostic["missing_config_vmids"]:
        gaps.append(f"Disk settings could not be read for {len(diagnostic['missing_config_vmids'])} machines.")
    if diagnostic["unidentified_disk_references"]:
        gaps.append(f"The storage location could not be confirmed for {diagnostic['unidentified_disk_references']} disk references. They are left out of known subtotals to avoid counting them incorrectly.")
    if measured < identified:
        gaps.append(f"Storage-used measurements are available for {measured} of {identified} identified disks. The remaining {identified-measured} disks have no usable measurement.")
    if len(diagnostic["methods"]) > 1:
        gaps.append("The storage systems count usage differently. Their known amounts are shown separately because they cannot safely be combined.")
    if not gaps:
        gaps.append("All identified disks have comparable usage measurements." if diagnostic["complete"] else "No complete set of supported disk usage measurements was available.")
    subtotals = ''.join('<div class="subtotal"><span>' + esc(method_label(t["metric"].removeprefix("Allocation subtotal: "))) + ' · ' + str(t["measured_volumes"]) + ' disks</span><strong>' + human(t["known_subtotal_bytes"]) + '</strong></div>' for t in totals[2:]) or '<p class="muted">No supported usage measurements were returned. This does not mean the machines use zero space.</p>'
    missing = ''.join('<article class="disk-detail"><h3>' + esc(v["name"]) + ' · VM ' + esc(v["vmid"]) + '</h3><ul class="reason-list">' + ''.join('<li>' + esc(r) + '</li>' for r in allocation_vm_reason(v)) + '</ul></article>' for v in vms if v["actual_bytes"] is None) or '<p>No machines are missing their table-level storage-used measurement.</p>'
    storage = '<p class="eyebrow">What could be measured</p><h2 tabindex="-1">Storage measurements</h2><p class="page-intro">Assigned disk size and space actually used are different numbers. ' + report_help_link("measured", "Explain the difference") + '</p><section class="panel"><div class="section-head"><h2>' + ('The full measured total is available' if diagnostic["complete"] else 'Understanding Partial Storage Totals') + '</h2><span class="badge">' + str(measured) + ' of ' + str(identified) + ' disks</span></div><p>Known measurements are included even when some disks could not be measured. Complete means all disk identities and comparable measurements were available.</p><div class="coverage" role="meter" aria-label="Identified disks with a storage-used measurement" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + str(percent) + '"><span style="width:' + str(percent) + '%"></span></div><ul class="reason-list">' + ''.join('<li>' + esc(item) + '</li>' for item in gaps) + '</ul>' + subtotals + '<p class="hint">These amounts cover only the disks listed for each measurement type. Do not add them to the full total again. ' + report_help_link("missing", "What does N/A mean?") + '</p><button id="show-missing" class="button" type="button">View Missing Measurements</button></section><section class="panel"><h2>Machines with unavailable usage</h2><p class="hint">These are gaps in what this report could measure, not proof that a machine or its storage has failed.</p>' + missing + '</section>'
    headings = [("VM ID", "machines"), ("Name", "machines"), ("PVE node", "hosts"), ("Power", "power"), ("Labels", "labels"), ("Assigned disk space", "assigned"), ("Storage used", "measured"), ("RAM assigned", "memory"), ("RAM in use", "memory"), ("vCPUs assigned", "cpu"), ("Scheduled for backup", "backups"), ("VM information", None)]
    headers = ''.join('<th scope="col">' + ('<button type="button" data-col="' + str(i) + '">' + esc(label) + '</button> ' + report_help_link(topic, "?", title="Explain " + label.lower()) if topic else esc(label)) + '</th>' for i, (label, topic) in enumerate(headings))
    rows = []
    for vm in vms:
        values = [vm["vmid"], vm["name"], vm["node"], power_state(vm["status"]), vm["tags"], vm["configured_bytes"], vm["actual_bytes"], vm["ram_bytes"], vm["memory_used_bytes"], vm["vcpus"], "Unknown" if vm["backup"] == NA else vm["backup"]]
        cells = []
        for index, value in enumerate(values):
            label = human(value) if index in (5, 6, 7, 8) else display(value)
            content = esc(label)
            if index == 1:
                content = '<strong>' + content + '</strong>' + ('<small>Saved template</small>' if vm["template"] else '')
            elif index == 3:
                content = '<span class="badge ' + ('good' if value == 'On' else 'warn' if value == 'Unknown' else '') + '" title="PVE status: ' + esc(vm["status"]) + '">' + content + '</span>'
            elif index == 10:
                content = '<span class="badge ' + ('good' if value == 'Yes' else 'warn' if value == 'Unknown' else '') + '">' + content + '</span>'
            elif index == 6 and value is None:
                content = '<strong>' + esc(measurement_label(vm)) + '</strong><details class="metric-reason"><summary>N/A · why?</summary>' + ''.join('<p>' + esc(r) + '</p>' for r in allocation_vm_reason(vm)) + '</details>'
            elif value is None:
                content += '<small>' + report_help_link("missing", "Why unavailable?") + '</small>'
            unavailable = value is None or (index in (3, 10) and value == "Unknown")
            cells.append('<td data-partial="' + str(index == 6 and measurement_details(vm)['measurement_status'] == 'partial').lower() + '" data-value="' + esc(value) + '" data-missing="' + str(unavailable).lower() + '">' + content + '</td>')
        cells.append('<td><details class="vm-details"><summary>View VM Information</summary>' + vm_information_html(vm) + '</details></td>')
        disk_records = [dict(identity=d["identity"], capacity=str(d["capacity_bytes"]) if d["capacity_bytes"] is not None else None, used=str(d["actual_bytes"]) if d["actual_bytes"] is not None else None, method=d["actual_method"]) for d in unique_disks(vm["disks"])]
        search = ' '.join(display(vm[key]) for key in ("vmid", "name", "node", "tags")).lower()
        klass = ' class="top"' if vm["vmid"] in report["top_vmids"] else ''
        rows.append('<tr' + klass + ' data-search="' + esc(search) + '" data-node="' + esc(vm["node"]) + '" data-config-available="' + str(vm["config_available"]).lower() + '" data-disks="' + esc(json.dumps(disk_records)) + '" data-missing-allocation="' + str(vm["actual_bytes"] is None).lower() + '">' + ''.join(cells) + '</tr>')
    footer = '<tfoot><tr><th colspan="5" scope="row"><strong id="table-total-label">Storage total for all shown machines</strong><small>Shared disks count once</small></th><td id="table-assigned-total">' + human(totals[0]["total_bytes"]) + '</td><td id="table-used-total">' + esc(measured_total_label(totals)) + '</td><td id="table-ram-total">' + human(complete_sum([v['ram_bytes'] for v in vms])) + '</td><td colspan="4"><span id="table-total-note">Totals use the machines currently shown. Missing values are not treated as zero.</span> ' + report_help_link("measured", "Explain these totals") + '</td></tr></tfoot>'
    machines = '<p class="eyebrow">Machine list</p><h2 tabindex="-1">Virtual Machines</h2><p class="page-intro">Each row is a virtual computer or saved machine template. Click a heading to sort, or its ? link for an explanation. ' + report_help_link("units", "Explain the size units") + '</p><section class="panel" id="inventory"><div class="toolbar"><label class="sr-only" for="search">Search name, ID, host or label</label><input id="search" type="search" placeholder="Search name, ID, host or label…"><label class="sr-only" for="node-filter">Filter by host server</label><select id="node-filter"><option value="">All PVE nodes</option>' + ''.join('<option value="' + esc(n) + '">' + esc(n) + '</option>' for n in nodes) + '</select><button id="clear-filters" class="button" type="button">Show all VMs / Clear filters</button><label><input type="checkbox" id="missing-only">Usage not available</label><label><input type="checkbox" id="show-resources" checked>Show memory &amp; CPUs</label></div><p class="meta" id="result-count" aria-live="polite">' + str(count) + ' machines</p><div class="table-wrap"><table id="vms" data-resources="true"><thead><tr>' + headers + '</tr></thead><tbody>' + ''.join(rows) + '</tbody>' + footer + '</table></div><p id="no-results" class="hint" hidden>No machines match these filters.</p><p class="hint">Totals update when you filter. RAM assigned includes the displayed stopped machines and templates; it is not host memory usage. RAM in use is a snapshot at collection time. Assigned space and measured usage are separate totals; neither is PBS backup usage. On small screens, scroll the table sideways to see every column.</p></section>'
    jobs = report["jobs"]
    if jobs is None:
        job_table = '<p>Backup schedule settings could not be read. Ask IT to review access to these settings.</p>'
    elif not jobs:
        job_table = '<p>No backup schedules were visible to the connected account.</p>'
    else:
        job_rows = []
        for j in jobs:
            schedule = esc(schedule_description(j))
            if schedule_description(j) == 'Custom schedule':
                schedule += '<details><summary>View schedule notation</summary><p>' + esc(j.get("schedule", j.get("starttime"))) + '</p>' + report_help_link("schedule", "Help reading schedules") + '</details>'
            job_rows.append('<tr><td>' + esc(j["id"]) + '</td><td>' + ('Yes' if enabled(j.get("enabled", 1)) else 'No') + '</td><td>' + esc(j.get("storage")) + '</td><td>' + schedule + '</td><td>' + esc(retention_description(j)) + ' ' + report_help_link("retention", "Explain") + '<details><summary>IT settings</summary><p>' + esc(json.dumps(j.get("prune-backups", {}))) + '</p></details></td></tr>')
        job_table = '<div class="table-wrap"><table><thead><tr><th>Schedule name</th><th>Active?</th><th>Saved to</th><th>Planned start ' + report_help_link("schedule", "?") + '</th><th>Backups to keep ' + report_help_link("retention", "?") + '</th></tr></thead><tbody>' + ''.join(job_rows) + '</tbody></table></div>'
    backups = '<p class="eyebrow">Backup planning</p><h2 tabindex="-1">Backup schedules</h2><p class="page-intro">These are instructions telling PVE which backups to attempt and when. This report has not checked whether those backups succeeded. ' + report_help_link("backups", "What can I conclude from this?") + '</p><section class="panel">' + job_table + '<p class="hint">A job may use its own rules for keeping old backups or inherit other settings. Ask IT to check the complete PBS policy and recent cleanup results.</p></section>'
    quick_guide = [("assigned", "Disk size is not the same as files stored", "A machine can be assigned a 500 GiB disk while storing far less data. Use assigned size to identify large machines for review."), ("memory", "Memory is not file storage", "Memory is the workspace used while programs are running. More memory does not directly mean larger backups."), ("backups", "Scheduled does not mean successful", "A machine may be selected by a backup job even if the latest backup failed. Backup results need a separate check."), ("missing", "Unavailable does not mean zero", "N/A means the tool could not obtain a reliable number. The report explains what is missing instead of guessing."), ("pbs", "VM sizes do not explain PBS usage by themselves", "The backup server can compress data and share identical data between machines and backup versions. IT must also check retention and cleanup."), ("units", "Sizes use the same units throughout", "GiB and TiB are units of size. They differ from the GB and TB often printed on storage products.")]
    glossary = ''.join('<article id="help-' + topic + '" class="glossary-entry"><h3>' + esc(title) + '</h3><p>' + esc(body) + '</p></article>' for topic, (title, body) in REPORT_HELP.items())
    help_page = '<p class="eyebrow">Plain-language guide</p><h2 tabindex="-1">Understanding the report</h2><p class="page-intro">You do not need to know Proxmox to read this report. Use the Explain and ? links anywhere for help with a specific term.</p><section class="panel"><div class="guide-grid">' + ''.join('<article><h3>' + esc(title) + '</h3><p>' + esc(body) + '</p>' + report_help_link(topic, "Read the explanation") + '</article>' for topic, title, body in quick_guide) + '</div></section><section class="panel"><details id="glossary-details" open><summary>All definitions</summary><div class="guide-grid">' + glossary + '</div></details></section>'
    raw_summary = ''.join('<dt>' + esc(k) + '</dt><dd>' + esc(v) + '</dd>' for k, v in report["summary"].items())
    technical = '<details><summary>Run details and slowest queries</summary><pre>' + esc(run_description(report)) + '</pre></details><p class="eyebrow">For the IT team</p><h2 tabindex="-1">IT Reference</h2><p class="page-intro">Original metrics, collection messages and measurement limits for troubleshooting. Manager-friendly definitions are on the Help page.</p><section class="panel technical"><details><summary>All resource totals and measurement counts</summary><dl>' + raw_summary + '</dl></details><details class="subsection"><summary>Original collection messages · ' + str(len(report["warnings"])) + '</summary><ul class="warning-list">' + (''.join('<li>' + esc(w) + '</li>' for w in report["warnings"]) or '<li>No collection messages were recorded.</li>') + '</ul></details><details class="subsection"><summary>Measurement rules and limitations</summary><ul class="warning-list">' + ''.join('<li>' + esc(item) + '</li>' for item in report["limitations"]) + '</ul></details><p class="meta">PVE version: ' + esc(json.dumps(report["pve_version"])) + '<br>Collection method: ' + esc(report.get("access_mode", "local")) + '</p></section>'
    if report.get("access_mode") == "demo":
        overview = '<p class="callout"><strong>Example report:</strong> all machine names and numbers on these pages are synthetic preview data.</p>' + overview
    pbs_status = report.get('pbs', {}).get('status', 'not requested')
    overview = overview.replace('PBS Backup Storage was not measured.', 'PBS reporting: ' + esc(pbs_status) + '. <a href="#pbs">View backup-server information</a>.')
    overview += '<section class="panel"><h2>Collection status</h2><p>Hosts below include nodes with no visible VMs. A missing storage reading is not proof of a host outage.</p>' + record_table([dict(node=h['node'], status=h.get('status'), host_read='Available' if h.get('details') is not None else 'Unavailable', storage_read='Available' if h.get('storage') is not None else 'Unavailable') for h in report.get('hosts', [])], [('node','Host'),('status','Reported state'),('host_read','Host readings'),('storage_read','Storage readings')]) + '</section>'
    overview += '<section class="panel"><h2>Follow-up and handoff</h2><p>' + str(len(report.get('review_items', []))) + ' findings are listed for review. No changes are made automatically.</p><p><a class="button" href="#review">Open Needs Attention</a> <a class="button" href="#management">Open Management Summary</a></p></section>'
    if not diagnostic['complete']:
        overview += '<p><a class="button" href="#storage">View Missing Measurements</a></p>'
    storage += breakdown_page_html(report)
    management = '<p class="eyebrow">A short handoff</p><h2 tabindex="-1">Management Summary</h2><div class="print-toolbar"><a class="button" href="management-summary.html" target="_blank" rel="noopener">Open Print / PDF View</a><p class="hint">The separate summary has a Print / Save As PDF button. It includes the first five findings and largest machines. Longer summaries flow across pages; select Letter or A4 in the print dialog. The full report retains all records.</p></div>' + management_body(report)
    if report.get('scope', {}).get('pbs_only'):
        overview = '<section class="panel"><h2>PBS-only report</h2><p>PVE inventory and backup ownership matching were not requested. Open PBS Backup Storage for capacity, cleanup, retention, freshness and verification information.</p><a href="#pbs">Open PBS report</a></section>'
    views = [("overview", "Summary", overview), ("review", "Needs Attention", review_page_html(report)),
             ("hosts", "PVE Hosts", hosts_page_html(report)), ("machines", "Virtual Machines", machines), ("storage", "Storage", storage),
             ("backups", "PVE Backup Jobs", backups), ("pbs", "PBS Backup Storage", pbs_page_html(report) if include_scopes else ""),
             ("management", "Management Summary", management), ("downloads", "Downloads", downloads_html()), ("help", "Help", help_page), ("technical", "IT Reference", technical)]
    scope_views = {}
    if include_scopes and not report.get('scope', {}).get('pbs_only'):
        for node in nodes:
            selected = dict(report)
            selected['vms'] = [vm for vm in selected['vms'] if vm['node'] == node]
            selected['hosts'] = [host for host in selected.get('hosts', []) if host['node'] == node]
            selected['scope'] = dict(selected.get('scope', {}), targeted=True, selected=len(selected['vms']))
            try:
                sort_report(selected, report['sort_metric'])
            except ValueError:
                sort_report(selected, 'configured')
            rendered = html_report(selected, include_scopes=False)
            scope_views[node] = {key:content for key,content in re.findall(r'<section class="report-page" id="page-([^" ]+)" data-page="[^" ]+">(.*?)</section>(?=<section class="report-page"|<footer)', rendered, re.S) if key in ('overview','hosts','review','storage')}
    scope_data = json.dumps(scope_views).replace('<', '\\u003c')
    scope_control = '<label class="scope-control">PVE view: <select id="global-node-filter"><option value="">All collected nodes</option>' + ''.join('<option>'+html.escape(n)+'</option>' for n in nodes) + '</select></label><p id="global-scope-note" class="hint">PVE views show all collected nodes. Management Summary, print, PBS and exported files retain their own full collected scope.</p>' if include_scopes and nodes else ''
    nav = ''.join('<a href="#' + key + '" data-page-link="' + key + '">' + label + '</a>' for key, label, _ in views if not report.get('scope',{}).get('pbs_only') or key not in ('machines','storage','backups','hosts'))
    pages = ''.join('<section class="report-page" id="page-' + key + '" data-page="' + key + '">' + content + '</section>' for key, _, content in views)
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ProxInfo · Storage and backup review</title><style>' + HTML_STYLE + '</style></head><body><main class="shell"><header class="masthead"><div class="brand"><div class="brand-mark">PI</div><div><p class="eyebrow">ProxInfo</p><h1>Environment overview</h1></div></div><button class="button" id="theme-toggle" type="button" aria-pressed="false">Dark Mode</button></header><p class="meta">Collected ' + esc(date) + ' · ' + str(len(nodes)) + ' host servers · Read-only report</p><nav class="nav" aria-label="Report pages">' + nav + '</nav>' + scope_control + pages + '<script type="application/json" id="node-view-data">' + scope_data + '</script><footer class="footer"><span>ProxInfo · report layout 4</span><span>N/A means unavailable. Zero means a known zero. ' + report_help_link("pbs", "About backup storage") + '</span></footer></main><dialog id="explanation-dialog" aria-labelledby="explanation-title"><form method="dialog"><button class="button" aria-label="Close explanation">Close</button></form><h2 id="explanation-title"></h2><div id="explanation-content"></div></dialog><script>' + HTML_SCRIPT + '</script></body></html>'


def run_description(report):
    run = report.get("run", {})
    scope = report.get("scope", {})
    return ("PBS ONLY; PVE inventory not requested" if scope.get("pbs_only") else "SELECTED VMS ONLY — " + json.dumps(scope) if scope.get("targeted") else "All visible VMs") + " | " + json.dumps(run, ensure_ascii=False)


def export_report(report, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".writing-", dir=directory) as staging:
        result = _export_report(report, Path(staging))
        write_csv(Path(staging) / "run_info.csv", ["setting", "value"],
                  [dict(setting=k, value=v) for k,v in dict(report.get("run", {}), scope=report.get("scope", {})).items()])
        # Once replacement starts, an old marker must not certify mixed-run files.
        (directory / "report-complete.json").unlink(missing_ok=True)
        for path in Path(staging).iterdir():
            path.replace(directory / path.name)
    marker = directory / '.report-complete.tmp'
    marker.write_text(json.dumps(dict(run_id=report.get('run', {}).get('run_id'), collection_status=report.get('run', {}).get('status'), export_complete=True)), encoding='utf-8')
    marker.replace(directory / 'report-complete.json')
    return result


def _export_report(report, directory):
    directory.mkdir(parents=True, exist_ok=True)
    write_csv(directory / 'hosts.csv', ['node','address','status','details','storage'], report.get('hosts', []))
    write_csv(directory / 'changes.csv', ['vmid','change','previous','current'], report.get('changes', {}).get('items', []))
    terminal = terminal_report(report)
    (directory / "inventory.txt").write_text(terminal, encoding="utf-8")
    (directory / "inventory.html").write_text(html_report(report), encoding="utf-8")
    (directory / "management-summary.html").write_text(management_summary_html(report), encoding="utf-8")
    (directory / "inventory.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    rows = []
    for vm in report["vms"]:
        row = dict(vm, **measurement_details(vm))
        for key in ("configured_bytes", "actual_bytes", "ram_bytes", "memory_used_bytes"):
            row[key.replace("_bytes", "_human")] = human(vm[key])
        row["disks_json"], row["filesystems_json"] = vm["disks"], vm["filesystems"]
        rows.append(row)
    extra = [k.replace("_bytes", "_human") for k in ("configured_bytes", "actual_bytes", "ram_bytes", "memory_used_bytes")]
    write_csv(directory / "vms.csv", VM_COLUMNS + extra + ["measurement_status", "known_actual_bytes", "measured_disk_count", "counted_disk_count", "measured_subtotals", "disks_json", "filesystems_json"], rows)
    disk_columns = ["vmid", "node", "slot", "volume", "storage", "counted", "excluded_reason", "capacity_bytes",
                    "capacity_reason", "actual_bytes", "actual_method", "actual_reason", "backup_disk", "identity", "measurement_status", "measurement_seconds", "cached_measurement"]
    write_csv(directory / "disks.csv", disk_columns,
              [dict(d, vmid=v["vmid"], node=v["node"]) for v in report["vms"] for d in v["disks"]])
    write_csv(directory / "filesystems.csv", ["vmid", "node", "mountpoint", "name", "type", "used_bytes", "free_bytes", "total_bytes", "reason"],
              [dict(f, vmid=v["vmid"], node=v["node"]) for v in report["vms"] for f in v["filesystems"]])
    write_csv(directory / "summary.csv", ["metric", "value"], [dict(metric=k, value=v) for k, v in report["summary"].items()])
    write_csv(directory / "storage_totals.csv", ["metric", "total_bytes", "known_subtotal_bytes", "measured_volumes",
                                                "known_identity_volumes", "complete_coverage", "notes"], report["storage_totals"])
    write_csv(directory / "review_items.csv", ["rule", "category", "vmid", "name", "node", "title", "evidence", "follow_up"], report.get("review_items", []))
    write_csv(directory / "storage_breakdowns.csv", ["grouping", "name", "host", "scope", "vm_count", "unique_disk_count",
        "configured_bytes", "known_configured_bytes", "actual_bytes", "measured_disk_count", "actual_methods", "unidentified_disk_references", "missing_vm_configs"], report.get("storage_breakdowns", []))
    pbs = report.get("pbs", {})
    datastore_rows = [dict(s, gc_last_result=(s.get('gc') or {}).get('last-run-state'),
        gc_last_completed=(s.get('gc') or {}).get('last-run-endtime'), gc_removed_bytes=gc_measurement(s.get('gc'), 'removed-bytes'),
        gc_pending_bytes=gc_measurement(s.get('gc'), 'pending-bytes')) for s in pbs.get('datastores', [])]
    write_csv(directory / "pbs_datastores.csv", ["store", "total_bytes", "used_bytes", "available_bytes", "capacity_scope",
        "namespaces", "inventory_complete", "visible_group_count", "listed_snapshot_count", "datastore_retention", "prune_config",
        "prune_status", "gc_last_result", "gc_last_completed", "gc_removed_bytes", "gc_pending_bytes"], datastore_rows)
    write_csv(directory / "pbs_checks.csv", ["path", "status"], pbs.get("checks", []))
    write_csv(directory / "pbs_groups.csv", ["pbs_prune_policies", "oldest_backup_time", "protected_count", "age_hours", "freshness", "verification_state", "older_verification_failures", "matching_pve","store", "namespace", "backup_type", "backup_id", "snapshot_count", "count_source", "latest_backup_time", "latest_logical_bytes"], pbs.get("groups", []))
    write_csv(directory / "pbs_snapshots.csv", ["store", "namespace", "backup_type", "backup_id", "backup_time", "logical_bytes", "protected", "verification"], pbs.get("snapshots", []))
    return terminal


def diagnostic_secrets(config):
    values = []
    for key, value in config.items():
        if isinstance(value, dict):
            values.extend(diagnostic_secrets(value))
        elif isinstance(value, str) and any(word in key.lower() for word in ('token', 'secret', 'password', 'authorization')):
            values.append(value)
    return values


def redact_diagnostics(value, secrets=()):
    if isinstance(value, dict):
        return {key: redact_diagnostics(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_diagnostics(item, secrets) for item in value]
    if not isinstance(value, str):
        return value
    for secret in sorted(set(filter(None, secrets)), key=len, reverse=True):
        value = value.replace(secret, '[REDACTED]')
    value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[REDACTED KEY]', value, flags=re.S)
    value = re.sub(r'(?i)(?:PVEAPIToken|PBSAPIToken)=[^\s\"\'<>]+', '[REDACTED TOKEN]', value)
    value = re.sub(r'(?i)((?:authorization|cookie|PBSAuthCookie|CSRFPreventionToken|ticket|tfa-challenge)\s*[:=]\s*)[^\r\n]+', r'\1[REDACTED]', value)
    value = re.sub(r'(?i)((?:password|token_secret|secret)\s*[:=]\s*)[^\s,;]+', r'\1[REDACTED]', value)
    return re.sub(r'(https?://)[^/\s@]+@', r'\1[REDACTED]@', value)


def write_diagnostics(report, tracker, root, secrets=()):
    """Explicit allowlist: no credential config, environment dump, guest data or raw API bodies."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / ('storage-' + tracker.run_id)
    destination.mkdir(exist_ok=False)
    disk_keys = ('slot', 'volume', 'storage', 'storage_backend', 'storage_vg', 'storage_shared',
                 'capacity_bytes', 'actual_bytes', 'actual_method', 'actual_reason', 'identity',
                 'counted', 'excluded_reason', 'measurement_status', 'measurement_seconds', 'cached_measurement')
    vms = []
    for vm in report['vms']:
        row = {key: vm.get(key) for key in ('vmid', 'node', 'status', 'cached_status', 'config_available',
                                          'configured_bytes', 'actual_bytes', 'actual_method')}
        row.update(measurement_details(vm))
        row['disks'] = [{key: disk.get(key) for key in disk_keys} for disk in vm['disks']]
        vms.append(row)
    data = redact_diagnostics(dict(bundle_schema=1, tool_version=VERSION,
        generated_at=report['generated_at'], pve_version=report.get('pve_version'),
        scope=report.get('scope'), environment=report.get('environment'), node_routes=report.get("node_routes", []),
        run=report.get('run'), vms=vms), secrets)
    atomic_text(destination / 'storage-diagnostics.json', json.dumps(data, indent=2))
    rows = [dict(vmid=vm['vmid'], node=vm['node'], vm_status=vm['status'], **disk)
            for vm in data['vms'] for disk in vm['disks']]
    write_csv(destination / 'disks.csv', ['vmid', 'node', 'vm_status'] + list(disk_keys), rows)
    tracker.log.flush()
    tracker.stream.flush()
    for name in ('collection.log', 'query_timings.csv'):
        source = tracker.directory / name
        atomic_text(destination / name, redact_diagnostics(source.read_text(encoding='utf-8-sig'), secrets))
    lines = ['ProxInfo storage troubleshooting', 'Run: ' + tracker.run_id,
             'Tool: ' + VERSION, 'PVE version: ' + json.dumps(data['pve_version']),
             'This bundle contains node names, disk identifiers and storage paths. Review before committing.',
             'Credential settings, guest filesystems, VM names/tags and full configurations are excluded.',
             'Known credentials and common credential patterns in error text are redacted.',
             'Collection logs are a snapshot taken after report export.', '']
    for vm in data['vms']:
        lines.append(f"VM {vm['vmid']} on {vm['node']}: {vm['measurement_status']}; {vm['measured_disk_count']}/{vm['counted_disk_count']} disks measured")
        for disk in vm['disks']:
            lines.append(f"  {disk['slot']} | {disk['storage_backend']} | {disk['volume']} | used={human(disk['actual_bytes'])} | {disk['actual_reason']}")
    atomic_text(destination / 'README.txt', '\n'.join(lines) + '\n')
    return destination


def main(argv=None):
    launched = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cron", action="store_true", help="Unattended scheduled run; requires --mode and absolute --output-root")
    parser.add_argument("--version", action="version", version="ProxInfo " + VERSION)
    parser.add_argument("--vmid", action="append", type=int, help="Only collect this VM ID; repeat to select more")
    parser.add_argument("--node", action="append", help="Only collect VMs on this node; repeat to select more")
    parser.add_argument("--progress", choices=("auto", "plain", "off"), default="auto")
    parser.add_argument("--max-runtime", type=float, help="Stop starting queries after this many collection seconds")
    parser.add_argument("--mode", choices=("ssh", "local", "cli", "api"), help="ssh=remote PVE from WSL, local=pvesh, cli=standalone commands, api=HTTPS token")
    parser.add_argument("--ssh-host", help="PVE hostname/IP or ~/.ssh/config alias")
    parser.add_argument("--ssh-user", help="Remote username (default root)")
    parser.add_argument("--ssh-port", type=int, help="SSH port (default from SSH config, normally 22)")
    parser.add_argument("--ssh-key", help="Local SSH private key path (optional)")
    parser.add_argument("--ssh-auth", choices=("auto", "password", "key"), help="SSH sign-in method (interactive prompt if unspecified)")
    parser.add_argument("--ssh-sudo", action="store_true", help="Prefix remote pvesh commands with sudo -n; requires preconfigured noninteractive sudo")
    parser.add_argument("--ssh-auth-timeout", type=float, default=120, help="Seconds allowed for initial SSH authentication (default 120)")
    parser.add_argument("--environment", choices=("auto", "cluster", "standalone"), default="auto")
    parser.add_argument("--no-prompt", action="store_true", help="Use explicit options/detection without startup questions")
    parser.add_argument("--config", type=Path, help="Optional JSON credentials/settings file; environment overrides it")
    parser.add_argument('--diagnostics', nargs='?', const=Path('logs'), type=Path,
                        help='Save a credential-redacted storage troubleshooting bundle (default folder: logs)')
    outputs = parser.add_mutually_exclusive_group()
    outputs.add_argument("--output-dir", type=Path, help="Report folder; existing report and log files are replaced")
    outputs.add_argument("--output-root", type=Path, help="Parent folder for unique run directories")
    parser.add_argument("--previous-report", type=Path, help="Previous inventory.json for a local comparison")
    parser.add_argument("--restore-tests", type=Path, help="Optional local JSON list of operator-recorded restore tests")
    parser.add_argument("--sort", choices=("configured", "actual"), default="configured")
    parser.add_argument("--storage-timeout", type=float, default=60, help="Seconds per storage measurement (default 60); separate from connection/general queries")
    parser.add_argument("--timeout", type=float, default=20, help="Seconds per API/local command (default 20)")
    parser.add_argument("--no-guest-agent", action="store_true", help="Skip guest filesystem queries")
    pbs_flags = parser.add_mutually_exclusive_group()
    pbs_flags.add_argument("--pbs", dest="pbs", action="store_true", default=None, help="Include read-only PBS API reporting")
    pbs_flags.add_argument("--no-pbs", dest="pbs", action="store_false", help="Skip PBS without asking")
    parser.add_argument("--pbs-only", action="store_true", help="Collect PBS without any PVE connection")
    parser.add_argument("--pbs-namespace", action="append", help="Exact namespace; / means root; repeat as needed")
    parser.add_argument("--pbs-timeout", type=float, default=60)
    parser.add_argument("--pbs-task-days", type=int, default=14)
    parser.add_argument("--pbs-task-limit", type=int, default=1000)
    parser.add_argument("--pbs-verification-days", type=float, help="Optional age threshold for recorded verification-job completion")
    parser.add_argument("--pbs-fresh-hours", type=float, help="Optional backup age expectation in hours")
    parser.add_argument("--pbs-history", action="store_true", help="Save local capacity history under --output-root")
    parser.add_argument("--pbs-url", help="PBS HTTPS URL; credentials use PBS_TOKEN_ID / PBS_TOKEN_SECRET")
    parser.add_argument('--pbs-auth', choices=('token', 'password', 'ssh'), help='PBS sign-in method; PBS remains optional')
    parser.add_argument('--pbs-ssh-host', help='PBS SSH hostname, IP or configured alias')
    parser.add_argument('--pbs-ssh-user', help='PBS system account for SSH')
    parser.add_argument('--pbs-ssh-key', help='Local PBS SSH private key path')
    parser.add_argument('--pbs-ssh-auth', choices=('password', 'key', 'auto'), help='PBS SSH authentication method')
    parser.add_argument('--pbs-ssh-port', type=int, help='Override PBS SSH port; otherwise honor SSH configuration')
    parser.add_argument('--pbs-ca-file', help='Trusted PBS CA certificate for HTTPS')
    parser.add_argument('--pbs-username', help='PBS web account including realm (password is prompted or supplied separately)')
    parser.add_argument("--pbs-store", action="append", help="Only inspect this PBS datastore (repeat for more); default all visible")
    parser.add_argument("--large-vm-gib", type=float, default=500, help="Assigned-size review threshold in GiB (default 500)")
    parser.add_argument("--pbs-warning-percent", type=float, default=90, help="PBS filesystem capacity review threshold (default 90 percent)")
    args = parser.parse_args(argv)
    if not all(0 < value < float("inf") for value in (args.timeout, args.ssh_auth_timeout, args.storage_timeout)):
        parser.error("Timeouts must be finite and positive")
    if not 0 < args.large_vm_gib < float('inf') or not 0 < args.pbs_warning_percent <= 100:
        parser.error("Review size must be positive and PBS review percentage must be in (0,100]")
    if args.max_runtime is not None and not 0 < args.max_runtime < float("inf"):
        parser.error("--max-runtime must be finite and positive")
    if args.vmid and any(v <= 0 for v in args.vmid):
        parser.error("VM IDs must be positive")
    if args.pbs_only:
        if args.pbs is False or args.vmid or args.node or args.mode:
            parser.error("--pbs-only cannot combine with --no-pbs, PVE selectors, or --mode")
        args.pbs = True
    if not 0 < args.pbs_timeout < float('inf') or args.pbs_task_days <= 0 or args.pbs_task_limit <= 0:
        parser.error("PBS timeout/history limits must be positive and finite")
    if args.pbs_verification_days is not None and not 0 < args.pbs_verification_days < float("inf"):
        parser.error("Verification days must be positive and finite")
    if args.pbs_fresh_hours is not None and not 0 < args.pbs_fresh_hours < float('inf'):
        parser.error("PBS freshness hours must be positive and finite")
    if args.pbs_history and (args.output_root is None or args.pbs is False):
        parser.error("--pbs-history requires --output-root and enabled PBS")
    if args.cron:
        if not (args.mode or args.pbs_only) or args.output_root is None or not args.output_root.is_absolute():
            parser.error("--cron requires --mode and an absolute --output-root")
        if args.ssh_auth == "password":
            parser.error("Cron cannot answer SSH password prompts; use a key, API token, or local mode")
        args.no_prompt = True
        if args.progress == "auto":
            args.progress = "plain"
        if args.pbs is None:
            args.pbs = False
        if args.max_runtime is None:
            args.max_runtime = 3600
    run_folder = "pve-report-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    args.output_dir = ((args.output_root / run_folder) if args.output_root is not None else args.output_dir or Path(run_folder))
    ssh_client = None
    tracker = None
    cron_lock = None
    cron_status = "failed"
    try:
        if args.cron:
            args.output_root.mkdir(parents=True, exist_ok=True)
            cron_lock = CronLock(args.output_root / ".proxinfo.lock")
            args.output_dir.mkdir()
            tracker = RunTracker(args.output_dir, args.progress, args.max_runtime)
            atomic_text(args.output_root / "last-run.json", json.dumps(dict(status="running", run_id=tracker.run_id, directory=str(args.output_dir), started_at=tracker.started_at)))
        environment, mode = ("pbs-only", "pbs-api") if args.pbs_only else startup_options(args)
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
        def setting(name):
            return os.environ.get("PVE_" + name.upper(), config.get(name))
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=args.output_dir):
            pass
        for command in ({"ssh": ["ssh"], "local": ["pvesh"], "cli": ["qm", "pvesm"]}.get(mode, [])):
            if shutil.which(command) is None:
                raise ValueError("Required local command not found: " + command)
        if args.pbs_only:
            if tracker is None:
                tracker = RunTracker(args.output_dir, args.progress, args.max_runtime)
            report = dict(generated_at=datetime.now(timezone.utc).isoformat(), pve_version=None,
                          vms=[], nodes=[], jobs=None, warnings=[], skipped_containers=None,
                          limitations=["PVE inventory was not requested. PBS group ownership matching was not performed."],
                          scope=dict(pbs_only=True, selected=0, discovered=0, targeted=False))
        else:
            if mode == "ssh":
                ssh_client = SSHClient(**ssh_settings(args, setting))
                ssh_client.connect()
                client = ssh_client
            else:
                client = (CLIClient(args.timeout) if mode == "cli" else
                          Client(mode, args.timeout, **api_settings(args, setting)) if mode == "api" else
                          Client(mode, args.timeout))
            if tracker is None:
                tracker = RunTracker(args.output_dir, args.progress, args.max_runtime)
            else:
                tracker.started = time.monotonic()  # Collection budget begins after authentication.
            client.node_address_overrides = config.get("node_addresses", {})
            if not isinstance(client.node_address_overrides, dict):
                raise ValueError("node_addresses must map PVE node names to IP addresses")
            client = ObservedClient(client, tracker)
            report = Collector(client, not args.no_guest_agent, args.storage_timeout, args.vmid, args.node).collect()
        report["node_routes"] = [] if args.pbs_only else list(getattr(client, "node_routes", {}).values())
        for route in report['node_routes']:
            if route['error']:
                affected = [v for v in report['vms'] if v['node'] == route['node']]
                disks = sum(len(unique_disks(v['disks'])) for v in affected)
                report['warnings'].append(f"Storage connection to {route['node']} failed; affects {len(affected)} VMs and {disks} disks: {route['error']}")
        report["large_vm_bytes"] = int(args.large_vm_gib * 1024**3)
        report["pbs_warning_percent"] = args.pbs_warning_percent
        report["pbs"] = dict(requested=False, status="not requested", datastores=[], groups=[], snapshots=[], warnings=[])
        pbs_config = None
        try:
            pbs_config = None if tracker.cancelled or tracker.expired else pbs_settings(args, config)
            if pbs_config is not None:
                pbs_client = PBSClient(**pbs_config)
                try:
                    report["pbs"] = PBSCollector(ObservedClient(pbs_client, tracker), args.pbs_store, args.pbs_namespace, args.pbs_task_days, args.pbs_task_limit, args.pbs_fresh_hours).collect()
                    if getattr(pbs_client, 'ssh', None):
                        pbs_location_details(report, config, args, tracker, existing=pbs_client.ssh)
                finally:
                    if hasattr(pbs_client, 'close'):
                        pbs_client.close()
        except (ReadError, OSError, ValueError, TypeError, EOFError) as exc:
            report["pbs"] = dict(requested=True, status="unavailable", datastores=[], groups=[], snapshots=[],
                                 warnings=["PBS reporting could not be completed: " + str(exc)[:600]])
        if mode == "ssh" and environment == "standalone" and len(report.get("nodes") or []) > 1:
            environment = "cluster"
            report["warnings"].append("SSH target exposes multiple nodes; report scope changed from standalone to cluster. All visible VMs were collected.")
        report["environment"], report["access_mode"] = environment, mode
        report["pbs"]["server"] = parse.urlparse(pbs_config["url"]).hostname if pbs_config else None
        if mode == "ssh":
            report["limitations"].append("Python and reports run locally. PVE API reads use pvesh get over SSH; PVE proxies other nodes. "
                                          "Exact active LVM-thin reads use dmsetup status on the owning host, with existing noninteractive root SSH trust between PVE nodes when needed. "
                                          "No Python, script upload or report files are required on PVE.")
        if mode == "cli":
            report["limitations"].append("Standalone CLI mode inventories only this server using qm/pvesm. "
                                          "Container count is N/A because pct was not queried. Backup jobs use optional read-only pvesh; "
                                          "if unavailable, membership is N/A. Allocation sources are shown in disk details.")
        if report['pbs']['requested'] and pbs_config and report['pbs'].get('groups') is not None:
            pbs_relationships(report, pbs_config['url'], config.get('pbs_freshness_overrides', []))
        for store in report['pbs'].get('datastores',[]):
            for job in store.get('verification_jobs') or []:
                ended=number(job.get('last-run-endtime'))
                job['age_review']='no expectation configured' if args.pbs_verification_days is None else 'unavailable' if ended is None else 'older than expected' if time.time()-ended>args.pbs_verification_days*86400 else 'within expectation'
        try:
            sort_report(report, args.sort)
        except ValueError as exc:
            report["warnings"].append(str(exc) + " Fell back to configured sorting.")
            sort_report(report, "configured")
        report["run"] = dict(tracker.metadata(), total_runtime_seconds=round(time.monotonic()-launched, 3), status=("cancelled" if tracker.cancelled else "time limit reached" if tracker.expired else "finished"), access_mode=mode, storage_timeout=args.storage_timeout,
                             query_timeout=args.timeout, guest_agent_enabled=not args.no_guest_agent,
                             pbs_requested=report['pbs']['requested'])
        gaps = Counter()
        for vm in report['vms']:
            for disk in unique_disks(vm['disks']):
                if disk['actual_bytes'] is None:
                    reason = (disk['actual_reason'] or '').lower()
                    category = ('timeout' if 'timed out' in reason or 'timeout' in reason else
                                'unsupported' if 'no validated' in reason else
                                'not queried' if 'not queried' in reason or 'cancelled' in reason else
                                'access denied' if '403' in reason or 'permission denied' in reason else 'unavailable')
                    gaps[category] += 1
        report['run']['unmeasured_disk_references'] = dict(gaps)
        if report['run']['status'] == 'finished' and (report['warnings'] or gaps or report['pbs'].get('warnings')):
            report['run']['status'] = 'finished with missing measurements'
        if args.pbs_only:
            report['summary'] = {key: 'N/A (PVE inventory not requested)' for key in report['summary']}
            for total in report['storage_totals']:
                total['total_bytes'] = total['known_subtotal_bytes'] = None
        if args.pbs_history and report['pbs']['requested'] and pbs_config:
            try:
                pbs_history(report, args.output_root, pbs_config['url'].rstrip('/'))
            except (OSError, sqlite3.Error) as exc:
                report['pbs']['warnings'].append('Local capacity history unavailable: ' + str(exc))
                report['run']['status'] = 'finished with history unavailable'
        if not tracker.cancelled and not tracker.expired:
            if (pbs_config or {}).get('auth') != 'ssh':
                pbs_location_details(report, config, args, tracker)
        report['changes'] = compare_inventory(report, args.previous_report)
        report['restore_tests'] = []
        if args.restore_tests:
            records = json.loads(args.restore_tests.read_text(encoding='utf-8'))
            if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
                raise ValueError('Restore-test file must be a JSON list of records')
            report['restore_tests'] = [{k:r.get(k) for k in ('server','datastore','source','date','result','notes')} for r in records]
        print(export_report(report, args.output_dir))
        if args.diagnostics is not None:
            secrets = diagnostic_secrets(config)
            for name, value in os.environ.items():
                if name.startswith(('PVE_', 'PBS_')) and any(word in name.lower() for word in ('token', 'secret', 'password')):
                    secrets.append(value)
            if not args.pbs_only:
                auth = getattr(client, 'auth', '')
                if auth:
                    secrets.extend([auth, auth.rsplit('=', 1)[-1]])
            secrets.extend(diagnostic_secrets(pbs_config or {}))
            try:
                bundle = write_diagnostics(report, tracker, args.diagnostics, secrets)
                print(f'Troubleshooting files written to {bundle.resolve()}', file=sys.stderr)
            except OSError as exc:
                print('Report saved, but troubleshooting bundle could not be written: ' + str(exc), file=sys.stderr)
        complete_configs = sum(v['config_available'] for v in report['vms'])
        incomplete = tracker.cancelled or tracker.expired or bool(report['warnings']) or any(v['actual_bytes'] is None for v in report['vms'])
        tracker.message(("Report created with missing measurements" if incomplete else "Report created") +
                        f" | {complete_configs}/{len(report['vms'])} VM configurations read | {report['run']['elapsed_seconds']}s")
        tracker.message("Query outcomes: " + json.dumps(report['run']['query_outcomes']))
        tracker.message(f"Disks measured: {report['allocation_diagnostics']['measured_disks']}/{report['allocation_diagnostics']['identified_disks']} | selected {report['scope']['selected']} of {report['scope']['discovered']} visible VMs")
        print(f"Reports written to {args.output_dir.resolve()}", file=sys.stderr)
        if args.cron:
            cron_status = ("cancelled" if tracker.cancelled else "incomplete" if incomplete or report['pbs'].get('warnings') else "complete")
            target = parse.quote(args.output_dir.name + "/inventory.html", safe="/")
            atomic_text(args.output_root / "latest.html", '<!doctype html><meta charset="utf-8"><title>Latest ProxInfo report</title><p>Latest exported report: ' + html.escape(cron_status) + ' | ' + html.escape(report['generated_at']) + '</p><a href="' + target + '">Open report</a><p>Check last-run.json for the latest attempt, which may have failed after this export.</p>')
        return 130 if tracker.cancelled else 3 if args.cron and cron_status == "incomplete" else 0
    except CronBusy as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        cron_status = "cancelled"
        print("Inventory cancelled.", file=sys.stderr)
        return 130
    except (ReadError, OSError, ValueError, TypeError, EOFError) as exc:
        if tracker:
            tracker.message("Collection failed; no complete report was produced. See terminal error.")
        print(f"Inventory failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            if tracker is not None:
                if args.cron:
                    try:
                        atomic_text(args.output_root / "last-run.json", json.dumps(dict(status=cron_status, run_id=tracker.run_id, directory=str(args.output_dir), finished_at=datetime.now(timezone.utc).isoformat())))
                    except OSError as exc:
                        print("Unable to write scheduled-run status: " + str(exc), file=sys.stderr)
                tracker.close()
            if ssh_client is not None:
                ssh_client.close()
        finally:
            if cron_lock is not None:
                cron_lock.close()


if __name__ == "__main__":
    sys.exit(main())
