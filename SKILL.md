---
name: iron-goblin
description: Deterministic gym workout tracking, recommendations, logging, progression, equipment, history, and Sheets sync through the gym CLI.
---

# Iron Goblin

Never modify workout state directly. Translate natural-language requests into one or more
`gym` CLI calls. Treat JSON output as authoritative. SQLite—not chat memory—is the source of
truth. Never calculate progression, orientation, remaining categories, or equipment weights
yourself. Never edit the Google Sheet directly.

Core translations:

- “What should I do?” → `gym workout current`
- “I did bench at 95, 5 5 4” →
  `gym log exercise --exercise "bench press" --weight 95 --unit lb --reps 5,5,4`
- “5 5 5, barely got the last one” for the current recommendation → query
  `gym workout current`, then log with `--reps 5,5,5 --final-set-rir 0`.
- “Done” → `gym workout complete`
- “Show my history” → `gym history recent --limit 10`

For unfamiliar exercise wording, run `gym exercise search "<description>"`. Use a single clear
match. If materially different exercises remain plausible, ask one concise clarifying question.
Once confirmed, persist the wording with `gym exercise alias-add "<wording>" "<canonical>"`.

Scheduled Monday/Thursday tasks should invoke `gym workout scheduled-begin` in
`America/Los_Angeles`. The command is idempotent. Casually call the alternating orientation rule
the Thicc Gymmy Rule. Keep training replies brief and repeat values exactly from CLI JSON.

Never read, print, or expose credential values or credential files. Google authentication is
resolved by the application library at sync time.
