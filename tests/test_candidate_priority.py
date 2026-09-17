from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage1b_daily_discovery import Stage1BDailyDiscoveryService
from scripts.mcp.creation_assistant_mcp_server import CreationAssistantMcpApplication
from tests.test_stage2b1_creation_assistant_mcp import CreationAssistantMcpStage2B1Tests, _StdioMcpClient
from tests.test_stage2a_external_intelligence_boundary import _valid_source_to_topic_output


def add_candidate(core, suffix, run_id=None, candidate_payload=None, material_payload=None):
    ids = CreationAssistantMcpStage2B1Tests._prepare_external_source(core, suffix, run_id, material_payload=material_payload)
    service = Stage1BDailyDiscoveryService(core=core, gateway=None)
    task = service.prepare_source_to_topic_external_task(**ids)
    service.external_executor = lambda task: {'execution_id':'source-'+suffix,'executor_id':'test-agent','model_ref':None,'output':_valid_source_to_topic_output(task['input']['source_evidence_refs'][0])}
    receipt, judgement = service._run_source_to_topic_skill(**ids, input_payload=core.load_discovery_external_assembly_payload(**ids))
    payload = candidate_payload or {**judgement,'title':'测试候选 '+suffix}
    if candidate_payload is not None:
        payload = deepcopy(candidate_payload)
        payload['source_reference']['source_version_id'] = ids['source_version_id']
    candidate = core.create_discovery_candidate(
        run_id=ids['run_id'],source_version_id=ids['source_version_id'],model_run_id=receipt.model_run_id,
        candidate_id='candidate-'+suffix,payload=payload,idempotency_key='candidate-'+suffix,
    )
    return {**ids,**candidate}


def assessments(task, scores=None):
    return {'schema_version':'candidate_priority.output.v1', 'input_fingerprint':task['input']['input_fingerprint'],
            'assessments':[{'candidate_version_id':c['candidate_version_id'], 'score':(scores or {}).get(c['candidate_version_id'],70),
                            'reason':'已有材料提供可比较的作品片段，观众能听见具体差异。',
                            'limitations':'测试输入；实际价值仍需人工及发布反馈检验。',
                            'evidence_refs':[c['materials'][0]['source_version_id']]} for c in task['input']['candidates']]}


class CandidatePriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name)/'candidate.sqlite3'
        self.core = Stage0ContentProductionCore.open(self.path,data_identity='test')
        self.first = add_candidate(self.core,'first')
        self.run_id = self.first['run_id']

    def tearDown(self):
        self.core.close()
        self.tmp.cleanup()

    def task(self):
        return self.core.prepare_candidate_priority_external_task(run_id=self.run_id,domain_label='music_entertainment')

    def submit(self, output):
        return self.core.submit_candidate_priority_external_result(run_id=self.run_id,domain_label='music_entertainment',
            output=output,execution_id=hashlib.sha256(json.dumps(output,sort_keys=True).encode()).hexdigest(),executor_id='current-agent',model_ref='agent-reported-model')

    def finish(self):
        return self.core.complete_discovery_run(run_id=self.run_id,domains=('music_entertainment',),idempotency_key='finish:'+self.run_id)

    def test_no_field_based_score_and_all_candidates_scored_before_top_ten(self):
        for i in range(11): add_candidate(self.core,str(i),self.run_id)
        pending = self.finish()
        self.assertEqual(pending['status'],'requires_external_intelligence')
        self.assertEqual(self.core._discovery_run(self.run_id)['status'],'processing')
        self.assertEqual(len(pending['task']['input']['candidates']),12)
        output = assessments(pending['task'])
        partial = deepcopy(output); partial['assessments'].pop()
        with self.assertRaisesRegex(ValueError,'every candidate'): self.submit(partial)
        self.submit(output)
        self.assertEqual(self.finish()['status'],'completed')
        rows = self.core.get_discovery_snapshot(run_id=self.run_id,domain_label='music_entertainment')
        self.assertEqual(len(rows),10)
        self.assertEqual({r['score']['total'] for r in rows},{70})
        self.assertEqual(self.core.conn.execute('SELECT count(*) FROM stage1b_candidate_priority').fetchone()[0],12)
        expected = [c['candidate_version_id'] for c in pending['task']['input']['candidates']][:10]
        self.assertEqual([r['candidate_version_id'] for r in rows],expected)

    def test_changed_support_rejects_stale_output_and_requires_reassessment(self):
        original = self.task(); self.submit(assessments(original))
        second = CreationAssistantMcpStage2B1Tests._prepare_external_source(self.core,'support',self.run_id)
        self.core.record_candidate_support(candidate_version_id=self.first['candidate_version_id'],
            source_version_id=second['source_version_id'],relation_reason='同角度补充材料')
        with self.assertRaisesRegex(ValueError,'changed'): self.submit(assessments(original))
        self.assertEqual(self.finish()['status'],'requires_external_intelligence')
        refreshed = self.task()
        self.assertEqual(len(refreshed['input']['candidates'][0]['materials']),2)
        self.submit(assessments(refreshed,{self.first['candidate_version_id']:42}))
        self.finish()
        self.assertEqual(self.core.get_discovery_snapshot(run_id=self.run_id,domain_label='music_entertainment')[0]['score']['total'],42)

    def test_foreign_evidence_and_invalid_numbers_cannot_be_written(self):
        original = assessments(self.task())
        for bad in [-1,101,True,70.5]:
            output = deepcopy(original); output['assessments'][0]['score']=bad
            with self.assertRaises((ValueError,RuntimeError)): self.submit(output)
        output = deepcopy(original); output['assessments'][0]['evidence_refs']=['invented-source']
        with self.assertRaisesRegex(ValueError,'evidence'): self.submit(output)
        self.assertEqual(self.core.conn.execute('SELECT count(*) FROM stage1b_candidate_priority').fetchone()[0],0)

    def test_snapshot_keeps_its_own_assessment_after_later_run(self):
        self.submit(assessments(self.task(),{self.first['candidate_version_id']:42})); self.finish()
        second = self.core.create_discovery_run(discovery_date='2026-09-08',actor='test',execution_mode='test_isolated',
            domains=('music_entertainment',),idempotency_key='second-run')
        old_run = self.run_id; self.run_id=second['run_id']
        self.submit(assessments(self.task(),{self.first['candidate_version_id']:81})); self.finish()
        old = self.core.get_discovery_snapshot(run_id=old_run,domain_label='music_entertainment')
        new = self.core.get_discovery_snapshot(run_id=self.run_id,domain_label='music_entertainment')
        self.assertEqual((old[0]['score']['total'],new[0]['score']['total']),(42,81))

    def test_stdio_mcp_uses_current_agent_submission_and_core_persistence(self):
        client = _StdioMcpClient(self.path)
        def call(name,args):
            result=client.request('tools/call',{'name':name,'arguments':args})['result']
            self.assertFalse(result['isError'],result)
            return result['structuredContent']
        try:
            client.request('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'acceptance','version':'1'}})
            identity={'task_type':'candidate_priority','task_identity':{'run_id':self.run_id,'domain_label':'music_entertainment'}}
            task=call('creation_assistant_get_external_task',identity)['task']
            self.assertNotIn('model',task)
            result=call('creation_assistant_submit_external_result',{**identity,'output':assessments(task),
                'execution_id':'stdio-execution','executor_id':'codex-acceptance','model_ref':'agent-current'})
            self.assertEqual(result['status'],'accepted')
        finally: client.close()
        row=self.core.conn.execute('SELECT assessed_by,model_ref FROM stage1b_candidate_priority').fetchone()
        self.assertEqual(tuple(row),('codex-acceptance','agent-current'))
        self.assertEqual(self.finish()['status'],'completed')

    def test_daily_continuation_keeps_original_run_and_outer_status(self):
        from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
        pending = self.finish()
        class DailyService:
            def __init__(self, **kwargs): pass
            def run(self, **kwargs): return {"status":"completed"}
            def run_candidate_discovery(self, **kwargs): return pending
        result = CreationAssistantFormalBusinessCore(core=self.core).execute_daily(
            domain_label="music_entertainment",business_date="2026-09-08",daily_run_id="test-daily",
            resume=False,attempt_ref="test",service_factory=DailyService)
        self.assertEqual(result["status"],"requires_external_intelligence")
        self.assertEqual(result["task"]["task_type"],"candidate_priority")
        self.submit(assessments(pending["task"]))
        resumed = self.core.continue_candidate_priority_run(run_id=self.run_id)
        self.assertEqual(resumed["status"],"completed")
        self.assertEqual(resumed["run_id"],self.run_id)
        self.assertEqual(self.core.conn.execute("SELECT count(*) FROM stage1b_discovery_run").fetchone()[0],1)
        self.assertEqual(self.core.conn.execute("SELECT count(*) FROM stage1b_candidate_decision").fetchone()[0],0)

    def test_waiting_agent_is_not_recovered_as_stopped_or_recollected(self):
        from scripts.core.formal_business_entrypoints import CreationAssistantFormalBusinessCore
        from tests.test_core_unified_status import _seed_domain
        _seed_domain(self.core,"music_entertainment",cold_status="completed")
        daily = self.core.get_or_create_daily_run(domain_label="music_entertainment",business_date="2026-09-08",actor="test")
        daily = self.core.start_daily_run(daily_run_id=daily["daily_run_id"],resume=False,actor="test")
        self.run_id = self.core.create_discovery_run(discovery_date="2026-09-08",actor="test",
            execution_mode="production_daily",daily_run_id=daily["daily_run_id"],domains=("music_entertainment",),
            idempotency_key="daily-scoring-test")["run_id"]
        pending = self.finish()
        core = self.core
        calls = []
        class DailyService:
            def __init__(self, **kwargs): pass
            def run(self, **kwargs):
                calls.append("collection")
                return {"status":"completed","collection":[{"preserved":"collection evidence"}]}
            def run_candidate_discovery(self, **kwargs):
                return core.continue_candidate_priority_run(run_id=pending["run_id"])
        business = CreationAssistantFormalBusinessCore(core=core)
        args=dict(domain_label="music_entertainment",business_date="2026-09-08",daily_run_id=daily["daily_run_id"],resume=False,attempt_ref="test",service_factory=DailyService)
        first = business.execute_daily(**args)
        self.assertEqual(first["status"],"requires_external_intelligence")
        self.submit(assessments(first["task"]))
        prepared = business.prepare_daily_execution(domain_label="music_entertainment",business_date="2026-09-08",trigger="test",resume=False,attempt_ref="continue")
        self.assertTrue(prepared["execute"])
        self.assertEqual(prepared["daily_run"]["lifecycle"],"running")
        result = business.execute_daily(**args)
        self.assertEqual(result["status"],"completed")
        self.assertEqual(result["collection"],[{"preserved":"collection evidence"}])
        self.assertEqual(calls,["collection"])
        final = business.finalize_daily_execution(daily_run=prepared["daily_run"],execution_result=result,resume=False)
        self.assertEqual(final["daily_run_status"],"completed")
        self.assertEqual(core.conn.execute("SELECT count(*) FROM stage1b_run_execution_context WHERE execution_mode='production_daily'").fetchone()[0],1)

    def test_upgrade_removes_retired_score_tables_without_deleting_candidate(self):
        self.core.conn.execute('CREATE TABLE stage1b_candidate_assessment (old_score REAL)')
        self.core.conn.execute('CREATE TABLE stage1b_candidate_assessment_revision (old_score REAL)')
        self.core.conn.execute('INSERT INTO stage1b_candidate_assessment VALUES (99)')
        self.core.conn.commit()
        self.core.install_schema()
        names={r[0] for r in self.core.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('stage1b_candidate_assessment',names)
        self.assertNotIn('stage1b_candidate_assessment_revision',names)
        self.assertEqual(self.core.conn.execute('SELECT count(*) FROM stage1b_candidate_version').fetchone()[0],1)
        self.assertEqual(self.finish()['status'],'requires_external_intelligence')


if __name__ == '__main__': unittest.main()
