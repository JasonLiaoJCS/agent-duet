"""Shift schedule for a long task: the bridge is the alarm clock.

At each due time it leaves a check-in in the mailbox and wakes the conversation whose turn it is.
Neither Codex nor Claude needs a scheduler of its own, so a host rule against recurring automations
on one side does not matter. The schedule is only the longest the task goes unwatched: either side
may still wake the other at any time. No OS service is installed; the timer is a small background
process that any later bridge call restarts if it is gone (after a reboot, for example).
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import duet

PROVIDERS = ("claude", "codex")
MIN_MINUTES, MAX_MINUTES = 10, 720
PRESETS = {
    # name: (steps, what it is for). A step is (who checks, minutes after the previous check).
    "alternate":    ([("claude", 60), ("codex", 60)], "one hour each, taking turns"),
    "codex-heavy":  ([("claude", 60), ("codex", 60), ("codex", 60)], "three-hour cycle: Claude once, Codex twice"),
    "claude-heavy": ([("codex", 60), ("claude", 60), ("claude", 60)], "three-hour cycle: Codex once, Claude twice"),
    "quick":        ([("claude", 30), ("codex", 30)], "every 30 minutes, taking turns; for work that moves fast"),
    "relaxed":      ([("claude", 120), ("codex", 120)], "every two hours, taking turns"),
    "overnight":    ([("claude", 180), ("codex", 180)], "every three hours, taking turns; for long runs such as training"),
}


def table(db):
    db.execute("CREATE TABLE IF NOT EXISTS schedule (singleton INTEGER PRIMARY KEY CHECK(singleton=1), data TEXT NOT NULL)")


def load(db):
    table(db)
    row = db.execute("SELECT data FROM schedule WHERE singleton=1").fetchone()
    return json.loads(row[0]) if row else None


def save(db, data):
    db.execute("INSERT INTO schedule(singleton,data) VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET data=excluded.data",
               (duet.encode(data),))


def minutes_ok(value):
    value = float(value)
    if not MIN_MINUTES <= value <= MAX_MINUTES:
        raise ValueError(f"A check-in interval must be between {MIN_MINUTES} and {MAX_MINUTES} minutes")
    return value


def parse_pattern(text):
    """'claude:60, codex:60, codex:90' -> [('claude', 60.0), ...]"""
    steps = []
    for part in str(text).replace("→", ",").replace(">", ",").split(","):
        part = part.strip()
        if not part:
            continue
        who, _, minutes = part.partition(":")
        who = who.strip().lower()
        if who not in PROVIDERS:
            raise ValueError("Each step is who:minutes with who = claude or codex, for example claude:60,codex:60,codex:90")
        steps.append((who, minutes_ok(minutes.strip() or 60)))
    if not 1 <= len(steps) <= 12:
        raise ValueError("A custom pattern needs 1 to 12 steps")
    return steps


def other(provider):
    return "codex" if provider == "claude" else "claude"


def participant(db, provider):
    """The conversation of that provider in this project's pairing, else the most recently seen bound one."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_run'").fetchone():
        run = db.execute("SELECT * FROM native_run").fetchone()
        if run:
            for name in (run["first_peer"], run["second_peer"]):
                row = db.execute("SELECT provider FROM peers WHERE name=?", (name,)).fetchone()
                if row and row["provider"] == provider:
                    return name
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_bindings'").fetchone():
        row = db.execute("SELECT p.name FROM peers p JOIN native_bindings b ON b.name=p.name WHERE p.provider=? ORDER BY p.seen DESC LIMIT 1",
                         (provider,)).fetchone()
        if row:
            return row["name"]
    return None


def describe(data, now=None):
    now = now or time.time()
    out = {k: data.get(k) for k in ("state", "mode", "preset", "checks_done", "max_checks", "note", "set_by", "ended")}
    out["cycle"] = [{"who": who, "after_minutes": minutes} for who, minutes in data.get("pattern", [])]
    if data.get("state") == "active":
        out["next_check"] = {"who": data["next_who"], "in_minutes": max(0, round((data["next_due"] - now) / 60)),
                             "reason": data.get("next_reason")}
        out["ends_in_hours"] = round(max(0, data["until"] - now) / 3600, 1)
    out["recent"] = data.get("history", [])[-5:]
    out["reminder"] = ("The schedule is only the longest the task goes unwatched. Either side may wake the other at any time "
                       "for something urgent (duet_send wake=true).")
    return out


def alive(data):
    return bool(data.get("pid")) and duet.alive_identity(data["pid"], data.get("birth")) is True


def launch(root):
    flags = duet.FLAGS
    if os.name == "nt":
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "run", "--root", str(root)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=flags, start_new_session=os.name != "nt", env=env)


def ensure_running(root, db):
    """Called from ordinary bridge operations: bring the timer back if it died (reboot, crash)."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schedule'").fetchone():
        return None
    data = load(db)
    if not data or data.get("state") != "active" or alive(data):
        return None
    if os.environ.get("AGENT_DUET_NO_SCHEDULER") == "1":
        return None
    proc = launch(root)
    data.update(pid=proc.pid, birth=duet.process_birth(proc.pid))
    save(db, data)
    db.commit()
    return "schedule timer restarted"


def operate(op, a):
    root, state, db = duet.connect(a["root"])
    try:
        project = db.execute("SELECT * FROM project WHERE singleton=1").fetchone()
        if not project or a.get("project_id") != project["id"]:
            raise ValueError("Project ID mismatch; join this exact root first")
        now = time.time()
        data = load(db)
        if op == "schedule_status":
            if not data:
                return {"state": "none", "presets": {name: text for name, (_, text) in PRESETS.items()},
                        "also": "mode=custom with pattern='claude:60,codex:60,codex:90', or mode=adaptive to decide after every check"}
            restarted = ensure_running(root, db)
            return describe(data, now) | ({"note_timer": restarted} if restarted else {})
        duet.peer(db, a["name"])
        if op == "schedule_next":
            if not data or data.get("state") != "active":
                raise ValueError("No active schedule in this project")
            who = (a.get("who") or other(db.execute("SELECT provider FROM peers WHERE name=?", (a["name"],)).fetchone()["provider"])).lower()
            if who not in PROVIDERS:
                raise ValueError("who must be claude or codex")
            minutes = minutes_ok(a["minutes"])
            data.update(next_who=who, next_due=now + minutes * 60, next_reason=str(a.get("reason") or "")[:300], awaiting_next=False)
            save(db, data)
            db.commit()
            return describe(data, now)
        if op != "schedule":
            raise ValueError("Unknown schedule operation")
        mode = a.get("mode", "fixed")
        if mode == "off":
            if data and data.get("state") == "active":
                data.update(state="stopped", ended=now)
                save(db, data)
                db.commit()
            return {"state": "stopped" if data else "none"}
        if mode not in ("fixed", "custom", "adaptive"):
            raise ValueError("mode must be fixed, custom, adaptive or off")
        mine = db.execute("SELECT provider FROM peers WHERE name=?", (a["name"],)).fetchone()["provider"]
        for provider in PROVIDERS:
            if not participant(db, provider):
                raise ValueError("Both conversations must have joined this project (with their session, so they can be woken) "
                                 "before a schedule can start; missing: " + provider)
        preset = None
        if mode == "adaptive":
            first_who = (a.get("first_who") or other(mine)).lower()
            if first_who not in PROVIDERS:
                raise ValueError("first_who must be claude or codex")
            fallback = minutes_ok(a.get("interval_minutes", 60))
            pattern = [(first_who, fallback), (other(first_who), fallback)]
            first_minutes = minutes_ok(a.get("first_minutes", 45))
        else:
            if mode == "custom":
                pattern = parse_pattern(a.get("pattern") or "")
            else:
                preset = a.get("preset", "alternate")
                if preset not in PRESETS:
                    raise ValueError("Unknown preset; choose one of " + ", ".join(PRESETS) + ", or use mode=custom / mode=adaptive")
                pattern = list(PRESETS[preset][0])
                if a.get("interval_minutes") is not None:
                    each = minutes_ok(a["interval_minutes"])
                    pattern = [(who, each) for who, _ in pattern]
            # Start with the side that is not the one setting this up: that one is at work right now.
            start = next((i for i, (who, _) in enumerate(pattern) if who != mine), 0)
            pattern = pattern[start:] + pattern[:start]
            first_who, first_minutes = pattern[0]
        hours = float(a.get("hours", 24))
        if not 0.5 <= hours <= 168:
            raise ValueError("hours must be between 0.5 and 168 (one week); set it again to continue longer")
        data = {"id": uuid.uuid4().hex[:12], "state": "active", "mode": mode, "preset": preset, "pattern": [list(step) for step in pattern], "index": 0,
                "next_who": first_who, "next_due": now + first_minutes * 60, "next_reason": None, "awaiting_next": False,
                "started": now, "until": now + hours * 3600, "checks_done": 0, "max_checks": int(a.get("max_checks", 200)),
                "note": str(a.get("note") or "")[:500], "set_by": a["name"], "history": [], "pid": None, "birth": None}
        save(db, data)
        resumed = False
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_run'").fetchone():
            # Asking for a schedule is asking for wakeups: a pairing that was paused or finished is active again.
            resumed = bool(db.execute("UPDATE native_run SET state='active' WHERE state IN ('paused','needs_user','completed','limit_reached')").rowcount)
        db.commit()
        if os.environ.get("AGENT_DUET_NO_SCHEDULER") != "1":
            proc = launch(root)
            data.update(pid=proc.pid, birth=duet.process_birth(proc.pid))
            save(db, data)
            db.commit()
        return describe(data, now) | {"started": True, "wakeups_resumed": resumed}
    finally:
        db.close()


def check_in_text(data, number, project):
    lines = [f"[Scheduled check-in #{number}] It is your turn to look at this project: {project['name']}.",
             "Goal: " + project["goal"]]
    if data.get("note"):
        lines.append("What to watch: " + data["note"])
    lines += ["Do, in this order: read duet_inbox; look at what actually changed since the last check (results, logs, the latest checkpoint); "
              "move the work forward or fix what is stuck; leave the peer one batched handoff; save a checkpoint.",
              "If nothing needs doing, say so in one line and end the turn; do not invent work.",
              "Wake the peer now only if it must act before its own next check-in."]
    if data.get("mode") == "adaptive":
        lines.append("Before you finish, call duet_schedule_next with who should check next, in how many minutes, and why "
                     "(sooner while things are moving or fragile, later while a long run is simply in progress). "
                     "If you do not, the other side checks in %d minutes." % data["pattern"][0][1])
    lines.append("When the goal is met and reviewed, turn the schedule off with duet_schedule mode=off.")
    return "\n".join(lines)


def fire(root, db, data, now):
    """One due check-in: mailbox first, then a wake unless the conversation is evidently already at work."""
    project = db.execute("SELECT * FROM project WHERE singleton=1").fetchone()
    who = data["next_who"]
    recipient = participant(db, who)
    number = data["checks_done"] + 1
    entry = {"at": now, "who": who, "number": number}
    step_minutes = data["pattern"][data["index"] % len(data["pattern"])][1]
    paused = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_run'").fetchone():
        run = db.execute("SELECT state FROM native_run").fetchone()
        if run and run["state"] in ("paused", "needs_user"):
            paused = run["state"]
    if not recipient:
        entry["result"] = "skipped: no conversation of that side has joined"
    elif paused:
        entry["result"] = "skipped: the collaboration is " + paused
    else:
        seen = db.execute("SELECT seen FROM peers WHERE name=?", (recipient,)).fetchone()["seen"] or 0
        if now - seen < min(15, step_minutes / 3) * 60:
            # It used the bridge minutes ago, so it is in the middle of a turn. A wake would only queue a second one.
            entry["result"] = "skipped: that conversation was active %d minutes ago" % round((now - seen) / 60)
        else:
            cur = db.execute("INSERT INTO messages(sender,recipient,kind,body,reply_to,dedupe,created) VALUES(?,?,?,?,?,?,?)",
                             ("scheduler", recipient, "task", check_in_text(data, number, project), None,
                              "schedule-%s-%d" % (data.get("id") or int(data["started"]), number), now))
            db.commit()
            try:
                import native
                woke = native.operate(duet, "native_wake", {"name": "scheduler", "message_id": cur.lastrowid, "_system": True},
                                      root, root / ".agent-duet", db, project)
                db.commit()
                entry["result"] = "woken: " + str(woke.get("status"))
                if woke.get("error"):
                    entry["detail"] = str(woke["error"])[:200]
            except Exception as exc:
                db.rollback()
                entry["result"] = "left in the mailbox; wake failed: " + str(exc)[:200]
    data["checks_done"] = number
    data["history"] = (data.get("history") or [])[-19:] + [entry]
    data["index"] = (data["index"] + 1) % len(data["pattern"])
    next_who, next_minutes = data["pattern"][data["index"]]
    if data["mode"] == "adaptive":
        # Until the checking side says otherwise, the other side looks next after the fallback interval.
        next_who, data["awaiting_next"] = other(who), True
    data.update(next_who=next_who, next_due=now + next_minutes * 60, next_reason=None)
    return entry


def runner(root):
    me = os.getpid()
    while True:
        root_path, state, db = duet.connect(root)
        try:
            data = load(db)
            if not data or data.get("state") != "active":
                return
            if data.get("pid") not in (None, me) and alive(data):
                return  # another timer owns this schedule
            if data.get("pid") != me:
                data.update(pid=me, birth=duet.process_birth(me))
                save(db, data)
                db.commit()
            now = time.time()
            if now >= data["until"] or data["checks_done"] >= data["max_checks"]:
                data.update(state="finished", ended=now)
                save(db, data)
                owner = data.get("set_by")
                if owner and db.execute("SELECT 1 FROM peers WHERE name=?", (owner,)).fetchone():
                    db.execute("INSERT INTO messages(sender,recipient,kind,body,reply_to,dedupe,created) VALUES(?,?,?,?,?,?,?)",
                               ("scheduler", owner, "message",
                                "The check-in schedule reached its end (%d checks). If the task is not finished, set it again with duet_schedule." % data["checks_done"],
                                None, "schedule-%s-end" % (data.get("id") or int(data["started"])), now))
                db.commit()
                return
            wait = data["next_due"] - now
            if wait <= 0:
                # After sleep or a long pause several checks may be overdue: do one, then count from now.
                fire(root_path, db, data, now)
                save(db, data)
                db.commit()
                wait = 5
        finally:
            db.close()
        time.sleep(max(1, min(60, wait)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    runner(args.root)
