import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zoho_timelog import cli
from zoho_timelog import drafts as dr
from zoho_timelog import hook, oscompat, scheduler
from zoho_timelog import sources as src
from zoho_timelog import zoho_api as zoho

DAY = datetime.date(2026, 10, 28)  # a Wednesday
TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
os.environ["TZ"] = "Asia/Kolkata"  # day_bounds uses local time; keep tests stable on any machine
if hasattr(__import__("time"), "tzset"):
    __import__("time").tzset()
# Fake Zoho credentials, so no test ever uses (or needs) a real .env or Connect Zoho file
os.environ.update(ZOHO_CLIENT_ID="env-id", ZOHO_CLIENT_SECRET="env-secret", ZOHO_REFRESH_TOKEN="env-refresh",
                  ZOHO_USER="env@example.com")


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
        midnight, half_hour = src.day_bounds(DAY)[1], datetime.timedelta(minutes=30)
        raw = [
            {"subject": "Standup", "start": "2026-10-28T11:00:00+05:30", "end": "2026-10-28T11:30:00+05:30",
             "show_as": "tentative", "all_day": False, "cancelled": False},
            {"subject": "Free slot", "start": "2026-10-28T12:00:00+05:30", "end": "2026-10-28T13:00:00+05:30",
             "show_as": "free", "all_day": False, "cancelled": False},
            {"subject": "Offsite", "start": "2026-10-28T00:00:00+05:30", "end": "2026-10-29T00:00:00+05:30",
             "show_as": "busy", "all_day": True, "cancelled": False},
            {"subject": "Cancelled", "start": "2026-10-28T15:00:00+05:30", "end": "2026-10-28T16:00:00+05:30",
             "show_as": "busy", "all_day": False, "cancelled": True},
            # 23:30–00:30 around this machine's midnight (Windows has no tzset, so the TZ pin doesn't apply there)
            {"subject": "Late", "start": (midnight - half_hour).isoformat(), "end": (midnight + half_hour).isoformat(),
             "show_as": "busy", "all_day": False, "cancelled": False},
        ]
        got = src.parse_meetings(raw, DAY)
        self.assertEqual([m["subject"] for m in got], ["Standup", "Late"])
        self.assertEqual((got[1]["end"] - got[1]["start"]).total_seconds(), 1800)  # clipped at midnight

    def meetings_with(self, *replies):
        """outlook_meetings(DAY) with run_claude returning or raising each reply in turn."""
        from unittest import mock
        with mock.patch.dict(os.environ, {"TIMELOG_MEETINGS": "1"}), \
                mock.patch("zoho_timelog.sources.other_mcp_servers", return_value=["mcp__claude_ai_Figma"]), \
                mock.patch("zoho_timelog.sources.run_claude", side_effect=replies) as run:
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
        with mock.patch("zoho_timelog.sources.claude_cmd", return_value=["claude"]), \
                mock.patch("zoho_timelog.oscompat.subprocess.run", return_value=done) as run:
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
        with tempfile.TemporaryDirectory() as tmp:
            for prompt in ("<ide_opened_file>x</ide_opened_file>\nwould this run", "<task-notification>done",
                           "<ide_selection>only context</ide_selection>"):
                proc = run_hook({"cwd": tmp, "prompt": prompt}, tmp)
                self.assertEqual((proc.returncode, proc.stdout), (0, b""))
            self.assertEqual([l["prompt"] for l in jsonl_lines(tmp)], ["would this run"])


ROOT = Path(__file__).resolve().parent.parent


def run_hook(event, timelog_dir: str, **env):
    """`python -m zoho_timelog hook` as Claude Code runs it: the event as JSON (or raw bytes) on stdin."""
    import subprocess
    data = event if isinstance(event, bytes) else json.dumps(event).encode()
    return subprocess.run([sys.executable, "-m", "zoho_timelog", "hook"], input=data, capture_output=True,
                          cwd=ROOT, env={**os.environ, "TIMELOG_DIR": timelog_dir, **env})


def jsonl_lines(timelog_dir: str) -> list[dict]:
    return [json.loads(l) for f in Path(timelog_dir).glob("*.jsonl")
            for l in f.read_text(encoding="utf-8").splitlines()]


class HookTest(unittest.TestCase):
    def git_repo(self, parent: str, remote: str) -> Path:
        import subprocess
        repo = Path(parent, "kawada-api")
        sub = repo / "src"
        sub.mkdir(parents=True)
        for args in (["init", "-q", "-b", "feat/x"], ["remote", "add", "origin", remote]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        return sub

    def test_fields_in_a_git_repo(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as logs:
            cwd = self.git_repo(tmp, "ssh://git@gitlab.codemonk.io:2224/codemonk/kawada/kawada-api.git")
            proc = run_hook({"cwd": str(cwd), "prompt": "fix the café → menü bug " + "x" * 300,
                             "session_id": "s1"}, logs)
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"", b""))
            [line] = jsonl_lines(logs)
        self.assertEqual(Path(line["repo_path"]).resolve(), cwd.parent.resolve())
        self.assertEqual((line["repo"], line["remote_group"], line["branch"], line["cwd"], line["session_id"]),
                         ("kawada-api", "codemonk/kawada", "feat/x", str(cwd), "s1"))
        self.assertTrue(line["prompt"].startswith("fix the café → menü bug"))
        self.assertEqual(len(line["prompt"]), 200)
        self.assertIsNotNone(datetime.datetime.fromisoformat(line["ts"]).tzinfo)

    def test_outside_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as logs:
            run_hook({"cwd": tmp, "prompt": "hello"}, logs)
            [line] = jsonl_lines(logs)
        self.assertEqual((line["repo"], line["repo_path"], line["remote_group"], line["branch"]),
                         (Path(tmp).name, tmp, "", ""))

    def test_writes_nothing_and_never_fails(self):
        with tempfile.TemporaryDirectory() as logs:
            for event, env in ((b"not json{", {}), (b"", {}), (b"[1, 2]", {}),
                               ({"cwd": logs, "prompt": "mine"}, {"TIMELOG_DISABLE": "1"}),
                               ({"cwd": logs, "prompt": "<system-reminder>x"}, {})):
                proc = run_hook(event, logs, **env)
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"", b""), event)
            self.assertEqual(jsonl_lines(logs), [])

    def test_remote_group(self):
        cases = {
            "ssh://git@gitlab.codemonk.io:2224/codemonk/kawada/x.git": "codemonk/kawada",
            "git@github.com:org/sub/repo.git": "org/sub",
            "https://gitlab.com/a/b/c.git": "a/b",
            "https://github.com/me/repo/": "me",
            "git@github.com:repo.git": "",
            "": "",
        }
        for url, group in cases.items():
            self.assertEqual(hook.remote_group(url), group, url)

    def test_same_dir_as_constants_and_imports_stay_light(self):
        import subprocess
        from zoho_timelog import constants
        self.assertEqual(hook.TIMELOG_DIR, constants.TIMELOG_DIR)
        heavy = ("dotenv", "requests", "starlette", "uvicorn", "zoho_timelog.constants", "zoho_timelog.drafts")
        check = ("import sys, zoho_timelog.hook; "
                 f"print([m for m in {heavy!r} if m in sys.modules])")
        out = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True, cwd=ROOT).stdout
        self.assertEqual(out.strip(), "[]")


class HookSettingsTest(unittest.TestCase):
    OTHER = {"type": "command", "command": "/usr/local/bin/other-tool notify"}

    def test_replaces_old_entry_and_keeps_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "settings.json")
            path.write_text(json.dumps({"model": "opus", "hooks": {
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/Users/a/.claude/hooks/timelog.sh"},
                                                self.OTHER]}],
                "Stop": [{"hooks": [self.OTHER]}]}}))
            new = '"C:/Users/a/.local/bin/timelog.exe" hook'
            self.assertTrue(cli.update_hook_settings(path, new))
            got = json.loads(path.read_text())
            self.assertEqual(got["hooks"]["UserPromptSubmit"],
                             [{"hooks": [self.OTHER]}, {"hooks": [{"type": "command", "command": new}]}])
            self.assertEqual((got["model"], got["hooks"]["Stop"]), ("opus", [{"hooks": [self.OTHER]}]))
            self.assertIn("timelog.sh", Path(tmp, "settings.json.bak").read_text())

            self.assertFalse(cli.update_hook_settings(path, new))          # second run: nothing to do
            moved = '"/home/a/.local/bin/timelog" hook'                     # reinstalled elsewhere
            self.assertTrue(cli.update_hook_settings(path, moved))
            commands = [h["command"] for g in json.loads(path.read_text())["hooks"]["UserPromptSubmit"]
                        for h in g["hooks"]]
            self.assertEqual(commands, [self.OTHER["command"], moved])

            self.assertTrue(cli.update_hook_settings(path, None))          # uninstall
            self.assertEqual(json.loads(path.read_text())["hooks"]["UserPromptSubmit"], [{"hooks": [self.OTHER]}])

    def test_missing_settings_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, ".claude", "settings.json")
            self.assertTrue(cli.update_hook_settings(path, '"/x/timelog" hook'))
            self.assertFalse(Path(tmp, ".claude", "settings.json.bak").exists())
            self.assertTrue(cli.update_hook_settings(path, None))
            self.assertEqual(json.loads(path.read_text()), {})

    def test_hook_command_quotes_with_forward_slashes(self):
        from unittest import mock
        with mock.patch.object(cli, "IS_WINDOWS", True), \
                mock.patch.object(cli, "self_command", return_value=[r"C:\Users\a b\.local\bin\timelog.exe"]):
            self.assertEqual(cli.hook_command(), '"C:/Users/a b/.local/bin/timelog.exe" hook')
        with mock.patch.object(cli, "self_command", return_value=["/usr/bin/python3", "/src/repo/timelog.py"]):
            self.assertEqual(cli.hook_command(), '"/usr/bin/python3" "/src/repo/timelog.py" hook')

    @unittest.skipIf(sys.platform == "win32", "the bash hook only ever existed on macOS/Linux")
    def test_old_hook_forwards(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp, "timelog.sh")
            self.assertFalse(cli.forward_old_hook(old, '"/x/timelog" hook'))   # never installed: left alone
            old.symlink_to(Path(tmp, "deleted-repo-script.sh"))
            self.assertTrue(cli.forward_old_hook(old, '"/x/timelog" hook'))
            self.assertFalse(old.is_symlink())
            self.assertIn('exec "/x/timelog" hook', old.read_text())
            self.assertTrue(os.access(old, os.X_OK))
            self.assertFalse(cli.forward_old_hook(old, '"/x/timelog" hook'))


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
        with mock.patch("zoho_timelog.zoho_api.requests.post", return_value=reply(
                {"response": {"status": 0, "result": [{"timeLogId": "77"}]}})) as post:
            res = zoho.add_timelog("j1", DAY, "Did x", "tok", project_id="p1", hours="01:30", label="P | J")
        self.assertEqual((res["success"], res["log_id"]), (True, "77"))
        sent = post.call_args.kwargs["data"]
        self.assertEqual((sent["hours"], sent["jobId"], sent["projectId"], sent["workDate"]), ("01:30", "j1", "p1", "2026-10-28"))
        self.assertNotIn("fromTime", sent)

        with mock.patch("zoho_timelog.zoho_api.requests.post", return_value=reply(
                {"response": {"status": 1, "errors": {"code": 9046, "message": "Overlaps"}}})):
            res = zoho.add_timelog("j1", DAY, "x", "tok", hours="01:00")
        self.assertEqual((res["success"], res["code"]), (False, 9046))
        self.assertIn("Overlaps", res["error"])


class OsCompatTest(unittest.TestCase):
    """What differs between macOS, Windows and Linux; these run on any of them."""

    def test_run_decodes_utf8(self):
        text = "héllo → ✓ 日本"
        out = oscompat.run([sys.executable, "-c", f"import sys; sys.stdout.buffer.write({text!r}.encode())"],
                           capture_output=True, text=True).stdout
        self.assertEqual(out, text)

    def test_file_lock_excludes_other_processes(self):
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as tmp:
            lock, marker = Path(tmp, ".x.lock"), Path(tmp, "got-it")
            child = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
                     "from pathlib import Path\nfrom zoho_timelog.oscompat import file_lock\n"
                     f"with file_lock(Path({str(lock)!r})):\n    Path({str(marker)!r}).write_text('1')\n")
            with oscompat.file_lock(lock):
                proc = subprocess.Popen([sys.executable, "-c", child])
                time.sleep(1)
                self.assertFalse(marker.exists(), "second process got the lock while it was held")
            proc.wait(timeout=20)
            self.assertTrue(marker.exists())

    def test_load_activity_non_ascii(self):
        with tempfile.TemporaryDirectory() as tmp:
            line = {"ts": "2026-10-28T10:00:00+0530", "repo": "r", "prompt": "fix the café → menü bug"}
            Path(tmp, "2026-10-28.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
            self.assertEqual(src.load_activity(DAY, Path(tmp))[0]["prompt"], "fix the café → menü bug")

    def test_toast_xml_escapes(self):
        import xml.etree.ElementTree as ET
        url = "http://127.0.0.1:8765/#2026-10-28?a=1&b=2"
        root = ET.fromstring(oscompat.toast_xml("Zoho <log>", 'Missed: "Mon" & Tue', url))
        self.assertEqual(root.get("launch"), url)
        self.assertEqual([t.text for t in root.iter("text")], ["Zoho <log>", 'Missed: "Mon" & Tue'])

    def test_notify_per_os(self):
        from unittest import mock
        with mock.patch.multiple(oscompat, IS_MAC=False, IS_WINDOWS=True), \
                mock.patch("zoho_timelog.oscompat.run") as run:
            oscompat.notify("T", "msg", "http://x/")
        self.assertEqual(run.call_args.args[0][0], "powershell.exe")
        self.assertIn('launch="http://x/"', run.call_args.kwargs["env"]["TIMELOG_TOAST"])

        with mock.patch.multiple(oscompat, IS_MAC=False, IS_WINDOWS=False), \
                mock.patch("zoho_timelog.oscompat.shutil.which", return_value="/usr/bin/notify-send"), \
                mock.patch("zoho_timelog.oscompat.run") as run:
            oscompat.notify("T", "msg", "http://x/")
        self.assertEqual(run.call_args.args[0][0], "notify-send")

        with mock.patch.multiple(oscompat, IS_MAC=False, IS_WINDOWS=True), \
                mock.patch("zoho_timelog.oscompat.run", side_effect=OSError("no powershell")):
            oscompat.notify("T", "msg", "http://x/")   # never raises

    def test_claude_cmd(self):
        from unittest import mock
        with mock.patch("zoho_timelog.sources.shutil.which", return_value=None):
            self.assertRaises(FileNotFoundError, src.claude_cmd)
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp, "claude.cmd")
            script = Path(tmp, "node_modules", "@anthropic-ai", "claude-code", "cli.js")
            script.parent.mkdir(parents=True)
            script.touch()
            which = {"claude": str(shim), "node": "C:/node/node.exe"}.get
            with mock.patch("zoho_timelog.sources.shutil.which", side_effect=which):
                self.assertEqual(src.claude_cmd(), ["C:/node/node.exe", str(script)])
        with mock.patch("zoho_timelog.sources.shutil.which", return_value="/usr/local/bin/claude"):
            self.assertEqual(src.claude_cmd(), ["/usr/local/bin/claude"])

    def test_log_option_sends_output_to_file(self):
        saved = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "sub", "scheduler.log")
            try:
                cli.setup_output(str(log))
                print("drafted → 2 · ok")
                print("oops", file=sys.stderr)
            finally:
                sys.stdout.close()
                sys.stdout, sys.stderr = saved
            self.assertEqual(log.read_text(encoding="utf-8"), "drafted → 2 · ok\noops\n")


class SchedulerTest(unittest.TestCase):
    """Every OS's job files and install calls, checked on whatever OS runs the tests."""
    CMD = ["/home/a b/.local/bin/timelog"]

    def test_launchd(self):
        import plistlib
        draft = plistlib.loads(scheduler.render_launchd("draft", self.CMD, "/x/bin:/usr/bin"))
        self.assertEqual(draft["Label"], "com.codemonk.timelog")
        self.assertEqual(draft["ProgramArguments"], [*self.CMD, "--log", str(scheduler.LOGS["draft"]), "draft"])
        self.assertEqual(draft["StartCalendarInterval"], [{"Hour": 10, "Minute": 0}, {"Hour": 15, "Minute": 0}])
        self.assertEqual((draft["RunAtLoad"], draft["EnvironmentVariables"]["PATH"]), (True, "/x/bin:/usr/bin"))
        self.assertNotIn("KeepAlive", draft)
        app = plistlib.loads(scheduler.render_launchd("app", self.CMD, "/x"))
        self.assertEqual((app["Label"], app["KeepAlive"], app["ProgramArguments"][-1]),
                         ("com.codemonk.timelog.app", True, "app"))
        self.assertNotIn("StartCalendarInterval", app)

    def test_task_xml(self):
        import subprocess
        import xml.etree.ElementTree as ET
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        cmd = [r"C:\Users\a b\.local\bin\timelogw.exe"]
        text = scheduler.render_task_xml("draft", cmd, "CORP\\a&b")
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-16"?>'))
        root = ET.fromstring(text.split("\n", 1)[1])
        self.assertEqual(len(root.findall("t:Triggers/t:LogonTrigger", ns)), 1)
        starts = [e.text for e in root.findall("t:Triggers/t:CalendarTrigger/t:StartBoundary", ns)]
        self.assertEqual([s[-8:] for s in starts], ["10:00:00", "15:00:00"])
        self.assertEqual(root.find("t:Principals/t:Principal/t:UserId", ns).text, "CORP\\a&b")
        self.assertEqual(root.find("t:Principals/t:Principal/t:LogonType", ns).text, "InteractiveToken")
        self.assertEqual(root.find("t:Settings/t:StartWhenAvailable", ns).text, "true")
        self.assertEqual(root.find("t:Actions/t:Exec/t:Command", ns).text, cmd[0])
        log = str(scheduler.LOGS["draft"])
        self.assertEqual(root.find("t:Actions/t:Exec/t:Arguments", ns).text,
                         subprocess.list2cmdline(["--log", log, "draft"]))

        app = ET.fromstring(scheduler.render_task_xml("app", cmd, "u").split("\n", 1)[1])
        self.assertEqual(app.findall("t:Triggers/t:CalendarTrigger", ns), [])
        self.assertEqual(app.find("t:Settings/t:ExecutionTimeLimit", ns).text, "PT0S")
        self.assertEqual(app.find("t:Settings/t:RestartOnFailure/t:Count", ns).text, "999")

    def test_systemd(self):
        units = scheduler.render_systemd(self.CMD, "/x/bin:/usr/bin")
        self.assertEqual(set(units), {"timelog-app.service", "timelog-draft.service", "timelog-draft.timer"})
        timer = units["timelog-draft.timer"]
        for line in ("OnCalendar=*-*-* 10,15:00:00", "Persistent=true", "OnStartupSec=1min", "WantedBy=timers.target"):
            self.assertIn(line, timer)
        app = units["timelog-app.service"]
        log = scheduler._sd_quote(str(scheduler.LOGS["app"]))   # a Windows path's backslashes get doubled
        self.assertIn(f'ExecStart="/home/a b/.local/bin/timelog" "--log" {log} "app"', app)
        self.assertIn('Environment="PATH=/x/bin:/usr/bin"', app)
        self.assertIn("Restart=always", app)
        self.assertIn("Type=oneshot", units["timelog-draft.service"])
        self.assertIn('"50%%"', scheduler.render_systemd(["50%"], "")["timelog-draft.service"])

    def fake_os(self, mac=False, windows=False, returncode=0):
        """Pretend to be another OS: a temp home, and every command recorded instead of run."""
        from unittest import mock
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=returncode, stdout="", stderr="")
        self.enterContext(mock.patch.multiple(oscompat, IS_MAC=mac, IS_WINDOWS=windows))
        self.enterContext(mock.patch.object(Path, "home", return_value=home))
        self.enterContext(mock.patch.object(scheduler, "run", side_effect=fake_run))
        self.enterContext(mock.patch.object(scheduler, "TIMELOG_DIR", home / ".timelog"))
        if mac:
            self.enterContext(mock.patch.object(scheduler.os, "getuid", return_value=501, create=True))
        return home, calls

    def test_install_linux(self):
        home, calls = self.fake_os()
        scheduler.install(self.CMD)
        units = home / ".config" / "systemd" / "user"
        self.assertEqual(sorted(p.name for p in units.iterdir()),
                         ["timelog-app.service", "timelog-draft.service", "timelog-draft.timer"])
        self.assertIn(["systemctl", "--user", "enable", "--now", "timelog-draft.timer", "timelog-app.service"], calls)
        scheduler.uninstall()
        self.assertEqual(list(units.iterdir()), [])

    def test_install_linux_without_systemd(self):
        self.fake_os(returncode=1)
        with self.assertRaises(scheduler.Unavailable) as ctx:
            scheduler.install(self.CMD)
        self.assertIn("0 10,15 * * * '/home/a b/.local/bin/timelog' --log", str(ctx.exception))
        self.assertIn("@reboot '/home/a b/.local/bin/timelog' --log", str(ctx.exception))
        self.assertEqual(scheduler.loaded(), {"crontab (no systemd here)": False})   # doctor checks the crontab

    def test_install_windows(self):
        home, calls = self.fake_os(windows=True)
        scheduler.install([r"C:\x\timelog.exe"])
        creates = [c for c in calls if c[:2] == ["schtasks", "/Create"]]
        self.assertEqual([c[3] for c in creates], ["codemonk-timelog-draft", "codemonk-timelog-app"])
        self.assertEqual(calls[-1], ["schtasks", "/Run", "/TN", "codemonk-timelog-app"])
        xml = home / ".timelog" / "codemonk-timelog-draft.xml"
        self.assertTrue(xml.read_bytes().startswith((b"\xff\xfe", b"\xfe\xff")))   # UTF-16 with a BOM
        self.assertIn("<Command>C:\\x\\timelog.exe</Command>", xml.read_text(encoding="utf-16"))

    def test_install_mac(self):
        home, calls = self.fake_os(mac=True)
        scheduler.install(self.CMD)
        agents = home / "Library" / "LaunchAgents"
        self.assertEqual(sorted(p.name for p in agents.iterdir()),
                         ["com.codemonk.timelog.app.plist", "com.codemonk.timelog.plist"])
        boots = [c for c in calls if c[:2] == ["launchctl", "bootstrap"]]
        self.assertEqual([c[2] for c in boots], ["gui/501", "gui/501"])

    def test_install_failure_says_why(self):
        from unittest import mock
        self.fake_os()
        with mock.patch.object(scheduler, "run", side_effect=lambda cmd, **kw: mock.Mock(
                returncode=0 if "show-environment" in cmd else 1, stdout="", stderr="Failed to connect to bus")):
            with self.assertRaises(RuntimeError) as ctx:
                scheduler.install(self.CMD)
        self.assertIn("Failed to connect to bus", str(ctx.exception))

    def test_uninstall_removes_hook_and_forwarder(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            settings, old = Path(tmp, "settings.json"), Path(tmp, "timelog.sh")
            other = {"type": "command", "command": "/bin/other"}
            settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
                {"hooks": [other]}, {"hooks": [{"type": "command", "command": '"/x/timelog" hook'}]}]}}))
            old.write_text('#!/bin/sh\nexec "/x/timelog" hook\n')
            with mock.patch.object(cli, "CLAUDE_SETTINGS", settings), mock.patch.object(cli, "OLD_HOOK_FILE", old), \
                    mock.patch.object(scheduler, "uninstall", return_value=[]), \
                    mock.patch("sys.stdout", new_callable=__import__("io").StringIO):
                cli.cmd_uninstall(None)
            self.assertEqual(json.loads(settings.read_text())["hooks"]["UserPromptSubmit"], [{"hooks": [other]}])
            self.assertFalse(old.exists())


class ZohoAuthTest(unittest.TestCase):
    """Personal Self Client auth: Connect Zoho saves your own credentials; ZOHO_* env values win."""
    ENV = ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_USER")

    def setUp(self):
        from unittest import mock
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.file = tmp / "zoho_token.json"
        self.enterContext(mock.patch.object(zoho, "ZOHO_TOKEN_FILE", self.file))
        self.enterContext(mock.patch.dict(os.environ))
        for key in self.ENV:
            os.environ.pop(key, None)
        zoho.disconnect()   # clears the cached access token too

    def reply(self, body):
        from unittest import mock
        return mock.Mock(json=mock.Mock(return_value=body))

    def test_not_connected(self):
        self.assertEqual(zoho.auth_status(), {"connected": False})
        self.assertRaises(zoho.NotConnected, zoho.credentials)
        self.assertRaises(zoho.NotConnected, zoho.token)

    def test_connect_saves_your_client_and_token(self):
        from unittest import mock
        with mock.patch.object(zoho.requests, "post", return_value=self.reply(
                {"access_token": "at-1", "refresh_token": "rt-1"})) as post, \
                mock.patch.object(zoho.requests, "get", return_value=self.reply({"Email": "me@codemonk.io"})):
            self.assertEqual(zoho.connect(" id-1 ", "secret-1\n", " code-1 "), "me@codemonk.io")
        self.assertTrue(post.call_args.args[0].endswith("/oauth/v2/token"))
        self.assertEqual(post.call_args.kwargs["data"], {"grant_type": "authorization_code", "client_id": "id-1",
                                                         "client_secret": "secret-1", "code": "code-1"})
        saved = json.loads(self.file.read_text())
        self.assertEqual((saved["client_id"], saved["client_secret"], saved["refresh_token"], saved["user"]),
                         ("id-1", "secret-1", "rt-1", "me@codemonk.io"))
        if os.name == "posix":
            self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(zoho.auth_status(), {"connected": True, "user": "me@codemonk.io", "source": "file"})
        self.assertEqual(zoho.user(), "me@codemonk.io")
        self.assertEqual(zoho.token(), "at-1")   # cached from the exchange: no second request

        with mock.patch.object(zoho.requests, "post", return_value=self.reply({"access_token": "at-2"})) as post:
            zoho._token_cache.update(at=0.0, token=None)
            self.assertEqual(zoho.token(), "at-2")
        self.assertEqual(post.call_args.kwargs["data"]["refresh_token"], "rt-1")

        self.assertTrue(zoho.disconnect())
        self.assertFalse(self.file.exists())
        self.assertEqual(zoho.auth_status(), {"connected": False})

    def test_connect_errors_say_what_to_do(self):
        from unittest import mock
        self.assertRaisesRegex(ValueError, "Paste the Client ID", zoho.connect, "id", "", "code")
        for error, hint in (("invalid_code", "expired or was already used"), ("invalid_client", "Client ID"),
                            ("something_new", "something_new")):
            with mock.patch.object(zoho.requests, "post", return_value=self.reply({"error": error})):
                self.assertRaisesRegex(ValueError, hint, zoho.connect, "id", "secret", "code")
        self.assertFalse(self.file.exists())

        tokens = self.reply({"access_token": "at", "refresh_token": "rt"})
        with mock.patch.object(zoho.requests, "post", return_value=tokens), \
                mock.patch.object(zoho.requests, "get", return_value=self.reply({})):
            self.assertRaisesRegex(ValueError, "email", zoho.connect, "id", "secret", "code")
        with mock.patch.object(zoho.requests, "post", return_value=tokens), \
                mock.patch.object(zoho.requests, "get") as get:
            self.assertEqual(zoho.connect("id", "secret", "code", "typed@codemonk.io"), "typed@codemonk.io")
        get.assert_not_called()

    def test_env_wins_over_the_file(self):
        self.file.write_text(json.dumps({"client_id": "f", "client_secret": "f", "refresh_token": "f",
                                         "user": "file@x.io"}))
        self.assertEqual(zoho.credentials()["source"], "file")
        os.environ.update(ZOHO_CLIENT_ID="env-id", ZOHO_CLIENT_SECRET="env-secret-xyz", ZOHO_REFRESH_TOKEN="env-rt",
                          ZOHO_USER="env@x.io")
        self.assertEqual((zoho.credentials()["source"], zoho.user()), ("env", "env@x.io"))
        self.assertEqual(zoho._redact("failed for env-secret-xyz"), "failed for ***")

    @unittest.skipUnless(__import__("importlib").util.find_spec("httpx"), "needs httpx (uv sync --group dev)")
    def test_routes(self):
        from unittest import mock
        from starlette.testclient import TestClient
        from zoho_timelog import review_app
        client = TestClient(review_app.make_app(8765), base_url="http://127.0.0.1:8765")
        status = client.get("/api/auth").json()
        self.assertEqual((status["connected"], status["scopes"]), (False, review_app.ZOHO_SCOPES))
        self.assertEqual(client.get("/api/days").status_code, 401)            # not connected → the page shows the form
        self.assertTrue(client.get("/api/days").json()["connect"])
        body = {"client_id": "id", "client_secret": "s", "code": "c"}
        self.assertEqual(client.post("/api/auth/connect", json=body).status_code, 403)   # no X-Timelog header
        hdr = {"X-Timelog": "1"}
        with mock.patch.object(zoho.requests, "post", return_value=self.reply({"error": "invalid_code"})):
            bad = client.post("/api/auth/connect", json=body, headers=hdr)
        self.assertEqual(bad.status_code, 400)
        self.assertIn("expired", bad.json()["error"])
        with mock.patch.object(zoho.requests, "post", return_value=self.reply({"access_token": "a", "refresh_token": "r"})), \
                mock.patch.object(zoho.requests, "get", return_value=self.reply({"Email": "me@codemonk.io"})):
            ok = client.post("/api/auth/connect", json=body, headers=hdr)
        self.assertEqual(ok.json(), {"connected": True, "user": "me@codemonk.io"})
        self.assertEqual(client.post("/api/auth/disconnect", headers=hdr).json(), {"connected": False})


if __name__ == "__main__":
    unittest.main()
