# ProxInfo environment inventory

On the **VM's** page, click a VM row to open its information. Close the panel with
**Back To VM List**, the Escape key, or a click on the dimmed area outside it.
The close button stays visible while scrolling. Keyboard users can focus a row
and press Enter or Space; closing the panel returns focus to that row. Links and
expandable explanations inside rows retain their own actions. VM information no
longer needs a separate table column.

## PBS sign-in and measured totals

Run manually from WSL/Ubuntu using `python3 pve_inventory.py --mode ssh --pbs`.
The PBS connection question comes before PBS credentials: press Enter for `ssh`
(the default), choose `password` for a PBS web account, or `token` for an API token.
SSH then offers password, key, or configured authentication. PBS stays off
unless requested; complete existing token configurations continue to work.

Web-account example:

```bash
python3 pve_inventory.py --pbs-only --pbs-auth password --pbs-url https://pbs.example:8007
```

Enter the account including its realm, such as `inventory@pbs`. Password entry
is hidden. Supply a trusted CA with `PBS_CA_FILE` or `pbs_ca_file` in your config
if your computer does not trust the PBS certificate. TLS verification stays on.
Interactive TOTP challenges are supported; browser/security-key sign-in requires
an API token instead. Sessions renew after an hour; rejected sessions are reported
without repeated password prompts. No password or ticket is saved to a report.

SSH example:

```bash
python3 pve_inventory.py --pbs-only --pbs-auth ssh
```

Python and output files stay on your machine. PBS must already have
`proxmox-backup-debug`; the script runs only approved `api get` queries and
optional `findmnt` reads. Use root or an account whose sudo policy permits the
required commands. For a separate password/MFA prompt, add
`--pbs-ssh-sudo-mode interactive`. For an existing noninteractive sudo setup,
use `--pbs-ssh-sudo-mode noninteractive`; the legacy `pbs_ssh_sudo: true` config
setting retains that noninteractive behavior. A PBS web account alone does not grant SSH access.
The program does not install tools or change sudo permissions. API password and
token users need the audit permissions documented below; they do not need root.

For scheduled SSH collection, place these non-secret settings in a JSON file:

```json
{
  "pbs_auth": "ssh",
  "pbs_url": "https://192.0.2.40:8007",
  "pbs_ssh_host": "192.0.2.40",
  "pbs_ssh_user": "root",
  "pbs_ssh_auth": "key",
  "pbs_ssh_key": "/root/.ssh/id_ed25519"
}
```

```bash
python3 pve_inventory.py --cron --pbs-only --config /root/proxinfo.json --output-root /root/pve-report
```

Establish SSH host trust beforehand. Cron never prompts. SSH password mode is
rejected for unattended runs. API tokens are also suitable for cron. HTTPS
password credentials may be supplied through `PBS_USERNAME`/`PBS_PASSWORD` or
protected config keys `pbs_username`/`pbs_password`, but interactive MFA cannot
run in cron. Do not put secrets in command arguments or commit credential files.

PBS settings also accept environment variables: `PBS_AUTH`, `PBS_URL`,
`PBS_SSH_HOST`, `PBS_SSH_USER`, `PBS_SSH_KEY`, `PBS_SSH_AUTH`, `PBS_SSH_PORT`,
`PBS_CA_FILE`, and the existing `PBS_TOKEN_ID`/`PBS_TOKEN_SECRET`. SSH port uses
your SSH configuration or port 22; there is no port prompt. Set `pbs_url` to the
same server identity when switching transports to retain matching/history scope;
aliases are not automatically assumed to identify the same server.

Summary, Management Summary, and the Virtual Machines footer now show known
measured amounts with **Complete** or **Partial** status. Unreadable disks do not
hide measurements from other disks. Shared disk identities count once; conflicting
observations are excluded. Incompatible measurement types have separate numeric
subtotals. A measured zero is valid; no readings is not zero. Complete-total JSON
fields keep their original meaning, with known subtotal fields for partial data.

Summary follows the node selection; the VM footer follows its node/search filters.
Management Summary, print, and downloaded files cover the full collected scope.
Compare totals at the same scope. Assigned capacity, storage allocation, guest
filesystem usage, and PBS usage remain distinct.

Navigation is now Summary, Needs Attention, PVE Hosts, Virtual Machines, Storage,
PVE Backup Jobs, PBS Backup Storage, Management Summary, Downloads, Help, IT Reference.
The Changes web page was removed; optional `--previous-report` comparison exports
remain supported. Buttons use capitalized words while preserving acronyms.

Before relying on a new PBS connection in production, run one datastore manually,
compare capacity and visible backup counts with PBS, then test the same settings
with `--no-prompt`. Automated tests use simulated servers, not your live PBS.

## Environment overview additions (1.4.0)

Run the updated script normally; no new options are required for the VM table, host overview or downloads:

```bash
python3 pve_inventory.py --mode ssh --no-pbs --diagnostics --output-dir ./pve-report
```

RAM assigned, RAM in use and assigned vCPUs are visible by default. RAM usage is a snapshot, not a live monitor. The VM footer totals assigned disk space, measured disk usage and assigned RAM for shown rows; it does not sum CPUs. Unknown RAM produces a known subtotal and coverage, not a false complete total. Powered-off VMs and displayed templates retain their configured RAM in this total. The optional resource checkbox can still hide these columns.

**PVE view** selects a node across Summary, PVE hosts, Virtual machines, Storage and Review list. Search and missing-usage filters continue to affect the VM table. PBS, backup-job definitions and downloadable files retain their full collected scope, as indicated on screen. VM information opens in a separate panel so it does not widen table rows. Shared storage references remain deduplicated within the selected view.

**PVE Hosts** shows host status, discovered IP, PVE version, uptime, CPU details, physical memory and storage-location capacity. New GET reads are `/cluster/status`, `/nodes/N/status` and `/nodes/N/storage`; node audit and storage audit visibility are needed. Offline hosts are retained without querying their status/storage. Regular standalone CLI mode currently leaves this added host-detail page unavailable; the original VM inventory remains usable. Storage capacities can overlap across locations and hosts and are never summed into a claimed physical cluster total.

**Downloads** links to existing CSV/JSON/print files plus `hosts.csv` and `changes.csv`. Keep the exported files together. Collection status summarizes which host and storage reads returned data; that is not an overall infrastructure health certification.

### Comparing two collected reports

Keep an earlier `inventory.json`, then provide it explicitly:

```bash
python3 pve_inventory.py --mode ssh --previous-report ./earlier/inventory.json --output-dir ./pve-report
```

Use reports from the same environment. Selection scope and node membership must match; otherwise comparison is withheld. Changes include newly visible/missing IDs, node placement and disk/RAM/vCPU assignments. Comparable complete measured disk totals can also change; missing values are never treated as zero, and changed disk identities or measurement methods prevent an allocation comparison. Changed fields ending in `_bytes` are exported as exact bytes. VM ID reuse and changed permissions can affect interpretation. An absent VM is not automatically labeled deleted. Without `--previous-report`, this page explains that no baseline was selected. This is an explicit local comparison, not automatic PVE history retention.

### PBS additions

PBS remains off by default. Enable it with the existing `--pbs` options. Its page now has Overview, Backups, Activity, Retention & cleanup, and Evidence controls. Configured datastore folders and the configured PBS server are shown. Backup task outcomes/durations and the latest confirmed successful task in checked history are separate from snapshot freshness; they do not establish every VM's backup success. Sync-job visibility uses GET `/admin/sync`; applicable datastore audit and remote audit visibility are required, depending on server version and job direction. Missing access remains unavailable; no job is started.

Retention review candidates expose protected snapshots and groups without a visible applicable PBS prune policy. Other retention mechanisms may apply. These are review prompts, not deletion advice or recoverable-byte estimates. Backup coverage is displayed as a table while retaining ownership uncertainty. CSV/JSON preserve the collected detail, with the added task and sync metadata in JSON.

For optional backing-filesystem discovery, add these settings to your existing protected config file:

```json
{
  "pbs_ssh_host": "192.0.2.40",
  "pbs_ssh_user": "audit",
  "pbs_ssh_key": "/home/your-user/.ssh/id_ed25519"
}
```

Replace the documentation address with your PBS host. This is separate from the PVE SSH connection. It runs only `findmnt` read queries for configured datastore folders, reporting the observed mount point, filesystem and device/dataset/share. The operator must configure the SSH host to match the PBS API server; the report labels the observation source. It does not recursively inspect physical disks, mount storage or identify S3 bucket usage from a local cache. API-only PBS reports remain supported without these settings. Manual SSH authentication follows OpenSSH; cron requires preconfigured noninteractive keys and host trust. Keep host-specific SSH port changes in `pbs_ssh_port` if needed. Optional filesystem lookup honors the same PBS SSH sudo flags, environment variables and configuration keys as primary PBS SSH reporting. When primary reporting already uses SSH, it reuses that session instead of opening another login.

An optional locally maintained JSON file can record completed restore tests:

```json
[
  {"server":"pbs01","datastore":"backups","source":"vm/100","date":"2026-11-04","result":"Passed","notes":"Operator confirmed application startup in isolated test."}
]
```

Supply it using `--restore-tests ./restore-tests.json`. These are explicitly labeled operator records, not tests performed or independently verified by ProxInfo. Verification results and restore tests remain separate. No restore operations are launched.

Validate new host and PBS readings against your own web consoles before relying on scheduled output. Automated tests simulate endpoints and cannot establish visibility or correctness on your live servers.

Read-only Python inventory that runs on **WSL or Ubuntu and connects to PVE over SSH**, or runs directly on PVE. Optional PBS reporting is **off by default**. No third-party Python dependencies. Cluster mode inventories all visible QEMU VMs and templates. LXC containers are outside this VM inventory; SSH/API/pvesh mode counts visible containers, while regular CLI mode reports that count as N/A.

## Recommended: run from WSL or Ubuntu over SSH

**Python runs on your WSL/Ubuntu machine. PVE does not need Python.** The script connects to one PVE host, executes its existing read-only `pvesh get` commands, and produces every report on the machine where you launched it. No script upload or manual report download is needed. The same SSH mode supports clusters and standalone servers.

### 1. Open your WSL or Ubuntu terminal

For WSL using this project's existing Windows location:

```sh
cd /mnt/c/Users/tbrigham/dev/ProxInfo
```

On a separate Ubuntu machine, copy `pve_inventory.py` to a folder there and open a terminal in that folder. Only the script is needed; the `tests` folder is for development checks.

Check your **local** requirements:

```sh
python3 --version
ssh -V
```

You need Python 3.9+ and OpenSSH locally, plus SSH access to PVE. If either command is missing on Ubuntu/Ubuntu under WSL, install the local packages:

```sh
sudo apt update
sudo apt install python3 openssh-client
```

No `pip install` or virtual environment is required. Run the inventory as your normal local user; local `sudo` is not required.

### 2. Run the script and answer its prompts

```sh
python3 pve_inventory.py --output-dir ./pve-report
```

First choose how to connect. Use **`ssh` for your normal PVE account login**, or **`api` to paste a token created in the PVE web interface**. Only the questions for your chosen method are shown. For your cluster over SSH, choose `cluster` and enter the address of **one PVE cluster member**:

```text
Connection method (ssh/api/local/cli) [ssh]: ssh
Environment (cluster/standalone) [cluster]: cluster
SSH sign-in method (password/key/auto) [password]: password
PVE SSH hostname or IP address: 192.168.1.10
SSH username [root]:
```

Replace the example address with yours. Press Enter for defaults. For a single server, choose `standalone`. Supplying `--mode ssh` or `--mode api` skips the connection-method question.

Choose **`password`** to sign in with the account password you normally use for SSH to PVE. OpenSSH will ask for it when connecting. Password entry shows no characters; type it and press Enter. The script never reads or stores the password. Choose **`key`** if you already have an SSH key file; it then asks where that file is on your local computer. You can press Enter at the key-file question to use your existing default SSH keys. **`auto`** lets OpenSSH try its normal configured methods. The confusing key-file question is no longer shown to password users.

To use a token instead, select **`api`** at the first question. The script asks for the PVE API host, **API Token ID** (such as `inventory@pve!report`), **API Token Secret** (the Value shown when you created the token), and an optional trusted CA certificate file. Paste the secret at the hidden-input prompt. The token secret is not your account password or the token's name. It is not written into the reports. If your server uses the private PVE CA, provide a trusted copy of its CA PEM file; certificate verification remains enabled. See the API permissions section below.

PBS backup-server reporting is optional and **off by default**. At `Optional PBS backup-server report (include/skip) [skip]`, press Enter to skip it. Only selecting `include` asks for PBS connection details. `--no-pbs` skips this question too. PVE inventory and all PVE review/summary exports still work when PBS is skipped or unreachable.

On a first connection, SSH may ask you to confirm the host fingerprint. Verify it against the PVE server before accepting. Existing host keys remain checked. WSL uses its own SSH configuration and keys unless you have explicitly configured access to Windows keys.

### 3. Open the local reports

Wait for the VM progress messages and `Reports written to ...`. The output directory is **`pve-report` inside the directory where you ran the command**, not on PVE. Start with `inventory.html` for storage totals and the sortable VM table, or `vms.csv` for spreadsheets. All other exports are saved alongside them.

From WSL, open the output folder in Windows Explorer:

```sh
explorer.exe ./pve-report
```

Double-click `inventory.html`. The top navigation switches between **Summary**, **Needs Attention**, **PVE Hosts**, **Downloads**, **Virtual Machines**, **Storage**, **PVE backup jobs**, **PBS backup storage**, **Management Summary**, **Help**, and **IT reference**. Each is its own report view. The main report remains one offline HTML file; the printable management summary is also exported separately. No web server or external assets are required. A link ending in `#machines`, for example, opens the Virtual machines view directly.

The **Summary** uses plain-language cards: machines reviewed, assigned disk space, measured storage used and machines scheduled for backup. **Explain**, **What does this mean?**, and **?** links open readable explanations without leaving your place. Use **Help** for a short guide and all definitions. Original collection errors, measurement rules and technical totals are kept in **IT reference** for the IT team.

In **Virtual Machines**, search by name, ID, host server or label. Filter by host server or show only machines whose usage could not be measured. **Power** shows On, Off or Unknown. Click column headings to sort, or their **?** links for definitions. RAM and CPU columns are shown by default. Uncheck **Show memory & CPUs** for a narrower view.

The storage-total row **below the VM rows** updates for the machines currently shown. It keeps **assigned disk space** separate from **storage used**, counts a shared disk ID once, and shows known measured amounts with coverage, separating incompatible measurement types. Known partial amounts and an explanation are shown when available. Filtering can therefore give a complete total for a readable subset even when the whole report has missing data. These are VM disk totals, not PBS backup usage; shared data and other storage overhead prevent treating them as a unique physical-storage footprint.

Open **View VM information** for separate sections: **Disks and storage**, **Space inside the VM**, **Backup schedules**, **Memory and processing**, **Data availability**, and **IT reference for this VM**. Data-collection problems are explained separately from the disk and drive information. **N/A · why?** in the Storage used column gives a specific explanation, and **Storage measurements** explains why a full total may be unavailable.

Use **Dark mode / Light mode** at the top to change the theme. It initially follows your system preference and saves your choice when browser storage is available. To see changes made by a newer script, run the updated script and open its newly generated `inventory.html`.

On Ubuntu with a desktop, open the folder with your file manager and open `inventory.html` in a browser. On either system, read the terminal report with:

```sh
less ./pve-report/inventory.txt
```

Press `q` to exit `less`. Use a new output directory for later runs to preserve previous results; reusing a directory overwrites its report files.

### SSH options and permissions

```sh
# Supply the host now; still prompt for remaining connection settings.
python3 pve_inventory.py --mode ssh --ssh-host 192.168.1.10 --output-dir ./pve-report

# Key-authenticated unattended run: no interactive questions or SSH prompts.
python3 pve_inventory.py --mode ssh --environment cluster --ssh-host pve1 --ssh-user root --no-prompt --output-dir ./pve-report

# Custom port and local private key.
python3 pve_inventory.py --mode ssh --ssh-host pve1 --ssh-port 2222 --ssh-key ~/.ssh/id_ed25519 --output-dir ./pve-report
```

`--ssh-auth-timeout` defaults to 120 seconds for SSH authentication; `--timeout` defaults to 20 seconds per query. SSH reuses its connection. Interactive sudo uses a persistent remote terminal session and can request fresh authentication when the server's policy requires it. Its read timeout is the query timeout plus the authentication timeout, capped by the remaining `--max-runtime` budget, because PAM prompts and command execution share the same terminal. On completion, failure or Ctrl+C, the script closes its connection and removes its local temporary socket. No script or report files are created on PVE.

Use `root` on PVE for direct access, or another remote account with permission to run the required commands through sudo. Select `--ssh-sudo-mode interactive` when sudo asks for a password or MFA response. With an already configured noninteractive sudo policy, use `--ssh-sudo-mode noninteractive` or the existing `--ssh-sudo` flag. The script does not configure sudo or grant permissions. Without sufficient permission, metrics remain unavailable or the initial inventory fails. HTTPS API-token mode remains available for server-enforced audit-only access.

Use `--ssh-auth password`, `--ssh-auth key` or `--ssh-auth auto` to preselect the SSH sign-in method. Optional environment variables are `PVE_SSH_HOST`, `PVE_SSH_USER`, `PVE_SSH_PORT`, `PVE_SSH_KEY`, `PVE_SSH_AUTH`; corresponding JSON settings are `ssh_host`, `ssh_user`, `ssh_port`, `ssh_key`, `ssh_auth`. Command-line SSH fields override environment/config settings. SSH config aliases and identities are supported; the default username is root unless overridden. `--no-prompt` also enables SSH batch mode: configure key authentication and establish host trust beforehand. Explicit password login is incompatible with `--no-prompt`.

For SSH failures, read the SSH error above the script's failure message and check address, account, credentials and network access. A missing `pvesh` command usually means the target is not a PVE host. Launch this mode from WSL rather than native PowerShell, because it uses POSIX OpenSSH connection multiplexing. **Regular Ubuntu and WSL use the same script and options.** Live testing against your PVE environment remains outstanding.

### Separate sudo password or Okta authentication

Available in ProxInfo 1.5.0. Check your installed copy with
`python3 pve_inventory.py --version` after pulling the update.

SSH login and sudo authentication are separate steps. If you normally SSH as your
own account and then enter a password and Okta token when running sudo, use:

```bash
python3 pve_inventory.py --mode ssh --ssh-user your-user --ssh-sudo-mode interactive
```

For PBS SSH reporting with the same kind of workflow:

```bash
python3 pve_inventory.py --pbs-only --pbs-auth ssh --pbs-ssh-user your-user --pbs-ssh-sudo-mode interactive
```

After SSH authentication, follow the remote sudo/PAM prompts. If your organization
requires `password,Okta-token`, enter the entire response in the hidden prompt
exactly as you normally would. The script does not split, save, or replay that
response, and does not reuse the SSH password. Multiple authentication prompts
are supported. Never put the password/token in a command, configuration file, or
report. Use a normal interactive WSL/Ubuntu terminal for this mode.

Manual runs ask which sudo mode to use for a non-root account, with `interactive`
as the suggested choice. Explicit flags or saved settings skip that question.
An account running as root does not need sudo. SSH authentication can use a key
even when sudo still needs a separate password/MFA response.

Interactive sudo keeps one unprivileged remote terminal session open and runs
approved inventory commands through sudo in that session. It does not start a
persistent root shell or alter the server's sudo policy. The account must already
be allowed to run the needed commands; a successful password/MFA challenge does
not grant commands denied by sudoers.

This mode checks that the remote server already has Perl and its core `JSON::PP`,
`MIME::Base64`, `Text::ParseWords` and `Socket` modules. It never installs missing tools. No remote Python,
uploaded helper file, or report file is required; the session helper runs in
memory, and command output is returned to the local script for report generation.
An actual read-only `/version` query checks access before the PVE or PBS inventory
starts, so an unusable session is identified early.

Your policy determines how long authentication can be reused. A short timeout or
per-command authentication policy may require another fresh response during the
run. Each actual sudo command follows that policy; the script does not depend on
a separate `sudo -v` establishing authorization for later commands. Query start
and completion remain visible, but repeated waiting messages pause for the entire
interactive read so they cannot interrupt prompts. Query timings include any
password/MFA prompt time. Authentication input/transcripts are excluded from
collection logs, diagnostics and reports. Remote sudo/PAM prompts and command
error output are displayed live in the terminal, not saved as captured transcripts.
Starting with ProxInfo 1.5.1, a running VM is queried for guest filesystems only
when its current configuration enables the QEMU guest agent. A missing, disabled,
or unreadable agent setting leaves guest filesystem usage unavailable; disk,
RAM and CPU collection continues. `--no-guest-agent` skips these optional queries
entirely. Guest tools are not required to measure storage-side disk allocation.

Starting with ProxInfo 1.5.2, these recognized PVE query failures leave the
interactive session open so other disks, VMs and hosts can still be collected:

- Guest filesystem reads: “No QEMU guest agent configured” or “QEMU guest agent
  is not running,” including the proxy-handler form.
- Storage-content reads: ZFS reports “cannot open …: dataset does not exist,”
  including the proxy-handler form. This applies to any node/storage name and
  includes EFI disks. Only that disk's measured usage is unavailable; its known
  assigned capacity remains in the report.

These exceptions require a short, exact diagnostic from the appropriate endpoint.
Logs receive a fixed explanation and the requested resource identity, never the
captured sudo/PAM conversation. A missing-dataset lookup does not establish whether
that disk was deleted, moved, or incorrectly referenced. ProxInfo does not recreate
disks, change references, activate volumes, or repair storage.

Other failed interactive reads remain conservative: unrecognized errors,
sudo/PAM failures, connection loss, cancellation and timeouts close the session.
Credentials and failed queries are never automatically replayed. Already collected
data is preserved. Later reads needing the closed session are recorded as
“not collected,” with the original failed operation identified, rather than counted
as new remote failures. The report groups the affected VMs under that interruption.
PBS uses a separate session; these PVE exceptions do not apply to PBS commands.

Query outcomes distinguish successful reads, optional metrics unavailable, disks
not found, failed requests and reads not collected. The sudo/MFA reminder appears
once per session; actual authentication prompts still appear whenever the server
requires them. Query timing still includes time spent at authentication prompts.

Partial storage totals show compatible known readings and their coverage across
terminal, HTML and CSV reports. For example, “121 of 122 identified disks; 31 VM
configurations unavailable” means the number of disks in those 31 VMs is unknown.
It does not mean all but one disk in the cluster was measured. Known subtotals are
not complete physical-pool usage or PBS consumption; incompatible methods remain
separate, and missing values are never substituted with zero or assigned capacity.

PBS alerts explicitly flag zero available capacity and unsuccessful cleanup tasks,
including locking errors such as ENOLCK. These are prioritized in Needs Attention
and the Management Summary. “PBS collection succeeded” means the status could be
read, not that the server's backup/cleanup jobs succeeded. Old GC statistics are
not proof of how much a later successful cleanup would free. ProxInfo does not
run prune/GC, remove locks, or change retention.

For a manual check after updating, verify the version and run:

```bash
python3 pve_inventory.py --version
python3 pve_inventory.py --mode ssh --ssh-sudo-mode interactive --no-pbs
```

Leave off `--no-pbs` to choose whether to include PBS during setup. An unavailable
guest agent or missing ZFS disk should leave a specific report entry while later
VM configurations and host reads continue. If the session actually closes, the
report should identify the original operation and show the remaining reads as
not collected. Live Linux/SSH/Okta verification is performed in your environment.


PVE and PBS use separate sessions and permissions. Sudo on the entry PVE node does
not authenticate a different node: host-level reads on other cluster members still
depend on the existing cluster routing and trust described below.

Use `none` for direct execution without sudo, `interactive` for manual password/MFA
prompts, or `noninteractive` when the required sudo commands already work without
prompts. `--cron` and `--no-prompt` reject interactive sudo before collection.
For scheduled runs, use local PVE access, an API token, or SSH keys together with
already configured noninteractive command permissions.

PVE settings can use `--ssh-sudo-mode`, environment variable `PVE_SSH_SUDO_MODE`,
or JSON key `ssh_sudo_mode`. PBS uses `--pbs-ssh-sudo-mode`,
`PBS_SSH_SUDO_MODE`, or `pbs_ssh_sudo_mode`. Command-line values take precedence
over environment variables and config values. Existing `--ssh-sudo` and
`pbs_ssh_sudo: true` retain their noninteractive meaning.

For slower authentication, increase `--ssh-auth-timeout` on PVE or
`--pbs-ssh-auth-timeout` on PBS; both default to 120 seconds. Interactive PBS reads
use the PBS query timeout (`--pbs-timeout`) plus that authentication timeout.
The remaining `--max-runtime` collection budget caps that combined wait.
These are bounded waits, not guarantees that the server will allow more time;
server-side authentication policies still apply.

Tests use simulated sessions. Before relying on this workflow in production, run
a small manual inventory against your Linux/Okta environment and confirm both
the initial sudo prompt and any policy-required reauthentication work correctly.

## Alternative: run directly on PVE (no Python experience needed)

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
Connection method (ssh/api/local/cli) [local]: local
Environment (cluster/standalone) [cluster]: cluster
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

1. **Connection method**: SSH (account login), API (GUI-created token), local `pvesh`, or regular standalone CLI commands.
2. **Cluster or standalone**, with a default detected from local PVE files for local methods. Remote methods ask about the remote target, rather than assuming your local machine is PVE.

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

You can reuse an output directory; its report and diagnostic log files are replaced on each run. Other files and subdirectories are left alone. Use a new directory when you want to preserve previous results. Reports are written locally. SSH mode also uses a temporary local connection socket; OpenSSH may update local known-host records when you accept a new host. Restrict access to reports because they contain infrastructure names and storage identifiers. A partial report exits 0 and includes collection warnings and coverage counts; failure to obtain the VM list exits 1. No live cluster validation has been performed for your environment yet; PVE version and backend details have not been supplied.

## Optional HTTPS API-token mode

Use this when you prefer enforcement of read-only access by PVE's ACLs. It also runs from a non-PVE machine. Only Python's standard library is needed.

For an interactive setup, select `api` at the first connection-method prompt, or run `python3 pve_inventory.py --mode api`. The script prompts for the API host, Token ID, hidden Token Secret and optional trusted CA file. You do not need to create a configuration file. The variables below are an alternative if you prefer to supply settings in advance.

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

## Review list, storage breakdowns and management handoff

**Needs Attention** contains findings with the machine/datastore, evidence and a suggested follow-up. The rules flag large assigned disk space, large machines currently off, disks marked `backup=0`, unreadable settings, incomplete measurements, PBS capacity at the review threshold and reported prune/GC failures. An off machine is not assumed unused. Intentionally unselected VMs do not generate missing-schedule warnings. These are review prompts; nothing is excluded, changed or deleted.

Defaults are **500 GiB** assigned space for a large machine and **90%** used filesystem capacity for a PBS capacity review. Override them explicitly:

```sh
python3 pve_inventory.py --large-vm-gib 750 --pbs-warning-percent 85
```

On **Storage**, the host breakdown counts **local disks on each host**, then lists **shared disks in a separate cluster-wide row**. The storage-location breakdown separates identically named local storage on different hosts, while counting a shared volume ID once. Machine counts may repeat when a VM has disks in several locations. These are attached-disk totals, not pool capacities. Missing settings prevent a full total; known subtotals remain labeled. Do not add the host table to the location table: they are two views of the same disks.

**Management Summary** gives a short handoff with key totals, up to five findings, five largest machines, and up to three PBS datastore capacity entries. Open **Print / PDF view**, or open `management-summary.html` directly, then use **Print / Save as PDF** in that file. The summary flows across pages as needed. Choose Letter or A4 in your browser print dialog; table headings repeat and rows stay together where possible. All findings, machines and stores remain in the full report and CSV files. Keep `management-summary.html` beside `inventory.html` to use its link back to the full report.

Additional exports:

| File | Contents |
| --- | --- |
| `review_items.csv` | Every finding, supporting evidence and suggested follow-up |
| `storage_breakdowns.csv` | Host/local/shared and storage-location totals, coverage and unknown-data counts |
| `management-summary.html` | Separate printable summary; no additional Python libraries are needed |
| `pbs_datastores.csv` | Filesystem capacity, inventory coverage, retention/prune metadata and recorded GC results |
| `pbs_groups.csv` | Group identity including namespace, version count, last backup time and latest logical size |
| `pbs_snapshots.csv` | Each collected snapshot and its reported logical archive size |

When PBS is skipped, PBS CSV files contain headers only and the report explicitly says PBS was not requested. The complete structured data, including collection status and warnings, is in `inventory.json`.

## Optional PBS reporting — off by default

Choose `include` at the optional prompt or enable it explicitly:

```sh
python3 pve_inventory.py --pbs
```

The script asks for the **PBS** hostname, **PBS Token ID**, hidden **PBS Token Secret**, and optional trusted **PBS CA certificate file**. These are separate from your PVE login/token. Use a token created in the PBS web interface for a user with the audit permissions below. The script never uses PVE's stored backup credentials or reads backup contents.

For saved settings, copy `config.pbs.example.json` to a protected configuration file, edit the placeholders, and run:

```sh
chmod 600 config.pbs.json
python3 pve_inventory.py --pbs --config ./config.pbs.json
```

You can combine the `pbs_*` fields with PVE fields in the same configuration file. Environment variables override those fields: `PBS_URL`, `PBS_TOKEN_ID`, `PBS_TOKEN_SECRET`, `PBS_CA_FILE`. Set the URL to `https://pbs-hostname:8007`. A private CA certificate path is local to the WSL/Ubuntu machine. Certificate and hostname verification are enabled, and redirects are refused.

**Saved credentials or `--pbs-url` alone do not enable PBS reporting.** In an unattended run you must pass `--pbs`; otherwise it stays off. For example, after setting credentials in your environment/config:

```sh
# All PBS datastores visible to the token:
python3 pve_inventory.py --mode ssh --environment cluster --ssh-host pve1 --no-prompt --pbs --config ./config.pbs.json

# Limit PBS queries to named datastores (option may be repeated):
python3 pve_inventory.py --pbs --pbs-store backup-store

# PVE only, without the optional PBS question:
python3 pve_inventory.py --no-pbs
```

PBS metadata requires **`Datastore.Audit` on `/datastore/STORE` with propagation to its namespaces**, for both the token and its owning user. Optional recent-task visibility uses **`Sys.Audit` on `/system/tasks`**. Narrower ACLs can silently hide stores, namespaces or tasks; read successes prove visibility only within the token's scope. Missing privilege, unsupported endpoints or an unreachable PBS produce unavailable/partial PBS data while the PVE report continues. PBS API support varies by version; older namespace/prune endpoints may be unavailable.

The PBS collector sends **GET only** to `/version`, `/admin/datastore`, `/config/datastore/STORE`, `/config/prune`, `/admin/prune`, `/admin/datastore/STORE/status`, `/namespace`, `/groups`, `/snapshots`, `/gc`, and `/nodes/localhost/tasks` (the short subpaths are under that datastore). It never starts prune, GC, verification, backup or deletion operations. Namespace inventory includes the root and all visible descendants up to PBS's supported depth; failed enumeration is explicitly incomplete. Groups are kept distinct by datastore, namespace, backup type and ID. Reused VM IDs across different clusters prevent proving group ownership from ID alone.

PBS capacity is **filesystem capacity reported for each datastore**, and may include other data or overlap another datastore's filesystem. It is not summed across datastores. `latest_logical_bytes` and snapshot `logical_bytes` come from PBS's snapshot archive-size field. They are logical referenced backup data, not compressed unique chunk usage or a VM's share of physical PBS storage. They are never added into a physical-use claim. GC counters without a recorded statistics run are N/A rather than a placeholder zero; recorded statistics may be older than the latest job result. Pending GC bytes can remain held by safety rules.

## Exact backend measurements and additional host permissions

For **LVM-thin**, SSH/local modes use only `hostname -s` and `dmsetup status --target thin --noflush`. The mapped-sector counter is converted exactly to bytes using 512 bytes per sector. Stopped/inactive volumes can lack a counter and remain N/A; no disk is activated and no rounded `Data%` is converted into an estimate. This reports mapped blocks for a volume, including blocks that can be shared with snapshots/clones, not exclusive pool allocation or deletion savings.

For another PVE cluster member, the entry host uses its **existing noninteractive root SSH trust**, with strict host-key checking, to run that exact status command on the owning node. No trust entries, keys or permissions are added, and no SSH agent is forwarded. Missing tools, privilege or host trust leave a clear measurement gap. Root or an already suitable sudo policy is needed for host counters; a PVE audit API token alone is not sufficient. Other inventory data still covers all visible nodes. Ceph RBD uses PVE's read-only volume API and its existing Ceph credentials; the script does not export them or execute direct Ceph management commands.

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
| Power (HTML table) | On = running, Off = stopped, Unknown = current power state could not be confirmed. The original PVE status remains in the row details and the CSV `status` column. |
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
| LVM-thin | Exact mapped sectors × 512 from `dmsetup status --target thin --noflush`, via local/SSH access. Only active volumes can be measured this way. Shared blocks may appear in several volumes, so this is not unique pool usage. No rounded `Data%` calculation or volume activation is used. PVE API-token mode alone cannot obtain this host-level counter. |
| Ceph RBD on PVE 8+ | PVE volume-info `used` backed by `rbd du` for the current image, excluding its snapshots. This does not include all Ceph replication, shared-clone data or cluster overhead. Older/unrecognized PVE versions stay N/A. A slow or unsupported query is recorded as unavailable. |
| Plain LVM, iSCSI, ZFS over iSCSI, other plugins | N/A: no validated allocation metric is implemented here. Virtual size and placeholder zeros are not substituted. |

Absent API `used` on supported file/ZFS/RBD backends remains N/A; genuine returned zero is preserved. LVM-thin ignores that generic field and uses only the exact active-volume counter described above. Even complete per-VM allocation counters do not establish a unique cluster physical footprint. There is deliberately no summed physical-use claim. Different measurement families cannot be mixed for `--sort actual`. Snapshots, unreferenced disks, base images and other storage consumers are not a complete datastore audit; the report is a VM review inventory.

Guest usage requires a running VM, an installed/enabled QEMU guest agent and permission. Agent data may omit usage counters, especially on older guest agents. Free is calculated only from valid reported total/used values and means `total-used`, not necessarily the space available to a particular guest user. Repeated mounts, network shares, pseudo-filesystems and overlapping devices make a guest-wide total unsafe, so each returned filesystem remains separate.

## Investigating the 2 TB PBS problem

Use the largest-VM list to prioritize investigation, then inspect enabled job membership and `backup=0` disks. The report lists visible PVE backup-job schedules, destination storage and explicit job-level prune overrides; these are configuration hints, **not** effective PBS retention. Unset overrides can inherit other settings. Disabled jobs do not imply old backups have been removed. Current jobs also do not reveal every VM with historical snapshots.

Nominal 2 TB is about 1.82 TiB before filesystem overhead. Neither sum(configured disks) nor sum(guest used) is PBS consumption. PBS stores deduplicated, compressed chunks shared by VMs and snapshots. Requirements depend on the initially backed-up data, changed data rate, retention windows, sharing/compression, and cleanup progress. One VM's logical backup size cannot reliably attribute shared PBS chunks to that VM. Old/deleted VMs, containers and other clients may still have backup groups. Prune and GC are separate steps, with GC safety delays.

Enable the optional PBS phase below to collect datastore filesystem total/used/available, visible groups and snapshot counts, configured retention/prune settings, and recent prune/GC status. It highlights capacity thresholds and reported cleanup failures for review, without claiming that every listed setting is the complete effective policy or attributing shared backup data to individual VMs. It performs no changes.

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
- [OpenSSH command execution and authenticated background connections](https://man.openbsd.org/ssh)
- [OpenSSH connection multiplexing and host verification](https://man.openbsd.org/ssh_config)
- [Linux thin-device status: mapped sectors](https://docs.kernel.org/admin-guide/device-mapper/thin-provisioning.html)
- [PVE Ceph RBD allocation implementation](https://github.com/proxmox/pve-storage/blob/master/src/PVE/Storage/RBDPlugin.pm)
- [PBS API tokens and audit permissions](https://pbs.proxmox.com/docs/user-management.html)
- [PBS datastore API](https://github.com/proxmox/proxmox-backup/blob/master/src/api2/admin/datastore.rs)
- [PBS prune-job status API](https://github.com/proxmox/proxmox-backup/blob/master/src/api2/admin/prune.rs)

## Guided setup and report navigation

Run `python3 pve_inventory.py` for guided setup. Choose the connection method by name or number. Supplying `--mode ssh` selects SSH and skips that menu; the script now explains this. SSH asks password/key authentication before the host and login details. The flag is `--mode ssh`, not `--ssh`. PBS remains off by default.

The PVE node dropdown combines with any search and usage filter. The count line lists active filters. **Show all VMs / Clear filters** restores the full list. **Find VM** clears filters and highlights the exact VM without hiding other machines. Click **PVE node** to sort naturally in either direction.

## Storage measurements and timeouts

### Cluster routing (1.3.2)

Host-level storage reads now discover node IPs through the read-only `/cluster/status` endpoint. They connect to the owning node's IP, verify `hostname -s` matches the expected PVE node, and preserve strict SSH host-key checking using the node's host-key alias and Proxmox per-node known-hosts file (with root's existing known-hosts file as an additional source). IPv4 and IPv6 are supported. DNS is unnecessary when you supply an IP for the initial connection and cluster discovery returns usable IPs.

The SSH port is no longer prompted. `--ssh-port`, `PVE_SSH_PORT`, and configuration `ssh_port` still control the initial computer-to-PVE connection. When omitted, OpenSSH configuration/defaults apply. Connections between PVE nodes use that PVE host's OpenSSH configuration/defaults independently; the initial port is not copied to all cluster nodes. Cluster connection attempts have a ten-second connection limit, bounded further by the remaining collection budget in observed runs.

If address discovery is restricted or a different address is needed, add a `node_addresses` object to your existing config, for example `"node_addresses": {"pve02": "192.0.2.22", "pve03": "2001:db8::23"}`. Replace these documentation addresses with reachable addresses. Node identity is still checked; missing or invalid addresses do not trigger hostname guessing. Root SSH trust between PVE nodes and permission to read counters are still required. API tokens alone do not supply host-level thin counters.

Routes and failures are cached only within the current run and rediscovered on the next run. One failed node does not stop other nodes; warnings identify the affected VM/disk counts. Troubleshooting JSON includes source node, destination IP, verified identity and route error. It records that the port comes from OpenSSH configuration/defaults; it does not claim to resolve the effective port from every SSH configuration. VM placement is checked again at collection end; detected migrations or disappearances invalidate affected allocation measurements. Collection is not an atomic cluster snapshot.

To verify this fix, rerun your normal command with `--diagnostics`, and compare the new bundle's `node_routes` and disk results. Automated tests simulate routing failures; successful collection on your actual cluster remains an onsite verification step.

### Collect troubleshooting files for review

Add `--diagnostics` to your normal command to write a separate folder under **`logs/`**, relative to where you run the script:

```bash
python3 pve_inventory.py --mode ssh --no-pbs --diagnostics --output-dir ./pve-report
```

For a smaller comparison, add `--vmid 100 --vmid 101`, replacing those IDs with one working VM and one affected VM. You can also use `--mode local` on PVE. This option adds no prompts or extra server queries. To select another destination, use `--diagnostics /absolute/path/to/logs` (recommended for cron).

Each run creates `logs/storage-RUN_ID/` containing:

- `README.txt`: readable per-VM and per-disk measurement results.
- `storage-diagnostics.json`: PVE/tool version, selected scope, run settings, power state, storage backend, volume group, measurement source, exact error, timing and coverage.
- `disks.csv`: the same disk evidence in tabular form.
- `collection.log` and `query_timings.csv`: collection progress and query outcomes through report export.

The bundle is generated after report export, including reports with N/A values. Startup/discovery failures that cannot produce a report do not create this bundle. Existing diagnostic runs are retained. No credentials/configuration files, guest filesystem details or full API responses are copied. Known credentials and common credential patterns are redacted from included text. Node names, disk identifiers and storage paths remain so measurements can be traced; review the files before publishing them, especially in a public repository.

`pve-report/` and timestamped report folders are ignored by Git. `logs/` is intentionally **not ignored**, allowing you to commit just the selected troubleshooting run:

```bash
git status --short
git add logs/storage-REPLACE_WITH_RUN_ID
git commit -m "Add storage troubleshooting bundle"
git push
```

Use the actual folder printed by the script. Git ignore rules do not untrack files already committed; if `pve-report` is already tracked, `git rm -r --cached -- pve-report` removes it from Git's index while leaving local files intact.

### Checking incomplete VM measurements (1.3.1)

An unreadable EFI/startup disk, TPM/security disk or additional data disk no longer hides measurements from the other disks. The VM row shows the known amount and coverage, for example **30 GiB measured; partial; 1 of 2 disks measured**. The complete total remains N/A. Zero is a valid measurement; zero plus an unreadable disk is still partial. Measurements that count space differently remain separate subtotals.

`actual_bytes` retains its complete-total meaning. CSV/JSON also include `known_actual_bytes`, `measured_subtotals`, `measurement_status`, `measured_disk_count` and `counted_disk_count`. Disk CSV includes the outcome, local collection elapsed seconds and whether the result was cached. Cached times measure lookup time, not a new host query. Query timings include the new host metadata commands.

HTML storage sorting puts complete values first, then partial results, then unavailable results; partial results are not ranked as full totals. Configured capacity remains the default ordering and top-10 basis. Command-line `--sort actual` retains its requirement for complete, comparable measurements; incomplete runs fall back to configured capacity with an explanation.

For raw regular-file disks, SSH/local collection now reads allocated file blocks on the owning node using `pvesm path` and `stat`. This avoids opening the running disk image. It requires permission to resolve and stat the disk, and the existing cluster SSH access when the owner is another node. No additional Python installation on PVE is needed. API-token collection continues through the PVE API. QCOW2 and other image formats use PVE's image-aware measurement, including in standalone mode: measuring only their primary file could miss additional data files. Missing active thin counters remain unavailable; a backend-reported failure and an unrecognized response are now distinguished from an absent counter. No disk is activated or unlocked.

Run this from the folder containing the script on WSL/Ubuntu, replacing 100 and 101 with one affected VM and one working VM:

```bash
python3 pve_inventory.py --mode ssh --vmid 100 --vmid 101 --no-pbs --no-guest-agent --output-dir ./storage-check
```

Open `storage-check/inventory.html` on that machine. Expand **View VM information → IT reference for this VM**, and compare every attached disk, not only the main disk. `disks.csv` contains the corresponding raw results. Guest-agent collection is disabled here to demonstrate that host allocation is independent of guest filesystem reporting. No files need to be transferred to another environment.

For a raw file, IT can compare the result on its owning PVE node using these read-only commands. Substitute the exact volume identifier from the disk detail:

```bash
pvesm path 'local:100/vm-100-disk-0.raw'
stat -L --printf='type=%F allocated_blocks=%b bytes_per_block=%B\n' -- '/path/returned/above'
```

Allocated blocks multiplied by bytes per block should agree with the recorded file allocation, allowing for writes between observations. This is not filesystem free space or exclusive physical usage. For thin volumes, `dmsetup status --target thin --noflush` reports mapped sectors; multiply the matching target's mapped-sector count by 512. A stopped VM does not necessarily mean its disk target is inactive. Do not activate a missing target just for this check.

Manual authentication and cron behavior are unchanged. Automated tests use simulated hosts; the first selected-VM run is still required to validate the specific failure in your environment.

Storage used does not require guest tools. Only guest filesystem used/free information requires the QEMU Guest Agent. Backend detection remains automatic; unsupported or inaccessible measurements remain N/A with an explanation. Configured disk capacity is never substituted for measured usage.

Storage measurements now have a separate 60-second default budget. For slower storage, run:

```bash
python3 pve_inventory.py --mode ssh --storage-timeout 120
```

`--timeout` still controls general queries and connection timing. Storage diagnostics identify the node, storage/volume or backend counter, and timeout budget. A timeout does not establish that storage is offline. Timed-out remote commands are not automatically retried because they may still be running. This avoids issuing duplicate expensive reads. Shared failure results are reused within a run; a subsequent run collects fresh measurements.

Known measurements remain available as subtotals by measurement type on the Storage page and in filtered table totals. Incompatible measurement types are not combined. Measurement/access findings are grouped into an expandable section of Review list, with individual VM details retained. The print summary is concise rather than guaranteed to occupy one page.

## Collection progress, diagnostics, and targeted runs

Run `python3 pve_inventory.py --version` to confirm the version without connecting. Normal startup checks required local commands and write access before collecting data. You can reuse **`--output-dir ./pve-report`** on every run without a confirmation prompt. Existing report files are replaced when export succeeds, and diagnostic logs are restarted for each collection. Unrelated files and subdirectories are left alone. Omitting `--output-dir` creates a timestamped folder with a random suffix.

```sh
# Whole cluster, with PBS explicitly skipped
python3 pve_inventory.py --mode ssh --no-pbs
# One VM or node (repeat --vmid or --node to select more)
python3 pve_inventory.py --mode ssh --vmid 108 --no-pbs
python3 pve_inventory.py --mode ssh --node pve01 --no-pbs
# Stop starting requests after ten minutes of collection
python3 pve_inventory.py --mode ssh --max-runtime 600 --no-pbs
```

VM IDs are combined as a selection; node names are combined as a selection. When both are supplied, a VM must match both. Detailed queries are limited to selected VMs. Shared metadata queries still run. Unknown selections never fall back to a full inventory. Selected reports and their totals describe only selected VMs. PBS, if explicitly enabled, retains its separately labeled datastore scope.

Progress shows the current read, selected-VM count, elapsed query time, and timeout where available. Slow requests normally receive updates every five seconds. `--progress off` suppresses progress output; `--progress plain` and the default `auto` use readable lines, including when redirected. Initial SSH authentication occurs before collection. Interactive-sudo reads suppress repeated waiting messages for the whole read, preserving the remote password/MFA conversation; their elapsed times include prompt time. Query start and completion remain recorded. Collection duration excludes initial sign-in, but includes sudo prompts during collection; total runtime includes setup. Durations measure local request time, not isolated server processing time.

`collection.log` and `query_timings.csv` are written incrementally. They contain safe operation labels and outcomes, not raw authentication headers or exception text. Backend messages normally remain in the report's IT reference. Interactive-sudo authentication and command-error transcripts are displayed live only and are not copied into reports; those reads record safe status summaries. `run_info.csv` and `inventory.json` record the run ID, version, settings, selection, timings, and failure groups. The HTML IT reference lists slow queries. Keep files from the same run together.

Ctrl+C during a query saves unavailable placeholders for remaining discovered VMs and finishes an incomplete report (exit 130). The overall time limit stops new requests and reduces supported command timeouts to the remaining budget. Export may extend beyond that budget. Discovery failure cannot produce a reliable VM report; its diagnostic log is retained. A second interrupt may stop recovery/export immediately. File-stat system calls may not be interruptible promptly by the operating system.

Reports are staged and each final file is replaced atomically. `report-complete.json` is written last: it confirms export completion, not complete measurement coverage. If collection or staging fails before report replacement begins, the previous reports and their marker remain; check their date and run ID. Once replacement begins, the old marker is removed, so an interrupted replacement has no completion marker. Diagnostic logs describe the latest attempt. Do not interpret unfinished output as a finished report. Completed partial runs retain exit code 0, with their limitations recorded explicitly.

### Developer verification

The tests folder is only needed for development. Normal inventory use still needs no Python packages.

```sh
python3 -m unittest discover -s tests -q
# Optional browser tests, requiring Node and Playwright in the development environment:
node tests/browser_checks.cjs
```

Browser checks use synthetic data and make no PVE/PBS calls. `PYTHON` may select the Python executable; `BROWSER_PATH` may select an installed Chromium/Edge executable. Otherwise Playwright uses its installed Chromium. The browser test dependency is separate from the inventory script.

For live acceptance, compare VM counts on both nodes and compare identical storage metrics for a working VM and a previously timed-out disk. Test node filters and Find VM, then inspect logs for remaining gaps. Confirm targeted totals describe only the chosen selection. Live acceptance requires access to your environment and has not been completed by the automated tests.

## Optional cron jobs directly on PVE

**Manual use is unchanged.** Running `python3 pve_inventory.py` still shows the guided prompts. `--mode ssh` still supports interactive password login from WSL/Ubuntu. Only adding **`--cron`** enables the scheduled behavior described below. The script does not install, edit, or remove cron jobs itself.

Schedule on **one PVE node**, using root's crontab and `--mode local --environment cluster`. Local mode uses existing PVE command permissions and requires no SSH login or PVE API token. It inventories all visible cluster nodes. The scheduling node must be online; this is not a cluster failover service. All PVE/PBS operations remain read-only. A standalone host can use `--environment standalone` with local mode.

### 1. Copy and prepare the script

From WSL/Ubuntu, while in the project folder, copy the updated script to your chosen PVE node (replace `pve01` with its hostname/address):

```sh
scp ./pve_inventory.py root@pve01:/root/pve_inventory.py
ssh root@pve01
```

Then, on that PVE node as root:

```sh
/usr/bin/python3 --version
command -v pvesh
install -d -m 700 /opt/proxinfo /etc/proxinfo /var/lib/proxinfo/reports /var/log/proxinfo
install -m 600 /root/pve_inventory.py /opt/proxinfo/pve_inventory.py
/usr/bin/python3 /opt/proxinfo/pve_inventory.py --version
```

Python 3.9+ is required. No pip packages or tests folder are needed to run the inventory. For updates, copy and install the new script again. Local PVE-only collection needs no configuration file.

### 2. Test the exact unattended command

Run this on the scheduling node before adding cron:

```sh
/usr/bin/python3 /opt/proxinfo/pve_inventory.py --cron --mode local --environment cluster --no-pbs --output-root /var/lib/proxinfo/reports --max-runtime 1800
printf 'Exit status: %s\n' "$?"
```

The command must run without prompts. Check both nodes' VM counts, remaining measurement gaps, and the result files. An incomplete report can be useful and has exit status 3 in cron mode.

`--cron` requires an explicit `--mode` and an **absolute** `--output-root`. It implies `--no-prompt`, selects plain progress by default, keeps PBS off unless `--pbs` is supplied, and defaults to a 3,600-second collection budget unless overridden. `--output-root` and `--output-dir` are mutually exclusive. The time budget applies to collection, not initial authentication or export; some underlying system calls may finish later. In manual interactive-sudo mode, prompt time during a query is included in collection timing. No remote query is automatically repeated after a timeout.

### 3. Add the schedule

As root on the selected PVE node, run:

```sh
crontab -e
```

Add these lines. This example runs daily at **06:15 in the scheduling node's local time zone**; choose a time that suits your backup/storage workload. The cron command must be one line. Confirm the node's time zone using `date`.

```cron
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
15 6 * * * /usr/bin/python3 /opt/proxinfo/pve_inventory.py --cron --mode local --environment cluster --no-pbs --output-root /var/lib/proxinfo/reports --max-runtime 1800 >> /var/log/proxinfo/cron.log 2>&1
```

Use `crontab -l` to review the installed entry. The job uses absolute paths and does not depend on your interactive shell environment. Ensure the host's cron service is running. An ordinary cron schedule does not automatically replay a run missed while the node was powered off.

To change the schedule, edit the entry with `crontab -e`. To disable it, put `#` at the beginning of that job's line. Do not remove unrelated cron entries. Monitor the local report/log directory sizes and manage retention according to your policy; the script does not delete old reports or backups.

### 4. Find and retrieve reports

Each launch creates a unique `pve-report-DATE-TIME-ID` directory beneath `/var/lib/proxinfo/reports`. Each run contains the HTML, CSVs, JSON, print summary, and logs.

- `latest.html` links to the latest fully **exported** report, which may have incomplete measurements; its status and collection date are shown.
- `last-run.json` describes the latest acquired run: `running`, `complete`, `incomplete`, `failed`, or `cancelled`.
- A failed launch leaves the previous `latest.html` unchanged. Check `last-run.json` and timestamps before treating that link as current.
- `collection.log` captures startup and collection milestones. Authentication and other startup errors are also captured in `/var/log/proxinfo/cron.log` by the cron redirection.
- `report-complete.json` inside a run confirms export completion, not complete measurement coverage.

To retrieve a report, run this **from WSL/Ubuntu**, replacing the example folder name with the actual one:

```sh
scp -r root@pve01:/var/lib/proxinfo/reports/pve-report-DATE-TIME-ID ./
```

Open that folder's `inventory.html` locally. Keep its `management-summary.html` beside it. Copying just the root `latest.html` without its target run folder will not preserve the link.

### Scheduled outcomes and overlap protection

| Exit status | Meaning |
|---|---|
| 0 | Report exported with no detected collection gaps under the current checks |
| 1 | Startup, connection, collection, or export failed |
| 2 | Invalid command options |
| 3 | Report exported with incomplete measurements, warnings, requested PBS gaps, or a collection time limit |
| 4 | Another scheduled run holds the lock for this output root; launch skipped |
| 130 | Interrupted/cancelled |

A missing/unsupported measurement may consistently produce exit 3; this is not proof of a storage outage. Interactive/manual partial reports retain their existing exit code 0. Cron records output; it does not itself act on these codes as an alerting system.

The lock is held until collection, export, and connection cleanup finish. The OS releases it on process exit, including an unexpected termination. The `.proxinfo.lock` file is expected to remain; do not delete it while a run may be active. Locking is local to one scheduling host and output root: use the same root for overlapping jobs, and schedule on only one cluster node. An overlap skip does not overwrite the active run's status.

### Optional remote authentication and PBS

Cron mode also supports `--mode api` with a token or `--mode ssh --ssh-auth key` with unattended key authentication. Password prompts and unverified-host prompts cannot be answered by cron. Encrypted keys require an agent that is available to the scheduled account; local PVE mode avoids this setup entirely. SSH sudo, if used, must already work noninteractively. Set `--ssh-sudo-mode noninteractive` for PVE or `--pbs-ssh-sudo-mode noninteractive` for PBS. Interactive sudo is rejected with `--cron` or `--no-prompt`; the script never waits for an Okta response in a scheduled run. TLS certificate and SSH host-key verification remain enabled.

API/PBS credentials use the existing environment variables or `--config /etc/proxinfo/scheduled.json`. Protect that file with owner-only permissions (`chmod 600 /etc/proxinfo/scheduled.json`) and keep it outside Git. Use the example configuration files for field names and the API permissions sections above for audit access. Tokens/passwords do not belong in a crontab command or diagnostic log. The connected account still needs permission to read the selected objects.

PBS remains off by default even if credentials are saved. To include it, replace `--no-pbs` with `--pbs` and provide its separate API-token or unattended SSH settings. Local PVE permissions do not supply PBS authentication.

## PBS investigation tools (1.3)

PBS is still optional and off by default. PBS-only mode connects **only to PBS**, using the selected PBS authentication method. It needs no PVE connection or PVE API credentials. Manual runs support PBS SSH, a web-account password, or an API token:

```sh
python3 pve_inventory.py --pbs-only --pbs-store backups
# Root namespace only (/ means root; selection is exact, not recursive)
python3 pve_inventory.py --pbs-only --pbs-store backups --pbs-namespace /
# Daily backup expectation; omit to show age without an overdue judgment
python3 pve_inventory.py --pbs-only --pbs-fresh-hours 36
# Save comparable capacity observations locally
python3 pve_inventory.py --pbs-only --output-root ./pbs-reports --pbs-history
```

Datastore and namespace options can be repeated. Without namespace selection, all visible namespaces are queried. PBS-only mode cannot combine with PVE `--mode`, `--vmid`, `--node`, or `--no-pbs`. Combined collection still uses `--pbs` with your normal PVE options. PVE task/VM failures are avoided entirely by PBS-only mode.

### PBS-only cron on PVE

Root on PVE still needs separate PBS authentication: an API token or an SSH key
with the required noninteractive permissions. Save non-secret connection settings
and any API credentials in the protected configuration file described above.
A planned nightly entry can use this actual command (absolute paths required for cron output root):

```sh
/usr/bin/python3 /opt/proxinfo/pve_inventory.py --cron --pbs-only --config /etc/proxinfo/scheduled.json --output-root /var/lib/proxinfo/pbs-reports --pbs-history --max-runtime 1800
```

Use the same cron setup procedure as the PVE section. Omit `--mode` for PBS-only runs. `latest.html`, logs, locking, and cron exit codes work as for other scheduled runs. Manual prompting is unchanged.

### Connection checks, capacity and cleanup

The PBS page includes per-endpoint availability checks and version information in the exported JSON. HTTP authentication failures, permission failures, unavailable endpoints, and timeouts are distinguished. Visible scope is not proof of complete server access. `pbs_checks.csv` exports those checks.

`--pbs-timeout` defaults to 60 seconds per request. `--pbs-task-days` defaults to 14 and `--pbs-task-limit` to 1000 **per datastore**. Task reads are paginated. Coverage states whether the limit or an error stopped the history search. Empty task history means no matching task was found within that checked scope, not that cleanup never ran. Older servers that reject the history parameters are reported as unavailable rather than guessed.

Cleanup summaries separate the latest attempt from the latest confirmed success. A task without a completion time is shown as running or completion unavailable. GC statistics retain their own recorded context and may predate the latest attempted job. Pending removals remain subject to PBS safety rules. No prune, GC, or verification task is started.

Capacity is displayed only when the datastore configuration supports the validated local-filesystem interpretation. Unknown/object-storage backends currently show N/A for full capacity: local cache usage is not bucket consumption. No direct cloud/storage-provider access is added. Multiple datastore capacities may describe overlapping storage and are never summed.

### Retention, freshness and verification

The inventory table includes oldest/latest visible snapshot times, snapshot counts, protected counts, age, verification results, and PBS policy details. Missing protection fields mean unavailable protection counts, not zero. Matching PBS prune policies respect namespace depth; disabled policies stay labeled. PVE backup-job settings remain separately visible in combined reports. Multiple rules are not merged into a guessed deletion policy.

The table is searchable and sortable, with 50 groups per page and no 500-group display cutoff. Search can match datastore, namespace, type, ID, freshness, or verification state. CSV/JSON retain all collected records.

`--pbs-fresh-hours` sets a general age expectation. Optional configuration `pbs_freshness_overrides` applies in listed order (later matching entries win):

```json
{
  "pbs_freshness_overrides": [
    {"store": "backups", "namespace": "weekly", "hours": 192},
    {"store": "backups", "namespace": "", "backup_type": "vm", "backup_id": "108", "hours": 36}
  ]
}
```

Namespace `""` means root in JSON; omitted selector fields match any value. A freshness finding is based on the latest **visible snapshot**, not proof of a successful restore. Known PVE PBS destinations are compared by server hostname, datastore, namespace, backup type and ID. Matches remain candidates because multiple clusters can reuse IDs. Inaccessible or uncollected destinations are not labeled missing. PBS-only reports explicitly skip PVE ownership matching.

Verification states are passed, failed, explicitly not verified, or unavailable. Unknown/missing fields remain unavailable. Older snapshot failures are separate from latest-snapshot status. The script also reads `/config/verify` and `/admin/verify` using GET only. These reads use datastore audit access; do not grant verification-execution permission merely to improve the report. Unsupported or denied endpoints remain unavailable. Recorded job completion dates are available in the verification details; per-snapshot verification dates are not inferred from snapshot creation times.

`--pbs-verification-days` optionally marks recorded verification-job completions older than an expected age. It does not assume every snapshot was verified by that job. Verification results do not establish that a complete restore has been tested.

### Local capacity history

Capacity history is saved automatically when PBS is included, starting with 1.6.0. Use `--output-root ...` for a stable report/history folder, or `--pbs-history-dir ...` to choose a separate history folder. Without either, history is saved in `.proxinfo` beside the report folders. `--no-pbs-history` disables reading and saving it. The existing `--pbs-history` flag remains accepted.

A local SQLite file (Python standard library) stores valid readings; no third-party Python package is required. Readings are keyed by PBS URL, datastore, backend, backing path, total capacity, and visible retention/job scope. Changes start a separate series rather than mixing unlike observations. Repeated run IDs are deduplicated and writes use database transactions. Reports include the latest reading from each of up to 90 UTC dates. Use the same connection address, selection and history folder across runs. Reading unavailable settings can interrupt the comparable series; the script does not assume they stayed unchanged.

## Capacity Planning and storage-expansion requests

ProxInfo 1.6.0 adds a **Capacity Planning** page, printable plan, management-summary section, spreadsheets and copyable expansion-request text. It requires PBS capacity readings; PVE disk sizes alone cannot establish physical backup-storage needs. Python dependencies and server permissions are unchanged. All server operations remain read-only.

Run from the folder containing `pve_inventory.py`, using your normal connection and authentication prompts:

```bash
python3 pve_inventory.py --mode ssh --ssh-sudo-mode interactive --pbs --output-root ./reports
```

For PBS capacity only, without connecting to PVE:

```bash
python3 pve_inventory.py --pbs-only --output-root ./reports
```

Open `inventory.html` in the newly created report folder and select **Capacity Planning**. PBS-only runs cannot show PVE job selection. To inspect existing job scope alongside PBS, use the first command.

The report contains:

- Current PBS filesystem capacity, used space and available space.
- Additional capacity needed **now** to restore a free-space reserve, default **20%**. This is an editable planning assumption, not a Proxmox requirement.
- Total and additional capacity for **30, 90 and 180 days**, with evidence type and confidence shown.
- Estimated time until full or until the reserve is reached, when a usable growth rate exists. N/A is not an assurance that space will never run out.
- Existing PVE job destinations, schedules, visible VM selection, included logical disk capacity and disk exclusions. These are backup scope, not per-job physical PBS usage. Missing VM configurations or invisible/container members make scope incomplete.
- Visible PBS retention policies and latest cleanup results. The model assumes comparable workload, scope and retention; it does not simulate changing retention policies or multiply logical snapshot sizes by retention counts.

### First run versus measured history

On the first run, the immediate reserve requirement can be calculated from current capacity. Future growth needs more evidence. Until history contains at least **three distinct daily readings spanning seven days**, with the latest no older than seven days, automatic future-growth projections remain unavailable. No preset daily growth rates are supplied. A user-entered growth rate remains an explicit assumption, never a measured requirement for the selected VM workload.

With enough comparable history, the baseline uses first-to-last net growth. A highest-sampled-interval sensitivity scenario is also shown when higher; it is not a statistical confidence interval or guaranteed upper bound. Flat or declining history does not prove future demand is zero. A full datastore can suppress successful backups and hide unmet demand. Failed or unknown cleanup lowers confidence; the model credits **no unverified space recovery**.

You can supply an explicit planning assumption instead of waiting for history. For example, only if **10 GiB/day** is a rate you intend to evaluate for datastore `TrueNAS`:

```bash
python3 pve_inventory.py --mode ssh --ssh-sudo-mode interactive --pbs --output-root ./reports --pbs-growth-gib-per-day TrueNAS=10 --pbs-reserve-percent 20
```

Replace `TrueNAS` with the datastore's exact name. Repeat `--pbs-growth-gib-per-day OTHER_STORE=NUMBER` for other stores. Rates must be finite and between 0 and 1,000,000,000 GiB/day; a zero rate is a valid no-net-growth assumption. Unknown datastore names are called out in the plan and are not silently applied elsewhere. Reserve percentage must be between 0 inclusive and 100 exclusive. Command-line assumptions are labeled as assumptions, not historical evidence.

History works with normal manual runs and existing cron options. Cron does not gain any new prompts. Give scheduled runs a stable absolute `--output-root` and use the established noninteractive authentication options. Keep history enabled if you want measured growth to become available.

### Calculation and limits

```text
Occupied or unavailable space = reported total − reported available
Required total = (occupied or unavailable space + daily net growth × days)
                 / (1 − reserve percentage / 100)
Additional capacity = max(required total − current total, 0)
```

For example, a completely full **2 TiB** filesystem needs **2.5 TiB total**, or **0.5 TiB additional**, to provide a 20% reserve before allowing for future growth. At an explicitly assumed 10 GiB/day for 90 days, the target becomes about **3.60 TiB total**, or **1.60 TiB additional**. This is a hypothetical example, not your measured requirement.

Targets are usable/reportable filesystem capacity, **not raw disk purchases before RAID, replication or filesystem overhead**. Currently reserved/unavailable bytes are treated as fixed; percentage-based reservations may increase after expansion. Verify the storage system's resulting usable capacity. Existing backups are already included in current usage; do not add VM/snapshot sizes again. Do not sum datastore targets because datastores can share a filesystem. The model does not estimate the unique physical size of the next individual backup.

PBS shares chunks across snapshots, so logical backup sizes cannot establish unique physical per-job consumption. Pruning removes backup references; garbage collection reclaims eligible unreferenced chunks. See the official [technical overview](https://pbs.proxmox.com/docs/technical-overview.html) and [maintenance documentation](https://pbs.proxmox.com/docs/maintenance.html).

### Files to share with management

| File | Purpose |
| --- | --- |
| `management-summary.html` | Printable summary with reserve requirement, conditional 90-day target, confidence and cleanup result |
| `capacity-plan.html` | Standalone printable evidence, assumptions and scenario tables |
| `capacity-justification.txt` | Copyable expansion-request wording, policy context and limitations |
| `capacity_planning.csv` | Per-datastore/scenario/horizon totals and additional bytes, reserve, rate, confidence and history coverage |
| `backup_job_scope.csv` | Visible job selection and known included logical capacity, kept separate from physical forecasts |
| `inventory.json` | Full machine-readable evidence and `capacity_plan` |

CSV capacity/rate fields are bytes or bytes/day; HTML uses labeled binary units (GiB/TiB), which differ from decimal GB/TB on storage products. Keep the assumptions with any estimate you copy. Expanding storage does not fix failed cleanup or establish that backups are succeeding.

The chart plots successive valid observations, with exact timestamps and values below it. Gaps are not filled with zero and points are not joined to imply continuous measurements. Changes are observed differences, not proof that cleanup caused them. The first reading has no previous-change value. No per-VM physical attribution or date-when-full forecast is calculated. Failed local history writes are reported while the inventory can still be exported.

### First live PBS acceptance run

Start with one datastore and PBS-only mode. Compare reported capacity, visible namespaces/group counts, oldest/latest snapshots, protected entries, prune settings, GC statistics, and verification state with the PBS web interface. Confirm the account has the documented audit access. Then test an intentionally limited namespace and a combined PVE/PBS run. Only after matching those readings should the scheduled history be relied upon. Automated fixtures and browser tests cannot validate your installed server version or account visibility.


### Selected backup workload (1.7.0)

Run ProxInfo normally and open **Capacity Planning** in `inventory.html`. No new
switches are needed. The selected-workload section works with PVE inventory even
when you skip PBS; include PBS to add destination, task and filesystem evidence.
Manual and scheduled collection remain read-only.

**Selected Backup Workload** counts VMs selected by enabled PVE backup jobs, across
the collected cluster. A VM stays selected even if its PBS backups are missing or
failing because storage is full. Intentionally unselected VMs are outside this
analysis and do not produce a missing-backup-schedule warning.

- **Included Assigned Capacity** is the configured size of disks included in
  backup. It excludes `backup=0`, CD/cloud-init media and unused disks under the
  existing disk rules; included EFI/TPM disks remain counted. It is not measured
  storage consumption, guest filesystem usage or physical PBS backup data.
- **Known included total** counts a shared volume identity once, even across VMs
  and overlapping jobs. Conflicting sizes are unavailable. Partial totals label
  missing configuration, identity, membership or sizes; unavailable is not zero.
  Containers are not inventoried as VMs; selected guest IDs without VM details
  are shown as scope gaps. Limited PVE access can hide additional jobs or guests.
- **Top 10 Selected VMs** ranks complete included assigned capacities. Unknown
  sizes can change the ranking. The share of the total deduplicates the top-ten
  volume references; individual VM sizes should not be added when disks overlap.
- **By Backup Destination** separates PVE storage IDs. These may be aliases for
  the same PBS destination. Neither their workload totals nor filesystem
  capacities should be added into an overall physical-storage claim.
- **Backup Coverage** distinguishes visible matching groups and candidate task
  outcomes from confirmed protection. Matching checks use server, datastore and
  namespace. PBS backup task identifiers can omit namespace and do not establish
  which PVE cluster owns a reused VM ID. An OK candidate task alone therefore
  does not prove complete successful coverage of this selected workload.

**Physical PBS requirements:** current collection does not establish a reliable
physical-size estimate for all selected VMs. Catch-up backups, retained changes,
and peak writes before cleanup each show the missing evidence. No compression
ratio, deduplication ratio, per-VM chunk ownership, retention multiplier or
cleanup savings is invented. Existing PBS filesystem forecasts remain useful
context, but may understate unmet demand when backups are failing. They are not
an estimate for completing all selected backups. A full selected-workload
physical sizing model requires additional trustworthy evidence; this release
reports that limitation instead of manufacturing an expansion amount.

**Management Scenario** lets you temporarily deselect currently selected VMs.
It recalculates the unique known assigned capacity and top-ten chart without
changing the baseline, PVE jobs, or PBS data. **Reset Scenario** restores the
selection; **Export Scenario** saves a labeled JSON file with included/excluded
VM IDs and unknown physical requirements. **Print Scenario** opens a printable
comparison. General node filters do not change Capacity Planning's collected
baseline. Scenario choices are temporary and are not persisted into inventory
exports or a later run.

The Management Summary and expansion-request text include selected scope and
its limitations. Additional downloads:

| File | Contents |
| --- | --- |
| `selected_backup_vms.csv` | Included VMs, jobs, destinations, assigned bytes, excluded disks, completeness and candidate backup evidence |
| `selected_backup_totals.csv` | Overall unique scope and separate destination subtotals in bytes |
| `selected_backup_requirements.csv` | Catch-up, retained-change and peak requirements; unavailable numeric values and explanations |

Normal collection also includes this data in `inventory.json`. Reports and CSVs
may contain infrastructure names and backup details; share them appropriately.
Windows fixture and native browser tests do not establish compatibility with your
live PVE/PBS permissions or SSH/Okta flow. Live testing remains your next step.
