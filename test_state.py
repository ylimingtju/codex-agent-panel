from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import state


class ProgressTests(unittest.TestCase):
    def setUp(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(home),
                    "SAVVY_PROGRESS_HOME": str(home / "progress"),
                    "CODEX_THREAD_ID": "parent",
                },
            )
        )
        with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
            db.executescript("""
                CREATE TABLE threads(id,thread_source,agent_path,agent_nickname,model,reasoning_effort);
                CREATE TABLE thread_spawn_edges(parent_thread_id,child_thread_id);
                INSERT INTO threads VALUES ('parent','user',NULL,NULL,'gpt','medium'),
                   ('other','agent_created_thread',NULL,NULL,'gpt','high'),
                   ('worker','subagent','/root/a','worker','gpt','high'),
                   ('nested','subagent','/root/a/b','nested','gpt','medium'),
                   ('ambiguous','subagent','/root/missing',NULL,NULL,NULL);
                INSERT INTO thread_spawn_edges VALUES ('parent','worker'),('worker','nested');
            """)
        with closing(sqlite3.connect(home / "thread_history_1.sqlite")) as db:
            db.executescript("""
                CREATE TABLE thread_turns(thread_id,turn_id,status,started_at,completed_at,rollout_ordinal);
                INSERT INTO thread_turns VALUES ('parent','p1','inProgress',1,NULL,1),
                    ('worker','w1','inProgress',1,NULL,1),('other','o1','inProgress',1,NULL,1);
            """)

    def test_lifecycle_rework_and_closed_views(self):
        key = state.open_run()["view_key"]
        state.local(
            "progress",
            {"tasks": [{"id": "a", "title": "准备"}, {"id": "b", "title": "检查"}]},
        )
        state.write_for_thread(
            "parent", "progress", {"agents": [{"id": "worker", "task_id": "a"}]}
        )
        self.assertIsNone(state.snapshot(key)["agents"][0]["done"])
        state.write_for_thread(
            "worker", "step", {"done": 1, "total": 2, "note": "准备好了"}
        )
        state.write_for_thread(
            "worker", "step", {"status": "waiting", "note": "等待输入"}
        )
        self.assertEqual(state.snapshot(key)["agents"][0]["done"], 1)
        state.local("progress", {"agents": [{"id": "worker", "status": "ended"}]})
        self.assertEqual(state.snapshot(key)["accepted"], 0)
        with self.assertRaises(ValueError):
            state.local("progress", {"accepted": ["a"], "finished": True})
        self.assertEqual(state.snapshot(key)["accepted"], 0)
        for _ in range(2):
            state.local("progress", {"accepted": ["a"]})
        state.local(
            "progress",
            {"reopen": ["a"], "agents": [{"id": "worker", "status": "running"}]},
        )
        state.local("progress", {"remove": ["b"], "accepted": ["a"], "finished": True})
        new = state.open_run(True)["view_key"]
        self.assertNotEqual(new, key)
        self.assertEqual(
            (state.snapshot(new)["total_known"], state.snapshot(key)["accepted"]),
            (False, 1),
        )
        self.assertEqual(state.view_for_thread("parent", key)["view_key"], key)
        self.assertIsNone(state.view_for_thread("other", key))
        self.assertIsNone(state.view_for_thread("worker", new))
        self.assertIsNone(state.view_for_thread("ambiguous"))
        self.assertEqual(
            state.local("progress", {"finished": True})["status"], "completed"
        )

    def test_registered_cli_path_is_scoped_and_unique(self):
        home = state.codex_home()
        with closing(sqlite3.connect(home / "state_5.sqlite")) as db:
            db.executescript("""
                INSERT INTO threads VALUES ('unrelated','subagent','/root/a',NULL,NULL,NULL),
                    ('duplicate','subagent','/root/a',NULL,NULL,NULL);
                INSERT INTO thread_spawn_edges VALUES ('other','unrelated'),('parent','duplicate');
            """)
        key = state.open_run()["view_key"]
        state.local("progress", {"tasks": [{"id": "check", "title": "核查"}]})
        state.hook(
            {
                "hook_event_name": "SubagentStart",
                "agent_id": "worker",
                "session_id": "parent",
                "turn_id": "w1",
            }
        )
        state.local(
            "progress",
            {
                "agents": [{"id": "/root/a", "task_id": "check", "status": "ended"}],
                "accepted": ["check"],
            },
        )
        self.assertEqual(
            [(a["id"], a["task_id"]) for a in state.snapshot(key)["agents"]],
            [("worker", "check")],
        )
        with self.assertRaises(ValueError):
            state.write_for_thread(
                "other", "progress", {"agents": [{"id": "/root/a", "status": "ended"}]}
            )
        with self.assertRaises(ValueError):
            state.local(
                "progress", {"agents": [{"id": "unrelated", "status": "ended"}]}
            )
        state.local("progress", {"agents": [{"id": "duplicate", "status": "running"}]})
        with self.assertRaisesRegex(ValueError, "唯一"):
            state.local("progress", {"agents": [{"id": "/root/a", "status": "failed"}]})
        self.assertEqual(state.snapshot(key)["agents"][0]["status"], "ended")
        state.local("progress", {"finished": True})
        state.open_run(True)
        with self.assertRaisesRegex(ValueError, "唯一"):
            state.local(
                "progress", {"agents": [{"id": "/root/a", "status": "running"}]}
            )

    def test_assignment_can_arrive_before_start_hook(self):
        key = state.open_run()["view_key"]
        start = {"hook_event_name": "SubagentStart", "session_id": "parent",
                 "agent_id": "worker", "turn_id": "w1"}
        registration = threading.Timer(0.15, state.hook, args=(start,))
        registration.start()
        try:
            state.local("progress", {"tasks": [{"id": "check", "title": "核查"}],
                                     "agents": [{"id": "/root/a", "task_id": "check"}]})
        finally:
            registration.join()
        self.assertEqual(state.snapshot(key)["agents"][0]["task_id"], "check")

    def test_identity_and_action_tickets(self):
        self.assertEqual(state.identity("nested")["root"], "parent")
        for actor, kind, vals in [
            ("worker", "progress", {"accepted": ["a"]}),
            ("parent", "step", {"done": 1, "total": 1}),
            ("ambiguous", "progress", {"title": "test"}),
        ]:
            with self.assertRaises(ValueError):
                state.write_for_thread(actor, kind, vals)
        ticket = state.local("progress", {"title": "初稿"}, ticket=True)["binding"]
        with self.assertRaises(ValueError):
            state.progress(ticket, title="代办")
        self.assertEqual(
            state.progress(ticket, title="初稿"), state.progress(ticket, title="初稿")
        )
        with self.assertRaises(ValueError):
            state.progress(state.open_run()["view_key"], title="初稿")
        with self.assertRaises(ValueError):
            state.local("progress", {"agents": [{"id": "other", "status": "running"}]})

    def test_hooks_scope_and_once(self):
        start = {
            "hook_event_name": "SubagentStart",
            "turn_id": "w1",
            "session_id": "parent",
            "agent_id": "worker",
        }
        stop = dict(start, hook_event_name="SubagentStop")
        spawn = {
            "hook_event_name": "PostToolUse",
            "tool_name": "spawn_agent",
            "turn_id": "p1",
            "session_id": "parent",
            "tool_use_id": "new-agent",
        }
        self.assertIn("hookSpecificOutput", state.hook(start))
        self.assertEqual(state.hook(start), {})
        self.assertIn("hookSpecificOutput", state.hook(spawn))
        self.assertEqual(state.hook(dict(spawn, tool_use_id="again")), {})
        self.assertEqual(
            state.hook(dict(spawn, turn_id="w1", tool_use_id="nested-spawn")), {}
        )
        key = state.open_run()["view_key"]
        state.hook(stop)
        self.assertEqual(state.snapshot(key)["agents"][0]["status"], "stopped")
        state.hook(
            {"hook_event_name": "Interrupt", "turn_id": "p1", "session_id": "parent"}
        )
        self.assertEqual(state.snapshot(key)["status"], "interrupted")
        state.hook(dict(spawn, tool_use_id="next"))
        self.assertNotEqual(state.open_run()["view_key"], key)


if __name__ == "__main__":
    unittest.main()
