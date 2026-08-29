from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from scripts.core.production.cold_start_onboarding import ColdStartOnboardingService
from scripts.core.production.stage0_content_core import (
    FORMAL_DB_PATH,
    Stage0ContentProductionCore,
    StateTransitionError,
)


class FormalCopyMigrationRunTests(unittest.TestCase):
    def test_upgrade_legacy_formal_copy_leave_no_current_then_start_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            copy_db = Path(tempdir) / "production_activation.sqlite3"
            shutil.copy2(FORMAL_DB_PATH, copy_db)
            connection = sqlite3.connect(copy_db)
            connection.row_factory = sqlite3.Row
            core = Stage0ContentProductionCore(
                connection,
                db_path=copy_db,
                data_identity="production",
            )
            try:
                domains = ("泛科普-社会生活", "music_entertainment")
                old_run_ids = {
                    str(row["cold_start_id"])
                    for row in connection.execute(
                        "SELECT cold_start_id FROM stage0_cold_start "
                        "WHERE domain_label IN (?, ?) AND data_identity='production'",
                        domains,
                    ).fetchall()
                }
                old_daily_dates = {
                    domain: str(row["business_date"])
                    for domain in domains
                    for row in [connection.execute(
                        "SELECT business_date FROM stage0_daily_run "
                        "WHERE domain_label=? AND data_identity='production' "
                        "ORDER BY created_at LIMIT 1",
                        (domain,),
                    ).fetchone()]
                    if row is not None
                }
                tables_before = {
                    str(row["name"]): int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM \"{row['name']}\""
                        ).fetchone()[0]
                    )
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                candidate_rows_before = connection.execute(
                    "SELECT frozen_sources_json FROM stage0_experience_candidate "
                    "WHERE data_identity='production' ORDER BY experience_candidate_id"
                ).fetchall()
                research_rows_before = connection.execute(
                    "SELECT material_json FROM stage0_research_material "
                    "WHERE data_identity='production'"
                ).fetchall()

                core.install_schema()

                self.assertTrue(core.domain_activation_schema_available())
                self.assertEqual(
                    int(connection.execute(
                        "SELECT COUNT(*) FROM stage0_domain_activation"
                    ).fetchone()[0]),
                    0,
                )
                tables_after = {
                    str(row["name"]): int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM \"{row['name']}\""
                        ).fetchone()[0]
                    )
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                for table, count in tables_before.items():
                    self.assertEqual(tables_after[table], count, table)

                self.assertEqual(len(candidate_rows_before), 14)
                self.assertEqual(
                    sum(
                        len(json.loads(str(row["frozen_sources_json"])))
                        for row in candidate_rows_before
                    ),
                    112,
                )
                self.assertEqual(len(research_rows_before), 89)
                self.assertEqual(
                    int(connection.execute(
                        "SELECT COUNT(*) FROM stage0_confirmed_experience "
                        "WHERE data_identity='production'"
                    ).fetchone()[0]),
                    0,
                )
                for row in candidate_rows_before:
                    self.assertIsInstance(
                        json.loads(str(row["frozen_sources_json"])), list
                    )
                for row in research_rows_before:
                    self.assertIsInstance(json.loads(str(row["material_json"])), dict)

                for domain in domains:
                    self.assertIsNone(
                        core.get_current_domain_activation(domain_label=domain)
                    )
                    self.assertTrue(
                        core.domain_business_state(domain_label=domain)["zero_state"]
                    )
                    self.assertIsNone(core.current_domain_tag_ids(domain_label=domain))
                    self.assertIsNone(
                        core.current_domain_content_types_frozen(domain_label=domain)
                    )
                    self.assertIsNone(
                        core.current_domain_boundary_frozen(domain_label=domain)
                    )
                    if domain in old_daily_dates:
                        self.assertIsNone(
                            core.get_daily_run_for_domain_date(
                                domain_label=domain,
                                business_date=old_daily_dates[domain],
                            )
                        )

                service = ColdStartOnboardingService(core=core)
                for old_run_id in old_run_ids:
                    with self.assertRaises(StateTransitionError):
                        service.resume_current_cold_start(
                            cold_start_id=old_run_id,
                            actor="formal-copy-test",
                        )

                domain = "music_entertainment"
                old_config = connection.execute(
                    "SELECT domain_name, platform FROM stage0_cold_start_configuration "
                    "WHERE domain_label=? AND data_identity='production' "
                    "ORDER BY confirmed_at DESC LIMIT 1",
                    (domain,),
                ).fetchone()
                owned = connection.execute(
                    "SELECT display_name, external_account_ref FROM stage0_content_account "
                    "WHERE domain_label=? AND account_role='owned' AND status='active' "
                    "AND data_identity='production' ORDER BY created_at LIMIT 1",
                    (domain,),
                ).fetchone()
                competitors = connection.execute(
                    "SELECT display_name, external_account_ref FROM stage0_content_account "
                    "WHERE domain_label=? AND account_role='competitor' AND status='active' "
                    "AND data_identity='production' "
                    "ORDER BY created_at, content_account_id LIMIT 20",
                    (domain,),
                ).fetchall()
                self.assertIsNotNone(old_config)
                self.assertIsNotNone(owned)
                self.assertEqual(len(competitors), 20)
                old_owned_id = str(connection.execute(
                    "SELECT content_account_id FROM stage0_content_account "
                    "WHERE domain_label=? AND account_role='owned' AND status='active' "
                    "AND data_identity='production' ORDER BY created_at LIMIT 1",
                    (domain,),
                ).fetchone()[0])
                old_competitor_ids = [
                    str(row["content_account_id"])
                    for row in connection.execute(
                        "SELECT content_account_id FROM stage0_content_account "
                        "WHERE domain_label=? AND account_role='competitor' AND status='active' "
                        "AND data_identity='production' "
                        "ORDER BY created_at, content_account_id LIMIT 20",
                        (domain,),
                    ).fetchall()
                ]
                configuration = core.configure_cold_start_subjects(
                    domain_mode="reuse",
                    domain_label=domain,
                    domain_name=str(old_config["domain_name"]),
                    platform=str(old_config["platform"]),
                    owned_account={
                        "display_name": str(owned["display_name"]),
                        "external_account_ref": str(owned["external_account_ref"]),
                    },
                    competitor_accounts=tuple(
                        {
                            "display_name": str(row["display_name"]),
                            "external_account_ref": str(row["external_account_ref"]),
                        }
                        for row in competitors
                    ),
                    actor="formal-copy-test",
                )
                self.assertEqual(
                    str(configuration["owned_account_id"]), old_owned_id
                )
                self.assertEqual(
                    [str(value) for value in configuration["competitor_account_ids"]],
                    old_competitor_ids,
                )
                result = core.start_configured_cold_start(
                    configuration_id=str(configuration["configuration_id"]),
                    actor="formal-copy-test",
                    task_model_binding={
                        "route_id": "fixture-route",
                        "provider_ref": "fixture-provider",
                        "provider_name": "fixture",
                        "provider_type": "local",
                        "model_name": "fixture-model",
                        "endpoint": "fixture://no-model-call",
                        "source": "fixture",
                    },
                )
                activation = core.get_current_domain_activation(domain_label=domain)
                self.assertIsNotNone(activation)
                self.assertEqual(
                    str(activation["cold_start_id"]), str(result["cold_start_id"])
                )
                self.assertEqual(
                    str(activation["configuration_id"]),
                    str(configuration["configuration_id"]),
                )
                self.assertNotIn(str(result["cold_start_id"]), old_run_ids)
                self.assertTrue(core.domain_business_state(domain_label=domain)["blockers"])
                self.assertEqual(
                    int(connection.execute(
                        "SELECT COUNT(*) FROM stage0_experience_candidate "
                        "WHERE data_identity='production'"
                    ).fetchone()[0]),
                    14,
                )
                self.assertEqual(
                    int(connection.execute(
                        "SELECT COUNT(*) FROM stage0_research_material "
                        "WHERE data_identity='production'"
                    ).fetchone()[0]),
                    89,
                )
            finally:
                core.close()


if __name__ == "__main__":
    unittest.main()
