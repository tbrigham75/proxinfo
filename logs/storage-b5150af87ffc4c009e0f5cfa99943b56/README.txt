ProxInfo storage troubleshooting
Run: b5150af87ffc4c009e0f5cfa99943b56
Tool: 1.3.1
PVE version: {"release": "9.2", "repoid": "5e5ae681198514d4", "version": "9.2.4"}
This bundle contains node names, disk identifiers and storage paths. Review before committing.
Credential settings, guest filesystems, VM names/tags and full configurations are excluded.
Known credentials and common credential patterns in error text are redacted.
Collection logs are a snapshot taken after report export.

VM 101 on pve01: complete; 1/1 disks measured
  ide2 | None | none | used=N/A | N/A: CD-ROM
  scsi0 | lvmthin | local-lvm:vm-101-cloudinit | used=N/A | N/A: CD-ROM
  scsi1 | lvmthin | local-lvm:vm-101-disk-0 | used=148.50 GiB | LVM-thin mapped bytes (active volumes); source dmsetup status --target thin --noflush; mapped sectors × 512; shared blocks may be counted by multiple volumes
VM 106 on pve02: unavailable; 0/1 disks measured
  ide0 | lvmthin | local-lvm:vm-106-cloudinit | used=N/A | N/A: CD-ROM
  ide2 | None | none | used=N/A | N/A: CD-ROM
  scsi0 | lvmthin | local-lvm:vm-106-disk-0 | used=N/A | N/A: Node pve02, LVM-thin counters; storage query budget 60s: ssh: connect to host pve02 port 22: Connection timed out
VM 102 on pve01: complete; 1/1 disks measured
  ide0 | lvmthin | local-lvm:vm-102-cloudinit | used=N/A | N/A: CD-ROM
  ide2 | None | none | used=N/A | N/A: CD-ROM
  scsi0 | lvmthin | local-lvm:vm-102-disk-0 | used=108.65 GiB | LVM-thin mapped bytes (active volumes); source dmsetup status --target thin --noflush; mapped sectors × 512; shared blocks may be counted by multiple volumes
VM 100 on pve02: unavailable; 0/1 disks measured
  ide0 | None | none | used=N/A | N/A: CD-ROM
  ide1 | lvmthin | local-lvm:vm-100-disk-0 | used=N/A | N/A: Node pve02, LVM-thin counters; storage query budget 60s: ssh: connect to host pve02 port 22: Connection timed out
VM 107 on pve02: unavailable; 0/1 disks measured
  ide2 | dir | local:iso/ubuntu-24.04.4-live-server-amd64.iso | used=N/A | N/A: CD-ROM
  scsi0 | lvmthin | local-lvm:vm-107-disk-0 | used=N/A | N/A: Node pve02, LVM-thin counters; storage query budget 60s: ssh: connect to host pve02 port 22: Connection timed out
VM 108 on pve02: unavailable; 0/1 disks measured
  ide2 | dir | local:iso/ubuntu-24.04.4-live-server-amd64.iso | used=N/A | N/A: CD-ROM
  scsi0 | lvmthin | local-lvm:vm-108-disk-0 | used=N/A | N/A: Node pve02, LVM-thin counters; storage query budget 60s: ssh: connect to host pve02 port 22: Connection timed out
VM 112 on pve01: complete; 2/2 disks measured
  efidisk0 | lvmthin | local-lvm:vm-112-disk-0 | used=576.00 KiB | LVM-thin mapped bytes (active volumes); source dmsetup status --target thin --noflush; mapped sectors × 512; shared blocks may be counted by multiple volumes
  ide0 | lvmthin | local-lvm:vm-112-disk-1 | used=25.08 GiB | LVM-thin mapped bytes (active volumes); source dmsetup status --target thin --noflush; mapped sectors × 512; shared blocks may be counted by multiple volumes
  ide2 | None | none | used=N/A | N/A: CD-ROM
VM 105 on pve02: unavailable; 0/2 disks measured
  efidisk0 | lvmthin | local-lvm:vm-105-disk-1 | used=N/A | N/A: Node pve02, LVM-thin counters; storage query budget 60s: ssh: connect to host pve02 port 22: Connection timed out
  scsi0 | lvmthin | local-lvm:vm-105-disk-0 | used=N/A | N/A: Node pve02, LVM-thin counters; storage query budget 60s: ssh: connect to host pve02 port 22: Connection timed out
VM 111 on pve02: unavailable; 0/1 disks measured
  ide2 | None | none | used=N/A | N/A: CD-ROM
  scsi0 | lvmthin | local-lvm:vm-111-disk-0 | used=N/A | N/A: Node pve02, LVM-thin counters; storage query budget 60s: ssh: connect to host pve02 port 22: Connection timed out
VM 103 on pve01: complete; 2/2 disks measured
  efidisk0 | lvmthin | local-lvm:vm-103-disk-0 | used=576.00 KiB | LVM-thin mapped bytes (active volumes); source dmsetup status --target thin --noflush; mapped sectors × 512; shared blocks may be counted by multiple volumes
  ide0 | None | none | used=N/A | N/A: CD-ROM
  ide2 | lvmthin | local-lvm:vm-103-cloudinit | used=N/A | N/A: CD-ROM
  scsi0 | lvmthin | local-lvm:vm-103-disk-1 | used=6.71 GiB | LVM-thin mapped bytes (active volumes); source dmsetup status --target thin --noflush; mapped sectors × 512; shared blocks may be counted by multiple volumes
VM 104 on pve02: unavailable; 0/2 disks measured
  efidisk0 | lvmthin | local-lvm:vm-104-disk-0 | used=N/A | N/A: Node pve02, LVM-thin counters; storage query budget 60s: ssh: connect to host pve02 port 22: Connection timed out
  ide0 | None | none | used=N/A | N/A: CD-ROM
  ide2 | lvmthin | local-lvm:vm-104-cloudinit | used=N/A | N/A: CD-ROM
  scsi0 | lvmthin | local-lvm:vm-104-disk-1 | used=N/A | N/A: Node pve02, LVM-thin counters; storage query budget 60s: ssh: connect to host pve02 port 22: Connection timed out
VM 900 on pve01: complete; 1/1 disks measured
  ide0 | lvmthin | local-lvm:vm-900-cloudinit | used=N/A | N/A: CD-ROM
  ide2 | None | none | used=N/A | N/A: CD-ROM
  scsi0 | lvmthin | local-lvm:vm-900-disk-0 | used=8.67 GiB | LVM-thin mapped bytes (active volumes); source dmsetup status --target thin --noflush; mapped sectors × 512; shared blocks may be counted by multiple volumes
VM 901 on pve02: unavailable; 0/1 disks measured
  ide0 | lvmthin | local-lvm:vm-901-cloudinit | used=N/A | N/A: CD-ROM
  ide2 | None | none | used=N/A | N/A: CD-ROM
  scsi0 | lvmthin | local-lvm:base-901-disk-0 | used=N/A | N/A: Node pve02, LVM-thin counters; storage query budget 60s: ssh: connect to host pve02 port 22: Connection timed out
