import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from scripts.core.business_data.domain_labels import (
    DOMAIN_CONFIG_DIR,
    set_domain_pack_config_dir,
)
from scripts.core.production.stage0_content_core import (
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.production.stage1c_content_pipeline import (
    content_node_requires_human_confirmation,
)
from scripts.web.read_only_server import create_action_server
from tests.test_core_unified_status import _seed_domain


def _post_json(url: str, payload: dict) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


class WorkflowModeWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.database = root / "workflow-mode.sqlite3"
        self.config_dir = root / "domain-packs"
        self.config_dir.mkdir()
        self.domain = "p1a-workflow-domain"
        (self.config_dir / f"{self.domain}.yaml").write_text(
            "\n".join(
                (
                    f"name: {self.domain}",
                    f"formal_domain_label: {self.domain}",
                    "activation_status: approved",
                    "workflow_mode: manual_guard",
                )
            ),
            encoding="utf-8",
        )
        set_domain_pack_config_dir(self.config_dir)
        core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        try:
            _seed_domain(core, self.domain, cold_status="completed")
            core.propose_human_decision_carrier(
                carrier_binding_id="p1a-web-carrier",
                carrier_kind="web_test",
                entry_ref="p1a-workflow-mode-web",
                context_strategy="same_test_session",
                actor="p1a-test",
            )
            core.validate_human_decision_carrier(
                carrier_binding_id="p1a-web-carrier",
                validation_evidence={
                    "inbound_round_trip": True,
                    "outbound_round_trip": True,
                    "same_context_verified": True,
                    "decision_identity_verified": True,
                    "evidence_ref": "p1a-workflow-mode-round-trip",
                },
                actor="p1a-test",
                actor_kind="user",
            )
            core.conn.commit()
        finally:
            core.close()
        self.server = create_action_server(
            port=0,
            data_identity="test",
            database_path=self.database,
            actor="p1a-web-user",
            carrier_binding_id="p1a-web-carrier",
            config_dir=self.config_dir,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        set_domain_pack_config_dir(DOMAIN_CONFIG_DIR)
        self.tempdir.cleanup()

    def _status_domain(self) -> dict:
        with urlopen(self.url + "/api/status") as response:
            payload = json.loads(response.read().decode("utf-8"))
        return next(
            item
            for item in payload["status"]["domains"]
            if item["domain_identity"] == self.domain
        )

    def _switch(self, mode: str, reason: str) -> dict:
        return _post_json(
            self.url + "/api/action",
            {
                "action": "change_workflow_mode",
                "domain_label": self.domain,
                "workflow_mode": mode,
                "reason": reason,
            },
        )

    def test_web_status_and_two_way_switch_use_core(self) -> None:
        self.assertEqual(self._status_domain()["workflow_mode"], "manual_guard")

        to_mature = self._switch("mature_automatic", "TEST verifies mature mode")
        self.assertTrue(to_mature["ok"], to_mature)
        self.assertEqual(to_mature["status"]["domains"][0]["workflow_mode"], "mature_automatic")

        self.assertFalse(
            content_node_requires_human_confirmation(
                node="deep_research", domain_label=self.domain
            )
        )
        self.assertFalse(
            content_node_requires_human_confirmation(
                node="formal_draft", domain_label=self.domain
            )
        )
        for node in ("content_plan", "review"):
            self.assertTrue(
                content_node_requires_human_confirmation(
                    node=node, domain_label=self.domain
                )
            )

        to_manual = self._switch("manual_guard", "TEST restores manual guard")
        self.assertTrue(to_manual["ok"], to_manual)
        self.assertEqual(to_manual["status"]["domains"][0]["workflow_mode"], "manual_guard")
        self.assertTrue(
            content_node_requires_human_confirmation(
                node="deep_research", domain_label=self.domain
            )
        )
        self.assertTrue(
            content_node_requires_human_confirmation(
                node="formal_draft", domain_label=self.domain
            )
        )

    def test_empty_reason_is_rejected_and_mode_is_unchanged(self) -> None:
        rejected = _post_json(
            self.url + "/api/action",
            {
                "action": "change_workflow_mode",
                "domain_label": self.domain,
                "workflow_mode": "mature_automatic",
                "reason": "",
            },
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual(self._status_domain()["workflow_mode"], "manual_guard")

        core = Stage0ContentProductionCore.open(self.database, data_identity="test")
        try:
            from scripts.core.formal_business_entrypoints import (
                CreationAssistantFormalBusinessCore,
            )

            with self.assertRaises(StateTransitionError):
                CreationAssistantFormalBusinessCore(core=core).change_domain_workflow_mode(
                    domain_label=self.domain,
                    workflow_mode="mature_automatic",
                    actor="p1a-web-user",
                    reason="",
                    config_dir=self.config_dir,
                )
        finally:
            core.close()


if __name__ == "__main__":
    unittest.main()
