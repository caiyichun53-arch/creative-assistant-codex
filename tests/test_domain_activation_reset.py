from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.core.business_data.domain_labels import DOMAIN_CONFIG_DIR, set_domain_pack_config_dir
from scripts.core.production.cold_start_onboarding import ColdStartOnboardingService
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore, StateTransitionError
from tests._cold_start_test_model import resolve_task_model


class DomainActivationResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.config_dir = root / "domain_packs"
        self.config_dir.mkdir()
        set_domain_pack_config_dir(self.config_dir)
        self.connection = sqlite3.connect(":memory:")
        self.core = Stage0ContentProductionCore(
            self.connection,
            db_path=Path(":memory:"),
            data_identity="test",
        )
        self.core.install_schema()
        self.service = ColdStartOnboardingService(
            core=self.core,
            config_dir=self.config_dir,
            task_model_resolver=resolve_task_model,
        )

    def tearDown(self) -> None:
        self.core.close()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)
        self.tempdir.cleanup()

    @staticmethod
    def payload(domain: str, *, reuse_label: str | None = None) -> dict:
        payload = {
            "domain_mode": "reuse" if reuse_label else "create",
            "domain_name": domain,
            "platform": "douyin",
            "owned_account": {
                "display_name": "same-owned-account",
                "external_account_ref": f"douyin:{domain}:owned",
            },
            "competitor_accounts": [
                {
                    "display_name": f"same-competitor-{index}",
                    "external_account_ref": f"douyin:{domain}:competitor:{index}",
                }
                for index in range(20)
            ],
        }
        if reuse_label:
            payload["existing_domain_label"] = reuse_label
        return payload

    def _confirm_create(self, domain: str) -> dict:
        payload = self.payload(domain)
        result = self.service.confirm(payload, transport_actor="test-user")
        self.assertTrue(result["automatic_start"])
        return result

    def _confirm_reuse(self, domain_label: str, domain_name: str) -> dict:
        payload = self.payload(domain_name, reuse_label=domain_label)
        result = self.service.confirm(payload, transport_actor="test-user")
        self.assertTrue(result["automatic_start"])
        return result

    def test_missing_activation_schema_requires_migration(self) -> None:
        legacy_connection = sqlite3.connect(":memory:")
        legacy_core = Stage0ContentProductionCore(
            legacy_connection,
            db_path=Path(":memory:"),
            data_identity="test",
        )
        try:
            with self.assertRaisesRegex(
                StateTransitionError,
                "activation schema not initialized; migration required",
            ):
                legacy_core.reset_domain(domain_label="legacy-domain", actor="test-user")
            with self.assertRaisesRegex(
                StateTransitionError,
                "activation schema not initialized; migration required",
            ):
                legacy_core.domain_business_state(domain_label="legacy-domain")
        finally:
            legacy_core.close()

    def _seed_knowledge_assets(self, *, domain_label: str, cold_start_id: str) -> tuple[str, str]:
        config = self.core.get_current_domain_configuration(domain_label=domain_label)
        assert config is not None
        competitor_id = str(config["competitor_account_ids"][0])
        self.connection.execute(
            "INSERT INTO competitor_accounts("
            "account_id, platform, domain_label, domain_name, account_name, sec_uid, "
            "homepage_url, source_config_ref, registration_status, first_crawl_policy, comments_policy"
            ") VALUES (?, 'douyin', ?, ?, 'same-competitor-0', ?, ?, ?, 'active', 'fixture', 'fixture')",
            (
                competitor_id,
                domain_label,
                domain_label,
                f"sec:{competitor_id}",
                f"https://example.invalid/{competitor_id}",
                f"stage0_competitor_registration:fixture:{competitor_id}",
            ),
        )
        self.connection.execute(
            "INSERT INTO baselines("
            "baseline_id, account_id, baseline_mode, metric, observation_point, sample_count, "
            "median_value, run_id, computed_at"
            ") VALUES ('baseline-old', ?, 'mature_history', 'like_count', NULL, 20, 123.0, ?, ?)",
            (competitor_id, cold_start_id, "2026-08-29T00:00:00+00:00"),
        )
        self.connection.execute(
            "INSERT INTO stage0_cold_start_tag_library("
            "tag_library_id, cold_start_id, domain_label, extraction_method, source_item_count, "
            "minimum_source_support, candidate_set_json, tag_ids_json, status, data_identity, "
            "created_by, created_at"
            ") VALUES ('tag-library-old', ?, ?, 'fixture', 1, 1, '[]', '[]', 'accepted', 'test', 'fixture', ?)",
            (cold_start_id, domain_label, "2026-08-29T00:00:00+00:00"),
        )
        experience_run = self.core.start_pre_topic_experience_run(
            domain_label=domain_label,
            actor="test-user",
        )
        frozen_sources = [
            {
                "source_id": f"source-{index}",
                "account_ref": f"account-{index % 2}",
                "account_name": f"account-{index % 2}",
                "content_subject_type": "person",
                "expression_form": "analysis",
                "structure_assessment": {"statement": "fixture structure evidence"},
                "recurring_evidence_patterns": [
                    {
                        "spoken_action": "fixture evidence action",
                        "source_evidence": [{"text": "fixture evidence quote"}],
                    }
                ],
            }
            for index in range(3)
        ]
        candidate = self.core.open_experience_candidate(
            task_id=None,
            domain_label=domain_label,
            frozen_sources=frozen_sources,
            actor="test-user",
            experience_candidate_run_id=str(experience_run["experience_candidate_run_id"]),
        )
        self.core.complete_experience_candidate(
            experience_candidate_id=str(candidate["experience_candidate_id"]),
            proposal={
                "decision": "proposal",
                "candidate": {
                    "summary": "fixture formal experience",
                    "applicable_when": ["fixture condition"],
                    "method": {"step": "fixture method"},
                    "boundary": {"not_for": "fixture boundary"},
                },
                "source_ids": [item["source_id"] for item in frozen_sources],
            },
            model_run_id="fixture-model-run",
        )
        self.core.decide_experience_candidate(
            experience_candidate_id=str(candidate["experience_candidate_id"]),
            decision="accepted",
            actor="test-user",
            actor_kind="user",
            reason="fixture acceptance",
        )
        self.connection.commit()
        return str(candidate["experience_candidate_id"]), str(experience_run["experience_candidate_run_id"])

    def test_reset_releases_only_current_world_and_allows_repeated_reuse(self) -> None:
        first = self._confirm_create("reset-round-trip")
        first_config = self.core.get_cold_start_configuration(
            configuration_id=str(first["configuration_id"])
        )
        domain_label = str(first_config["domain_label"])
        first_daily = self.core.get_or_create_daily_run(
            domain_label=domain_label,
            business_date="2026-08-29",
            actor="test-user",
        )
        candidate_id, candidate_run_id = self._seed_knowledge_assets(
            domain_label=domain_label,
            cold_start_id=str(first["cold_start_id"]),
        )
        before_counts = {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "stage0_cold_start",
                "stage0_daily_run",
                "baselines",
                "stage0_experience_candidate",
                "stage0_confirmed_experience",
            )
        }
        before_cards = self.core.list_experience_candidate_source_cards(
            candidate_ids=[candidate_id]
        )[candidate_id]
        self.assertEqual(len(before_cards), 3)
        self.assertTrue(self.core.domain_business_state(domain_label=domain_label)["blockers"])

        reset_one = self.core.reset_domain(domain_label=domain_label, actor="test-user")
        self.assertTrue(reset_one["reset"])
        self.assertIsNone(self.core.get_current_domain_activation(domain_label=domain_label))
        self.assertTrue(self.core.domain_business_state(domain_label=domain_label)["zero_state"])
        self.assertIsNone(
            self.core.get_daily_run_for_domain_date(
                domain_label=domain_label,
                business_date="2026-08-29",
            )
        )
        with self.assertRaises(StateTransitionError):
            self.service.resume_current_cold_start(
                cold_start_id=str(first["cold_start_id"]),
                actor="test-user",
            )

        second = self._confirm_reuse(domain_label, "reset-round-trip")
        self.assertNotEqual(first["cold_start_id"], second["cold_start_id"])
        self.assertEqual(
            self.core.get_current_domain_activation(domain_label=domain_label)["cold_start_id"],
            second["cold_start_id"],
        )
        second_daily = self.core.get_or_create_daily_run(
            domain_label=domain_label,
            business_date="2026-08-29",
            actor="test-user",
        )
        self.assertNotEqual(first_daily["daily_run_id"], second_daily["daily_run_id"])

        reset_two = self.core.reset_domain(domain_label=domain_label, actor="test-user")
        self.assertTrue(reset_two["reset"])
        third = self._confirm_reuse(domain_label, "reset-round-trip")
        self.assertNotEqual(second["cold_start_id"], third["cold_start_id"])

        after_counts = {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in before_counts
        }
        self.assertEqual(after_counts["stage0_cold_start"], before_counts["stage0_cold_start"] + 2)
        self.assertEqual(after_counts["stage0_daily_run"], before_counts["stage0_daily_run"] + 1)
        self.assertEqual(after_counts["baselines"], before_counts["baselines"])
        self.assertEqual(after_counts["stage0_experience_candidate"], before_counts["stage0_experience_candidate"])
        self.assertEqual(after_counts["stage0_confirmed_experience"], before_counts["stage0_confirmed_experience"])
        after_cards = self.core.list_experience_candidate_source_cards(
            candidate_ids=[candidate_id]
        )[candidate_id]
        self.assertEqual(after_cards, before_cards)
        self.assertEqual(
            len(self.core.list_pre_topic_experience_candidates(domain_label=domain_label)),
            1,
        )
        self.assertEqual(candidate_run_id, self.core.list_pre_topic_experience_candidates(domain_label=domain_label)[0]["experience_candidate_run_id"])

        account_count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM stage0_content_account WHERE data_identity='test'"
            ).fetchone()[0]
        )
        self.assertEqual(account_count, 21)
        self.assertEqual(
            int(self.connection.execute("SELECT COUNT(*) FROM stage0_cold_start").fetchone()[0]),
            3,
        )

    def test_domains_are_isolated_and_old_state_does_not_block_new_round(self) -> None:
        first = self._confirm_create("reset-isolation-a")
        first_config = self.core.get_cold_start_configuration(
            configuration_id=str(first["configuration_id"])
        )
        domain_a = str(first_config["domain_label"])
        other = self._confirm_create("reset-isolation-b")
        other_config = self.core.get_cold_start_configuration(
            configuration_id=str(other["configuration_id"])
        )
        domain_b = str(other_config["domain_label"])
        other_activation = self.core.get_current_domain_activation(domain_label=domain_b)

        self.core.reset_domain(domain_label=domain_a, actor="test-user")
        self.assertTrue(self.core.domain_business_state(domain_label=domain_a)["zero_state"])
        self.assertFalse(self.core.domain_business_state(domain_label=domain_b)["zero_state"])
        self.assertEqual(
            self.core.get_current_domain_activation(domain_label=domain_b)["activation_id"],
            other_activation["activation_id"],
        )
        self._confirm_reuse(domain_a, "reset-isolation-a")
        self.assertFalse(self.core.domain_business_state(domain_label=domain_a)["zero_state"])


if __name__ == "__main__":
    unittest.main()
