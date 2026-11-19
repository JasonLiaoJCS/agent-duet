# Check-in schedules for long tasks

When the user asks the two of you to keep watching a long task (training, a multi-day build, anything "回來看一下"), set a schedule with `duet_schedule` instead of creating host automations. The bridge is the alarm clock: at each due time it leaves a check-in in the mailbox and wakes the conversation whose turn it is. Neither side needs a scheduler of its own, so a host rule against recurring automations on one side (Codex has had one for goal-pursuing loops) does not matter. One call from either side sets it up for both.

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
| 你們自己決定／依難度安排 | `mode=adaptive` |

If the user gave no preference, choose for them by the task: `adaptive` when the pace will change (a setup phase, then a long run), a fixed preset when it is steady. Say which you chose and why in one line. Use `note` to record what to watch, and `hours` for how long (default 24, at most 168; set it again to go on).

- **The schedule is a floor, not a fence.** It is only the longest the task goes unwatched. Either of you wakes the other at any time for something urgent (`duet_send wake=true`), exactly as without a schedule.
- **At a check-in:** read the inbox, look at what actually changed (results, logs, the latest checkpoint), move the work forward or fix what is stuck, leave one batched handoff, save a checkpoint. If nothing needs doing, say so in one line and end the turn. A check-in is not a reason to invent work.
- **Adaptive:** before ending the turn call `duet_schedule_next` with who checks next, in how many minutes (10–720), and why: sooner while things are moving or fragile, much later while a long run is simply in progress. If you forget, the other side checks after the default interval, so the schedule never dies.
- **It saves turns by itself:** a side that used the bridge minutes ago is not woken again, a paused collaboration is skipped, and after the computer slept only one check runs. The timer is a small background process; any later bridge call restarts it if it is gone, so after a reboot the user only needs to say 繼續.
- `duet_schedule_status` shows the cycle, the next check and the recent ones.

## What the timer does and does not do

- It is one small background process per project with an active schedule. It sleeps between checks and uses no model.
- A scheduled wake goes through the same transport and the same rate limits as any other wake, and is recorded in `duet_native_status`.
- If the side whose turn it is cannot be woken (conversation closed, CLI signed out), the check-in stays in its mailbox, the reason is recorded in the schedule history, and the schedule goes on. A signed-out CLI also triggers the sign-in notification.
- It ends when `hours` have passed, when `max_checks` is reached, when either side calls `duet_schedule mode=off`, or with `duet_finish`. At its natural end it leaves the side that set it a note.
- Setting a schedule on a collaboration that was paused or finished makes wakeups active again; that is what asking for a schedule means.
