"""Local progress state. Shell callers bind through Codex's current thread metadata."""

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def store_home():
    return Path(
        os.environ.get("SAVVY_PROGRESS_HOME") or codex_home() / "savvy-progress"
    )


@contextmanager
def store(*, read=False):
    home = store_home()
    path = home / "state.sqlite3"
    if read:
        if not path.is_file():
            raise ValueError("Unknown progress run")
        with closing(
            sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            yield db
        return
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        if db.execute("PRAGMA user_version").fetchone()[0] == 0:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, root TEXT NOT NULL, view_key TEXT UNIQUE NOT NULL,
                    title TEXT, phase TEXT, status TEXT, started_at REAL, updated_at REAL,
                    ended_at REAL, shown INTEGER DEFAULT 0, prompted INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS by_root ON runs(root);
                CREATE TABLE IF NOT EXISTS tasks(
                    flow TEXT, id TEXT, title TEXT, accepted INTEGER DEFAULT 0, PRIMARY KEY(flow,id));
                CREATE TABLE IF NOT EXISTS agents(
                    flow TEXT, id TEXT, name TEXT, model TEXT, effort TEXT, status TEXT,
                    started_at REAL, ended_at REAL, done INTEGER, total INTEGER, note TEXT DEFAULT '',
                    task_id TEXT, turn TEXT, PRIMARY KEY(flow,id));
                CREATE TABLE IF NOT EXISTS tickets(
                    token TEXT PRIMARY KEY, flow TEXT, actor TEXT, action TEXT, payload TEXT,
                    expires REAL, result TEXT);
                CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY);
                PRAGMA user_version=1;
            """)
        db.execute("BEGIN IMMEDIATE")
        with db:
            if db.execute("PRAGMA user_version").fetchone()[0] == 1:
                db.execute("ALTER TABLE agents ADD COLUMN tokens INTEGER")
                db.execute("PRAGMA user_version=2")
            yield db


def metadata(name):
    db = sqlite3.connect(
        (codex_home() / name).absolute().as_uri() + "?mode=ro", uri=True, timeout=0.4
    )
    db.row_factory = sqlite3.Row
    return closing(db)


def identity(thread):
    """Follow existing spawn edges, failing closed on missing or ambiguous metadata."""
    if not isinstance(thread, str) or not thread or len(thread) > 256:
        raise ValueError("Codex thread identity is unavailable")
    with metadata("state_5.sqlite") as db:
        seen = set()
        current = thread
        while current not in seen and len(seen) < 32:
            seen.add(current)
            row = db.execute(
                "SELECT thread_source, agent_path, agent_nickname, model, reasoning_effort "
                "FROM threads WHERE id=?",
                (current,),
            ).fetchone()
            if row is None:
                break
            if current == thread:
                own = dict(row)
            parents = db.execute(
                "SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id=?",
                (current,),
            ).fetchall()
            if len(parents) == 1:
                current = parents[0][0]
                continue
            if not parents and row["thread_source"] in (
                "user",
                "agent_created_thread",
                "chatgpt_handoff",
            ):
                return {
                    "actor": thread,
                    "root": current,
                    "caller": "manager" if thread == current else "worker",
                    "name": own["agent_path"] or own["agent_nickname"] or thread,
                    "model": own["model"],
                    "effort": own["reasoning_effort"],
                }
            break
    raise ValueError("Unverified Codex thread identity")


def shell_identity():
    return identity(os.environ.get("CODEX_THREAD_ID"))


def turn_owner(turn):
    if not turn:
        return None
    with metadata("thread_history_1.sqlite") as db:
        rows = db.execute(
            "SELECT thread_id FROM thread_turns WHERE turn_id=? LIMIT 2", (turn,)
        ).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def token():
    return secrets.token_urlsafe(24)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def current(db, root):
    return db.execute(
        "SELECT * FROM runs WHERE root=? ORDER BY rowid DESC LIMIT 1", (root,)
    ).fetchone()


def create_run(db, root):
    now = time.time()
    db.execute(
        "INSERT INTO runs(id,root,view_key,title,phase,status,started_at,updated_at) "
        "VALUES(?,?,?,'多代理任务','','active',?,?)",
        (token(), root, token(), now, now),
    )
    return current(db, root)


def entitled(db, info, new=False):
    latest = current(db, info["root"])
    if info["caller"] == "manager":
        if new and latest and latest["status"] == "active":
            raise ValueError("当前任务仍在进行中，请先验收完成或明确中断")
        return create_run(db, info["root"]) if latest is None or new else latest
    if new:
        raise ValueError("Only the manager may create a run")
    linked = db.execute(
        "SELECT r.* FROM agents a JOIN runs r ON a.flow=r.id "
        "WHERE a.id=? AND r.root=? ORDER BY r.rowid DESC LIMIT 1",
        (info["actor"], info["root"]),
    ).fetchone()
    if linked:
        return linked
    if latest is None or latest["status"] != "active":
        raise ValueError("Parent has no active progress run")
    record_agent(db, latest["id"], info, "running")
    return latest


def open_run(new=False):
    info = shell_identity()
    with store() as db:
        run = entitled(db, info, new)
        return {
            "flow_id": run["id"],
            "view_key": run["view_key"],
            "caller": info["caller"],
        }


def view(key):
    with store(read=True) as db:
        row = db.execute(
            "SELECT id,view_key FROM runs WHERE view_key=?", (key,)
        ).fetchone()
        if row is None:
            row = db.execute(
                "SELECT r.id,r.view_key FROM tickets t JOIN runs r ON t.flow=r.id "
                "WHERE t.token=? AND t.expires>?",
                (digest(key), time.time()),
            ).fetchone()
        if not row:
            raise ValueError("Unknown or expired view key")
        return {"flow_id": row["id"], "view_key": row["view_key"]}


def view_for_thread(thread_id, view_key=None):
    if not thread_id or not (store_home() / "state.sqlite3").is_file():
        return None
    try:
        info = identity(thread_id)
    except (ValueError, sqlite3.Error, OSError):
        return None
    with store(read=True) as db:
        sql = "SELECT r.id AS flow_id,r.view_key FROM runs r WHERE r.root=?"
        args = [info["root"]]
        if view_key is not None:
            sql += " AND r.view_key=?"
            args.append(view_key)
        if info["caller"] == "worker":
            sql += " AND EXISTS (SELECT 1 FROM agents a WHERE a.flow=r.id AND a.id=?)"
            args.append(thread_id)
        row = db.execute(sql + " ORDER BY r.rowid DESC LIMIT 1", args).fetchone()
        return dict(row) if row else None


def mark_shown(view_key):
    with store() as db:
        db.execute("UPDATE runs SET shown=1 WHERE view_key=?", (view_key,))


def text_field(value, length=240):
    if not isinstance(value, str) or not value.strip() or len(value) > length:
        raise ValueError("Invalid or oversized text")
    return value.strip()


AGENT_STATES = ("running", "waiting", "ended", "failed", "interrupted", "cancelled")


def normalize(action, payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    allowed = (
        {
            "title",
            "phase",
            "tasks",
            "accepted",
            "reopen",
            "remove",
            "agents",
            "finished",
        }
        if action == "progress"
        else {"done", "total", "note", "task_id", "status"}
        if action == "step"
        else set()
    )
    if not allowed or not set(payload) <= allowed:
        raise ValueError("Unknown action or field")
    out = {k: v for k, v in payload.items() if v is not None}
    for k, size in (("title", 240), ("phase", 160), ("task_id", 128)):
        if k in out:
            out[k] = text_field(out[k], size)
    for k in ("tasks", "accepted", "reopen", "remove"):
        if k in out:
            if not isinstance(out[k], list) or len(out[k]) > 1000:
                raise ValueError("Invalid task list")
            ids = []
            for item in out[k]:
                if k == "tasks":
                    if not isinstance(item, dict) or set(item) != {"id", "title"}:
                        raise ValueError("Tasks require id and title")
                    ids.append(
                        {
                            "id": text_field(item["id"], 128),
                            "title": text_field(item["title"]),
                        }
                    )
                else:
                    ids.append(text_field(item, 128))
            out[k] = ids
    if set(out.get("accepted", [])) & set(out.get("reopen", [])):
        raise ValueError("A task cannot be accepted and reopened together")
    if set(out.get("remove", [])) & (
        set(out.get("accepted", [])) | set(out.get("reopen", []))
    ):
        raise ValueError("Cannot both remove and update the same task")
    if "agents" in out:
        if not isinstance(out["agents"], list) or len(out["agents"]) > 300:
            raise ValueError("Invalid agent list")
        for a in out["agents"]:
            if (
                not isinstance(a, dict)
                or not set(a) <= {"id", "task_id", "status"}
                or "id" not in a
            ):
                raise ValueError("Invalid agent update")
            text_field(a["id"], 256)
            if "status" in a and a["status"] not in AGENT_STATES:
                raise ValueError("Invalid agent status")
            if "task_id" in a:
                text_field(a["task_id"], 128)
    if "finished" in out and type(out["finished"]) is not bool:
        raise ValueError("finished must be a boolean")
    if action == "step":
        if "status" in out and out["status"] not in AGENT_STATES:
            raise ValueError("Invalid agent status")
        counted = "done" in out or "total" in out
        if not counted and "status" not in out:
            raise ValueError("Supply steps or an explicit status")
        if counted and (
            type(out.get("done")) is not int
            or type(out.get("total")) is not int
            or not 0 <= out["done"] <= out["total"] <= 1000000
            or out["total"] < 1
        ):
            raise ValueError("Steps require 0 <= done <= total, and positive total")
        note = out.setdefault("note", "")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("Invalid note")
    return out


def summary(db, flow):
    row = db.execute("SELECT status FROM runs WHERE id=?", (flow,)).fetchone()
    count = db.execute(
        "SELECT coalesce(sum(accepted),0),count(*) FROM tasks WHERE flow=?", (flow,)
    ).fetchone()
    return {
        "flow_id": flow,
        "accepted": count[0],
        "total": count[1],
        "total_known": bool(count[1]),
        "status": row["status"],
    }


def record_agent(db, flow, info, status, turn=None):
    now = time.time()
    actor = info["actor"]
    old = db.execute(
        "SELECT * FROM agents WHERE flow=? AND id=?", (flow, actor)
    ).fetchone()
    if not old:
        observed = turn_info(actor, turn) if status == "running" else None
        if observed and observed["status"] == "inProgress" and observed["started_at"]:
            now = min(now, observed["started_at"])
            turn = observed["turn_id"]
        db.execute(
            "INSERT INTO agents(flow,id,name,model,effort,status,started_at,ended_at,turn) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                flow,
                actor,
                info["name"],
                info["model"],
                info["effort"],
                status,
                now if status == "running" else None,
                None if status == "running" else now,
                turn,
            ),
        )
    elif status == "running":
        if old["status"] != "running" and (turn is None or turn == old["turn"]):
            return
        db.execute(
            "UPDATE agents SET status='running',started_at=CASE WHEN status='running' THEN "
            "started_at ELSE ? END, ended_at=NULL,turn=coalesce(?,turn),model=coalesce(?,model),"
            "effort=coalesce(?,effort) WHERE flow=? AND id=?",
            (now, turn, info["model"], info["effort"], flow, actor),
        )
    else:
        if turn and old["turn"] and turn != old["turn"]:
            # If both are actual worker turns, ignore a delayed stop for a previous turn.
            if turn_info(actor, old["turn"]) is not None:
                return
        db.execute(
            "UPDATE agents SET status=?,ended_at=coalesce(ended_at,?),turn=coalesce(?,turn) "
            "WHERE flow=? AND id=?",
            (status, now, turn, flow, actor),
        )
    db.execute("UPDATE runs SET updated_at=? WHERE id=?", (time.time(), flow))


def assign(db, flow, actor, task_id):
    if not db.execute(
        "SELECT 1 FROM tasks WHERE flow=? AND id=?", (flow, task_id)
    ).fetchone():
        raise ValueError("Unknown task id")
    db.execute(
        "UPDATE agents SET task_id=? WHERE flow=? AND id=?", (task_id, flow, actor)
    )


def observed_status(db, flow, actor, status):
    now = time.time()
    terminal = status in ("ended", "failed", "interrupted", "cancelled")
    db.execute(
        "UPDATE agents SET status=?,ended_at=CASE WHEN ? THEN coalesce(ended_at,?) ELSE NULL END "
        "WHERE flow=? AND id=?",
        (status, terminal, now, flow, actor),
    )


class WorkerNotRegistered(ValueError):
    pass


def resolve_worker(db, run, reference):
    """Resolve the CLI's /root/name only among the workers registered in this run."""
    flow = run["id"]
    if reference.startswith("/"):
        rows = db.execute("SELECT id,name FROM agents WHERE flow=?", (flow,)).fetchall()
        # The start hook may have stored the agent type before Codex published its path.
        # Never query for a path globally: different chats can use the same name.
        matches = []
        for row in rows:
            try:
                worker = identity(row["id"])
            except ValueError:
                continue
            if (
                worker["caller"] == "worker"
                and worker["root"] == run["root"]
                and reference in (worker["name"], row["name"])
            ):
                matches.append(worker["actor"])
        if not matches:
            raise WorkerNotRegistered("代理路径在本轮尚无唯一匹配，请稍后重试或使用本轮详情中的线程 ID")
        if len(matches) != 1:
            raise ValueError("代理路径在本轮没有唯一匹配，请使用本轮详情中的线程 ID")
        return matches[0]
    info = identity(reference)
    if info["caller"] != "worker" or info["root"] != run["root"]:
        raise ValueError("Worker belongs to another chat")
    if not db.execute(
        "SELECT 1 FROM agents WHERE flow=? AND id=?", (flow, reference)
    ).fetchone():
        record_agent(db, flow, info, "running")
    return reference


def apply(db, run, actor, action, data):
    flow, now = run["id"], time.time()
    if action == "progress":
        if actor != run["root"]:
            raise ValueError("Manager authority required")
        if current(db, actor)["id"] != flow:
            raise ValueError("Superseded run is read-only")
        reopening = data.get("finished") is False or bool(data.get("reopen"))
        if run["status"] != "active" and not reopening:
            if data == {"finished": True} and run["status"] == "completed":
                return summary(db, flow)
            raise ValueError("Use finished=false or reopen to resume")
        if reopening:
            db.execute(
                "UPDATE runs SET status='active',ended_at=NULL WHERE id=?", (flow,)
            )
        for k in ("title", "phase"):
            if k in data:
                db.execute(f"UPDATE runs SET {k}=? WHERE id=?", (data[k], flow))
        for t in data.get("tasks", []):
            db.execute(
                "INSERT INTO tasks(flow,id,title) VALUES(?,?,?) ON CONFLICT(flow,id) "
                "DO UPDATE SET title=excluded.title",
                (flow, t["id"], t["title"]),
            )
        for field, value in (("accepted", 1), ("reopen", 0)):
            for tid in data.get(field, []):
                if not db.execute(
                    "UPDATE tasks SET accepted=? WHERE flow=? AND id=?",
                    (value, flow, tid),
                ).rowcount:
                    raise ValueError("Unknown task id")
        for tid in data.get("remove", []):
            existing = db.execute(
                "SELECT accepted FROM tasks WHERE flow=? AND id=?", (flow, tid)
            ).fetchone()
            if existing and existing[0]:
                raise ValueError("Reopen accepted task before removing it")
            db.execute("DELETE FROM tasks WHERE flow=? AND id=?", (flow, tid))
            db.execute(
                "UPDATE agents SET task_id=NULL WHERE flow=? AND task_id=?", (flow, tid)
            )
        for a in data.get("agents", []):
            aid = resolve_worker(db, run, a["id"])
            if "task_id" in a:
                assign(db, flow, aid, a["task_id"])
            if "status" in a:
                observed_status(db, flow, aid, a["status"])
        if data.get("finished"):
            s = summary(db, flow)
            if s["accepted"] != s["total"]:
                raise ValueError("Only a fully accepted plan can be finished")
            freeze_workers(db, flow, now)
            db.execute(
                "UPDATE runs SET status='completed',ended_at=? WHERE id=?", (now, flow)
            )
    else:
        if actor == run["root"]:
            raise ValueError("Worker authority required")
        if run["status"] != "active":
            raise ValueError("This run is closed")
        row = db.execute(
            "SELECT status FROM agents WHERE flow=? AND id=?", (flow, actor)
        ).fetchone()
        if row is None or (
            row["status"] not in ("running", "waiting")
            and data.get("status") != row["status"]
        ):
            raise ValueError("This worker is inactive")
        tid = data.get("task_id")
        if tid:
            assign(db, flow, actor, tid)
        if "status" in data:
            observed_status(db, flow, actor, data["status"])
        db.execute(
            "UPDATE agents SET done=coalesce(?,done),total=coalesce(?,total),note=? WHERE flow=? AND id=?",
            (data.get("done"), data.get("total"), data["note"], flow, actor),
        )
    db.execute("UPDATE runs SET updated_at=? WHERE id=?", (now, flow))
    return summary(db, flow)


def local(action, payload, ticket=False):
    return write_for_thread(shell_identity()["actor"], action, payload, ticket=ticket)


def write_for_thread(thread_id, action, payload, *, ticket=False):
    info = identity(thread_id)
    if (action == "progress") != (info["caller"] == "manager"):
        raise ValueError("Action is unavailable for this actor")
    data = normalize(action, payload)
    if not ticket:
        # Spawn can return a path before the lifecycle hook commits its row.
        # Release the write lock between attempts so that hook can register it.
        deadline = time.monotonic() + 2
        while True:
            try:
                with store() as db:
                    return apply(db, entitled(db, info), info["actor"], action, data)
            except WorkerNotRegistered:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
    with store() as db:
        run = entitled(db, info)
        key = token()
        now = time.time()
        db.execute("DELETE FROM tickets WHERE expires<?", (now,))
        db.execute(
            "INSERT INTO tickets VALUES(?,?,?,?,?,?,NULL)",
            (
                digest(key),
                run["id"],
                info["actor"],
                action,
                json.dumps(data, sort_keys=True),
                now + 300,
            ),
        )
        return {"binding": key, "flow_id": run["id"]}


def redeem(key, action, payload):
    data = normalize(action, payload)
    with store() as db:
        t = db.execute("SELECT * FROM tickets WHERE token=?", (digest(key),)).fetchone()
        if (
            not t
            or t["expires"] < time.time()
            or t["action"] != action
            or json.loads(t["payload"]) != data
        ):
            raise ValueError("Invalid action ticket")
        if t["result"] is not None:
            return json.loads(t["result"])
        run = db.execute("SELECT * FROM runs WHERE id=?", (t["flow"],)).fetchone()
        result = apply(db, run, t["actor"], action, data)
        db.execute(
            "UPDATE tickets SET result=? WHERE token=?",
            (json.dumps(result), t["token"]),
        )
        return result


def progress(binding, **payload):
    return redeem(binding, "progress", payload)


def step(binding, **payload):
    return redeem(binding, "step", payload)


def snapshot(view_key):
    with store(read=True) as db:
        run = db.execute("SELECT * FROM runs WHERE view_key=?", (view_key,)).fetchone()
        if not run:
            raise ValueError("Unknown view key")
        flow = run["id"]
        result = summary(db, flow)
        result.update(
            {
                k: run[k]
                for k in ("title", "phase", "started_at", "updated_at", "ended_at")
            }
        )
        tasks = db.execute(
            "SELECT id,title,accepted FROM tasks WHERE flow=? ORDER BY rowid", (flow,)
        )
        result["tasks"] = [dict(t, accepted=bool(t["accepted"])) for t in tasks]
        tokens_column = (
            "tokens"
            if db.execute("PRAGMA user_version").fetchone()[0] >= 2
            else "NULL AS tokens"
        )
        agents = db.execute(
            "SELECT id,name,model,effort,status,started_at,ended_at,done,total,note,"
            f"task_id,{tokens_column} FROM agents WHERE flow=? ORDER BY rowid",
            (flow,),
        )
        result["agents"] = [
            dict(a, context_used=None, context_window=None, cost=None) for a in agents
        ]
    if result["status"] == "active":
        enrich_workers(result)
    return result


def enrich_workers(result):
    """Token counters belong to the worker thread across runs. History times are exact turn observations."""
    agents = result["agents"]
    if not agents:
        return
    ids = [agent["id"] for agent in agents]
    marks = ",".join("?" for _ in ids)
    try:
        with metadata("state_5.sqlite") as db:
            rows = {
                r["id"]: r
                for r in db.execute(
                    f"SELECT id,tokens_used,model,reasoning_effort,agent_path FROM threads WHERE id IN ({marks})",
                    ids,
                )
            }
        for agent in agents:
            r = rows.get(agent["id"])
            if r is None:
                continue
            tokens = r["tokens_used"]
            agent.update(
                tokens=tokens if type(tokens) is int and tokens > 0 else None,
                model=r["model"] or agent["model"],
                effort=r["reasoning_effort"] or agent["effort"],
                name=r["agent_path"] or agent["name"],
            )
    except (sqlite3.Error, OSError):
        pass
    # The private schema can change. A positive identified exit can correct a missed hook.
    if result["status"] != "active":
        return
    try:
        with metadata("thread_history_1.sqlite") as db:
            for agent in agents:
                if agent["status"] not in ("running", "waiting"):
                    continue
                r = db.execute(
                    "SELECT status,started_at,completed_at FROM thread_turns WHERE thread_id=? "
                    "ORDER BY rollout_ordinal DESC LIMIT 1",
                    (agent["id"],),
                ).fetchone()
                if (
                    not r
                    or not r["completed_at"]
                    or not agent["started_at"]
                    or r["completed_at"] < int(agent["started_at"])
                ):
                    continue
                status = {
                    "completed": "stopped",
                    "failed": "failed",
                    "interrupted": "interrupted",
                }.get(r["status"])
                if status:
                    agent.update(status=status, ended_at=r["completed_at"])
    except (sqlite3.Error, OSError):
        pass


def freeze_workers(db, flow, now):
    agents = [dict(a) for a in db.execute("SELECT * FROM agents WHERE flow=?", (flow,))]
    enrich_workers({"agents": agents, "status": "active"})
    for a in agents:
        status = "stopped" if a["status"] in ("running", "waiting") else a["status"]
        db.execute(
            "UPDATE agents SET status=?,ended_at=?,tokens=?,model=?,effort=?,name=? WHERE flow=? AND id=?",
            (
                status,
                a["ended_at"] or now,
                a["tokens"],
                a["model"],
                a["effort"],
                a["name"],
                flow,
                a["id"],
            ),
        )


def turn_info(actor, turn=None):
    try:
        with metadata("thread_history_1.sqlite") as db:
            if turn:
                return db.execute(
                    "SELECT turn_id,status,started_at,completed_at FROM thread_turns "
                    "WHERE thread_id=? AND turn_id=?",
                    (actor, turn),
                ).fetchone()
            return db.execute(
                "SELECT turn_id,status,started_at,completed_at FROM thread_turns "
                "WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1",
                (actor,),
            ).fetchone()
    except sqlite3.Error:
        return None


def hook(event):
    kind = event.get("hook_event_name")
    session, turn = event.get("session_id"), event.get("turn_id")
    if not session or not turn:
        return {}
    spawn = kind == "PostToolUse" and str(event.get("tool_name", "")).split(".")[
        -1
    ] in ("spawn_agent", "Agent")
    if not spawn and kind not in ("SubagentStart", "SubagentStop", "Interrupt"):
        return {}
    if spawn or kind == "Interrupt":
        owner = turn_owner(turn)
        if owner != session:
            return {}
        info = identity(owner)
        if info["caller"] != "manager":
            return {}
    else:
        actor = event.get("agent_id")
        if not actor:
            return {}
        # Host lifecycle events can precede publication of the spawn edge.
        try:
            info = identity(actor)
        except ValueError:
            info = None
        if not info:
            if kind != "SubagentStart":
                return {}
            parent = identity(session)
            if actor == parent["root"]:
                return {}
            info = {
                "caller": "worker",
                "actor": actor,
                "root": parent["root"],
                "name": event.get("agent_type") or actor,
                "model": event.get("model"),
                "effort": None,
            }
        elif info["caller"] != "worker" or identity(session)["root"] != info["root"]:
            return {}
    root = info["root"]
    event_key = json.dumps([kind, root, turn, info["actor"], event.get("tool_use_id")])
    with store() as db:
        if not db.execute(
            "INSERT OR IGNORE INTO seen VALUES(?)", (event_key,)
        ).rowcount:
            return {}
        run = current(db, root)
        if spawn:
            if not run or run["status"] != "active":
                run = create_run(db, root)
            if not run["shown"] and not run["prompted"]:
                db.execute("UPDATE runs SET prompted=1 WHERE id=?", (run["id"],))
                note = (
                    "本次主聊天已开始多代理协作。现在使用 savvy-progress skill：运行 state.py open，"
                    "用返回的 view_key 调用 show_track 一次；更新实际计划并仅在验收后计数。"
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": kind,
                        "additionalContext": note,
                    }
                }
        elif kind == "Interrupt":
            if run and run["status"] == "active":
                now = time.time()
                freeze_workers(db, run["id"], now)
                db.execute(
                    "UPDATE runs SET status='interrupted',ended_at=?,updated_at=? WHERE id=?",
                    (now, now, run["id"]),
                )
        else:
            old = db.execute(
                "SELECT r.* FROM agents a JOIN runs r ON a.flow=r.id WHERE r.root=? "
                "AND a.id=? ORDER BY r.rowid DESC LIMIT 1",
                (root, info["actor"]),
            ).fetchone()
            if kind == "SubagentStart":
                if not run or run["status"] != "active":
                    run = create_run(db, root)
                record_agent(db, run["id"], info, "running", turn)
                script = str(Path(__file__).resolve())
                note = (
                    "可按实际里程碑上报自己的进度。运行 python3 "
                    + json.dumps(script, ensure_ascii=False)
                    + ' step \'{"done":1,"total":3,"note":"实际已完成的步骤"}\'，请替换为真实数据。'
                )
                if (
                    not run["shown"]
                    and not run["prompted"]
                    and db.execute(
                        "SELECT count(*) FROM agents WHERE flow=?", (run["id"],)
                    ).fetchone()[0]
                    == 1
                ):
                    note += (
                        " 如果本次宿主没有向主代理发送派工提示，在你给主代理的首条常规进展消息里"
                        "附一句：本轮可调用 savvy-progress 的 show_track 查看代理进度。无须单独发送消息。"
                    )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": kind,
                        "additionalContext": note,
                    }
                }
            if old and old["status"] == "active":
                actual = turn_info(info["actor"], turn)
                status = {"failed": "failed", "interrupted": "interrupted"}.get(
                    actual["status"] if actual else None, "stopped"
                )
                if (
                    status == "stopped"
                    and isinstance(event.get("last_assistant_message"), str)
                    and event["last_assistant_message"].strip()
                ):
                    status = "ended"
                if status == "stopped":
                    previous = db.execute(
                        "SELECT status FROM agents WHERE flow=? AND id=?",
                        (old["id"], info["actor"]),
                    ).fetchone()
                    if previous and previous[0] in (
                        "ended",
                        "failed",
                        "interrupted",
                        "cancelled",
                    ):
                        return {}
                record_agent(db, old["id"], info, status, turn)
    return {}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("open", "bind"):
        sub.add_parser(name).add_argument("--new", action="store_true")
    for name in ("progress", "step", "authorize"):
        p = sub.add_parser(name)
        if name == "authorize":
            p.add_argument("action", choices=("progress", "step"))
        p.add_argument("payload", help="JSON object, or - to read stdin")
    sub.add_parser("hook")
    args = parser.parse_args()
    if args.command == "hook":
        try:
            result = hook(json.load(sys.stdin))
        except (KeyError, ValueError, TypeError, sqlite3.Error, OSError):
            result = {}
    elif args.command in ("open", "bind"):
        result = open_run(args.new)
    else:
        payload = (
            json.load(sys.stdin) if args.payload == "-" else json.loads(args.payload)
        )
        result = local(
            getattr(args, "action", args.command),
            payload,
            ticket=args.command == "authorize",
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, sqlite3.Error, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
