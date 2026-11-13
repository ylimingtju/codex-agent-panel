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
        start = {
            "hook_event_name": "SubagentStart",
            "session_id": "parent",
            "agent_id": "worker",
            "turn_id": "w1",
        }
        registration = threading.Timer(0.15, state.hook, args=(start,))
        registration.start()
        try:
            state.local(
                "progress",
                {
                    "tasks": [{"id": "check", "title": "核查"}],
                    "agents": [{"id": "/root/a", "task_id": "check"}],
                },
            )
        finally:
            registration.join()
        self.assertEqual(state.snapshot(key)["agents"][0]["task_id"], "check")

    def test_legacy_completion_uses_only_registered_turn(self):
        key = state.open_run()["view_key"]
        state.hook(
            {
                "hook_event_name": "SubagentStart",
                "agent_id": "worker",
                "session_id": "parent",
                "turn_id": "w1",
            }
        )
        with closing(
            sqlite3.connect(state.codex_home() / "thread_history_1.sqlite")
        ) as db:
            db.executescript("""
                UPDATE thread_turns SET completed_at=30,status='completed' WHERE turn_id='w1';
                INSERT INTO thread_turns VALUES('worker','w2','failed',31,40,2);
            """)
        agent = state.snapshot(key)["agents"][0]
        self.assertEqual((agent["status"], agent["ended_at"]), ("stopped", 30))

    def test_reused_turns_late_open_late_spawn_and_freeze(self):
        history = state.codex_home() / "thread_history_1.sqlite"

        def sql(script):
            with closing(sqlite3.connect(history)) as db:
                db.executescript(script)

        sql("""
            ALTER TABLE thread_turns ADD COLUMN root_turn_id TEXT;
            ALTER TABLE thread_turns ADD COLUMN final_agent_item_id TEXT;
            CREATE TABLE thread_items(thread_id,turn_id,item_id,item_type,item_json,PRIMARY KEY(thread_id,turn_id,item_id));
            UPDATE thread_turns SET root_turn_id=turn_id;
            UPDATE thread_turns SET root_turn_id='p1' WHERE thread_id='worker';
        """)

        def snapshot(key):
            return {a["id"]: a for a in state.snapshot(key)["agents"]}

        with patch("state.time.time", return_value=10):
            first = state.open_run()["view_key"]
            self.assertEqual(snapshot(first)["worker"]["started_at"], 1)
            state.local("progress", {"finished": True})
            self.assertEqual(snapshot(first)["worker"]["status"], "stopped")
        sql("""
            UPDATE thread_turns SET status='completed',completed_at=12 WHERE turn_id IN ('w1','p1');
            INSERT INTO thread_turns VALUES ('parent','p2','inProgress',14,NULL,2,'p2',NULL),
                ('worker','w2','inProgress',15,NULL,2,'p2',NULL);
        """)
        with patch("state.time.time", return_value=18):
            second = state.open_run(True)["view_key"]
            state.write_for_thread("worker", "step", {"done": 2, "total": 4})
            self.assertEqual(snapshot(second)["worker"]["started_at"], 15)
        sql("""
            INSERT INTO thread_turns VALUES ('nested','n','inProgress',19,NULL,1,'p2',NULL),
                ('other','impostor','inProgress',19,NULL,2,'p2',NULL);
            UPDATE thread_turns SET status='completed', completed_at=20, final_agent_item_id='final' WHERE turn_id='w2';
            INSERT INTO thread_items VALUES('worker','w2','final','agentMessage','{"phase":"final_answer","text":"reply"}');
        """)
        with patch("state.time.time", return_value=22):
            crew = snapshot(second)
            self.assertEqual(set(crew), {"worker", "nested"})
            self.assertEqual(
                (crew["worker"]["status"], crew["worker"]["ended_at"]), ("ended", 20)
            )
            self.assertEqual(
                (crew["nested"]["status"], crew["nested"]["done"]), ("running", None)
            )
            self.assertEqual(crew["worker"]["done"], 2)
        sql("""
            INSERT INTO thread_turns VALUES('worker','w3','inProgress',23,NULL,3,'p2',NULL);
            UPDATE thread_turns SET status='completed',completed_at=24 WHERE turn_id='n';
        """)
        with patch("state.time.time", return_value=25):
            self.assertIsNone(snapshot(second)["worker"]["done"])
            self.assertEqual(snapshot(second)["nested"]["status"], "stopped")
            state.hook(
                {
                    "session_id": "parent",
                    "agent_id": "worker",
                    "hook_event_name": "SubagentStop",
                    "turn_id": "w2",
                }
            )
            self.assertEqual(snapshot(second)["worker"]["status"], "running")
        sql("""
            UPDATE thread_turns SET status='completed',completed_at=26 WHERE turn_id='p2';
            INSERT INTO thread_turns VALUES ('parent','p3','inProgress',30,NULL,3,'p3',NULL),
                ('worker','w4','inProgress',31,NULL,4,'p3',NULL);
        """)
        with patch("state.time.time", return_value=32):
            # A later request in the same chat does not establish continuation of this open run.
            self.assertEqual(snapshot(second)["worker"]["started_at"], 23)
            with self.assertRaisesRegex(ValueError, "not part"):
                state.write_for_thread("worker", "step", {"done": 1, "total": 2})
            state.hook(
                {
                    "session_id": "parent",
                    "turn_id": "p2",
                    "hook_event_name": "Interrupt",
                }
            )
            self.assertEqual(state.snapshot(second)["status"], "active")
            # The manager explicitly continues, then the real interrupt freezes this exact turn.
            state.local("progress", {"phase": "continued"})
            self.assertEqual(snapshot(second)["worker"]["started_at"], 31)
            state.hook(
                {
                    "session_id": "parent",
                    "turn_id": "p3",
                    "hook_event_name": "Interrupt",
                }
            )
            self.assertEqual(state.snapshot(second)["status"], "interrupted")
            self.assertEqual(snapshot(second)["worker"]["ended_at"], 32)
        sql(
            "UPDATE thread_turns SET status='failed',completed_at=33 WHERE turn_id='w4';"
        )
        self.assertEqual(snapshot(first)["worker"]["ended_at"], 10)
        self.assertEqual(snapshot(second)["worker"]["status"], "stopped")

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
