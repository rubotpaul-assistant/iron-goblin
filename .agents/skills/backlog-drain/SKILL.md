---
name: backlog-drain
description: "Carry out an authorized Goblin backlog pass with live dependency checks, protected review and CI gates, and a resumable checkpoint."
---

# Goblin backlog drain

Use this workflow when the user authorizes a backlog pass. Read the repo's
`AGENTS.md`, its pilot work order, and [Iron #15](https://github.com/rubotpaul-assistant/iron-goblin/issues/15).
Macro uses its own repository, instructions and [pilot work order](https://github.com/rubotpaul-assistant/macro-goblin/issues/5).
Honor the scope and approval boundaries in the current request.

## Select an item

1. Inspect the working tree, current GitHub issues, existing branches and PRs,
   reviews, and current checks. Use an isolated worktree if the checkout has
   unrelated changes.
2. Check dependencies against the merged base and read comments or linked work
   for recent claims. Resume an in-scope PR when it carries the needed change.
   Skip equivalent shipped work and active claims. Pick the smallest ready
   unclaimed item allowed by the current approvals.
3. Before moving to the next item, repeat the live lookup. If a pending PR
   blocks descendants, proceed with an independent ready item.

## Validate and land

1. Exercise the changed public entry point with disposable data. Run a focused
   test and the full commands in the repo's `AGENTS.md`, then match the checks
   in its GitHub workflow. Open a focused PR with the issue, commands, proof of
   acceptance, and any remaining verification conditions.
2. Read the PR head SHA, all required workflow and status results, required
   reviews, and review threads. A workflow for a previous head provides no
   merge evidence. Diagnose a failure from the job output before rerunning.
   Fix actionable review feedback, explain the change in the existing thread,
   and rerun the relevant checks. Wait for any required new review.
3. Merge only with explicit authority, successful required checks and reviews
   on the current head, and resolved blocking feedback. Re-read the PR head
   immediately before submitting the merge. Pass the expected head SHA to
   GitHub and use the ordinary protected merge path. If GitHub blocks the
   merge, record the specific requirement and continue independent work.
4. Verify the merged PR and linked issue state. If the PR implements only part
   of an issue, leave it open with the remaining acceptance evidence named.

## Pause and resume

On pause or handoff, record the live issue or PR links, branch and head SHA,
tested and missing gates, completed items, and the smallest independent ready
item. Keep the checkpoint in the coordinating issue when authorized. A local
checkpoint can include test commands and current unpushed files. Exclude
credentials, tokens and user records. To resume, read the checkpoint, inspect
local Git status, and verify each live issue and PR again before mutating
anything. Honor a stop request immediately and preserve existing work.

This workflow is manually invoked. A recurring job and operations outside the
approved GitHub and fixture scope need their own authorization.

[Dry-run cases](references/dry-run.md)
