# Sweep (`/cortex sweep`): a short, cold, mixed re-check
Only offer it (for example when much is due or many results are only imported); never start it unasked. About 10 minutes.
1. `cortex_start(mode="sweep")`. Its `today` lists up to 10 concepts, already mixed across domains. Keep that order.
2. Build **one** quiz widget (see `modes/grading.md`) with one question per `today` item:
   - one line each, closed-book and cold: no hints, no options that give the answer away, no teaching;
   - a "how sure? 1–4" control under each question (1 = guessing … 4 = certain);
   - a fixed `item_type` per question (most are `recall`).
3. Say nothing about the answers until the learner submits the whole widget.
4. Grade it as **one batch**: `cortex_grade(items=[...])`, one item per question with its `concept`, question, answer, verdict, `confidence` and `item_type`.
5. Then, for each miss only: a short explanation (2–3 lines), and `cortex_log(kind="taught", payload={concept, summary})`. Correct answers get no extra teaching.
6. Show the batch `display`, then `cortex_end(summary)`.
If the learner stops early, grade only the questions they answered.
