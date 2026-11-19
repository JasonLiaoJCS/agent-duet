"""Shift schedule for a long task: the bridge is the alarm clock.

At each due time it leaves a check-in in the mailbox and wakes the conversation whose turn it is.
Neither Codex nor Claude has to keep a scheduler of its own. The schedule is only the longest the
task goes unwatched: either side may still wake the other at any time. No OS service is installed;
the timer is a small background process that a later bridge call restarts if it is gone (after a
reboot, for example).

Every writer re-reads the row inside a write transaction and changes only what it owns, so a timer
that is in the middle of a check-in can never bring back a schedule that was stopped meanwhile.
"""
from __future__ import annotations
import argparse
import hashlib
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


def write(db):
    """Start a write transaction so the row cannot change between reading and saving it."""
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    return load(db)


def stop(db, now, state="stopped"):
    """End the active schedule, if any. Returns (data before, timer identity) or (None, None)."""
    data = write(db)
    if not data or data.get("state") != "active":
        return None, None
    timer = (data.get("pid"), data.get("birth"))
    data.update(state=state, ended=now, rev=int(data.get("rev", 0)) + 1)
    save(db, data)
    return data, timer


def minutes_ok(value, low=MIN_MINUTES, high=MAX_MINUTES):
    value = float(value)
    if not low <= value <= high:
        raise ValueError(f"A check-in interval must be between {low:g} and {high:g} minutes")
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


def parse_watch(root, text):
    """Project-relative files or globs whose change means a check-in is worth a turn."""
    patterns = [part.strip().replace("\\", "/") for part in str(text or "").split(",") if part.strip()]
    if len(patterns) > 10:
        raise ValueError("watch takes at most 10 paths or patterns")
    for pattern in patterns:
        if pattern.startswith("/") or ":" in pattern or ".." in Path(pattern).parts:
            raise ValueError("watch paths are relative to the project folder, for example runs/log.txt or results/*.json")
    return patterns


SCAN_LIMIT = 2000


def scan(root, patterns):
    """(fingerprint, files matched, truncated). Names, sizes and nanosecond modification times of the
    watched files: cheap, and a rewrite within the same second still shows."""
    seen, truncated = [], False
    for pattern in patterns:
        if truncated:
            break
        try:
            matches = sorted(Path(root).glob(pattern))
        except (OSError, ValueError):
            continue
        for match in matches:
            files = [match] if match.is_file() else (p for p in match.rglob("*") if p.is_file()) if match.is_dir() else []
            for path in files:
                try:
                    info = path.stat()
                    seen.append((path.relative_to(root).as_posix(), info.st_size, info.st_mtime_ns))
                except (OSError, ValueError):
                    continue
                if len(seen) >= SCAN_LIMIT:
                    truncated = True
                    break
            if truncated:
                break
    digest = hashlib.sha1(json.dumps(sorted(seen)).encode()).hexdigest() if seen else "nothing matched"
    return digest, len(seen), truncated


def fingerprint(root, patterns):
    return scan(root, patterns)[0]


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


def bounds(data):
    """How far the next check may be: the user's rough scale allows half to double of it."""
    scale = data.get("around_minutes")
    if scale and data.get("around_strict"):
        return max(MIN_MINUTES, scale / 2), min(MAX_MINUTES, scale * 2)
    return MIN_MINUTES, MAX_MINUTES  # without "strict" the scale is only how much the user wants to spend


def describe(data, now=None):
    now = now or time.time()
    out = {k: data.get(k) for k in ("state", "mode", "preset", "checks_done", "max_checks", "note", "set_by", "ended", "watch",
                                    "around_minutes", "around_strict")}
    out["cycle"] = [{"who": who, "after_minutes": minutes} for who, minutes in data.get("pattern", [])]
    if data.get("state") == "active":
        out["next_check"] = {"who": data["next_who"], "in_minutes": max(0, round((data["next_due"] - now) / 60)),
                             "reason": data.get("next_reason")}
        out["ends_in_hours"] = round(max(0, data["until"] - now) / 3600, 1)
        if data.get("mode") == "adaptive":
            low, high = bounds(data)
            out["next_may_be_between_minutes"] = [round(low), round(high)]
    out["skipped_without_a_wake"] = data.get("skipped", 0)
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


def start_timer(root, db):
    """Start exactly one timer. The right to start is a one-time token claimed inside a write
    transaction, so a starter that was slow can never overwrite the timer another one registered."""
    if os.environ.get("AGENT_DUET_NO_SCHEDULER") == "1":
        return None
    now = time.time()
    data = write(db)
    if not data or data.get("state") != "active" or alive(data) or now - float(data.get("claimed_at") or 0) < 30:
        db.commit()
        return None
    token = uuid.uuid4().hex
    data.update(claimed_at=now, claim=token)
    save(db, data)
    db.commit()
    proc = launch(root)
    current = write(db)
    if current and current.get("id") == data.get("id") and current.get("state") == "active" and current.get("claim") == token:
        # The claim only covers the instant of launching. Once the timer is recorded, a later death
        # (a reboot, a kill) is noticed from its process identity and it is restarted at once.
        current.update(pid=proc.pid, birth=duet.process_birth(proc.pid), claimed_at=None, claim=None)
        save(db, current)
    db.commit()  # if the token no longer matches, the timer we launched finds another owner and exits
    return proc.pid


def resume(root, db, now):
    """Bring back a schedule that was parked with a pause. Returns True when one was resumed."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schedule'").fetchone():
        return False
    data = write(db)
    if not data or data.get("state") != "paused" or now >= data.get("until", 0):
        db.commit()
        return False
    step = data["pattern"][data["index"] % len(data["pattern"])][1]
    data.update(state="active", ended=None, next_due=now + step * 60, pid=None, birth=None, claimed_at=None,
                rev=int(data.get("rev", 0)) + 1)
    save(db, data)
    db.commit()
    start_timer(root, db)
    return True


def ensure_running(root, db):
    """Called from ordinary bridge operations: bring the timer back if it died (reboot, crash)."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schedule'").fetchone():
        return None
    data = load(db)
    if not data or data.get("state") != "active" or alive(data):
        return None
    return "schedule timer restarted" if start_timer(root, db) else None


def operate(op, a):
    root, state, db = duet.connect(a["root"])
    try:
        project = db.execute("SELECT * FROM project WHERE singleton=1").fetchone()
        if not project or a.get("project_id") != project["id"]:
            raise ValueError("Project ID mismatch; join this exact root first")
        now = time.time()
        if op == "schedule_status":
            data = load(db)
            if not data:
                return {"state": "none", "presets": {name: text for name, (_, text) in PRESETS.items()},
                        "also": "mode=custom with pattern='claude:60,codex:60,codex:90'; mode=adaptive to decide after every check, "
                                "optionally with around_minutes as the user's rough scale; watch='path,glob' to wake only on change"}
            restarted = ensure_running(root, db)
            return describe(load(db), now) | ({"note_timer": restarted} if restarted else {})
        duet.peer(db, a["name"])
        mine = db.execute("SELECT provider FROM peers WHERE name=?", (a["name"],)).fetchone()["provider"]
        if op == "schedule_next":
            minutes = float(a["minutes"])
            data = write(db)
            if not data or data.get("state") != "active":
                raise ValueError("No active schedule in this project")
            who = (a.get("who") or other(mine)).lower()
            if who not in PROVIDERS:
                raise ValueError("who must be claude or codex")
            low, high = bounds(data)
            note = None
            if not low <= minutes <= high:
                if not (data.get("around_minutes") and data.get("around_strict")):
                    minutes_ok(minutes)
                # The user made the scale a limit; stay inside it and say so instead of failing the turn.
                clamped = min(high, max(low, minutes))
                note = "The user asked for about every %g minutes, so %g was adjusted to %g." % (data["around_minutes"], minutes, clamped)
                minutes = clamped
            data.update(next_who=who, next_due=now + minutes * 60, next_reason=str(a.get("reason") or "")[:300],
                        awaiting_next=False, rev=int(data.get("rev", 0)) + 1)
            save(db, data)
            db.commit()
            return describe(data, now) | ({"adjusted": note} if note else {})
        if op != "schedule":
            raise ValueError("Unknown schedule operation")
        mode = a.get("mode", "fixed")
        if mode == "off":
            data, timer = stop(db, now)
            db.commit()
            return {"state": "stopped" if data else "none"}
        if mode not in ("fixed", "custom", "adaptive"):
            raise ValueError("mode must be fixed, custom, adaptive or off")
        import native
        native.schema(db)
        native.guard_supervisor(db)  # a background run owns the project; do not wake original conversations into it
        names = {mine: a["name"], other(mine): participant(db, other(mine))}
        if not names[other(mine)]:
            raise ValueError("Both conversations must have joined this project (with their session, so they can be woken) "
                             "before a schedule can start; missing: " + other(mine))
        preset, around = None, None
        strict = duet.flag(a, "strict")
        if mode == "adaptive":
            first_who = (a.get("first_who") or other(mine)).lower()
            if first_who not in PROVIDERS:
                raise ValueError("first_who must be claude or codex")
            if a.get("around_minutes") is not None:
                around = minutes_ok(a["around_minutes"])
            fallback = minutes_ok(a.get("interval_minutes", around or 60))
            first_minutes = minutes_ok(a.get("first_minutes", min(45, fallback)))
            if around and strict:
                # A strict scale binds every interval, including the first check and the fallback.
                low, high = max(MIN_MINUTES, around / 2), min(MAX_MINUTES, around * 2)
                fallback = min(high, max(low, fallback))
                first_minutes = min(high, max(low, first_minutes))
            pattern = [(first_who, fallback), (other(first_who), fallback)]
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
        watch = parse_watch(root, a.get("watch"))
        watch_fp, matched, truncated = scan(root, watch) if watch else (None, 0, False)  # before the write transaction
        longest = max(minutes for _, minutes in pattern)
        quiet = minutes_ok(a.get("max_quiet_minutes", min(1440, longest * 4)), 30, 1440)
        write(db)
        data = {"id": uuid.uuid4().hex[:12], "rev": 1, "state": "active", "mode": mode, "preset": preset,
                "pattern": [list(step) for step in pattern], "index": 0, "around_minutes": around,
                "around_strict": bool(around) and strict,
                "next_who": first_who, "next_due": now + first_minutes * 60, "next_reason": None, "awaiting_next": False,
                "started": now, "until": now + hours * 3600, "checks_done": 0, "ticks": 0, "skipped": 0,
                "max_checks": int(a.get("max_checks", 200)), "note": str(a.get("note") or "")[:500], "set_by": a["name"],
                "participants": names, "watch": watch, "watch_fp": watch_fp,
                "max_quiet": quiet * 60, "last_real_check": now, "history": [], "pid": None, "birth": None}
        save(db, data)
        # Asking for a schedule is asking for wakeups: a pairing that was paused or finished is active again.
        resumed = bool(db.execute("UPDATE native_run SET state='active' WHERE state IN ('paused','needs_user','completed','limit_reached')").rowcount)
        db.commit()
        start_timer(root, db)
        warnings = []
        if watch and matched == 0:
            warnings.append("watch matches no file yet (%s). If that is a typo, set the schedule again; until a file appears, "
                            "a check-in only happens after the quiet limit." % ", ".join(watch))
        if truncated:
            warnings.append("watch matches more than %d files; only the first %d are compared. Narrow the pattern." % (SCAN_LIMIT, SCAN_LIMIT))
        return describe(load(db), now) | {"started": True, "wakeups_resumed": resumed} | ({"warnings": warnings} if warnings else {})
    finally:
        db.close()


def check_in_text(data, number, project, missed=0, why=None):
    lines = [f"[Scheduled check-in #{number}] It is your turn to look at this project: {project['name']}.",
             "Goal: " + project["goal"]]
    if data.get("note"):
        lines.append("What to watch: " + data["note"])
    if why:
        lines.append(why)
    if missed:
        lines.append("%d earlier check-in(s) were not picked up and are replaced by this one." % missed)
    lines += ["Do, in this order: read duet_inbox; look at what actually changed since the last check (results, logs, the latest checkpoint); "
              "move the work forward or fix what is stuck; leave the peer one batched handoff; save a checkpoint.",
              "If nothing needs doing, say so in one line and end the turn; do not invent work.",
              "Wake the peer now only if it must act before its own next check-in."]
    if data.get("mode") == "adaptive":
        low, high = bounds(data)
        if data.get("around_minutes") and not data.get("around_strict"):
            lines.append("The user's rough scale is a check about every %g minutes. It tells you how much they want to spend, "
                         "not a rule: go faster or much slower than that whenever the task calls for it." % data["around_minutes"])
        lines.append("Before you finish, call duet_schedule_next with who should check next, in how many minutes (%d to %d), and why: "
                     "sooner while things are moving or fragile, later while a long run is simply in progress. "
                     "If you do not, the other side checks in %d minutes." % (round(low), round(high), data["pattern"][0][1]))
    lines.append("When the goal is met and reviewed, end everything with duet_finish.")
    return "\n".join(lines)


def prepare(root, db, data, now, watch_fp=None):
    """Decide what this due check-in is, write its mail, and move the schedule on. No transport here.
    Returns (history entry, message id to wake or None). The timer passes watch_fp, computed before
    its write transaction, so scanning files never holds the mailbox."""
    project = db.execute("SELECT * FROM project WHERE singleton=1").fetchone()
    who = data["next_who"]
    recipient = (data.get("participants") or {}).get(who) or participant(db, who)
    if recipient and not db.execute("SELECT 1 FROM peers WHERE name=?", (recipient,)).fetchone():
        recipient = participant(db, who)
    data["ticks"] = int(data.get("ticks", 0)) + 1
    entry = {"at": now, "who": who, "tick": data["ticks"]}
    step_minutes = data["pattern"][data["index"] % len(data["pattern"])][1]
    paused = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_run'").fetchone():
        run = db.execute("SELECT state FROM native_run").fetchone()
        if run and run["state"] in ("paused", "needs_user"):
            paused = run["state"]
    message_id, why = None, None
    if not recipient:
        entry["result"] = "skipped: no conversation of that side has joined"
    elif paused:
        entry["result"] = "skipped: the collaboration is " + paused
    else:
        seen = db.execute("SELECT seen FROM peers WHERE name=?", (recipient,)).fetchone()["seen"] or 0
        changed, current = None, None
        if data.get("watch"):
            current = watch_fp or fingerprint(root, data["watch"])
            changed = current != data.get("watch_fp")
            quiet_for = now - float(data.get("last_real_check") or data["started"])
            if changed:
                why = "The watched files changed since the last check: " + ", ".join(data["watch"])
            elif quiet_for >= data.get("max_quiet", 4 * 3600):
                why = "The watched files have not changed for %d minutes; check that the run is still alive." % round(quiet_for / 60)
        if now - seen < min(15, step_minutes / 3) * 60:
            # It used the bridge minutes ago, so it is in the middle of a turn. A wake would only queue a second one.
            entry["result"] = "skipped: that conversation was active %d minutes ago" % round((now - seen) / 60)
        elif data.get("watch") and not why:
            # Nothing moved and it has not been quiet for long: a whole model turn would only confirm that.
            entry["result"] = "skipped: nothing changed in the watched files"
        else:
            number = int(data.get("checks_done", 0)) + 1
            # One open check-in per side: an older one that was never picked up is replaced, not piled up.
            replaced = db.execute("UPDATE messages SET ack=? WHERE sender='scheduler' AND recipient=? AND kind='task' AND ack IS NULL",
                                  (now, recipient)).rowcount
            streak = data.setdefault("unanswered", {})
            streak[recipient] = streak.get(recipient, 0) + 1 if replaced else 0   # how many in a row went unread
            missed = streak[recipient]
            cur = db.execute("INSERT INTO messages(sender,recipient,kind,body,reply_to,dedupe,created) VALUES(?,?,?,?,?,?,?)",
                             ("scheduler", recipient, "task", check_in_text(data, number, project, missed, why), None,
                              "schedule-%s-%d" % (data.get("id") or int(data["started"]), data["ticks"]), now))
            message_id = cur.lastrowid
            data["checks_done"] = number
            data["last_real_check"] = now
            if current is not None:
                # The baseline moves only when a check-in was really handed over, so a change seen
                # while that side was busy is still pending at the next check.
                data["watch_fp"] = current
            entry.update(number=number, result="check-in left in the mailbox")
    if message_id is None:
        data["skipped"] = int(data.get("skipped", 0)) + 1
    data["history"] = (data.get("history") or [])[-19:] + [entry]
    data["index"] = (data["index"] + 1) % len(data["pattern"])
    next_who, next_minutes = data["pattern"][data["index"]]
    if data["mode"] == "adaptive":
        # Until the checking side says otherwise, the other side looks next after the fallback interval.
        next_who, data["awaiting_next"] = other(who), True
    data.update(next_who=next_who, next_due=now + next_minutes * 60, next_reason=None, rev=int(data.get("rev", 0)) + 1)
    return entry, message_id


def deliver(root, db, message_id, schedule_id=None):
    """Wake the conversation the check-in is addressed to. Returns a short result for the history.
    The wake itself re-checks, inside its own transaction, that this schedule is still active."""
    project = db.execute("SELECT * FROM project WHERE singleton=1").fetchone()
    try:
        import native
        woke = native.operate(duet, "native_wake", {"name": "scheduler", "message_id": message_id, "_system": True,
                                                    "_schedule_id": schedule_id},
                              root, root / ".agent-duet", db, project)
        db.commit()
        return "woken: " + str(woke.get("status")) + ((" (" + str(woke["error"])[:160] + ")") if woke.get("error") else "")
    except Exception as exc:
        db.rollback()
        return "left in the mailbox; wake failed: " + str(exc)[:200]


def fire(root, db, data, now):
    """One due check-in from start to end, on an in-memory copy (the timer uses the same three steps)."""
    entry, message_id = prepare(root, db, data, now)
    db.commit()
    if message_id is not None:
        entry["result"] = deliver(root, db, message_id, data.get("id"))
    return entry


def runner(root):
    me = os.getpid()
    while True:
        root_path, state, db = duet.connect(root)
        try:
            glance = load(db)
            watch_fp = None
            if glance and glance.get("state") == "active" and glance.get("watch") and glance["next_due"] <= time.time():
                watch_fp = fingerprint(root_path, glance["watch"])  # outside the write transaction
            data = write(db)
            if not data or data.get("state") != "active":
                db.commit()
                return
            if data.get("pid") not in (None, me) and alive(data):
                db.commit()
                return  # another timer owns this schedule
            if data.get("pid") != me:
                data.update(pid=me, birth=duet.process_birth(me))
                save(db, data)
            now = time.time()
            longest = max(minutes for _, minutes in data["pattern"]) * 60
            if data["next_due"] - now > max(longest, MAX_MINUTES * 60) + 3600:
                data["next_due"] = now + longest  # the clock was set back; do not wait for the old time
                save(db, data)
            if now >= data["until"] or data["checks_done"] >= data["max_checks"]:
                data.update(state="finished", ended=now, rev=int(data.get("rev", 0)) + 1)
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
            message_id, entry = None, None
            if wait <= 0:
                # After sleep or a long pause several checks may be overdue: do one, then count from now.
                # The decision, the mail and the next due time are saved in this one transaction.
                entry, message_id = prepare(root_path, db, data, now, watch_fp)
                save(db, data)
                wait = 5
            db.commit()
            if message_id is not None:
                result = deliver(root_path, db, message_id, data.get("id"))
                # Only the outcome of this check is added afterwards, to whatever the schedule is now:
                # a schedule that was stopped or changed during the wake stays exactly as it was left.
                current = write(db)
                if current and current.get("id") == data.get("id"):
                    for item in current.get("history", []):
                        if item.get("tick") == entry["tick"]:
                            item["result"] = result
                    save(db, current)
                db.commit()
        finally:
            db.close()
        time.sleep(max(1, min(60, wait)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    runner(args.root)
