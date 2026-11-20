# Help (`/cortex help`)
No session: do not call `cortex_start`. `/cortex help <word>` (for example `help study`, `help terminal`) shows only that section.

1. **Greet them.** One warm line with their name (from prime or the brief) and the time of day, for example "Good evening, Sam!" Then: "I'm Cortex, your second brain. I remember what you study, what you get wrong and who you are, and I plan what to revise next."
2. **Show the reference below**, as written. It is the full list; don't drop rows.
3. **Close** by asking what they would like to do, and suggest three things to try: `/cortex learn`, `/cortex sweep`, and `/cortex short` if it is late.

---

## Study
| Command | What it does |
|---|---|
| `/cortex` | Home: a short map of what's due, what's coming and what's waiting, then it asks what you want |
| `/cortex learn` | Today's plan: ask first, teach the gap, re-test. Add a topic to focus: `/cortex learn dbms` |
| `/cortex review` | Only what is due for a refresh |
| `/cortex short` | Tired? At most 5 small steps, your confident misses first |
| `/cortex sweep` | 10 mixed cold questions with "how sure? 1–4"; explanations at the end |
| `/cortex mock` | A mock interview (coding, aptitude, HR, project rounds) with a debrief |
| `/cortex mock bank` | Drafts STAR stories from your projects; saves one only after you approve it |
| `/cortex log` | Records a result you report ("4/5 on the TCS aptitude test") |
| `/cortex week` | This week's numbers |

## Second brain
| Command | What it does |
|---|---|
| `/cortex about <topic>` (or `/cortex <topic>`) | What you know and have about a topic |
| "what do I know about X?" | Search your whole vault by meaning; no command needed |
| `/cortex capture` | Saves a source (article, PDF, video) and the ideas worth keeping |
| `/cortex project <name>` | One project: status, decisions, next steps |
| `/cortex reflect` | Weekly review: confirm or drop what's waiting about you, idea clusters, spot-checks of how a chat graded you |
| `/cortex map` | Opens the brain and lists in the Cortex app; Obsidian's graph is Ctrl+G |

## Chats
| Command | What it does |
|---|---|
| `/cortex save` | Saves what's worth keeping from this chat; you approve the list first |
| `/cortex history` | Saves older Claude Code chats, a few at a time |
| `/cortex harvest` | Distils your imported Gemini, Perplexity and Claude chats, about 10 minutes a night. It reads only your own messages and writes "What I explored" lines on topic pages. |

## About you
| Command | What it does |
|---|---|
| "save this about me" | Saves a preference or personality line, after your yes |
| `/cortex help` | This list |

## Without any command
In every chat Cortex already knows you from your first message, saves what's durable (facts, sources, decisions, quiz answers) and tells you when it wrote something. Each chat's changes can be undone in one step.

## Terminal (`cortex …` in any terminal; from a source checkout, prefix `uv run`)
| Command | What it does |
|---|---|
| `today` / `brief` | Today's plan / brief, teaching contract and Today |
| `dashboard` | Opens the Cortex app's home (starts the app if needed) |
| `recall "query"` | Searches the vault by meaning and keywords |
| `log assessment\|result\|solved\|taught` | Records results by hand |
| `reflect` | The weekly review pack |
| `claims` (`--summary`, `--unasked`) | Lines about you: all, how well Cortex knows you per page, or saved without asking |
| `needs` | Facts a chat wished it had |
| `chats` / `import-chats` | Past chats / import a claude.ai export |
| `digest` | Review chats imported since the last digest; each topic you confirm is a touch, never a grade |
| `harvest status` | Harvest progress and undo commands |
| `export context` | A paste-ready "about me" file for ChatGPT, Gemini or Cursor |
| `undo <session>` / `void` | Revert one chat's changes / cancel one record |
| `backup` | Zip the whole vault |
| `doctor` / `lint` / `relink` | Health check / vault problems / fix Obsidian links |
| `rollups` (`--since YYYY-MM`) | Write the week, month and year summary pages (code only, no model) |
| `eval` (`all`, `learning`, `recall`) | How well search and the study forecasts are doing |
| `topics` (`status`, `attach`, `build`, `eval`, `split`) | The topic tree |
| `concept` (`add`, `alias`, `merge`, `duplicates`) | Edit the concept list |
| `catalog sync` / `replay` | Keep the catalog in step / recompute all states |
| `model` (`download`, `build`, `status`) | The local search model |
| `browse` (`scan`, `import`, `status`, `pair`, `serve`) / `llm status` | Your browsing as day pages, filtered on this laptop / the optional local LLM |
| `exclude add\|list\|remove` | Folders where Cortex stays off (e.g. a work repo) |
| `voice import` / `forget` / `kill` / `access` | Your writing samples / delete a person's page / emergency stop / what was read as you |
| `session close\|sweep` | Close leftover sessions |
| `setup` / `init` / `sample remove` / `mcp` | Install, create a vault, remove the sample, run the server |

`cortex --help` prints every command; `docs/GUIDE.md` explains each in detail.
