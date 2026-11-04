import unittest
from types import SimpleNamespace
import pve_inventory as inv
from test_inventory import FakeClient, collect, GIB


class MeasurementTests(unittest.TestCase):
    def test_auxiliary_failure_keeps_known_amount_and_full_total_unknown(self):
        client = FakeClient()
        client.data['/nodes/a/qemu/100/config']['efidisk0'] = 'local:100/efi.raw,size=4M'
        vm = collect(client)['vms'][0]
        detail = inv.measurement_details(vm)
        self.assertIsNone(vm['actual_bytes'])
        self.assertEqual(detail['known_actual_bytes'], 20 * GIB)
        self.assertEqual(detail['measurement_status'], 'partial')
        self.assertEqual(detail['measured_disk_count'], 2)
        self.assertEqual(detail['counted_disk_count'], 3)
        self.assertIn('efidisk0', ' '.join(inv.allocation_vm_reason(vm)))

    def test_zero_with_unknown_is_partial(self):
        vm = collect()['vms'][0]
        for disk in vm['disks']:
            if disk['actual_bytes'] is not None:
                disk['actual_bytes'] = 0
        next(d for d in vm['disks'] if d['slot'] == 'scsi2')['actual_bytes'] = None
        vm['actual_bytes'] = None
        self.assertEqual(inv.measurement_details(vm)['known_actual_bytes'], 0)
        self.assertIn('partial', inv.measurement_label(vm))

    def test_failed_thin_target_is_distinct_from_absent(self):
        result = inv.parse_thin_status('pve-vm--100--disk--0: 0 100 thin Fail\nother: unexpected\n')
        self.assertIn('failure', result.errors['pve-vm--100--disk--0'])
        self.assertIn('not understood', result.errors['other'])
        self.assertNotIn('absent', result.errors)

    def test_raw_file_routed_to_owner_and_block_device_rejected(self):
        commands = []
        def run(command):
            self.assertTrue(inv.approved_backend_command(command))
            commands.append(command)
            return 'b\n' if command[-1] == 'hostname -s' else '/images/test.raw\n' if 'pvesm' in command[-1] else '81a4|8|512'
        client = SimpleNamespace(entry_node='a', backend_command=run, get=lambda path: [dict(type='node', name='b', ip='2001:db8::2')])
        self.assertEqual(inv.raw_file_allocation(client, 'b', 'local:100/test.raw'), 4096)
        self.assertTrue(all(command[-2] == 'root@2001:db8::2' for command in commands))
        client.backend_command = lambda command: '/dev/disk' if 'pvesm' in command[-1] else '61a4|8|512'
        with self.assertRaises(inv.ReadError):
            inv.raw_file_allocation(client, 'b', 'local:100/test.raw')

    def test_backend_command_rejects_mutations(self):
        for command in (['rm', '/images/test.raw'], ['pvesm', 'free', 'local:100/test.raw'], ['stat', '/tmp/x']):
            self.assertFalse(inv.approved_backend_command(command))
