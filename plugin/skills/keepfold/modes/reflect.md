# Reflect (weekly)
Call `keepfold_reflect()` and walk the learner through it. By default keep it to about five minutes: steps 1 and 1b, then ask once "More (clusters, lint, portrait)?". `/keepfold reflect full` walks every step.
1. **Unconfirmed "about me" lines:** ask about them together, never one per message. With a multiple-choice question tool, put up to 4 lines per card (yes / no / skip; free text for "sometimes, when…" or an edit); otherwise show a numbered list the user answers like "1 yes 2 no". Split a line that holds two facts. Yes: patch with `user_confirmed=true`. No: `keepfold_patch(id=page, op="reject", target=<the proposed line>)` (it goes to `## Not me`). Skip: leave it. Always allow "none of these, clear them".
1b. **Saved about you unasked (`saved.claims`):** ask about them the same way, 4 per card, and say which came from outside text. Yes: `keepfold_log(kind="reconfirm", payload={claim: <id>, verdict: "yes"})`; it keeps the line as theirs only if this reflect has read no outside text (a source, a raw file, a past chat or a web page), else the line is asked again next time. No: `keepfold_patch(id=page, op="retire", target=<claim id>)`. Mention `saved.changes` in one line; the user can run `keepfold claims --unasked` for the full list with undo steps.
2. **Emerging clusters:** offer to create the suggested structure note.
3. **Fleeting inbox lines older than 7 days:** turn each into a permanent note, merge it into an existing one, or drop it.
4. **The week's learning numbers:** what moved, what's slipping, mistakes.
5. **`keepfold_reflect(action="lint")`:** fix the orphans, broken links and long notes the learner cares about.
6. **Monthly self-portrait:** when `portrait.due`, follow the portrait step in `modes/second-brain.md`.
7. **Grader spot-check (`spot_check`):** show each row (question, answer, verdict) and ask "Keep or flip?" in one card. Log `keepfold_log(kind="spot_check", payload={event_id, verdict: correct|partial|wrong})`; a different verdict voids the grade and regrades it. The agreement rate shows in `keepfold eval learning`.
End with `keepfold_end`.
