#!/usr/bin/env python3
"""Read-only, standard-library PVE cluster VM inventory. Python 3.9+."""

import argparse
import csv
import html
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
from datetime import datetime, timezone
from urllib import error, parse, request


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
}
LIMITATIONS = (
    "PVE-only inventory; PBS capacity, snapshots, retention, prune and GC status were not queried.",
    "Configured capacity is logical provisioning, not host allocation, guest usage or PBS consumption.",
    "Actual means backend-reported allocation: file allocated bytes or ZFS usedbydataset. "
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

    def get(self, path, **params):
        # Fixed GET-only surface, including exactly one read-only guest-agent command.
        allowed = (
            r"/version", r"/nodes", r"/cluster/resources", r"/cluster/backup",
            r"/storage", r"/cluster/backup/[^/]+/included_volumes",
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
                result = subprocess.run(command, capture_output=True, text=True,
                                        timeout=self.timeout, check=False, shell=False)
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


def key_value_output(text):
    """Top-level qm config/status properties only; skip nested verbose blocks."""
    return dict(match.groups() for line in text.splitlines()
                if (match := re.match(r"^([^\s:#]+):\s*(.*)$", line)))


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
            option = re.match(r"^\s+(shared|path|pool)\s+(.+?)\s*$", line)
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
        if answer in choices:
            return answer
        print("Choose one of: " + ", ".join(choices), file=sys.stderr)


def startup_options(args):
    detected = detect_environment() if args.mode != "api" else None
    interactive = not args.no_prompt and sys.stdin.isatty()
    environment = args.environment
    mode = args.mode
    if environment == "auto":
        if interactive:
            print("Read-only PVE inventory setup. PBS reporting is deferred.", file=sys.stderr)
            print(f"Local environment detection: {detected or 'unavailable'}", file=sys.stderr)
            environment = prompt_choice("Environment", ("cluster", "standalone"), detected or "cluster")
        else:
            environment = detected or "cluster"
    if mode is None:
        default = "local" if shutil.which("pvesh") or environment == "cluster" else "cli"
        mode = prompt_choice("Access: local=pvesh, cli=regular standalone commands, api=HTTPS token",
                             ("local", "cli", "api") if environment == "standalone" else ("local", "api"), default) if interactive else default
    if detected == "cluster" and environment == "standalone":
        raise ValueError("Local cluster configuration was detected. Use --environment cluster to avoid an incomplete local-only inventory")
    if mode == "cli" and environment != "standalone":
        raise ValueError("Regular CLI mode is for standalone servers. Use --mode local or api to inventory all cluster nodes")
    return environment, mode


class Collector:
    def __init__(self, client, guest_agent=True):
        self.client = client
        self.guest_agent = guest_agent
        self.warnings = []
        self.volume_cache = {}

    def read(self, path, **params):
        try:
            return self.client.get(path, **params), None
        except ReadError as exc:
            message = f"GET {path}: {exc}"
            self.warnings.append(message)
            return None, message

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
            path = f"/nodes/{segment(node)}/storage/{segment(storage)}/content/{segment(volume)}"
            info, err = self.read(path)
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
            if not excluded:
                if storage:
                    identity, actual, reason = self.volume(node, storage, volume, storages.get(storage))
                else:
                    reason = "N/A: passthrough/unmanaged disk has no storage API allocation metric"
            disks.append(dict(slot=slot, volume=volume, storage=storage, capacity_bytes=capacity,
                              capacity_reason=("configuration size=" + options["size"] if capacity is not None else
                                               "N/A: " + (excluded or "no parseable configured size; volume size is not substituted")),
                              actual_bytes=actual, actual_reason=reason,
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
        resources, err = self.read("/cluster/resources", type="vm")
        if err or not isinstance(resources, list):
            raise ReadError(err or "Cluster resources response was not a list; inventory cannot proceed")
        nodes, _ = self.read("/nodes")
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
        for resource in resources:
            if resource.get("type") != "qemu":
                continue
            vmid, node = resource["vmid"], resource["node"]
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
        return dict(generated_at=datetime.now(timezone.utc).isoformat(), pve_version=version,
                    vms=vms, nodes=nodes, jobs=jobs, warnings=self.warnings,
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


def sort_report(report, metric):
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


def summary(report):
    vms = report["vms"]
    volumes, allocations, missing = {}, {}, 0
    identity_unknown = False
    for vm in vms:
        if not vm["config_available"]:
            missing += 1
        for disk in unique_disks(vm["disks"]):
            if not disk["identity"]:
                identity_unknown = True
                continue
            key = tuple(disk["identity"])
            capacity = disk["capacity_bytes"]
            if key in volumes and volumes[key] != capacity:
                volumes[key] = None  # conflicting config observations are not silently resolved
            else:
                volumes[key] = capacity
            allocation = (disk["actual_bytes"], disk["actual_method"])
            if key in allocations and allocations[key] != allocation:
                allocations[key] = (None, None)
            else:
                allocations[key] = allocation
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
    accounting_note = ("Distinct attached volume IDs counted once. Allocation counters are not exclusive physical "
                       "storage, reclaimable space or PBS usage; shared extents and storage aliases may affect accounting.")
    report["storage_totals"] = [
        dict(metric="Configured logical capacity", total_bytes=known if full else None,
             known_subtotal_bytes=known if len(volumes) > unknown_sizes or full else None,
             measured_volumes=len(volumes)-unknown_sizes, known_identity_volumes=len(volumes),
             complete_coverage=full, notes=accounting_note),
        dict(metric="Measured VM disk allocation", total_bytes=allocation_total,
             known_subtotal_bytes=allocation_total, measured_volumes=len(measured),
             known_identity_volumes=len(volumes), complete_coverage=allocation_complete,
             notes="A combined total requires complete coverage and one allocation metric meaning. " + accounting_note),
    ] + [dict(metric="Allocation subtotal: " + method, total_bytes=None, known_subtotal_bytes=subtotal,
              measured_volumes=sum(kind == method for _, kind in measured), known_identity_volumes=len(volumes),
              complete_coverage=False, notes="Known volumes with this metric only; not a full inventory total. " + accounting_note)
         for method, subtotal in subtotals.items()]
    values = [
        ("Unique configured logical capacity", human(known) if full else "N/A (incomplete identity/configuration/size coverage)"),
        ("Total measured VM disk allocation", human(allocation_total) if allocation_complete else
         "N/A (incomplete coverage or different allocation metric meanings)"),
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
    lines = ["PVE VM INVENTORY", f"Generated: {report['generated_at']}",
             f"Environment: {report.get('environment', 'cluster')}; access: {report.get('access_mode', 'local')}",
             f"PVE version: {json.dumps(report['pve_version'], ensure_ascii=False)}",
             f"Largest first by {report['sort_metric']} bytes; * = top 10 measured VMs", ""]
    lines.extend(f"{key}: {value}" for key, value in report["summary"].items())
    lines.extend(["", "  VMID  NODE / NAME / STATUS | CONFIGURED | ACTUAL | RAM allocated / PVE used | vCPU | BACKUP"])
    for vm in report["vms"]:
        star = "*" if vm["vmid"] in report["top_vmids"] else " "
        lines.append(f"{star} {vm['vmid']}  {vm['node']} / {display(vm['name'])} / {display(vm['status'])} "
                     f"| {human(vm['configured_bytes'])} | {human(vm['actual_bytes'])} "
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


def html_report(report):
    esc = lambda value: html.escape(display(value), quote=True)
    headings = ["VM ID", "Name", "Node", "Status", "Tags", "Configured capacity", "Actual allocation",
                "RAM allocated", "PVE memory used", "vCPU", "Enabled backup job?", "Details"]
    rows = []
    for vm in report["vms"]:
        values = [vm["vmid"], vm["name"], vm["node"], vm["status"], vm["tags"],
                  vm["configured_bytes"], vm["actual_bytes"], vm["ram_bytes"],
                  vm["memory_used_bytes"], vm["vcpus"], vm["backup"]]
        cells = []
        for index, value in enumerate(values):
            label = human(value) if index in (5, 6, 7, 8) else display(value)
            cells.append(f'<td data-value="{esc(value)}" data-missing="{str(value is None).lower()}">{esc(label)}</td>')
        details = ["Template: " + str(vm["template"]),
                   "Enabled jobs: " + (", ".join(vm["enabled_jobs"]) or "(none)"),
                   "Disabled jobs: " + (", ".join(vm["disabled_jobs"]) or "(none)"),
                   vm["filesystem_reason"]]
        details += [disk_text(d) for d in vm["disks"]]
        details += [fs_text(f) for f in vm["filesystems"]] + vm["notes"]
        cells.append('<td><details><summary>Disks, filesystems, reasons</summary><ul>' +
                     "".join("<li>" + esc(d) + "</li>" for d in details) + "</ul></details></td>")
        klass = ' class="top"' if vm["vmid"] in report["top_vmids"] else ""
        rows.append(f"<tr{klass}>" + "".join(cells) + "</tr>")
    summary_html = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in report["summary"].items())
    storage_html = '<div class="storage-totals">' + "".join(
        '<div><strong>' + esc(label) + '</strong><p>' + esc(report["summary"][key]) + '</p></div>'
        for label, key in (("Total configured VM disks", "Unique configured logical capacity"),
                           ("Total measured VM disk allocation", "Total measured VM disk allocation"))) + '</div>'
    headers = "".join(f'<th scope="col"><button type="button" data-col="{i}">{esc(h)}</button></th>'
                      if i < 11 else f"<th>{esc(h)}</th>" for i, h in enumerate(headings))
    top_names = ", ".join(f"{v['vmid']} ({display(v['name'])})" for v in report["vms"] if v["vmid"] in report["top_vmids"])
    jobs = [job_text(j) for j in report["jobs"] or []] or ["N/A: job API unavailable" if report["jobs"] is None else "No visible scheduled jobs"]
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PVE VM inventory</title><style>
body{font:15px system-ui,sans-serif;margin:2rem;color:#17263c;background:#f8fafc}
h1,h2{color:#143c65}dl{display:grid;grid-template-columns:minmax(15rem,1fr) 2fr;gap:.5rem;max-width:1100px}
dt{font-weight:bold}dd{margin:0}table{border-collapse:collapse;width:100%;background:white}
td,th{padding:.65rem;border:1px solid #cbd5e1;text-align:left;vertical-align:top}
th{background:#e6edf5}button{font:inherit;font-weight:bold;cursor:pointer;border:0;background:none;color:inherit}
.top{background:#fff4cc}.table-wrap{overflow:auto}li{margin:.4rem 0}details{min-width:18rem}footer{margin-top:2rem}
.storage-totals{display:flex;flex-wrap:wrap;gap:1rem;margin:1rem 0}.storage-totals>div{background:#e6edf5;border:1px solid #cbd5e1;padding:1rem;flex:1;min-width:16rem}.storage-totals p{font-size:1.35rem;margin:.5rem 0 0}
</style><h1>PVE VM inventory</h1>''' + f"<p>Environment: {esc(report.get('environment', 'cluster'))}; access: {esc(report.get('access_mode', 'local'))}</p>" + \
        f"<p>Generated {esc(report['generated_at'])} (UTC). PVE: {esc(json.dumps(report['pve_version']))}</p>" + \
        f"<p>Sorted by <strong>{esc(report['sort_metric'])}</strong> bytes, largest first. Units use powers of 1024 (GiB/TiB). N/A differs from zero.</p>" + \
        "<h2>Storage totals</h2>" + storage_html + \
        "<p>Allocation totals add distinct volume counters. They do not establish unique physical storage or PBS usage. See coverage and known subtotals below.</p>" + \
        "<h2>Summary</h2><dl>" + summary_html + "</dl><h2>Top 10 measured VMs</h2><p>" + esc(top_names or NA) + \
        "</p><p>Highlighted rows retain their original top-10 membership when columns are sorted. Click a column heading to sort.</p>" + \
        '<div class="table-wrap"><table id="vms"><thead><tr>' + headers + "</tr></thead><tbody>" + "".join(rows) + \
        "</tbody></table></div><h2>PVE backup jobs (configuration only)</h2><ul>" + \
        "".join("<li>" + esc(j) + "</li>" for j in jobs) + "</ul><h2>Interpretation and limits</h2><ul>" + \
        "".join("<li>" + esc(n) + "</li>" for n in report["limitations"]) + "</ul><h2>Collection warnings</h2><ul>" + \
        "".join("<li>" + esc(n) + "</li>" for n in report["warnings"] or ["None"]) + '''</ul>
<script>
const table=document.querySelector('#vms'), body=table.tBodies[0];
const numeric=new Set([0,5,6,7,8,9]);
document.querySelectorAll('button[data-col]').forEach(button=>{
  button.addEventListener('click',()=>{
    const col=Number(button.dataset.col), asc=button.dataset.direction!=='asc';
    const rows=Array.from(body.rows);
    rows.sort((a,b)=>{
      const ac=a.cells[col],bc=b.cells[col];
      const am=ac.dataset.missing==='true',bm=bc.dataset.missing==='true';
      if(am!==bm)return am?1:-1;
      if(am)return 0;
      const x=ac.dataset.value,y=bc.dataset.value;
      const diff=numeric.has(col)?Number(x)-Number(y):x.localeCompare(y);
      return asc?diff:-diff;
    });
    document.querySelectorAll('th').forEach(h=>h.removeAttribute('aria-sort'));
    button.closest('th').setAttribute('aria-sort',asc?'ascending':'descending');
    button.dataset.direction=asc?'asc':'desc';
    body.append(...rows);
  });
});
</script></html>'''


def export_report(report, directory):
    directory.mkdir(parents=True, exist_ok=True)
    terminal = terminal_report(report)
    (directory / "inventory.txt").write_text(terminal, encoding="utf-8")
    (directory / "inventory.html").write_text(html_report(report), encoding="utf-8")
    (directory / "inventory.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    rows = []
    for vm in report["vms"]:
        row = dict(vm)
        for key in ("configured_bytes", "actual_bytes", "ram_bytes", "memory_used_bytes"):
            row[key.replace("_bytes", "_human")] = human(vm[key])
        row["disks_json"], row["filesystems_json"] = vm["disks"], vm["filesystems"]
        rows.append(row)
    extra = [k.replace("_bytes", "_human") for k in ("configured_bytes", "actual_bytes", "ram_bytes", "memory_used_bytes")]
    write_csv(directory / "vms.csv", VM_COLUMNS + extra + ["disks_json", "filesystems_json"], rows)
    disk_columns = ["vmid", "node", "slot", "volume", "storage", "counted", "excluded_reason", "capacity_bytes",
                    "capacity_reason", "actual_bytes", "actual_method", "actual_reason", "backup_disk", "identity"]
    write_csv(directory / "disks.csv", disk_columns,
              [dict(d, vmid=v["vmid"], node=v["node"]) for v in report["vms"] for d in v["disks"]])
    write_csv(directory / "filesystems.csv", ["vmid", "node", "mountpoint", "name", "type", "used_bytes", "free_bytes", "total_bytes", "reason"],
              [dict(f, vmid=v["vmid"], node=v["node"]) for v in report["vms"] for f in v["filesystems"]])
    write_csv(directory / "summary.csv", ["metric", "value"], [dict(metric=k, value=v) for k, v in report["summary"].items()])
    write_csv(directory / "storage_totals.csv", ["metric", "total_bytes", "known_subtotal_bytes", "measured_volumes",
                                                "known_identity_volumes", "complete_coverage", "notes"], report["storage_totals"])
    return terminal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("local", "cli", "api"), help="local=pvesh, cli=regular standalone commands, api=HTTPS token")
    parser.add_argument("--environment", choices=("auto", "cluster", "standalone"), default="auto")
    parser.add_argument("--no-prompt", action="store_true", help="Use explicit options/detection without startup questions")
    parser.add_argument("--config", type=Path, help="Optional JSON credentials/settings file; environment overrides it")
    parser.add_argument("--output-dir", type=Path, default=Path("pve-report-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
    parser.add_argument("--sort", choices=("configured", "actual"), default="configured")
    parser.add_argument("--timeout", type=float, default=20, help="Seconds per API/local command (default 20)")
    parser.add_argument("--no-guest-agent", action="store_true", help="Skip guest filesystem queries")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        environment, mode = startup_options(args)
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
        def setting(name):
            return os.environ.get("PVE_" + name.upper(), config.get(name))
        client = (CLIClient(args.timeout) if mode == "cli" else
                  Client(mode, args.timeout, setting("url"), setting("token_id"),
                         setting("token_secret"), setting("ca_file")))
        report = Collector(client, not args.no_guest_agent).collect()
        report["environment"], report["access_mode"] = environment, mode
        if mode == "cli":
            report["limitations"].append("Standalone CLI mode inventories only this server using qm/pvesm. "
                                          "Container count is N/A because pct was not queried. Backup jobs use optional read-only pvesh; "
                                          "if unavailable, membership is N/A. Allocation sources are shown in disk details.")
        try:
            sort_report(report, args.sort)
        except ValueError as exc:
            report["warnings"].append(str(exc) + " Fell back to configured sorting.")
            sort_report(report, "configured")
        print(export_report(report, args.output_dir))
        print(f"Reports written to {args.output_dir.resolve()}", file=sys.stderr)
        return 0
    except (ReadError, OSError, ValueError, TypeError) as exc:
        print(f"Inventory failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
