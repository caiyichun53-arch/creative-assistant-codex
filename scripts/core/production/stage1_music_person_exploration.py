from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.core.external_adapters.music_audience_browser import LocalMusicAudienceBrowserExecutor
from scripts.core.external_adapters.runtime_config import external_runtime_value
from scripts.core.production.stage0_content_core import Stage0ContentProductionCore, StateTransitionError


ROOT = Path(__file__).resolve().parents[3]


class MusicAudienceMaterialCollector:
    """Connect a confirmed person scan to separate NetEase/Douban sessions and one unified echo library."""

    def __init__(self, *, core: Stage0ContentProductionCore, browser: LocalMusicAudienceBrowserExecutor):
        self.core = core
        self.browser = browser

    def collect_initial_scan(
        self, *, exploration_id: str, person_name: str, works: tuple[dict[str, Any], ...], actor: str
    ) -> dict[str, Any]:
        packet = self.core.get_manual_exploration_packet(exploration_id=exploration_id)
        if packet["exploration_kind"] != "person_exploration":
            raise StateTransitionError("music audience collection is only available for person exploration")
        if not 10 <= len(works) <= 12 or any(not str(item.get("title") or "").strip() for item in works):
            raise StateTransitionError("initial person scan requires 10 to 12 confirmed representative works")
        if packet["status"] == "awaiting_material_collection":
            self.core.begin_manual_exploration_collection(
                exploration_id=exploration_id, actor=actor, reason="confirmed representative works are ready for audience collection",
            )
        elif packet["status"] != "collecting":
            raise StateTransitionError("person exploration is not available for initial audience collection")
        result = self.browser.collect_initial_person_scan(person_name=person_name, works=works)
        if int(result.get("viewed_count") or 0) > 600:
            raise StateTransitionError("initial music audience collection exceeded the confirmed total viewed limit")
        collected_at = datetime.now(timezone.utc).isoformat()
        retained = self.core.record_music_audience_echo(
            exploration_id=exploration_id, materials=tuple(result["materials"]), collected_at=collected_at,
        ) if result["materials"] else {"retained_count": 0}
        gaps = [
            {"platform": platform, **status}
            for platform, status in result["platforms"].items()
            if status.get("status") != "completed"
        ]
        if int(retained["retained_count"]) == 0:
            return {
                "exploration_id": exploration_id, "status": "collecting", "retained_count": 0,
                "platform_gaps": gaps, "manual_login_required": bool(gaps), "automatic_substitution": False,
            }
        completion = self.core.complete_manual_exploration_collection(
            exploration_id=exploration_id, actor=actor,
            reason="audience materials retained; platform gaps remain explicit and user direction is required",
        )
        return {
            **completion, "retained_count": retained["retained_count"], "viewed_count": result["viewed_count"],
            "platform_gaps": gaps, "manual_login_required": bool(gaps), "automatic_substitution": False,
        }


def build_music_audience_material_collector(core: Stage0ContentProductionCore) -> MusicAudienceMaterialCollector:
    python_value = external_runtime_value("MUSIC_AUDIENCE_PYTHON")
    netease_value = external_runtime_value("NETEASE_MUSIC_PROFILE_DIR")
    douban_value = external_runtime_value("DOUBAN_MUSIC_PROFILE_DIR")
    python_executable, netease_profile, douban_profile = Path(python_value), Path(netease_value), Path(douban_value)
    return MusicAudienceMaterialCollector(
        core=core,
        browser=LocalMusicAudienceBrowserExecutor(
            python_executable=python_executable,
            worker_path=ROOT / "scripts" / "core" / "external_adapters" / "music_audience_worker.py",
            netease_profile_dir=netease_profile,
            douban_profile_dir=douban_profile,
        ),
    )
