# Check-in schedules for long tasks

**Default: no schedule.** Pairing only connects the two conversations. Nothing is scheduled, no timer runs and no turn is spent until the user asks for a schedule. Never set one on your own initiative; suggest it in one line if a task will clearly run for hours.

When the user asks the two of you to keep watching a long task, set the schedule with `duet_schedule`. The bridge is the alarm clock: at each due time it leaves a check-in in the mailbox and wakes the conversation whose turn it is. Neither side has to keep a recurring automation of its own; one call from either side sets it up for both. The schedule itself still follows whatever rules your environment sets for background work.

## Choosing the schedule

| The user says | Call |
|---|---|
| 輪流、每小時換一邊 | `preset=alternate` |
| Codex 多一點（Claude 一次、Codex 兩次） | `preset=codex-heavy` |
| Claude 多一點 | `preset=claude-heavy` |
| 密集一點／每半小時 | `preset=quick` |
| 寬鬆一點／每兩小時 | `preset=relaxed` |
| 過夜、長時間跑 | `preset=overnight` |
| a different interval | any preset plus `interval_minutes` |
| their own sequence | `mode=custom`, `pattern="claude:60,codex:60,codex:90"` |
| 你們自己決定 | `mode=adaptive` |
| 你們自己決定，大約每 N 分鐘 | `mode=adaptive`, `around_minutes=N` |
| …，不要超出這個範圍 | add `strict=true` |
| 有變化才叫醒／盯著某個檔案 | add `watch="runs/train.log,results/*.json"` to any of the above |

If the user gave no preference, choose by the task: `adaptive` when the pace will change (a setup phase, then a long run), a fixed preset when it is steady, and add `watch` whenever progress shows up in a file. Say which you chose and why in one line. Use `note` for what to watch and `hours` for how long (default 24, at most 168; set it again to go on).

A custom or preset cycle starts with the side that did not set it up, because the side that set it up is at work right now. The order of the cycle is otherwise kept.

## The rough scale (`around_minutes`)

In adaptive mode the user can say roughly how often they want a look. That number tells you how much they are willing to spend.

- Without `strict` it is a hint. Decide freely after every check: faster while things are moving or fragile, much slower while a long run is simply in progress, anywhere from 10 to 720 minutes.
- With `strict=true` every check stays between half and double the scale. A value outside is adjusted, and the result says so.

## Waking only when something changed (`watch`)

`watch` takes up to ten project-relative files or globs. At a due time the bridge compares their names, sizes and modification times with the last check. If nothing changed, nobody is woken and no model turn is spent; the check is recorded as skipped. A change wakes the side whose turn it is and tells it which files to look at. Silence longer than `max_quiet_minutes` (default four times the longest step) also wakes it, because a run that stopped writing may have died.

For a long training run this is the cheapest setting by far: hours of steady progress cost nothing.

## Rules for both sides

- **The schedule is a floor, not a fence.** It is only the longest the task goes unwatched. Either of you wakes the other at any time for something urgent (`duet_send wake=true`), exactly as without a schedule.
- **At a check-in:** read the inbox, look at what actually changed (results, logs, the latest checkpoint), move the work forward or fix what is stuck, leave one batched handoff, save a checkpoint. If nothing needs doing, say so in one line and end the turn. A check-in is not a reason to invent work.
- **Adaptive:** before ending the turn call `duet_schedule_next` with who checks next, in how many minutes, and why. If you forget, the other side checks after the default interval, so the schedule never dies.
- **When the goal is met and reviewed, call `duet_finish`.** Do not leave a schedule running on a finished project.

## What it costs

The timer is one small background process per project with an active schedule. Waiting uses no model. Each check-in that actually wakes a side costs one turn of that original conversation, on whatever model the user selected for it in the app, plus a short courier call when the side is Claude. An hourly schedule is about 24 turns a day; on a flagship conversation that is a real amount, so prefer `watch`, a longer interval or adaptive mode for long steady runs, and tell the user when their conversation is on a flagship model.

Checks that are skipped cost nothing and do not count toward `max_checks`:

- the side whose turn it is used the bridge a few minutes ago (it is in the middle of a turn);
- the collaboration is paused;
- nothing changed in the watched files;
- after the computer slept, only one check runs and the next is counted from then.

Only one check-in per side stays open. If a side did not pick the previous one up, the new one replaces it and says how many in a row went unread.

## Lifecycle

- A scheduled wake uses the same transport and the same wake limit as any other wake, and appears in `duet_native_status`.
- If the side cannot be woken (conversation closed, CLI signed out), the check-in stays in its mailbox, the reason goes into the schedule history, and the schedule goes on. A signed-out CLI also triggers the sign-in notification.
- The timer is restarted by the next `duet_inbox`, `duet_send`, `duet_status`, `duet_checkpoint` or `duet_schedule_status` call if it died while the schedule is active. After a reboot that happens as soon as either conversation is used again.
- It ends when `hours` have passed, when `max_checks` real check-ins were done, with `duet_schedule mode=off`, or with `duet_finish`. At its natural end it leaves the side that set it a note.
- Setting a schedule on a collaboration that was paused or finished makes wakeups active again; that is what asking for a schedule means. It is refused while a background (autopilot) run owns the project.
- `duet_schedule_status` shows the cycle, the next check, how many were skipped and the recent ones.
