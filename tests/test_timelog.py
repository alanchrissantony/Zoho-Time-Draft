import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import drafts as dr
import sources as src
import zoho_api as zoho

DAY = datetime.date(2026, 10, 28)  # a Wednesday
TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
os.environ["TZ"] = "Asia/Kolkata"  # day_bounds uses local time; keep tests stable on any machine
if hasattr(__import__("time"), "tzset"):
    __import__("time").tzset()


def at(hhmm: str) -> datetime.datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.datetime(2026, 10, 28, h, m, tzinfo=TZ)


def rec(repo: str, hhmm: str, group: str = "", prompt: str = "p", branch: str = "main") -> dict:
    return {"repo": repo, "dt": at(hhmm), "remote_group": group, "repo_path": f"/src/{repo}",
            "branch": branch, "prompt": prompt}


def meeting(subject: str, start: str, end: str) -> dict:
    return {"subject": subject, "start": at(start), "end": at(end)}


MAPPING = {
    "groups": {
        "codemonk/kawada": {"project": "Kawada", "job": "auto"},
        "codemonk/tools": {"project": "Internal", "job": "Internal Projects"},
    },
    "repos": {"bruno": {"project": "Internal", "job": "Internal Projects"}},
    "ignore": ["dotfiles"],
    "meetings": [{"match": "kawada sync", "project": "Kawada", "job": "auto"}],
    "ignore_meetings": ["Farewell"],
}

CATALOG = [
    {"id": "p1", "name": "Kawada", "jobs": [{"id": "j1", "name": "Development - Oct"},
                                             {"id": "j2", "name": "Daily Standup"}]},
    {"id": "p2", "name": "Internal", "jobs": [{"id": "j3", "name": "Internal Projects"}]},
]


class TimeMathTest(unittest.TestCase):
    def test_blocks(self):
        self.assertEqual(dr.block_minutes([at("10:00")]), 15)
        self.assertEqual(dr.block_minutes([at("10:00"), at("10:30")]), 45)   # gap of exactly 30 stays
        self.assertEqual(dr.block_minutes([at("10:00"), at("10:31")]), 30)   # 31 splits
        self.assertEqual(dr.block_minutes([]), 0)

    def test_rounding(self):
        for raw, expected in [(7, 0), (7.5, 15), (8, 15), (52, 45), (53, 60), (113, 120)]:
            self.assertEqual(dr.round_minutes(raw), expected, raw)

    def test_parse_hhmm(self):
        self.assertEqual(dr.parse_hhmm("2:30"), 150)
        self.assertEqual(dr.parse_hhmm("1.5"), 90)
        for bad in ("2:75", "abc", ""):
            with self.assertRaises(ValueError):
                dr.parse_hhmm(bad)


class MappingTest(unittest.TestCase):
    def test_repo_beats_group_and_longest_prefix(self):
        mapping = {"groups": {"codemonk": {"project": "A"}, "codemonk/kawada": {"project": "B"}},
                   "repos": {"x": {"project": "P"}}}
        self.assertEqual(dr.resolve_mapping("x", "codemonk/kawada", mapping)[1]["project"], "P")
        self.assertEqual(dr.resolve_mapping("api", "codemonk/kawada/y", mapping)[1]["project"], "B")
        self.assertEqual(dr.resolve_mapping("api", "codemonk/kawada-old", MAPPING), ("unmapped", None))
        self.assertEqual(dr.resolve_mapping("dotfiles", "", MAPPING), ("ignored", None))

    def test_meetings(self):
        self.assertEqual(dr.match_meeting("Kawada Sync weekly", MAPPING)[0], "mapped")
        self.assertEqual(dr.match_meeting("Farewell to X", MAPPING), ("ignored", None))
        self.assertEqual(dr.match_meeting("Backend Standup", MAPPING), ("unmapped", None))


class TimelineTest(unittest.TestCase):
    def test_meeting_minutes_win_over_claude_blocks(self):
        # Claude block 10:00–10:45 (two prompts + tail), meeting 10:30–11:00 mapped to the same project
        records = [rec("api", "10:00", "codemonk/kawada/app"), rec("api", "10:30", "codemonk/kawada/app")]
        tl = dr.build_timeline(DAY, records, [meeting("Kawada sync", "10:30", "11:00")], MAPPING)
        [g] = tl["groups"]
        self.assertEqual(g["raw"], 60)  # 10:00–11:00, the overlap counted once
        self.assertEqual(g["sources"], {"Meeting": 30, "Claude Code": 30})

    def test_unmapped_buckets_and_ignored(self):
        records = [rec("random", "09:00"), rec("dotfiles", "09:00"), rec("bruno", "12:00")]
        meetings = [meeting("Backend Standup", "18:00", "18:15"), meeting("Farewell to X", "17:00", "17:30")]
        tl = dr.build_timeline(DAY, records, meetings, MAPPING)
        kinds = sorted((g["kind"], g.get("label", g.get("project"))) for g in tl["groups"])
        self.assertEqual(kinds, [("mapped", "Internal"), ("meeting", "Backend Standup"), ("repo", "random")])
        self.assertEqual(sorted(i["label"] for i in tl["ignored"]), ["Farewell to X", "dotfiles"])


class BuildDraftsTest(unittest.TestCase):
    def timeline(self):
        records = [rec("api", "10:00", "codemonk/kawada/app"), rec("api", "11:00", "codemonk/kawada/app")]
        meetings = [meeting("Kawada standup", "09:00", "09:30"), meeting("Team lunch", "13:00", "14:00")]
        return dr.build_timeline(DAY, records, meetings, MAPPING)

    def test_bucket_merges_into_same_job_and_personal_not_counted(self):
        tl = self.timeline()
        ids = {g.get("label", g.get("project")): gid for gid, g in dr.enumerate_groups(tl)}
        answers = {"entries": {ids["Kawada"]: {"job": "Development - Oct", "description": "Built search"}},
                   "buckets": {ids["Kawada standup"]: {"personal": False, "project": "Kawada",
                                                        "job": "Development - Oct", "description": "Standup"},
                               ids["Team lunch"]: {"personal": True, "project": None, "job": None}}}
        drafts, not_counted = dr.build_drafts(tl, CATALOG, answers, [])
        [d] = drafts
        self.assertEqual((d["job_id"], d["minutes"]), ("j1", 60))  # 15 + 15 Claude + 30 meeting
        self.assertEqual(d["suggested_minutes"], 30)
        self.assertTrue(d["suggested"])
        self.assertEqual(d["description"], "Built search; Standup")
        self.assertEqual([n["label"] for n in not_counted], ["Team lunch"])

    def test_missing_job_still_drafts_and_partial_zoho(self):
        tl = dr.build_timeline(DAY, [rec("api", "10:00", "codemonk/kawada/app"),
                                     rec("api", "11:00", "codemonk/kawada/app")], [], MAPPING)
        drafts, _ = dr.build_drafts(tl, CATALOG, {"entries": {}, "buckets": {}}, [])
        self.assertIsNone(drafts[0]["job_id"])
        self.assertIn("Pick a job", drafts[0]["error"])

        answers = {"entries": {"e1": {"job": "Development - Oct", "description": "x"}}, "buckets": {}}
        drafts, _ = dr.build_drafts(tl, CATALOG, answers, [{"jobId": "j1", "hoursInMins": "15"}])
        self.assertEqual((drafts[0]["status"], drafts[0]["zoho_minutes"]), ("pending", 15))
        drafts, _ = dr.build_drafts(tl, CATALOG, answers, [{"jobId": "j1", "hoursInMins": "30"}])
        self.assertEqual(drafts[0]["status"], "in_zoho")


class StoreTest(unittest.TestCase):
    def test_regeneration_keeps_your_decisions(self):
        old = dr.empty_day(DAY)
        declined = dr.new_draft(job_id="j1", minutes=60, status="declined")
        edited = dr.new_draft(job_id="j2", minutes=30, edited=True)
        manual = dr.new_draft(job_id="j9", minutes=30, manual=True)
        untouched = dr.new_draft(job_id="j3", minutes=15)
        old["drafts"] = [declined, edited, manual, untouched]
        old["counted"] = ["Team lunch"]
        new = {**dr.empty_day(DAY), "drafts": [dr.new_draft(job_id=j, minutes=45) for j in ("j1", "j2", "j3", "j4")],
               "not_counted": [{"id": "n1", "label": "Team lunch"}, {"id": "n2", "label": "Other"}]}
        merged = dr.merge_regenerated(old, new)
        jobs = sorted((d["job_id"], d["minutes"]) for d in merged["drafts"])
        self.assertEqual(jobs, [("j1", 60), ("j2", 30), ("j3", 45), ("j4", 45), ("j9", 30)])
        self.assertEqual([n["label"] for n in merged["not_counted"]], ["Other"])

    def test_update_day_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig = dr.DRAFTS_DIR
            dr.DRAFTS_DIR = Path(tmp)
            try:
                dr.update_day(DAY, lambda data: data["drafts"].append(dr.new_draft(minutes=30)))
                self.assertEqual(dr.load_day(DAY)["drafts"][0]["hours"], "00:30")
            finally:
                dr.DRAFTS_DIR = orig


class MissedDaysTest(unittest.TestCase):
    def test_missed(self):
        logged = {"2026-10-26": 300}
        marks = {"2026-10-22": "leave"}
        missed = dr.missed_days(DAY, logged, marks, since="2026-10-20", window=14)
        # Oct 20 (since), 21, 23, 27 missed; 22 leave; 24/25 weekend; 26 logged; 28 is today
        self.assertEqual(missed, [datetime.date(2026, 10, d) for d in (20, 21, 23, 27)])

    def test_notification_text(self):
        self.assertIsNone(dr.notification_text([], 0))
        self.assertEqual(dr.notification_text([datetime.date(2026, 10, 27)], 2), "Missed: Tue 27 Oct · 2 drafts to review")


class SourcesTest(unittest.TestCase):
    def test_load_activity_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            line = {"ts": "2026-10-28T10:00:00+0530", "repo": "bruno", "prompt": "x"}
            Path(tmp, "2026-10-28.jsonl").write_text(json.dumps(line) + "\nnot json\n{}\n")
            self.assertEqual(len(src.load_activity(DAY, Path(tmp))), 1)
            self.assertEqual(src.load_activity(DAY - datetime.timedelta(days=1), Path(tmp)), [])

    def test_parse_meetings(self):
        raw = [
            {"subject": "Standup", "start": "2026-10-28T11:00:00+05:30", "end": "2026-10-28T11:30:00+05:30",
             "show_as": "tentative", "all_day": False, "cancelled": False},
            {"subject": "Free slot", "start": "2026-10-28T12:00:00+05:30", "end": "2026-10-28T13:00:00+05:30",
             "show_as": "free", "all_day": False, "cancelled": False},
            {"subject": "Offsite", "start": "2026-10-28T00:00:00+05:30", "end": "2026-10-29T00:00:00+05:30",
             "show_as": "busy", "all_day": True, "cancelled": False},
            {"subject": "Cancelled", "start": "2026-10-28T15:00:00+05:30", "end": "2026-10-28T16:00:00+05:30",
             "show_as": "busy", "all_day": False, "cancelled": True},
            {"subject": "Late", "start": "2026-10-28T23:30:00+05:30", "end": "2026-10-29T00:30:00+05:30",
             "show_as": "busy", "all_day": False, "cancelled": False},
        ]
        got = src.parse_meetings(raw, DAY)
        self.assertEqual([m["subject"] for m in got], ["Standup", "Late"])
        self.assertEqual((got[1]["end"] - got[1]["start"]).total_seconds(), 1800)  # clipped at midnight

    def meetings_with(self, *replies):
        """outlook_meetings(DAY) with run_claude returning or raising each reply in turn."""
        from unittest import mock
        with mock.patch.dict(os.environ, {"TIMELOG_MEETINGS": "1"}), \
                mock.patch("sources.other_mcp_servers", return_value=["mcp__claude_ai_Figma"]), \
                mock.patch("sources.run_claude", side_effect=replies) as run:
            result = src.outlook_meetings(DAY)
        leave_out = run.call_args.kwargs["disallowed_tools"]
        self.assertIn("mcp__claude_ai_Figma", leave_out)
        self.assertIn("mcp__claude_ai_Microsoft_365__outlook_email_search", leave_out)
        self.assertNotIn(src.MEETINGS_TOOL, leave_out)
        return result, run.call_count

    def test_parse_mcp_list(self):
        listing = "\n".join([
            "Checking MCP server health…", "",
            "claude.ai Claude Docs: https://api.anthropic.com/v1/pages/mcp - ✔ Connected",
            "claude.ai Notion: https://mcp.notion.com/mcp - ! Needs authentication",
            "claude.ai Microsoft 365: https://microsoft365.mcp.claude.com/mcp - ✔ Connected",
            "claude.ai monday.com: https://mcp.monday.com/mcp - ✔ Connected",
            "plugin:vercel:vercel: https://mcp.vercel.com (HTTP) - ✔ Connected",
            "freehire: npx -y freehire-mcp@0.6.0 - ✔ Connected",
            "broken: node x.js - ✗ Failed to connect",
        ])
        self.assertEqual(src.parse_mcp_list(listing), ["mcp__claude_ai_Claude_Docs", "mcp__claude_ai_monday_com",
                                                       "mcp__plugin_vercel_vercel", "mcp__freehire"])
        self.assertEqual(src.parse_mcp_list(""), [])

    def test_run_claude_disallowed_tools(self):
        from unittest import mock
        done = mock.Mock(returncode=0, stdout=json.dumps({"structured_output": {"ok": 1}}), stderr="")
        with mock.patch("sources.subprocess.run", return_value=done) as run:
            src.run_claude("hi", {}, allowed_tools="t1", disallowed_tools=["mcp__a", "mcp__b"])
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[cmd.index("--disallowedTools"):], ["--disallowedTools", "mcp__a", "mcp__b"])
            src.run_claude("hi", {})
            self.assertNotIn("--disallowedTools", run.call_args.args[0])

    def test_meetings_retry_when_answered_without_searching(self):
        prose = json.JSONDecodeError("Expecting value", "I haven't checked your calendar", 0)
        event = {"subject": "Standup", "start": "2026-10-28T11:00:00+05:30", "end": "2026-10-28T11:30:00+05:30",
                 "show_as": "busy", "all_day": False, "cancelled": False}
        (meetings, note), calls = self.meetings_with(prose, ({"meetings": [event]}, {"num_turns": 3}))
        self.assertEqual(([m["subject"] for m in meetings], note, calls), (["Standup"], None, 2))

        (meetings, note), calls = self.meetings_with(prose, prose)
        self.assertEqual((meetings, calls), ([], 2))
        self.assertIn("wasn't searched", note)

    def test_meetings_real_errors_are_not_retried(self):
        (meetings, note), calls = self.meetings_with(RuntimeError("claude exited 1: not logged in"))
        self.assertEqual((meetings, calls), ([], 1))
        self.assertIn("not read (claude exited 1", note)


class OpenJobsTest(unittest.TestCase):
    CATALOG = [
        {"id": "p1", "name": "Radyus", "status": "In-Progress", "jobs": [
            {"id": "a", "name": "Development - Oct", "status": "In-Progress", "active": True, "from": "2026-10-01", "to": "2026-10-31"},
            {"id": "b", "name": "UAT", "status": "Completed", "active": True, "from": None, "to": None},
            {"id": "c", "name": "Milestone 3", "status": "In-Progress", "active": True, "from": "2025-12-01", "to": "2025-12-12"},
            {"id": "d", "name": "Standup", "status": "In-Progress", "active": True, "from": None, "to": None},
            {"id": "e", "name": "Paused", "status": "In-Progress", "active": False, "from": None, "to": None},
        ]},
        {"id": "p2", "name": "Old", "status": "Completed", "jobs": [
            {"id": "f", "name": "Anything", "status": "In-Progress", "active": True, "from": None, "to": None}]},
    ]

    def test_open_catalog(self):
        oct28 = zoho.open_catalog(self.CATALOG, DAY)
        self.assertEqual([(p["name"], [j["id"] for j in p["jobs"]]) for p in oct28], [("Radyus", ["a", "d"])])
        nov3 = zoho.open_catalog(self.CATALOG, datetime.date(2026, 11, 3))
        self.assertEqual([j["id"] for j in nov3[0]["jobs"]], ["d"])  # the October job has ended
        self.assertTrue(zoho.job_open(self.CATALOG[0]["jobs"][0], datetime.date(2026, 10, 31)))  # bounds inclusive


class InjectedPromptsTest(unittest.TestCase):
    def test_clean_prompt(self):
        self.assertEqual(src.clean_prompt("<ide_opened_file>opened x</ide_opened_file>\nfix the bug"), "fix the bug")
        self.assertIsNone(src.clean_prompt("<ide_selection>cut off by the 200 char lim"))
        self.assertIsNone(src.clean_prompt("<task-notification>\n<task-id>1</task-id>"))
        self.assertIsNone(src.clean_prompt("   "))
        self.assertEqual(src.clean_prompt("make <b> bold"), "make <b> bold")

    def test_hook_records_only_what_you_typed(self):
        import subprocess
        hook = Path(__file__).resolve().parent.parent / "hooks" / "timelog.sh"
        with tempfile.TemporaryDirectory() as tmp:
            for prompt in ("<ide_opened_file>x</ide_opened_file>\nwould this run", "<task-notification>done",
                           "<ide_selection>only context</ide_selection>"):
                proc = subprocess.run(["bash", str(hook)], input=json.dumps({"cwd": tmp, "prompt": prompt}),
                                      capture_output=True, text=True, env={**os.environ, "TIMELOG_DIR": tmp})
                self.assertEqual((proc.returncode, proc.stdout), (0, ""))
            lines = [json.loads(l) for f in Path(tmp).glob("*.jsonl") for l in f.read_text().splitlines()]
            self.assertEqual([l["prompt"] for l in lines], ["would this run"])


class ZohoHelpersTest(unittest.TestCase):
    def test_redact(self):
        text = "Max retries with url: /oauth/v2/token?refresh_token=1000.a.b&client_id=1000.X&client_secret=s3cr3t&grant_type=x"
        out = zoho._redact(text)
        self.assertNotIn("1000.a.b", out)
        self.assertNotIn("s3cr3t", out)
        self.assertIn("grant_type=x", out)

    def test_log_minutes(self):
        # Zoho returns hours-mode logs with fromTime/toTime "00:00", normalised to 0
        self.assertEqual(zoho.log_minutes({"hours": "02:00", "fromTime": 0, "toTime": 0}), 120)
        self.assertEqual(zoho.log_minutes({"fromTime": 540, "toTime": 630}), 90)
        self.assertEqual(zoho.log_minutes({"hoursInMins": "45", "fromTime": 0, "toTime": 0}), 45)

    def test_post_parses_success_and_errors(self):
        from unittest import mock

        def reply(body):
            return mock.Mock(json=mock.Mock(return_value=body))
        with mock.patch("zoho_api.requests.post", return_value=reply(
                {"response": {"status": 0, "result": [{"timeLogId": "77"}]}})) as post:
            res = zoho.add_timelog("j1", DAY, "Did x", "tok", project_id="p1", hours="01:30", label="P | J")
        self.assertEqual((res["success"], res["log_id"]), (True, "77"))
        sent = post.call_args.kwargs["data"]
        self.assertEqual((sent["hours"], sent["jobId"], sent["projectId"], sent["workDate"]), ("01:30", "j1", "p1", "2026-10-28"))
        self.assertNotIn("fromTime", sent)

        with mock.patch("zoho_api.requests.post", return_value=reply(
                {"response": {"status": 1, "errors": {"code": 9046, "message": "Overlaps"}}})):
            res = zoho.add_timelog("j1", DAY, "x", "tok", hours="01:00")
        self.assertEqual((res["success"], res["code"]), (False, 9046))
        self.assertIn("Overlaps", res["error"])


if __name__ == "__main__":
    unittest.main()
