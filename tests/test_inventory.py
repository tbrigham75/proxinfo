import copy
import csv
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pve_inventory as inv


GIB = 1024**3


class FakeClient:
    def __init__(self, overrides=None):
        self.calls = []
        self.data = {
            "/version": {"version": "synthetic-test"},
            "/nodes": [{"node": "a", "status": "online"}, {"node": "b", "status": "online"}],
            "/cluster/resources": [
                {"vmid": 100, "type": "qemu", "node": "a", "name": "large", "status": "running"},
                {"vmid": 101, "type": "qemu", "node": "b", "name": "<script>bad</script>", "status": "stopped"},
                {"vmid": 102, "type": "lxc", "node": "a", "name": "container"},
            ],
            "/storage": [{"storage": "shared", "type": "nfs", "shared": 1},
                         {"storage": "thin", "type": "lvmthin"},
                         {"storage": "local", "type": "dir"}],
            "/cluster/backup": [
                {"id": "nightly", "enabled": 1, "all": 1, "storage": "pbs", "prune-backups": {"keep-last": 7}},
                {"id": "disabled", "enabled": 0, "vmid": "101"},
            ],
            "/cluster/backup/nightly/included_volumes": {"children": [{"id": 100}]},
            "/cluster/backup/disabled/included_volumes": {"children": [{"id": 101}]},
            "/nodes/a/qemu/100/config": {
                "memory": 4096, "cores": 4, "tags": "prod;db",
                "scsi0": "shared:100/vm-100-disk-0.raw,size=100G,backup=0",
                "scsi1": "shared:100/vm-100-disk-0.raw,size=100G",  # duplicate attachment
                "scsi2": "local:100/vm-100-disk-1.raw,size=50G",
                "ide2": "local:iso/os.iso,media=cdrom,size=4G",
                "unused0": "thin:vm-100-disk-9,size=800G",
            },
            "/nodes/b/qemu/101/config": {
                "memory": 2048, "sockets": 2, "cores": 2,
                "scsi0": "shared:100/vm-100-disk-0.raw,size=100G",
            },
            "/nodes/a/qemu/100/status/current": {"status": "running", "mem": 2*GIB},
            "/nodes/b/qemu/101/status/current": {"status": "stopped", "mem": 0},
            "/nodes/a/qemu/100/agent/get-fsinfo": {"result": [
                {"name": "vda1", "mountpoint": "/", "type": "ext4", "total-bytes": 10*GIB, "used-bytes": 4*GIB},
                {"name": "vda1", "mountpoint": "/alias", "type": "ext4", "total-bytes": 10*GIB, "used-bytes": 4*GIB},
                {"name": "tmp", "mountpoint": "/unknown", "type": "tmpfs"},
            ]},
            "/nodes/a/storage/shared/content/shared%3A100%2Fvm-100-disk-0.raw": {"size": 100*GIB, "used": 20*GIB},
            "/nodes/a/storage/local/content/local%3A100%2Fvm-100-disk-1.raw": {"size": 50*GIB, "used": 0},
        }
        if overrides:
            self.data.update(overrides)

    def get(self, path, **params):
        self.calls.append((path, params))
        result = self.data.get(path, inv.ReadError("fixture endpoint unavailable"))
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


def collect(client=None):
    with patch("sys.stderr", new=io.StringIO()):
        return inv.Collector(client or FakeClient()).collect()


class AccountingTests(unittest.TestCase):
    def test_shared_volumes_and_duplicate_attachments(self):
        client = FakeClient()
        report = collect(client)
        inv.sort_report(report, "configured")
        first, second = report["vms"]
        self.assertEqual(first["configured_bytes"], 150*GIB)
        self.assertEqual(second["configured_bytes"], 100*GIB)
        self.assertEqual(first["actual_bytes"], 20*GIB)
        self.assertEqual(first["disks"][0]["capacity_bytes"], 4*GIB)  # CD displayed, excluded
        self.assertEqual(report["summary"]["Unique configured logical capacity"], "150.00 GiB")
        self.assertEqual(report["summary"]["Visible distinct volumes with known identity"], "2")
        self.assertEqual(report["summary"]["Total measured VM disk allocation"], "20.00 GiB")
        self.assertEqual(report["storage_totals"][1]["total_bytes"], 20*GIB)
        self.assertEqual(report["storage_totals"][1]["measured_volumes"], 2)
        self.assertEqual(report["skipped_containers"], 1)
        storage_calls = [p for p, _ in client.calls if "/storage/" in p]
        self.assertEqual(len(storage_calls), 2)  # shared API counter requested once
        self.assertIn("backup=0", " ".join(first["notes"]))
        self.assertEqual(first["filesystems"][0]["free_bytes"], 6*GIB)
        self.assertIsNone(first["filesystems"][2]["used_bytes"])
        self.assertTrue(report["summary"]["Guest filesystem usage total"].startswith("N/A"))

    def test_zero_is_not_missing_and_actual_sort(self):
        report = collect()
        first, second = report["vms"]
        self.assertEqual(second["memory_used_bytes"], 0)
        disk = next(d for d in first["disks"] if d["slot"] == "scsi2")
        self.assertEqual(disk["actual_bytes"], 0)
        inv.sort_report(report, "actual")
        self.assertEqual(report["sort_metric"], "actual")
        self.assertEqual(inv.human(0), "0.00 B")
        self.assertEqual(inv.human(None), "N/A")

    def test_missing_size_and_unsupported_thin_backend(self):
        client = FakeClient({"/nodes/b/qemu/101/config": {"scsi0": "thin:vm-101-disk-0"}})
        report = collect(client)
        vm = report["vms"][1]
        self.assertIsNone(vm["configured_bytes"])
        self.assertIsNone(vm["actual_bytes"])
        self.assertIn("no validated", vm["disks"][0]["actual_reason"])
        self.assertFalse(any("/storage/thin/" in p for p, _ in client.calls))
        inv.sort_report(report, "configured")
        self.assertEqual(report["vms"][-1]["vmid"], 101)
        self.assertTrue(report["summary"]["Unique configured logical capacity"].startswith("N/A"))
        with self.assertRaises(ValueError):
            inv.sort_report(report, "actual")

    def test_offline_node_retains_vm_and_avoids_node_queries(self):
        client = FakeClient({"/nodes": [{"node": "a", "status": "online"}, {"node": "b", "status": "offline"}]})
        report = collect(client)
        vm = report["vms"][1]
        self.assertIsNone(vm["configured_bytes"])
        self.assertIsNone(vm["ram_bytes"])
        self.assertIsNone(vm["status"])
        self.assertFalse(any(p.startswith("/nodes/b/") for p, _ in client.calls))

    def test_permission_and_agent_failures_continue(self):
        report = collect(FakeClient({
            "/nodes/a/qemu/100/agent/get-fsinfo": inv.ReadError("HTTP 403"),
            "/cluster/backup/nightly/included_volumes": inv.ReadError("unsupported on older release"),
        }))
        self.assertEqual(len(report["vms"]), 2)
        self.assertEqual(report["vms"][0]["backup"], "N/A")
        self.assertEqual(report["vms"][1]["backup"], "N/A")
        self.assertEqual(report["vms"][1]["disabled_jobs"], ["disabled"])
        self.assertIn("HTTP 403", report["vms"][0]["filesystem_reason"])
        self.assertTrue(report["warnings"])

    def test_backup_enabled_disabled_and_invalid_response(self):
        report = collect()
        self.assertEqual(report["vms"][0]["backup"], "Yes")
        self.assertEqual(report["vms"][1]["backup"], "No")
        self.assertEqual(report["vms"][1]["disabled_jobs"], ["disabled"])
        report = collect(FakeClient({"/cluster/backup/nightly/included_volumes": {}}))
        self.assertEqual(report["vms"][1]["backup"], "N/A")

    def test_local_identical_ids_on_different_nodes_not_deduplicated(self):
        client = FakeClient({
            "/nodes/b/qemu/101/config": {"scsi0": "local:100/vm-100-disk-1.raw,size=50G"},
            "/nodes/b/storage/local/content/local%3A100%2Fvm-100-disk-1.raw": {"used": 10*GIB},
        })
        report = collect(client)
        inv.sort_report(report, "configured")
        self.assertEqual(report["summary"]["Unique configured logical capacity"], "200.00 GiB")

    def test_unknown_storage_identity_blocks_complete_total(self):
        report = collect(FakeClient({"/storage": inv.ReadError("HTTP 403")}))
        inv.sort_report(report, "configured")
        self.assertEqual(report["vms"][0]["configured_bytes"], 150*GIB)
        self.assertTrue(report["summary"]["Unique configured logical capacity"].startswith("N/A"))
        self.assertEqual(report["summary"]["Known unique logical-capacity subtotal"], "N/A")

    def test_mixed_actual_semantics_refuse_sort(self):
        report = collect()
        report["vms"][1]["actual_method"] = "ZFS usedbydataset"
        with self.assertRaises(ValueError):
            inv.sort_report(report, "actual")

    def test_missing_allocation_counter_is_not_zero(self):
        report = collect(FakeClient({
            "/nodes/a/storage/local/content/local%3A100%2Fvm-100-disk-1.raw": {"size": 50*GIB},
        }))
        self.assertIsNone(report["vms"][0]["actual_bytes"])
        self.assertIn("did not return used", report["vms"][0]["disks"][3]["actual_reason"])
        inv.sort_report(report, "configured")
        self.assertIsNone(report["storage_totals"][1]["total_bytes"])
        self.assertEqual(report["storage_totals"][1]["measured_volumes"], 1)
        self.assertEqual(report["summary"]["Known allocation subtotal: file allocated bytes"], "20.00 GiB")

    def test_storage_totals_keep_zero_distinct_from_no_measurements(self):
        report = collect()
        for vm in report["vms"]:
            for disk in vm["disks"]:
                if disk["counted"]:
                    disk["actual_bytes"] = 0
        inv.sort_report(report, "configured")
        self.assertEqual(report["storage_totals"][1]["total_bytes"], 0)
        self.assertEqual(report["summary"]["Total measured VM disk allocation"], "0.00 B")
        for vm in report["vms"]:
            for disk in vm["disks"]:
                disk["actual_bytes"] = None
        inv.sort_report(report, "configured")
        self.assertIsNone(report["storage_totals"][1]["total_bytes"])
        self.assertTrue(report["summary"]["Total measured VM disk allocation"].startswith("N/A"))

    def test_storage_totals_do_not_combine_different_allocation_methods(self):
        report = collect()
        local_disk = next(d for d in report["vms"][0]["disks"] if d["slot"] == "scsi2")
        local_disk["actual_method"] = "ZFS usedbydataset"
        local_disk["actual_bytes"] = 5*GIB
        inv.sort_report(report, "configured")
        self.assertIsNone(report["storage_totals"][1]["total_bytes"])
        self.assertEqual(report["summary"]["Known allocation subtotal: ZFS usedbydataset"], "5.00 GiB")
        self.assertEqual(report["summary"]["Known allocation subtotal: file allocated bytes"], "20.00 GiB")

    def test_storage_totals_with_missing_configuration_are_partial(self):
        report = collect(FakeClient({"/nodes/b/qemu/101/config": inv.ReadError("offline")}))
        inv.sort_report(report, "configured")
        self.assertIsNone(report["storage_totals"][1]["total_bytes"])
        self.assertFalse(report["storage_totals"][1]["complete_coverage"])
        self.assertEqual(report["summary"]["Known allocation subtotal: file allocated bytes"], "20.00 GiB")

    def test_conflicting_duplicate_sizes_are_unavailable(self):
        client = FakeClient()
        client.data["/nodes/a/qemu/100/config"]["scsi1"] = "shared:100/vm-100-disk-0.raw,size=90G"
        report = collect(client)
        self.assertIsNone(report["vms"][0]["configured_bytes"])
        inv.sort_report(report, "configured")
        self.assertTrue(report["summary"]["Unique configured logical capacity"].startswith("N/A"))

    def test_cluster_list_failure_is_fatal(self):
        with self.assertRaises(inv.ReadError):
            collect(FakeClient({"/cluster/resources": inv.ReadError("unreachable")}))


class ExportAndTransportTests(unittest.TestCase):
    def test_exports_preserve_unknowns_and_escape_html_and_csv(self):
        report = collect()
        report["vms"][0]["name"] = "=HYPERLINK(1)"
        report["vms"][1]["memory_used_bytes"] = None
        inv.sort_report(report, "configured")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            text = inv.export_report(report, directory)
            self.assertIn("150.00 GiB", text)
            with (directory / "vms.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["name"], "'=HYPERLINK(1)")
            self.assertEqual(rows[1]["memory_used_bytes"], "N/A")
            self.assertEqual(rows[0]["configured_bytes"], str(150*GIB))
            markup = (directory / "inventory.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>bad</script>", markup)
            self.assertIn("&lt;script&gt;bad&lt;/script&gt;", markup)
            self.assertIn("Total configured VM disks", markup)
            with (directory / "storage_totals.csv").open(encoding="utf-8-sig", newline="") as stream:
                totals = list(csv.DictReader(stream))
            self.assertEqual(totals[0]["total_bytes"], str(150*GIB))
            self.assertEqual(totals[1]["total_bytes"], str(20*GIB))
            self.assertEqual(markup.count('class="top"'), 2)
            self.assertIn("if(am!==bm)return am?1:-1", markup)
            self.assertIsNone(json.loads((directory / "inventory.json").read_text(encoding="utf-8"))["vms"][1]["memory_used_bytes"])

    def test_only_inventory_paths_are_accepted(self):
        client = inv.Client()
        for path in ("/nodes/a/qemu/100/status/stop", "/nodes/a/qemu/100/agent/exec", "/storage/foo", "/cluster/backup/foo"):
            with self.assertRaises(ValueError):
                client.get(path)
        with patch("pve_inventory.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "[]"
            self.assertEqual(client.get("/cluster/resources", type="vm"), [])
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["pvesh", "get", "/cluster/resources", "--output-format", "json", "--type", "vm"])
            self.assertFalse(kwargs["shell"])
            client.get("/nodes/a/storage/shared/content/shared%3A100%2Fvm-100-disk-0.raw")
            self.assertEqual(run.call_args.args[0][2], "/nodes/a/storage/shared/content/shared:100/vm-100-disk-0.raw")

    def test_local_timeout_is_an_unavailable_read(self):
        import subprocess
        with patch("pve_inventory.subprocess.run", side_effect=subprocess.TimeoutExpired("pvesh", 20)):
            with self.assertRaises(inv.ReadError):
                inv.Client().get("/version")

    def test_https_validates_ca_and_uses_get(self):
        with patch("pve_inventory.ssl.create_default_context") as tls, patch("pve_inventory.request.build_opener") as build:
            client = inv.Client("api", url="https://pve:8006", token_id="user@pve!r", token_secret="secret", ca_file="ca.pem")
            tls.assert_called_once_with(cafile="ca.pem")
            response = build.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b'{"data":[]}'
            client.get("/cluster/resources", type="vm")
            req = build.return_value.open.call_args.args[0]
            self.assertEqual(req.get_method(), "GET")
            self.assertEqual(req.full_url, "https://pve:8006/api2/json/cluster/resources?type=vm")
        self.assertIsNone(inv.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil"))
        with self.assertRaises(ValueError):
            inv.Client("api", url="http://pve", token_id="id", token_secret="secret")

    def test_config_uses_current_and_agent_can_be_disabled(self):
        client = FakeClient()
        with patch("sys.stderr", new=io.StringIO()):
            report = inv.Collector(client, guest_agent=False).collect()
        self.assertFalse(any("/agent/" in p for p, _ in client.calls))
        self.assertIn("disabled", report["vms"][0]["filesystem_reason"])
        config_calls = [params for p, params in client.calls if p.endswith("/config")]
        self.assertTrue(all(params == {"current": 1} for params in config_calls))


class StandaloneAndPromptTests(unittest.TestCase):
    def test_qm_list_and_nested_status_parser(self):
        rows = inv.parse_qm_list("VMID NAME STATUS MEM(MB) BOOTDISK(GB) PID\n100 app running 2048 10.0 123\n", "pve")
        self.assertEqual(rows[0]["vmid"], 100)
        self.assertEqual(rows[0]["node"], "pve")
        parsed = inv.key_value_output("status: running\nmem: 100\nblockstat:\n\tscsi0:\n\t\tused: 99\n")
        self.assertEqual(parsed["mem"], "100")
        self.assertNotIn("used", parsed)
        with self.assertRaises(inv.ReadError):
            inv.parse_qm_list("Something unexpected\n", "pve")

    def test_regular_cli_inventory_without_pvesh(self):
        outputs = {
            ("pveversion",): "pve-manager/test-version\n",
            ("qm", "list"): "VMID NAME STATUS MEM(MB) BOOTDISK(GB) PID\n100 app stopped 2048 1.0 0\n",
            ("pvesm", "status"): "Name Type Status Total Used Available %\nlocal dir active 100 50 50 50%\n",
            ("qm", "config", "100", "--current", "1"): "name: app\nmemory: 2048\ncores: 2\nscsi0: local:100/disk.raw,size=20G\nscsi1: local:100/second.raw,size=30G\n",
            ("qm", "status", "100", "--verbose", "1"): "status: stopped\nmem: 0\n",
            ("pvesm", "path", "local:100/disk.raw"): "/images/100/disk.raw\n",
            ("pvesm", "path", "local:100/second.raw"): "/images/100/second.raw\n",
        }
        client = inv.CLIClient()
        with patch.object(client, "run", side_effect=lambda cmd: outputs[tuple(cmd)]), \
             patch.object(client.pvesh, "get", side_effect=inv.ReadError("pvesh unavailable")), \
             patch("pve_inventory.Path.read_text", return_value="dir: local\n\tpath /images\n"), \
             patch("pve_inventory.os.stat", return_value=SimpleNamespace(st_mode=inv.stat.S_IFREG, st_blocks=8)):
            report = collect(client)
        vm = report["vms"][0]
        self.assertEqual(vm["configured_bytes"], 50*GIB)  # ignores boot-disk-only list column
        self.assertEqual(vm["actual_bytes"], 8192)
        self.assertEqual(vm["backup"], "N/A")
        self.assertIsNone(report["skipped_containers"])
        self.assertIn("stat st_blocks", vm["disks"][0]["actual_reason"])

    def test_cli_command_allowlist_rejects_mutations(self):
        client = inv.CLIClient()
        for command in (["qm", "stop", "100"], ["qm", "guest", "cmd", "100", "shutdown"],
                        ["pvesm", "free", "local:disk"], ["zfs", "destroy", "pool/disk"]):
            with self.assertRaises(ValueError):
                client.run(command)
        with patch("pve_inventory.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "4096\n"
            self.assertEqual(client.run(["zfs", "get", "-H", "-p", "-o", "value", "usedbydataset", "pool/vol"]), "4096\n")
            self.assertFalse(run.call_args.kwargs["shell"])

    def test_cluster_prompts_and_standalone_guard(self):
        args = SimpleNamespace(mode=None, environment="auto", no_prompt=False)
        with patch("pve_inventory.detect_environment", return_value="cluster"), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=["cluster", "local"]) as ask, \
             patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(inv.startup_options(args), ("cluster", "local"))
            self.assertEqual(ask.call_count, 2)
        args = SimpleNamespace(mode="cli", environment="standalone", no_prompt=True)
        with patch("pve_inventory.detect_environment", return_value="cluster"):
            with self.assertRaises(ValueError):
                inv.startup_options(args)

    def test_standalone_prompts_and_unattended_run(self):
        args = SimpleNamespace(mode=None, environment="auto", no_prompt=False)
        with patch("pve_inventory.detect_environment", return_value="standalone"), \
             patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", side_effect=["standalone", "cli"]), \
             patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(inv.startup_options(args), ("standalone", "cli"))
        args = SimpleNamespace(mode="local", environment="cluster", no_prompt=True)
        with patch("pve_inventory.detect_environment", return_value=None), patch("builtins.input") as ask:
            self.assertEqual(inv.startup_options(args), ("cluster", "local"))
            ask.assert_not_called()

    def test_main_writes_reports_when_actual_sort_falls_back(self):
        client = FakeClient({"/nodes/b/qemu/101/config": inv.ReadError("unreachable")})
        with tempfile.TemporaryDirectory() as tmp, patch("pve_inventory.Client", return_value=client), \
             patch("pve_inventory.detect_environment", return_value=None), \
             patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=io.StringIO()):
            code = inv.main(["--mode", "local", "--environment", "cluster", "--no-prompt",
                             "--sort", "actual", "--output-dir", tmp])
            self.assertEqual(code, 0)
            report = json.loads((Path(tmp) / "inventory.json").read_text(encoding="utf-8"))
            self.assertEqual(report["sort_metric"], "configured")
            self.assertEqual(report["environment"], "cluster")
            self.assertTrue(any("Fell back" in w for w in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
