"""端到端业务规则测试：资料解析、换版、聚合、披露和出票追踪。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shortwarn.disclosure import render_advisory  # noqa: E402
from shortwarn.engine import analyse_programme, event_clearance  # noqa: E402
from shortwarn.intake import add_observation  # noqa: E402
from shortwarn.ledger import Ledger  # noqa: E402
from shortwarn.publishing import trace_issuance  # noqa: E402
from shortwarn.workspace import Workspace  # noqa: E402

DATA = ROOT / "data"


class ShortWarningToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = Workspace(DATA)
        cls.ledger = Ledger(cls.ws, None)

    def analysis(self, programme="east", revision=3, at=None):
        return analyse_programme(self.ws, programme, revision, at=at, ledger=self.ledger)

    def test_external_review_is_not_local_flash_evidence(self):
        assessment = next(a for a in self.analysis(revision=1)["assessments"] if a["cut"] == "night-a")
        quoted = [c for c in assessment["claims"] if c.get("stance") == "quoted"]
        self.assertTrue(any("长灯" in str(c.get("about_works")) for c in quoted))
        self.assertFalse(any(c["usable_as_evidence"] and c["stance"] == "quoted" for c in quoted))
        flash_shots = [s["shot"] for s in assessment["segments"] if s["event"] == "flash"]
        self.assertEqual(flash_shots, ["n2"])

    def test_discussed_stage_light_and_diegetic_second_sound_are_not_counted(self):
        west = self.analysis(programme="west", revision=1)
        water = next(a for a in west["assessments"] if a["cut"] == "water-a")
        self.assertEqual([s for s in water["segments"] if s["event"] in {"flash", "sound"}], [])
        self.assertTrue(any(c.get("stance") == "discussed" and c["event"] == "flash" for c in water["claims"]))

        night = next(a for a in self.analysis(revision=3)["assessments"] if a["cut"] == "night-a")
        speech = [c for c in night["claims"] if c.get("topic") == "diegetic_speech"]
        self.assertTrue(speech and speech[0]["usable_as_evidence"] is False)
        sound_segments = [s for s in night["segments"] if s["event"] == "sound"]
        self.assertEqual(len(sound_segments), 1)
        self.assertEqual(sound_segments[0]["start_ms"], 5000)
        self.assertEqual(set(sound_segments[0]["evidence_ids"]), {"o3", "o5"})

    def test_repeated_mentions_merge_without_double_counting(self):
        east1 = self.analysis(revision=1)
        lantern = next(a for a in east1["assessments"] if a["cut"] == "lantern-a")
        by_shot = {s["shot"]: s for s in lantern["segments"] if s["event"] == "flash"}
        self.assertEqual(by_shot["a2"]["duration_ms"], 6000)
        self.assertEqual(by_shot["a3"]["duration_ms"], 4000)
        self.assertEqual(set(by_shot["a2"]["evidence_ids"]), {"o1", "o2"})
        self.assertEqual(set(by_shot["a3"]["evidence_ids"]), {"o1", "o2"})
        self.assertEqual(by_shot["a3"]["confirmed_by"], ["s1"])
        flash_block = east1["events"]["flash"]["blocks"][0]
        self.assertEqual(flash_block["total_ms"], 10000)
        self.assertTrue(flash_block["accumulated_trigger"])

    def test_cut_change_maps_hall_two_and_drops_removed_hall_one(self):
        east2 = self.analysis(revision=2)
        lantern_b = next(a for a in east2["assessments"] if a["cut"] == "lantern-b")
        flash = [s for s in lantern_b["segments"] if s["event"] == "flash"]
        self.assertEqual(len(flash), 1)
        self.assertEqual((flash[0]["shot"], flash[0]["start_ms"], flash[0]["end_ms"]), ("b2", 12500, 16500))
        self.assertEqual(flash[0]["confirmed_by"], ["s1"])
        statuses = {(item["observation_id"], item["mapping_status"]) for item in lantern_b["lineage"]}
        self.assertIn(("o1", "dropped"), statuses)
        self.assertIn(("o2", "mapped"), statuses)

    def test_qualifying_break_resets_but_canceled_break_accumulates(self):
        east2 = self.analysis(revision=2)
        self.assertEqual(len(east2["events"]["flash"]["blocks"]), 2)
        self.assertEqual([b["total_ms"] for b in east2["events"]["flash"]["blocks"]], [4000, 5400])
        self.assertTrue(all(b["short_trigger"] for b in east2["events"]["flash"]["blocks"]))
        self.assertFalse(any(b["accumulated_trigger"] for b in east2["events"]["flash"]["blocks"]))
        self.assertTrue(east2["boundaries"][0]["staff_rest"])

        east3 = self.analysis(revision=3)
        self.assertEqual(len(east3["events"]["flash"]["blocks"]), 1)
        block = east3["events"]["flash"]["blocks"][0]
        self.assertEqual(block["cuts"], ["lantern-b", "night-a"])
        self.assertEqual(block["total_ms"], 9400)
        self.assertTrue(block["accumulated_trigger"])
        self.assertEqual(east3["boundaries"][0]["staff_rest"], False)

    def test_west_short_dark_gap_does_not_reset_but_water_adds_no_stimulus(self):
        west = self.analysis(programme="west", revision=1)
        self.assertEqual(len(west["events"]["flash"]["blocks"]), 1)
        self.assertEqual(west["events"]["flash"]["blocks"][0]["total_ms"], 10000)
        self.assertEqual(west["events"]["sound"]["positive_cuts"], [])
        self.assertFalse(west["boundaries"][0]["staff_rest"])

    def test_as_of_revision_time_keeps_single_uncertain_cut_reservation(self):
        east3_at_planning = self.analysis(revision=3, at="2026-11-22T15:30:00+08:00")
        uncertain = {r["cut"] for r in east3_at_planning["reservations"] if r["kind"] == "viewer_recheck"}
        self.assertEqual(uncertain, {"lantern-b"})
        clearance = event_clearance(east3_at_planning, "flash")
        self.assertEqual(clearance["overall"], "positive_with_reservations")
        self.assertFalse(clearance["all_clear_allowed"])
        self.assertEqual({cut["cut"] for cut in clearance["cuts"] if cut["status"] == "positive_with_reservation"}, {"lantern-b"})

        current = self.analysis(revision=3)
        self.assertEqual(current["reservations"], [])
        b = next(a for a in current["assessments"] if a["cut"] == "lantern-b")
        self.assertTrue(any("亮度不变" in c["quote"] for c in b["absences"]))

    def test_disclosure_levels_redact_spoiler_and_require_active_selection(self):
        east3 = self.analysis(revision=3)
        public = render_advisory(east3, "public")
        self.assertEqual({x["label"] for x in public["ticket_labels"]}, {"光线变化", "声响变化", "情绪主题"})
        self.assertNotIn("从未收到信", json.dumps(public, ensure_ascii=False))

        menu = render_advisory(east3, "audience")
        self.assertEqual(menu["level"], "audience_menu")
        audience = render_advisory(east3, "audience", selected=["story"])
        self.assertEqual(audience["details"]["story"]["label"], "亲属失联与等待")
        self.assertEqual(audience["details"]["story"]["ending_disclosure"], "redacted")
        self.assertNotIn("另一封信", json.dumps(audience, ensure_ascii=False))

        with self.assertRaises(PermissionError):
            render_advisory(east3, "editor", role="audience")
        editor = render_advisory(east3, "editor", role="editor")
        self.assertIn("另一封信", json.dumps(editor["analysis"]["assessments"], ensure_ascii=False))

    def test_trace_ticket_basis_and_affected_page(self):
        trace = trace_issuance(self.ws, self.ledger, "t8")
        self.assertTrue(trace["stale"])
        self.assertEqual(trace["basis"]["subject"], "east:1")
        self.assertEqual(trace["basis"]["advisory_id"], "s2")
        self.assertEqual(trace["current_subject"], "east:3")
        self.assertTrue(any(c["type"] == "cut_changed" for c in trace["changes_since_issue"]))
        self.assertTrue(any(c["type"] == "break_rule_changed" for c in trace["changes_since_issue"]))
        page = trace["affected_pages"][0]
        self.assertEqual(page["issuance_id"], "t8")
        self.assertEqual(page["advisory_version"], "s2")
        self.assertTrue(page["needs_replacement"])
        self.assertEqual(trace["updated_audience_query"]["level"], "audience")
        self.assertEqual({r["cut"] for r in trace["at_planned_reservations"]}, {"lantern-b"})
        self.assertIsNone(trace["updated_audience_query"]["reservation"])

    def test_intake_validates_cut_and_appends_manifest_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            shutil.copytree(DATA, data_dir)
            record = add_observation(
                data_dir,
                "water-a",
                "测试看片人",
                "第8秒突然高音量一声。",
                received="2026-11-25T10:00:00+08:00",
            )
            self.assertEqual(set(record), set(json.loads((data_dir / "index.json").read_text())["observations"]))
            reloaded = Workspace(data_dir)
            self.assertTrue(any(row["id"] == record["id"] for row in reloaded.observations))
            analysis = analyse_programme(reloaded, "west", 1, ledger=Ledger(reloaded, None))
            water = next(a for a in analysis["assessments"] if a["cut"] == "water-a")
            self.assertTrue(any(s["event"] == "sound" for s in water["segments"]))
            with self.assertRaises(KeyError):
                add_observation(data_dir, "missing-cut", "x", "y")

    def test_append_only_role_separated_ledger_in_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(self.ws, Path(tmp) / "ledger.jsonl")
            sig = ledger.sign_advisory("east:3", "售票", "含光线变化、突发声响及情绪主题。")
            ticket = ledger.issue_ticket("east:3", sig["id"], "学生票")
            self.assertTrue(Path(ledger.extra_path).exists())
            self.assertIn(sig["id"], ticket["copy"])
            reloaded = Ledger(self.ws, ledger.extra_path)
            self.assertEqual(reloaded.latest("east:3", "售票")["copy"], sig["copy"])


if __name__ == "__main__":
    unittest.main()
