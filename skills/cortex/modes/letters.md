# Letters for a tired you
A letter is the learner's own words, saved for a bad day. Cortex shows one on the start card when its trigger holds.

**Offering one**
- At the end of a session that went well, offer once: "Anything you'd tell a tired you?" Skipping is fine.
- Never offer on a bad night: late, low energy, or a run of wrong answers.

**Writing it**
1. Keep their words. Don't rewrite or polish them (5–300 characters).
2. Propose when it should show, from what they said, and confirm it with them before logging. Triggers, joined by `+` when all must hold:
   - `late`: 23:00–04:00;
   - `energy_low:n`: the last n journal energy values were all 2 or less (n 1–7);
   - `exam_within:n`: an exam is within n days (n 1–14);
   - `gap_days:n`: n or more days since the last activity (n 2–60);
   - `miss_streak:n`: the last n graded answers were wrong (n 2–10).
3. `cortex_log(kind="letter", payload={text, when})`. It goes to `me/letters.md`.
4. Never store self-harm text. If it comes up, gently point them to real help (a trusted person, a local crisis line) instead.

**When one is shown**
- The card has a line `A note from you (written …)` and the result has `letter`. Don't add numbers or commentary.
- Before the session ends, ask once whether it helped. Then call `cortex_log(kind="letter_feedback", payload={letter: <id>, verdict: helped|not|retire})`. `retire` moves it to `## Past`.
