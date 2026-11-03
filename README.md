# PVE cluster VM inventory

Read-only Python inventory for running on a PVE cluster node or a standalone PVE server. PBS reporting is deferred, as requested. No third-party Python dependencies. In cluster mode, the script queries the entire visible cluster, including QEMU templates. LXC containers are outside this VM inventory; API/pvesh mode counts visible containers, while regular CLI mode reports that count as N/A.

## First run: step-by-step directions (no Python experience needed)

You only need to copy one file, `pve_inventory.py`, onto a PVE server and run it. You do not need to edit the Python code, install Python libraries, create a virtual environment, or configure API credentials for the local method below. Use **one cluster node** for your cluster; it will collect the other nodes too.

### 1. Copy the script from Windows to PVE

Open **PowerShell on your Windows computer**. Replace `PVE_ADDRESS` in these examples with the IP address or hostname of a PVE node. Leave `root@` in front of it. The source path below is the location of the script in this workspace.

```powershell
scp "C:\Users\tbrigham\dev\ProxInfo\pve_inventory.py" root@PVE_ADDRESS:/root/pve_inventory.py
```

Enter your PVE root password if prompted. If using a different computer, replace the Windows file path with the location where you saved the script. If `scp` is unavailable, use an SFTP client to upload `pve_inventory.py` to `/root/` on the PVE server.

### 2. Open a shell on PVE

From **Windows PowerShell**, connect to the same PVE node:

```powershell
ssh root@PVE_ADDRESS
```

Alternatively, sign in to the Proxmox web interface, select that **PVE node** in the left panel, and open its **Shell**. Use the host's shell, not a VM's console.

The following commands run **in the PVE shell**, not Windows PowerShell:

```sh
cd /root
python3 --version
```

You need Python 3.9 or newer. If the command prints a suitable version, continue. If it says `python3: command not found`, have your PVE administrator install the distribution's `python3` package before continuing. No `pip install` command is required for this script.

### 3. Run the inventory

In the **PVE shell**, run:

```sh
python3 /root/pve_inventory.py --output-dir /root/pve-report
```

Because you are logged in as root, you do not need `sudo`. You also do not need `chmod +x`: the `python3` command opens the script for you.

For **your cluster**, answer the startup prompts as follows:

```text
Environment (cluster/standalone) [cluster]: cluster
Access: local=pvesh, cli=regular standalone commands, api=HTTPS token (local/api) [local]: local
```

Type the answer after each prompt and press Enter. Pressing Enter without typing accepts the answer in brackets. For a standalone PVE server, choose `standalone`, then either `local` for `pvesh` or `cli` for regular `qm`/`pvesm` commands. Actual default answers depend on what the script detects.

You will see progress such as `Reading VM 100 on pve1...`, followed by the report. Wait for `Reports written to /root/pve-report` and the shell prompt to return. Slow or unreachable nodes can take longer because each query has a timeout. You can stop collection with Ctrl+C.

### 4. Find and read your reports

All generated files are in **`/root/pve-report` on the PVE server**. These are the most useful starting points:

| File | What to do with it |
| --- | --- |
| `inventory.html` | Open on your Windows computer in a browser for storage totals, the largest VMs and sortable columns |
| `inventory.txt` | Read the same report as plain text |
| `vms.csv` | Open in Excel or another spreadsheet program for one row per VM |
| `storage_totals.csv` | Review overall configured capacity and measured allocation totals/subtotals |
| `disks.csv` | Inspect individual disks and reasons for unavailable metrics |
| `filesystems.csv` | Inspect available guest filesystem counters |

To read the saved text report directly **in the PVE shell**:

```sh
less /root/pve-report/inventory.txt
```

Use the arrow keys or Page Up/Page Down to scroll, then press `q` to exit `less`.

### 5. Copy the reports back to Windows

Open a **new PowerShell window on Windows**, or type `exit` to leave your SSH session first. Run this from Windows, replacing `PVE_ADDRESS` again:

```powershell
scp -r root@PVE_ADDRESS:/root/pve-report "$env:USERPROFILE\Downloads"
```

Open your Windows **Downloads → pve-report** folder, then double-click **`inventory.html`**. Click column headings to sort and expand a VM's details to see its disks and missing-data explanations. No web server is needed.

### 6. Run it again when needed

The script does not change VM configurations or backups. To preserve your previous results, use a new report directory each time:

```sh
python3 /root/pve_inventory.py --output-dir /root/pve-report-second-run
```

If you reuse `/root/pve-report`, its report files are overwritten. Copy the new directory back to Windows using the same `scp -r` pattern.

### Common first-run issues

| Message or situation | What to do |
| --- | --- |
| `can't open file ... No such file or directory` | Check that the upload succeeded. In the PVE shell, run `ls -l /root/pve_inventory.py`, then use the full run command above. |
| `Permission denied` or a message that a command requires root | Open the PVE host shell as root. Local reporting commands need root privileges. |
| `pvesh` or `qm` is not found | Confirm you are in the PVE host shell. These commands are not normally available on Windows or inside a guest VM. |
| Guest filesystem fields show `N/A` | The VM may be stopped, its guest agent may be unavailable, or access may be denied. Read the reason in the report; other VM data is still collected. |
| Actual disk allocation shows `N/A` | Some storage backends do not provide a supported per-disk allocation metric. Configured capacity remains separate and may still be available. |
| Report has collection warnings | Review those warnings and coverage counts before interpreting totals. A partial report is still produced when possible. |
| No startup questions appear | Prompts require an interactive terminal. Run the command directly in the PVE shell, or use the explicit options below. |

## Additional run options

Copy `pve_inventory.py` to a directory on a PVE node, then run:

```sh
python3 --version                       # Python 3.9 or newer
sudo python3 pve_inventory.py --output-dir ./pve-report
```

On an interactive terminal the script prompts **before collection** for:

1. **Cluster or standalone**, with a default detected from `/etc/pve/corosync.conf` and `/etc/pve`.
2. **Access method**: local `pvesh`, regular standalone CLI commands, or HTTPS API token. Cluster mode offers `pvesh` or HTTPS so all nodes are covered.

Local mode uses the installed `pvesh` command with your existing root privileges. It works for both clusters and standalone servers. It does not create a user or ask for credentials. PVE forwards node-specific API requests to the relevant cluster node. Local mode avoids an HTTPS connection; it does not disable TLS verification. If `python3` is absent, install your distribution's Python 3 package separately. Do not install Python packages into PVE's system environment for this tool.

For scheduled/non-interactive runs, give explicit options. `--no-prompt` suppresses all questions; redirected input also skips prompting. With environment `auto`, local detection is used; if detection is unavailable, cluster scope is the conservative default. HTTPS mode cannot identify a remote environment from local files: select the remote environment at the prompt or explicitly.

```sh
# Entire cluster, using pvesh from a member node:
sudo python3 pve_inventory.py --environment cluster --mode local --no-prompt --output-dir ./pve-report

# Standalone server, using regular PVE commands:
sudo python3 pve_inventory.py --environment standalone --mode cli --no-prompt --output-dir ./pve-report
```

The regular-command adapter uses only `qm list`, `qm config VMID --current 1`, `qm status VMID --verbose 1`, `qm guest cmd VMID get-fsinfo`, `pvesm status`, `pvesm path VOLUME`, and `pveversion`. It reads non-secret storage identity fields from `/etc/pve/storage.cfg`. File allocation uses the file's allocated-block counter (`stat.st_blocks * 512`); local ZFS allocation uses the read-only `zfs get -H -p -o value usedbydataset DATASET` command. It never treats `qm list`'s boot disk size or `pvesm status`'s whole-storage usage as a VM disk total.

Backup-job configuration and server-side inclusion still use optional `pvesh get` calls in CLI mode because `qm` and `pvesm` have no equivalent complete job-selection command. If `pvesh` is unavailable, VM inventory continues and backup membership is N/A. There is no automatic fallback that could turn a cluster inventory into a local-node-only report: regular CLI mode requires standalone scope, and detected cluster configuration blocks a standalone selection.

This creates `inventory.txt`, `inventory.html`, `vms.csv`, `disks.csv`, `filesystems.csv`, `summary.csv`, `storage_totals.csv`, and `inventory.json`. The same readable report is printed to the terminal; progress goes to stderr. Copy the HTML file to a workstation and open it in a browser. It is self-contained, works offline and loads no external scripts. All VM columns except the details column are sortable. Highlighting identifies the original ten largest VMs with known values, including stopped VMs and templates.

Storage totals appear first in the terminal summary and in prominent HTML cards:

- **Total configured VM disks:** logical capacity of distinct attached volumes, counted once across VMs/nodes according to storage identity.
- **Total measured VM disk allocation:** sum of distinct volume allocation counters, available only with complete configuration/identity coverage and one allocation metric meaning.
- **Known allocation subtotals:** available measurements grouped by their meaning, with volume coverage counts. Missing measurements remain N/A; a partial subtotal is not presented as the whole inventory's consumption.

`storage_totals.csv` exports integer `total_bytes` and `known_subtotal_bytes`, measured-volume and known-identity-volume counts, a completeness flag and accounting notes. These rows describe overlapping metrics and must not be summed together. JSON includes the same records under `storage_totals`. Allocation totals do not establish exclusive physical storage, space reclaimable by deleting VMs, or PBS consumption; shared extents, snapshots and backend overhead still affect accounting.

```sh
# Increase the per-call timeout for slow cluster/storage links.
sudo python3 pve_inventory.py --timeout 45 --output-dir ./pve-report-slow

# Skip guest-agent queries (e.g. if not permitted or agents are not installed).
sudo python3 pve_inventory.py --no-guest-agent --output-dir ./pve-report-no-agent

# Rank by allocation only if every VM has complete, comparable allocation data.
sudo python3 pve_inventory.py --sort actual --output-dir ./pve-report-actual
```

`--sort configured` is the default. With `--sort actual`, incomplete coverage, a diskless VM, or mixed allocation metric meanings cause a clearly recorded fallback to configured sorting. No VMs are silently removed to make an actual sort look complete. N/A always sorts after known numbers in both directions in the HTML table.

Use a fresh output directory for each run; an existing directory's report files will be overwritten. The only writes are these local report files. Restrict access to reports because they contain infrastructure names and storage identifiers. A partial report exits 0 and includes collection warnings and coverage counts; failure to obtain the VM list exits 1. No live cluster validation has been performed for your environment yet; PVE version and backend details have not been supplied.

## Optional HTTPS API-token mode

Use this when you prefer enforcement of read-only access by PVE's ACLs. It also runs from a non-PVE machine. Only Python's standard library is needed.

```sh
export PVE_URL='https://pve.example.internal:8006'
export PVE_TOKEN_ID='inventory@pve!report'
read -r -s -p 'Token secret: ' PVE_TOKEN_SECRET; echo
export PVE_TOKEN_SECRET
export PVE_CA_FILE='/etc/pve/pve-root-ca.pem'  # only for a private PVE CA
python3 pve_inventory.py --mode api --output-dir ./pve-report-api
unset PVE_TOKEN_SECRET
```

Use a hostname covered by the server certificate. System CA trust and hostname verification are enabled by default. For a private CA, set `PVE_CA_FILE` to the trusted CA PEM file. There is deliberately no insecure TLS switch. HTTPS redirects are refused so credentials cannot be forwarded to another host. A cluster API node must be reachable; other node-specific queries are proxied by PVE.

Alternatively, copy `config.example.json` to a separate `config.json`, replace its placeholders, protect it with `chmod 600 config.json`, and run:

```sh
python3 pve_inventory.py --mode api --config ./config.json --output-dir ./pve-report-api
```

Environment variables override corresponding JSON fields (`url`, `token_id`, `token_secret`, `ca_file`). Omit `ca_file` for a publicly trusted certificate. Secrets are not command-line arguments and configuration contents are not exported. The example contains placeholders only. Keep credential files out of source control.

## Permissions and read-only boundary

Local `pvesh` and regular PVE CLI commands require root; those privileges are broader than this script's actions. For least privilege enforced by the server, use token mode. The script exposes only fixed inventory GET endpoints and an allowlist of read commands. It never calls `pvesh set/create/delete`, POST/PUT/DELETE over HTTPS, guest command execution, or backend mutation commands. In CLI mode, `qm guest cmd` is restricted to the read-only `get-fsinfo` operation. It does not change VM states/configurations/jobs, prune, delete backups or run GC. Read operations can produce normal server access logs and some storage/guest-agent load.

For complete token-mode reporting, grant these privileges with propagation to the service user **and** its privilege-separated token:

| Privilege | ACL path | Purpose |
| --- | --- | --- |
| `Sys.Audit` | `/` (propagate) | Cluster/node visibility, backup-job configuration and selection |
| `VM.Audit` | `/vms` (propagate) | All VM configurations and current status; backup selection visibility |
| `Datastore.Audit` | `/storage` (propagate) | Storage configuration and volume attributes |
| `VM.GuestAgent.Audit`, where supported | `/vms` (propagate) | Optional guest filesystem query |

These form a custom reporting role; no allocation, modification, power-management or backup-execution privilege is needed. An existing `PVEAuditor` role is a convenient audit-only starting point, but inspect its privileges on your installed version. Both the token and owning user's ACLs apply when privilege separation is enabled. Verify permissions across all nodes, VMs, storages and pools before treating visible totals as complete. API resource lists can silently filter objects the caller cannot see.

Guest-agent privilege checks vary by PVE release: recent releases expose `VM.GuestAgent.Audit`; older releases can require the broader `VM.Monitor` permission. Do not add that broader privilege just to obtain this optional metric: use `--no-guest-agent` or accept N/A. Root local mode can read the endpoint if the VM is running and the guest agent is available. No agent is installed or enabled by this tool.

Only these API paths are used, always as GET:

```text
/version
/nodes
/cluster/resources?type=vm
/storage
/cluster/backup
/cluster/backup/{job-id}/included_volumes
/nodes/{node}/qemu/{vmid}/config?current=1
/nodes/{node}/qemu/{vmid}/status/current
/nodes/{node}/qemu/{vmid}/agent/get-fsinfo
/nodes/{node}/storage/{storage}/content/{volume-id}
```

Each command/request has a configurable timeout (local file metadata reads depend on the operating system's filesystem timeout). Offline nodes retain their VMs as N/A rows; other per-VM/per-storage failures are recorded and collection continues. The initial VM list is essential. A VM migration during collection can produce a partial row; rerun after migrations finish. Data is collected sequentially to limit load. Runtime scales with VM/disk count and failed-call timeouts.

## Columns and accounting

| Column | Meaning |
| --- | --- |
| VM ID, name, node | QEMU resource identity and current owning cluster node |
| Status | Current status API response; unavailable current status is N/A, with cached cluster status disclosed separately |
| Tags | Configured tags; an empty field means no tags, N/A means not observable |
| Template | Whether this resource is a QEMU template; included for storage review |
| Configured capacity | Sum of unique attached writable disk `size=` values in the current configuration, including EFI/TPM state; not the boot-disk-only `maxdisk` field |
| Actual allocation | Sum of unique attached disk allocation counters, only when every disk is measured and the metric meanings match; see backend notes below |
| RAM allocated | Maximum configured RAM, converted from MiB; PVE's documented 512 MiB default is used when an accessible configuration omits memory |
| PVE memory used | Current `mem` counter in bytes; PVE-reported usage, potentially influenced by ballooning/guest statistics, not a guest process/RSS total |
| vCPU | Configured `vcpus` if present; otherwise configured cores × sockets, using PVE's defaults of one each |
| Enabled backup job? | Yes if selected by an enabled scheduled PVE job, No if all job selections are known and none match, N/A if selection is unresolved |
| Enabled/disabled jobs | Matching job IDs separated by job enabled status; membership is evaluated by PVE's own `included_volumes` endpoint, including pools, exclusions and node constraints |
| Disk details | Slot, volume ID (storage location), storage ID, configured capacity/source, allocation/source or reason, and configured disk backup flag |
| Guest filesystems | Mount/device/type, agent total/used/free bytes, with per-filesystem reasons; these are not assigned to host disks or summed |
| Notes / warnings | Missing metrics, permission failures, timeouts, stale status and incomplete coverage |

All `*_bytes` CSV fields are integer bytes, with literal **N/A** for unavailable values, distinct from **0**. The VM CSV also has human-readable binary unit columns. JSON uses `null` for unavailable values. VM CSV order matches terminal order; structured disk and filesystem JSON fields preserve details in each VM row. `disks.csv` and `filesystems.csv` provide one row per attachment/mount for easier analysis. Text CSV fields that could be spreadsheet formulas are escaped with a leading apostrophe; JSON preserves raw text.

Read VM configs using `current=1`: pending disk changes are not counted. CD-ROM/ISO, cloud-init media and `unusedN` references are listed with exclusion reasons and do not enter totals. A missing disk size makes the VM's configured total N/A; no guessed size or boot-disk proxy is used. Per-VM disk totals deduplicate repeated references. Cluster logical totals additionally deduplicate shared volumes across nodes, and distinguish local volumes on different nodes. Missing storage identity/configuration prevents a complete cluster logical total; a separately labeled known-volume subtotal remains available. Storage IDs that alias the same backing volume cannot be discovered reliably, and volume sharing through clones is not equated with equal physical allocation. RAM/vCPU subtotals include only known VM metrics, with coverage counts.

### Actual allocation availability

The generic storage `used` field is **not** trusted for arbitrary plugins. The first version accepts the volume-info API's `used` field only for these backends:

| Backend | Meaning / limits |
| --- | --- |
| Directory, NFS, CIFS, CephFS, GlusterFS | PVE file allocation metric (allocated bytes, not virtual disk length). Does not establish unique physical usage on deduplicated/reflinked/compressed backing storage or include external backing-chain files. |
| Local ZFS (`zfspool`) | PVE's `usedbydataset` value. Excludes snapshot usage and reservations; compression, sharing and clones affect accounting. Not pool consumption or space that deleting the VM would reclaim. |
| LVM, LVM-thin, Ceph RBD, iSCSI, ZFS over iSCSI, other plugins | N/A: a validated per-volume allocation metric is not implemented here. The script will not substitute a virtual size or a plugin's placeholder zero. |

Absent `used` on a supported backend remains N/A; genuine returned zero is preserved. Even complete per-VM allocation counters do not establish a unique cluster physical footprint. There is deliberately no summed physical-use claim. File and ZFS allocation semantics cannot be mixed for `--sort actual`. Snapshots, unreferenced disks, base images and other storage consumers are not a complete datastore audit; the report is a VM review inventory.

Guest usage requires a running VM, an installed/enabled QEMU guest agent and permission. Agent data may omit usage counters, especially on older guest agents. Free is calculated only from valid reported total/used values and means `total-used`, not necessarily the space available to a particular guest user. Repeated mounts, network shares, pseudo-filesystems and overlapping devices make a guest-wide total unsafe, so each returned filesystem remains separate.

## Investigating the 2 TB PBS problem

Use the largest-VM list to prioritize investigation, then inspect enabled job membership and `backup=0` disks. The report lists visible PVE backup-job schedules, destination storage and explicit job-level prune overrides; these are configuration hints, **not** effective PBS retention. Unset overrides can inherit other settings. Disabled jobs do not imply old backups have been removed. Current jobs also do not reveal every VM with historical snapshots.

Nominal 2 TB is about 1.82 TiB before filesystem overhead. Neither sum(configured disks) nor sum(guest used) is PBS consumption. PBS stores deduplicated, compressed chunks shared by VMs and snapshots. Requirements depend on the initially backed-up data, changed data rate, retention windows, sharing/compression, and cleanup progress. One VM's logical backup size cannot reliably attribute shared PBS chunks to that VM. Old/deleted VMs, containers and other clients may still have backup groups. Prune and GC are separate steps, with GC safety delays.

A future PBS phase should collect datastore total/used/available, groups and snapshot counts, effective retention/prune configuration, and recent prune/GC results. This version explicitly makes **no conclusion** about whether the datastore is undersized or cleanup is failing, and performs no changes.

## Verification and sources

Run the offline tests on any Python 3.9+ machine:

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic API responses; they do not contact PVE or PBS. They cover accounting gaps, genuine zero, shared/local volume deduplication, offline nodes, backup-selection failures, metric consistency, HTML escaping, CSV formula escaping and the read-only transport boundary.

API behavior was checked against upstream Proxmox sources. Your installed version may differ; unsupported endpoints are reported as unavailable:

- [PVE shell API](https://github.com/proxmox/pve-docs/blob/master/pvesh.adoc)
- [QEMU configuration and status API](https://github.com/proxmox/qemu-server/blob/master/src/PVE/API2/Qemu.pm)
- [Guest-agent API methods and privileges](https://github.com/proxmox/qemu-server/blob/master/src/PVE/API2/Qemu/Agent.pm)
- [Storage volume API and warning about generic used metrics](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Storage/Content.pm)
- [File storage plugin](https://github.com/proxmox/pve-storage/blob/master/src/PVE/Storage/Plugin.pm)
- [ZFS storage plugin](https://github.com/proxmox/pve-storage/blob/master/src/PVE/Storage/ZFSPoolPlugin.pm)
- [Backup-job API and server-side inclusion endpoint](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Backup.pm)
- [Regular qm command implementation](https://github.com/proxmox/qemu-server/blob/master/src/PVE/CLI/qm.pm)
- [Regular pvesm command implementation](https://github.com/proxmox/pve-storage/blob/master/src/PVE/CLI/pvesm.pm)
