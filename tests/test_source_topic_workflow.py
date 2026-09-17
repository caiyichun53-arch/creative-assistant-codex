from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timezone
from unittest.mock import patch
import unittest

from scripts.core.model_gateway.formal_skill_adapter import FormalSkillContract, FormalSkillValidationError, validate_source_to_topic_output_semantics, validate_payload
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore
from scripts.core.production.stage1b_daily_discovery import Stage1BDailyDiscoveryService
from scripts.mcp.creation_assistant_mcp_server import CreationAssistantMcpApplication
from tests.test_stage2a_external_intelligence_boundary import _valid_source_to_topic_output
from tests.test_stage2b1_creation_assistant_mcp import CreationAssistantMcpStage2B1Tests


class SourceTopicWorkflowTests(unittest.TestCase):
    def test_hotspot_enters_science_workflow_without_report_or_manual_direction(self):
        domain = 'domain_1833831517eb'
        now = datetime.now(timezone.utc)
        source = {'source_type':'hotspot','source_object_id':'hotspot-fixture',
                  'source_object_version':'1','source_time':now.isoformat(),
                  'payload':{'title':'为什么商店很少出售猪奶','url':'https://example.org/pig-milk'}}
        with TemporaryDirectory() as directory:
            core = Stage0ContentProductionCore.open(Path(directory)/'test.sqlite3',data_identity='test')
            try:
                core.conn.execute("INSERT INTO trendradar_collection_run(collection_run_id, discovery_run_id, status, item_count, command_hash, started_at) VALUES ('fixture', 'fixture', 'completed', 1, 'fixture', ?)", (now.isoformat(),))
                core.conn.execute("INSERT INTO trendradar_hotspot_observation VALUES (?, 'fixture', ?, ?, 'fixture', 1, ?, '{}', 'fixture')", (source['source_object_id'],source['payload']['title'],source['payload']['url'],now.isoformat()))
                core.conn.commit()
                service = Stage1BDailyDiscoveryService(core=core)
                # Isolate routing from the science domain's missing formal boundary.
                boundary = {'scope':'test fixture only'}
                with patch(
                    'scripts.core.production.stage1b_daily_discovery.require_frozen_production_boundary', return_value=boundary
                ), patch('scripts.core.production.stage1b_daily_discovery.topic_domain_rules', return_value=boundary):
                    step = service.run_daily_discovery(discovery_date=now.date().isoformat(),actor='test',
                        idempotency_key='science-hotspot',execution_mode='test_isolated',now=now,
                        domains=(domain,),source_types=('hotspot',))
                self.assertEqual(step['status'],'requires_external_intelligence', step)
                task = step['task']
                self.assertEqual(task['task_type'],'source_to_topic')
                self.assertEqual(core.conn.execute('SELECT source_type FROM stage1b_source_version').fetchone()[0],'hotspot')
                output = _valid_source_to_topic_output(task['input']['source_evidence_refs'][0])
                app = CreationAssistantMcpApplication(core=core)
                with patch('scripts.core.production.candidate_priority.topic_domain_rules', return_value=boundary):
                    receipt = app.call_tool('creation_assistant_submit_external_result', {
                        'task_type':'source_to_topic','task_identity':task['task_identity'],
                        'execution_id':'test-hotspot','executor_id':'test-agent','output':output})
                self.assertEqual(core.conn.execute('SELECT COUNT(*) FROM stage1b_candidate_version').fetchone()[0],1)
                self.assertEqual(receipt['continuation']['task']['task_type'],'candidate_priority')
                from tests.test_candidate_priority import assessments
                priority = receipt['continuation']['task']
                with patch('scripts.core.production.candidate_priority.topic_domain_rules', return_value=boundary):
                    finished = app.call_tool('creation_assistant_submit_external_result', {
                        'task_type':'candidate_priority','task_identity':priority['task_identity'],
                        'execution_id':'test-priority','executor_id':'test-agent','output':assessments(priority)})
                self.assertEqual(finished['continuation']['status'],'completed')
                music = service.run_daily_discovery(discovery_date=now.date().isoformat(),actor='test',
                    idempotency_key='music-hotspot',execution_mode='test_isolated',now=now,
                    domains=('music_entertainment',),source_types=('hotspot',))
                self.assertEqual(music['summary']['source_evidence']['music_entertainment']['hotspot']['reason'],'disabled_for_music')
                self.assertEqual(core.conn.execute('SELECT COUNT(*) FROM stage1b_source_version WHERE run_id=?',(music['run_id'],)).fetchone()[0],0)
            finally:
                core.close()

    def test_one_mcp_chain_finishes_all_sources_then_priority_without_new_run(self):
        with TemporaryDirectory() as directory:
            core = Stage0ContentProductionCore.open(Path(directory)/'test.sqlite3',data_identity='test')
            try:
                first = CreationAssistantMcpStage2B1Tests._prepare_external_source(core,'first')
                CreationAssistantMcpStage2B1Tests._prepare_external_source(core,'second',run_id=first['run_id'])
                app = CreationAssistantMcpApplication(core=core)
                step = core.complete_discovery_run(run_id=first['run_id'],domains=('music_entertainment',),idempotency_key='complete-chain')
                for number in range(2):
                    task = step['task']
                    self.assertEqual(task['task_type'],'source_to_topic')
                    output = _valid_source_to_topic_output(task['input']['source_evidence_refs'][0])
                    if number == 1:
                        output.update(topic_status='no_result',candidate_topic='',topic_angle='',no_result_reason='unsupported_source')
                    receipt = app.call_tool('creation_assistant_submit_external_result',{'task_type':'source_to_topic','task_identity':task['task_identity'],'execution_id':str(number),'executor_id':'test-agent','output':output})
                    step = receipt['continuation']
                self.assertEqual(step['task']['task_type'],'candidate_priority')
                from tests.test_candidate_priority import assessments
                task = step['task']
                receipt = app.call_tool('creation_assistant_submit_external_result',{'task_type':'candidate_priority','task_identity':task['task_identity'],'execution_id':'priority','executor_id':'test-agent','output':assessments(task)})
                self.assertEqual(receipt['continuation']['status'],'completed')
                self.assertEqual(receipt['continuation']['run_id'],first['run_id'])
                self.assertEqual(core.conn.execute('SELECT COUNT(*) FROM stage1b_discovery_run').fetchone()[0],1)
                self.assertEqual(core.conn.execute('SELECT COUNT(*) FROM stage0_content_task').fetchone()[0],0)
            finally:
                core.close()

    def test_unsearched_clue_and_empty_delivery_cannot_become_candidates(self):
        supplied = {'source_evidence_items':['原始线索']}
        output = _valid_source_to_topic_output('原始线索')
        output['material_understanding']['input_kind'] = 'clue'
        with self.assertRaisesRegex(FormalSkillValidationError, 'search'):
            validate_source_to_topic_output_semantics(supplied, output)
        output['material_understanding']['input_kind'] = 'formed_topic'
        validate_source_to_topic_output_semantics(supplied, output)
        output['topic_shape']['one_piece_line'] = ''
        with self.assertRaisesRegex(FormalSkillValidationError, 'formed topic'):
            validate_source_to_topic_output_semantics(supplied, output)

    def test_retired_weak_candidate_status_is_not_an_alternate_entry(self):
        output = _valid_source_to_topic_output('原始线索')
        output['topic_status'] = 'valid_but_weak'
        with self.assertRaises(FormalSkillValidationError):
            validate_payload(output, FormalSkillContract.from_runtime_skill('source_to_topic').output_schema)

    def test_full_material_and_music_exclusions_reach_the_agent(self):
        text = '已有完整正文，不允许只传标题。' * 1000
        payload = Stage1BDailyDiscoveryService._assembly_payload(
            run_id='run',domain_label='music_entertainment',source_version_id='source',
            source={'source_type':'historical_high_signal','payload':{'title':'作品背后的真实经历','transcript':text}},
        )
        self.assertIn(text, payload['source_content'])
        self.assertIn(text, payload['source_evidence_items'])
        self.assertIn('专业乐评', payload['domain_rule_summary'])
        self.assertIn('演唱技术分析', payload['domain_rule_summary'])

    def test_mcp_submission_persists_topic_and_searched_material_for_priority(self):
        with TemporaryDirectory() as directory:
            core = Stage0ContentProductionCore.open(Path(directory)/'test.sqlite3',data_identity='test')
            try:
                ids = CreationAssistantMcpStage2B1Tests._prepare_external_source(core,'search-flow')
                app = CreationAssistantMcpApplication(core=core)
                identity = {'task_type':'source_to_topic','task_identity':ids}
                task = app.call_tool('creation_assistant_get_external_task',identity)['task']
                output = _valid_source_to_topic_output(task['input']['source_evidence_refs'][0])
                output['material_understanding'].update(input_kind='clue',search_status='searched',sources=[{'url':'https://example.org/fixture','title':'协议测试来源','findings':'仅验证材料回传，不是联网质量验收'}])
                output['supporting_evidence'] = ['https://example.org/fixture']
                app.call_tool('creation_assistant_submit_external_result',{**identity,'execution_id':'searched','executor_id':'test-agent','output':output})
                self.assertEqual(core.conn.execute('SELECT COUNT(*) FROM stage1b_candidate_version').fetchone()[0],1)
                task = core.prepare_candidate_priority_external_task(run_id=ids['run_id'],domain_label='music_entertainment')
                self.assertIn('domain_rules',task['input'])
                self.assertEqual(task['input']['candidates'][0]['materials'][0]['topic_formation']['material_understanding']['sources'],output['material_understanding']['sources'])
                # A new unconvertible clue is an absence, not a low-score candidate.
                other = CreationAssistantMcpStage2B1Tests._prepare_external_source(core,'no-topic',run_id=ids['run_id'])
                absent = deepcopy(output)
                absent.update(topic_status='no_result',candidate_topic='',topic_angle='',no_result_reason='unsupported_source')
                absent['material_understanding'].update(search_status='not_needed',reason='测试排除方向',sources=[])
                absent['supporting_evidence']=[]
                app.call_tool('creation_assistant_submit_external_result',{'task_type':'source_to_topic','task_identity':other,'execution_id':'absent','executor_id':'test-agent','output':absent})
                self.assertEqual(core.conn.execute('SELECT COUNT(*) FROM stage1b_candidate_version').fetchone()[0],1)
                self.assertEqual(core.conn.execute('SELECT COUNT(*) FROM stage1b_candidate_absence').fetchone()[0],1)
            finally:
                core.close()
