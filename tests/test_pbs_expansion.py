import contextlib,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pve_inventory as inv
from test_extensions import FakePBS,collect_pbs

class PBSExpansionTests(unittest.TestCase):
    def test_pbs_only_without_pve(self):
        with tempfile.TemporaryDirectory() as tmp, patch('pve_inventory.Client',side_effect=AssertionError('PVE used')), patch('pve_inventory.PBSClient',return_value=FakePBS()), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code=inv.main(['--pbs-only','--no-prompt','--pbs-url','https://pbs:8007','--output-root',tmp,'--pbs-history'])
            self.assertEqual(code,0)
            folder=next(Path(tmp).glob('pve-report-*'))
            r=json.loads((folder/'inventory.json').read_text(encoding='utf-8'))
            self.assertTrue(r['scope']['pbs_only'])
            self.assertTrue((Path(tmp)/'pbs-history.sqlite3').exists())
            self.assertIn('PBS-only report',(folder/'inventory.html').read_text(encoding='utf-8'))

    def test_root_namespace_selection_and_unknown_protection(self):
        with patch('sys.stderr'):
            report=inv.PBSCollector(FakePBS(),namespaces=['/']).collect()
        self.assertEqual({g['namespace'] for g in report['groups']},{''})
        self.assertIsNone(report['groups'][0]['protected_count'])
        self.assertEqual(report['groups'][0]['verification_state'],'unavailable')

    def test_task_history_paginates_and_marks_limit(self):
        client=FakePBS({'/nodes/localhost/tasks':[{'worker_type':'prunejob','status':'OK','starttime':100,'endtime':200}]*100})
        with patch('sys.stderr'):
            r=inv.PBSCollector(client,task_limit=150).collect()
        calls=[params for path,params in client.calls if path=='/nodes/localhost/tasks']
        self.assertEqual([c['start'] for c in calls],[0,100])
        self.assertFalse(r['datastores'][0]['task_history']['complete_within_visible_scope'])

    def test_unknown_backend_does_not_become_filesystem_capacity(self):
        r=collect_pbs(FakePBS({'/config/datastore/store':{'backend':{'type':'s3'}}}))
        self.assertIsNone(r['datastores'][0]['used_bytes'])

    def test_history_deduplicates_and_separates_instances(self):
        r={'pbs':collect_pbs(),'run':{'run_id':'one'},'generated_at':'2026-11-01T00:00:00+00:00'}
        with tempfile.TemporaryDirectory() as tmp:
            inv.pbs_history(r,Path(tmp),'https://one')
            inv.pbs_history(r,Path(tmp),'https://one')
            self.assertEqual(len(r['pbs']['datastores'][0]['capacity_history']),1)
            r['run']['run_id']='two';r['generated_at']='2026-11-02T00:00:00+00:00'
            inv.pbs_history(r,Path(tmp),'https://two')
            self.assertEqual(len(r['pbs']['datastores'][0]['capacity_history']),1)

    def test_verification_and_freshness_do_not_claim_ownership(self):
        r={'pbs':collect_pbs(),'vms':[],'scope':{'pbs_only':True},'jobs':None}
        inv.pbs_relationships(r,'https://pbs',[{'store':'store','namespace':'','hours':24}])
        g=next(g for g in r['pbs']['groups'] if g['namespace']=='')
        self.assertEqual(g['freshness'],'older than expected')
        self.assertEqual(g['matching_pve'],'PVE matching not requested')
        self.assertEqual(inv.verification_state({'state':'ok'}),'passed')
        self.assertEqual(inv.verification_state(None),'unavailable')
