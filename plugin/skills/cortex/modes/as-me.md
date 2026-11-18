# Writing as the user (`cortex_as_me`)
Use this before writing anything the user will send as themself: an email, a message, a reply, a cover letter. Cortex never sends anything; the user does.
1. Call `cortex_as_me(task, audience, act?, channel?)`. `audience` is a tag (`professor`, `friend`, `recruiter`) or a person id `people/<slug>`; `task` is one line of at most 300 characters. `act`: request, reply, thanks, apology, update, decline, intro, other. `channel`: email, chat, sms, social, ai, other.
2. Write only from `self` and `voice`:
   - `self.lines` are the user's own lines: use them as facts. They hold only pages this audience may see; `self.withheld` counts the rest. Never guess at what was withheld.
   - `voice.card` holds the user's style rules for this audience and for everyone. `voice.exemplars` are messages the user wrote: match their tone, length, greeting and sign-off; never copy their content.
   - `person` is a private page: use it for this message only.
3. Follow `boundaries`: `never` is absolute; `ask` means ask the user first; `alone` is what you may do without asking.
4. Ask the user about anything in `unknowns` instead of guessing. If `self.unconfirmed` is large, suggest confirming lines in `/cortex reflect`.
5. Show the draft, then log it: `cortex_log(kind="draft", payload={text, audience, act, channel})`, and keep the returned `id`. Log every draft.
6. If the user shows what they actually sent, log `cortex_log(kind="sent", payload={draft: <id>, text})`.
7. When the user pastes a message they wrote themself, save it with `cortex_log(kind="voice_sample", payload={text, audience, act, channel})` and tell them in one line. Never save your drafts or other people's text as a sample; text the user typed to an AI is channel `ai`.
8. When the user states a style rule ("to professors I always sign Regards"), propose it with `cortex_patch(id="me/voice", op="append_to_section", target=<audience or Everyone>, text)`; use `user_confirmed=true` only after they agree.

If `cortex_as_me` or a draft returns `killed`, the user has switched off acting as them: tell them and stop.
