# Probe (`/cortex probe`): does Cortex know the learner better than a stranger?
Only the user starts this. 20 questions over at least 5 areas.
1. `cortex_start(mode="probe")`. Tell the learner in one line: you will guess their answer twice, once from Cortex and once as for a typical university student, and they will judge both.
2. Pick 20 questions from the bank below (at least 5 areas, mixed order). You may reword one to fit the learner; never ask about other people's private details and nothing clinical.
3. For each question:
   1. `cortex_context(topic=<question>)` and, if useful, `cortex_recall`.
   2. Write two short guesses: **twin** (from what Cortex says about the learner) and **stranger**: for a bank question asked as written, copy its `Stranger:` answer verbatim (written once without any vault, so it cannot anchor on the learner); for a reworded or other question write it for a typical university student, ignoring the vault. Set `same` to true if they give the same answer. Note in `claims` the 1–3 claim lines the twin guess relied on, as served.
   3. `cortex_log(kind="forecast", payload={question, area, twin, stranger, same, claims})` and keep the returned `id`. **Do not show the guesses yet.** (The tool call is visible in some apps; ask the learner not to expand it.)
   4. Ask the question. Wait for the learner's answer.
   5. Show both guesses. Ask whether each was right: yes, partly or no.
   6. `cortex_log(kind="settle", payload={forecast: id, answer, twin_hit, stranger_hit})`.
4. At the end, call `cortex_reflect` and show `probes` (overall and per area: n, twin, stranger, uplift). Say plainly whether Cortex beat the stranger (uplift above 0), tied, or lost. If it lost in an area, suggest what to record there. Where `pull` is high, the twin's misses were typical-student answers: Cortex should ask there, not assume.
The same steps (twin, stranger, `same`, `claims`, then ask and settle with `stranger_hit`) apply to a tell-me tap from the home card, with `need: <id>` in the forecast.
5. `cortex_end(summary)`.
If the learner stops early, settle the last open forecast or leave it; never settle without their verdict.

## Question bank
Areas: values, decisions, preferences, habits, opinions, work, learning, social, taste, other.
- values: What matters more to you in a job, pay or interesting work? Stranger: Interesting work, if the pay is decent.
- values: Would you rather be respected or liked? Stranger: Respected.
- values: Is it fine to bend a rule that seems pointless? Stranger: Yes, if nobody gets hurt.
- decisions: Do you decide fast and adjust, or research first? Stranger: Research a bit first, then decide.
- decisions: When two options look equal, how do you break the tie? Stranger: Go with my gut.
- decisions: Would you take a sure small win or a risky big one? Stranger: The sure small win.
- preferences: Short answers or detailed explanations? Stranger: Short answers, detail if I ask.
- preferences: Learn from a video, a text or by doing? Stranger: A video.
- preferences: Morning or night for hard work? Stranger: Night.
- habits: What do you do first when you sit down to work? Stranger: Check my phone.
- habits: How do you usually handle a deadline: early, steady or last minute? Stranger: Last minute.
- habits: What do you do when you get stuck for 30 minutes? Stranger: Look it up online or ask an AI.
- opinions: Is AI help making students better or worse learners? Stranger: Depends how you use it; worse if it does the work for you.
- opinions: Are grades a good measure of skill? Stranger: No, not really.
- opinions: Tabs or spaces, and how much do you care? Stranger: No idea, and I don't care.
- work: Which do you enjoy more, building new things or fixing broken ones? Stranger: Building new things.
- work: Solo project or team project? Stranger: Solo project.
- work: What makes a task feel worth doing to you? Stranger: When it is useful to someone or counts for something.
- learning: Which subject do you find easiest to pick up? Stranger: Subjects in my own major.
- learning: When you get a question wrong, do you want the answer or a hint? Stranger: A hint first.
- learning: How do you know you have really learned something? Stranger: When I can explain it to someone else.
- social: Do you recharge alone or with people? Stranger: Alone.
- social: In a group, do you lead, support or work alone? Stranger: Support.
- social: Text, call or meet in person? Stranger: Text.
- taste: What kind of music do you put on while working? Stranger: Chill instrumental or lo-fi.
- taste: Clean minimal design or rich detailed design? Stranger: Clean minimal design.
- taste: A book, a show or a game for a free evening? Stranger: A show.
- other: What would you do with a free Saturday? Stranger: Sleep in, then see friends.
- other: Which tool or app could you not live without? Stranger: My messaging app.
- other: What is one thing you want to get better at this year? Stranger: Managing my time.
