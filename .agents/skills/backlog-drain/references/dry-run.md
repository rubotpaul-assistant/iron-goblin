# Backlog drain rehearsal

Run these fictitious snapshots in order. At each step, identify the one
authorized next action from the skill, record it, and compare it with the
expected action. Treat the first and second snapshots as separate sessions.
Use the issue and PR links from a real run when checkpointing actual work.

| Snapshot | GitHub and local state | Expected action |
| --- | --- | --- |
| 1 | Issue A is open with PR 101 at head `a1`. Issue B depends on A. Issue C is independent and unclaimed. Quality failed on `a1`. | Resume PR 101 and inspect the failing job. B stays blocked. C can advance if the failure requires waiting or outside access. |
| 2 | The previous session recorded the failing head. After a local fix, PR 101 is at `a2`. Quality passed on `a2`. A required reviewer requests changes on that head. | Read the new review and address its blocking finding. Keep B blocked. |
| 3 | After review changes, PR 101 reaches `a3`. The thread is resolved. Quality and required review evidence exist only for `a2`. | Re-run quality and obtain current-head review evidence. Do not merge. |
| 4 | Quality and required review pass at `a3`, but GitHub reports `a4` when attempting an expected-`a3` merge. | Re-read the PR. Discard stale merge evidence and verify `a4`. Keep B blocked. |
| 5 | GitHub later shows that a maintainer merged PR 101 at `a4`, closed A, and completed C in an equivalent PR. There is no active claim for B. | Verify both changes in the base. Start B. Skip A and C without opening duplicate issues or PRs. |
| 6 | B has uncommitted scoped changes when the operator pauses. | Record B, branch, local changes and verification still due. Resume only after verifying current issues, open PRs and the working tree. |

At this writing, the Iron drain exercised live exact-head checks on #27, #28
and #29, moved to independent items during reviews, and verified automatic
closure of #7 after merge. The synthetic steps cover blocked CI, blocking
reviews, a changed head and another maintainer completing work during a handoff.
