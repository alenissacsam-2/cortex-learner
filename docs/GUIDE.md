# Cortex: a user guide

Cortex is your second brain. It is a folder of notes (your vault, also an Obsidian vault) plus a small program that every Claude chat can read from and write to.

It has two jobs:
- **Remember you:** what you know, what you work on, what you get wrong, your projects, decisions and preferences.
- **Plan your study, if you study:** what to revise today, what you're forgetting, what an exam needs.

Cortex acts only when a chat (or you, in the app or a terminal) asks it to. The one exception is browsing: after your first browsing import, the app and the receiver import new history by themselves every few hours (section 6).

---

## 1. What happens without you doing anything

In every new Claude Code chat:
- **Prime.** Your first message is matched against your vault, and the chat starts already knowing your profile, your teaching rules, the preferences that fit the message, and a few related pages. A greeting or a plain "ok thanks" does not count as the first message: the prime waits for your first real one. When a later message turns to a new topic, the chat also gets the preference lines and the playbook that fit it, if it has not seen them yet. Lines you put in `me/core.md` (one `- ` line each, under its `## Core` heading) are pinned: every chat gets them first and word for word, up to 500 characters in all; a line that does not fit is left out whole. Only lines you write or confirm count there, never ones an assistant added.
- **What it can and cannot see.** Searches tell the chat how many pages they looked through and what they skip (private pages and private browsing), and the first context names your sources and their dates, so "not found" reads as "not recorded in Cortex", never as "it never happened". Search results show the passage that matched, and a long page can be read part by part. When a long Claude Code chat is compacted, resumed or cleared, the first context is served again.
- **Memory when idle.** After 10 minutes without a search, Cortex lets go of its search models, so it uses about 300 MB less memory while you are away; the first search after that takes about a second longer while they load again.
- **Recording.** The chat saves durable things as they come up: a fact about you, a source you shared, an insight, a real decision, a quiz answer. It tells you briefly when it wrote something.
- **Asking first.** Anything about who you are (a preference or personality line) is saved only after your yes. In Claude Code you'll get a small "save this line about you?" dialog.
- **Undo.** Each chat's writes are one commit in the vault's git. `cortex undo <session>` reverts it. You can turn git off (see "Vault history in git" in section 9); undo is then off.

The Claude desktop **Chat tab** gets the same tools but no automatic prime, so the chat calls `cortex_prime` itself. That call also starts the chat's log, and the chat logs each later message of yours as it goes, so every Chat-tab chat is saved as well ("Past AI chats saved automatically" below). The desktop **Code tab** runs Claude Code, so it works as above.

**Other AI apps.** Cursor, VS Code (Copilot) and Codex get your pinned core in their rules file when you connect them (not when their profile leaves out the core, and never a line you muted in Agent X-ray), and see only what their **agent profile** allows: by default your core, your coding preferences (your Workflow section included), your environment preferences, your setup page (`me/setup.md`: hardware and software) and your projects, never your teaching contract, profile, journal or people pages. An app Cortex does not know gets your core only. ChatGPT and Gemini on the web have no Cortex tools, so you paste a **context pack** (General, Coding or Writing) at the start of a chat. All three are in the app's **Connect AI apps**; [INSTALL.md](INSTALL.md#other-ai-apps-rules-files-profiles-and-context-packs) has the details.

**Codex CLI.** Connecting Codex also adds Cortex's hooks to `~/.codex/hooks.json`. Your first real message in each Codex chat is primed the way Claude Code's is (within Codex's agent profile), a later message on a new topic brings only the lines Codex has not seen yet, your `//pref:`, `//fact:` and `//forget:` lines are saved at once, and the prime is served again after Codex compacts, resumes or clears the chat. After Codex runs another server's MCP tool, your next Cortex write counts as exposed to outside text. Codex runs a new or changed hook only after you trust it: open `/hooks` in Codex once after connecting (and again after Cortex moves to a new folder). Removing Codex takes out only Cortex's hooks.

**Gemini CLI.** Connecting Gemini CLI also adds Cortex's hooks to `~/.gemini/settings.json`: the same prime on your first real message (`BeforeAgent`), only new lines after that, your `//` lines saved, the prime served again after `/clear` or a resumed chat, and a web fetch, a web search or another server's MCP tool marking outside text. Gemini knows no agent profile by default, so it gets your core only until you give it one. Removing Gemini CLI takes out only Cortex's hooks.

**Cursor** has hooks too, but its prompt hook can only allow or block a message, not add to it, so Cursor still relies on its rules file to call `cortex_prime`.

---

## 2. The `/cortex` commands

Type these in a Claude chat. The word after `/cortex` is the mode; anything after that is the topic.

### Study (placement prep)
| Command | What it does | When to use it |
|---|---|---|
| `/cortex` | Home card: your life map, what's due, what's waiting for you. Then it asks what you want. | Any time; the safe starting point |
| `/cortex learn` | Today's plan in order: things taught but not yet checked, confident misses, then the next steps of your study plans with due refreshes, the subject you studied most recently first, then new topics. Plans have no dates; only tests have. Retrieve first, teach the gap, re-test. | A normal study session |
| `/cortex learn dbms` | The same, focused on one topic | Revising a subject |
| `/cortex review` | Only what is due for a refresh | Short revision |
| `/cortex short` | At most 5 small steps; your confident misses and near-exam topics first | Tired, late night |
| `/cortex sweep` | 10 one-line cold questions mixed across subjects, each with "how sure? 1–4"; explanations at the end | Checking what you really remember |
| `/cortex mock` | A mock interview: coding phases, aptitude, HR and project rounds, then a debrief. Weak topics come back in your plan for 14 days. | Before an OA or interview |
| `/cortex mock bank` | Drafts STAR stories from your projects; saves one only after you approve the exact text | Preparing HR answers |
| `/cortex log` | You report results ("got 4/5 on the TCS aptitude test"); it records them | After a test outside Claude |
| `/cortex week` | This week's numbers, and how often you were right at each "how sure" level (1–4) over all your answers; a level with fewer than 10 answers says "not enough answers yet". The dashboard's Study tab shows the same. It is only a mirror: it never changes your plan. | Weekly check |

A topic you have forgotten 5 times after learning it is a **leech**: the Study tab tags it and says so above Today's plan, because rewriting or splitting its note helps more than drilling it again. A topic's page also shows how long your answers usually take, and the 90-second check shows each answer's time; timing is only shown, never used to grade or schedule.

### Second brain
| Command | What it does |
|---|---|
| `/cortex about <topic>` | Looks up what you know and have about a topic |
| "what do I know about X?" (plain words, no command) | Semantic recall over your whole vault |
| `/cortex capture` | Saves a source you paste or link (article, PDF, video) and the ideas worth keeping |
| `/cortex project <name>` | Works on one project page: status, decisions, next steps |
| `/cortex reflect` | The weekly review: confirm or drop the about-me lines waiting for you, emerging idea clusters, old fleeting notes, 3 spot-checks of how a chat graded you |
| `/cortex map` | Opens the Cortex app's home (Today, readiness, the graph) |

**Playbooks: how you want a kind of task done.** A playbook is an ordinary note with the tag `playbook` (`tags: [playbook]` in its properties), say "Writing a CV bullet" or "Starting a Python repo", with sections `## Steps`, `## Pitfalls` and `## Done when` (suggested, not required). When your message is about that kind of task, every AI app's first Cortex call gets the playbook's title and summary (at most two) and is told to read it and follow it, and to update it when you correct how it does the task. Ask an AI app to save one ("save this as a playbook"), or write it yourself. One an AI wrote reaches no AI app until you edit it yourself or tell the AI it is right, and never while it reads like an instruction to an AI. A private playbook never reaches an AI app; add `scope: coding` (or another scope from the app's **Agent profiles**) to give it only to the apps that read that scope.

### Past chats
| Command | What it does |
|---|---|
| `/cortex save` | Saves what's worth keeping from **this** chat (you approve the list first) |
| `/cortex history` | Saves older Claude Code chats from this laptop, a few at a time |
| `/cortex harvest` | Distils your **imported chats** (Gemini, Perplexity, Claude) into a few good pages, see below |

### About you
| Command | What it does |
|---|---|
| `/cortex help` | Greets you and lists every mode and command. `/cortex help study` or `/cortex help terminal` shows one section. |

**Telling Cortex directly.** Start a line of any chat message with one of these, and Cortex itself (not the AI) saves it, in Claude, Claude Code or a chat you import later:

- `//pref: short answers, no emoji` adds a line to your preferences.
- `//fact: I'm on the payments team` or `//fact: I study at IIT Madras` adds a line to your profile.
- `//forget: tabs for indentation` retires the line about you it clearly matches at once, so AI apps stop getting it, and the Cortex app then removes it from everywhere Cortex keeps it within 10 minutes, or when it next runs (below; this cannot be undone). If no line or several lines match, nothing changes and Review in the app lists the lines to pick from.

They count as your own words. Only your messages are read, never the AI's replies, so a tag the AI writes does nothing. If the chat had read a web page or file first, a `//pref` or `//fact` waits in Review for your Approve instead. Typing the same tag twice on one day saves it once. Cortex refuses a line that reads like an instruction to an AI ("ignore previous instructions…") or hides invisible characters, and saves nothing; in Claude Code the chat is told why, so it can tell you to reword it. A chat Cortex imports as private (health and the like) never adds its tags to your shared profile or preferences.

**Forgetting.** `cortex forget <page or line>` takes a page (`people/asha`, a note's id, a saved chat page) or one line about you (`me/profile^c-ab2cd`, the id at the end of the line) out of everything Cortex keeps: the page or line, its words in the event log (your levels stay the same), session notes and summaries, saved chats, the search index and vectors, and Cortex's working files. Forget on a Review card does the same at once; a //forget that matches one line does it through the Cortex app, within 10 minutes or when the app next runs. A common word such as a page title "Python" is not swept from your other chats; a person's name is, every mention. It asks first and cannot be undone. It then prints a receipt of what it removed and what it cannot remove: old versions in the vault's git history (with the command to remove them yourself), backups, raw exports, what AI apps already received, and your own pages that still mention it. [PRIVACY.md](PRIVACY.md#forgetting-one-thing) has the details.

---

## 3. Harvest, explained

Chats you import from Gemini, Perplexity and Claude are raw transcripts: searchable, but nothing has been learned from them.

`/cortex harvest` turns them into knowledge, about 10 minutes a night:
1. Cortex gives the chat a **bundle**: whole topic hubs (for example "SQL and databases"), about 12 chats at a time, placement topics first. Only **your own messages** are shown.
2. The chat writes short lines under that hub's **`## What I explored`** section, linked to your concept pages, plus at most one note and any decisions or projects it finds.
3. Anything about you goes on one yes/no card that cites the chat.
4. Harvested chats rank lower in search afterwards, so the distilled pages win.

It never edits or deletes an imported chat, and it never counts an old chat as a revision today. `cortex harvest status` shows progress and the undo command for each run.

Is it worth it now? It's not urgent while a test or a deadline is close. Try it on a quiet evening.

---

## 4. Terminal commands worth knowing

Run these in any terminal. From a source checkout, prefix `uv run`.

| Command | What it does |
|---|---|
| `cortex today` | Prints today's plan |
| `cortex app` | The Cortex app in your browser (stops after 30 minutes idle); `--window` opens it in its own window with a tray icon, as the **Cortex** shortcut does on Windows |
| `cortex brief` | Your brief, teaching contract and Today |
| `cortex dashboard` | Opens the Cortex app's home, starting the app if needed, and rewrites `index.md` |
| `cortex recall "query"` | Searches your vault by meaning and keywords |
| `cortex ask "question"` | Answers from your notes, saved AI chats and browsing with your local model, with sources; `--private` adds private notes and private browsing (run it yourself, not through an AI app) |
| `cortex claims --summary` | How well Cortex knows you, per page (and which lines you clicked vs a chat relayed) |
| `cortex claims --unasked` | What chats saved about you without asking, each with its undo |
| `cortex voice measure` | Counts how you write in the sent messages you added with `cortex voice import` (sentence length, questions, greetings and sign-offs, exclamation marks, lowercase, emoji; per audience with at least 8 messages, never what you typed to an AI) and turns the clear ones into short lines; `--propose` puts them in Review, and the ones you approve go to `me/voice.md`, where AI apps writing as you read them |
| `cortex export context` | A paste-ready "about me" file for ChatGPT, Gemini or Cursor |
| `cortex pack general\|coding\|writing` | A short context pack (at most 2,500 characters: your pinned core, then that pack's preferences) to paste at the start of a ChatGPT or Gemini chat; the app's **Connect AI apps** has the same packs with a Copy button |
| `cortex import <file>` | Imports a chat export, notes, a calendar, Google activity, documents, your git projects or reading highlights (below) |
| `cortex watch add <folder>` | Imports a folder's new and changed files by itself while the app runs ([below](#watched-folders-and-zotero)) |
| `cortex chats` | Your past chats, newest first |
| `cortex harvest status` | Harvest progress |
| `cortex undo <session>` | Reverts one chat's changes |
| `cortex doctor` | Health check, including which Claude apps are connected and when each last called Cortex, and per import source the page-length quartiles and the share of pages that are mostly links or numbers only (numbers only, never page text) |
| `cortex lint` / `cortex relink` | Find vault problems / fix the Obsidian links |
| `cortex agent-text` | Every fixed text Cortex shows an AI app, with its size per app |
| `cortex rollups [--since YYYY-MM]` | Writes the week, month and year summary pages in `rollups/` (below). The app also does this every 10 minutes |
| `cortex eval` | How well search and the study forecasts are doing |
| `cortex chat eval` | The Chat test: 60 questions on a made-up vault, scored in code ([below](#the-chat-test)) |
| `cortex backup` | Zips the whole vault, private browsing and its stored visit history included (not chat history; see [PRIVACY.md](PRIVACY.md#your-data)). Keep the zip as private as the vault |
| `cortex exclude add <folder>` | Turns Cortex off in a folder (e.g. a work repo) |
| `cortex kill` | Emergency stop for anything that acts as you |

`cortex --help` lists everything.

### Importing your history

Get the export from the service, then run `cortex import <the .zip, folder or file>`. It takes chat exports (claude.ai, ChatGPT, Gemini, Perplexity), notes (Obsidian, Notion, any folder of .md or .txt), a calendar (.ics), Google activity (Takeout), a folder of documents (.pdf, .docx), a folder of your git projects, reading highlights (Kindle, Readwise, Pocket), sent mail (.mbox) and a folder of voice memos (transcribed on this computer), and works out which one it got. A folder of PDFs that also holds .md notes counts as notes, so give `--source docs` for it. If it can't tell, add `--source` with `claude`, `chatgpt`, `gemini`, `perplexity`, `google-activity`, `obsidian`, `notion`, `notes`, `calendar`, `docs`, `repos`, `highlights`, `mail`, `selfchat`, `location`, `photos`, `spotify` or `zotero`. Notes to yourself and photo folders are never detected: a WhatsApp chat .txt looks like a note, so give `--source selfchat` for it, and `--source photos` for a photo folder. A full Google Takeout that holds activity or a calendar is imported as those; to import its mail or location history, run it again with `--source mail` (on the unzipped .mbox) or `--source location`. `--dry-run` prints the counts and writes nothing.

**Who may read imported notes, calendars, documents, projects and highlights.** Each of these imports has a tier, `--tier private`, `normal` or `skip`. `private` makes every imported page private (only local models read it), `normal` makes normal pages that Claude and the other AI apps you connect can read, and `skip` imports nothing. `--private` is short for `--tier private`. Without `--tier`, `cortex import` in a terminal window asks once per source and shows that source's default (private for sent mail, notes to self, location and voice memos, normal for the others). It remembers your answer in `.cortex/import-tiers.json` in the vault, so the next import of that source does not ask again. `--tier` changes it. When nothing can be asked (a script, or an AI app running the command), the import uses the last answer, or else the default, and the first line it prints says which one. In the app, the Notes, Calendar, Documents, code projects, Reading highlights, Sent mail, Notes to self, Location, Photos and Spotify cards ask the same question before **Import**: the **Who may read these pages** choice starts at your last answer, and the one you pick is remembered. `--tier` is refused for chats and Google activity, as `--private` is: chats are always readable by `/cortex`, and Google activity follows your Browsing settings.

Secrets are redacted on import ([PRIVACY.md](PRIVACY.md#what-cortex-never-captures) lists everything Cortex never captures, and from which sources). Importing a newer chat export again replaces the older copy instead of adding a duplicate; notes and events already imported are skipped. A large ChatGPT or claude.ai export split into several `conversations*.json` files is read whole; a conversation in two of them is kept once, its newest copy. Then run `/cortex` to learn from the chats. Imported notes and calendar months become pages in `resources/` that Claude can read, unless a note's frontmatter marks it private (`private: true`, `yes` or `on`, or a `private` tag) or you chose the private tier ([PRIVACY.md](PRIVACY.md)). An imported note keeps its `aliases:` (up to 20 names), so Cortex finds its page by those names too. `cortex import-chats` still imports chat exports only.

Every import ends with a report line, for example `Obsidian: read 10, imported 6, private 3, duplicate 0, skipped 4 (3 not a note, 1 over 5 MB)`: the items read, imported, made private, already imported (or twice in the export), and skipped, with the reason. An item of 200 characters or more that is not readable text is left out too, and the line ends with, for example, `Skipped 3 as unreadable (2 garbled, 1 link list)`: garbled (over 5% broken characters), mostly symbols (under a quarter letters or digits), link list (most lines just a web address) or repetitive (10 or more lines, under 30% of them different).

### Watched folders and Zotero

`cortex watch add <folder>` watches a folder: while the Cortex app runs, every 10 minutes it imports the folder's new and changed files by itself, as `cortex import` would. `--source` says what the folder holds: `docs` (the default: .pdf, .docx, .md and .txt), `notes`, `repos` (your git projects) or `zotero`, and `--tier` who may read its pages (without it, that source's last answer or its default). A file already read is not read again until its date or size changes, and at most 200 new or changed files are read per folder per pass, the rest on the next. A changed note or document rewrites its page, unless you edited the page. The watch never follows a shortcut or link out of the folder and skips hidden and system files and folders (and `node_modules`, `.git` and the like), files kept only online (OneDrive's online-only files) and your vault; the import's size limits still hold. A watched folder that is gone, such as an unplugged drive, is reported and kept. `cortex watch list` shows each folder and what its last pass did, and `cortex watch remove <folder>` stops watching it (its pages stay). In the app, **Add your history** lists them under **Watched folders**, each with **Stop watching**, and has a field to add one.

**Zotero.** `cortex import --source zotero` imports your Zotero library. Cortex finds it by itself (the data folder set in Zotero's settings, else `Zotero` in your home folder), or give it the data folder (in Zotero: Settings, Advanced, Files and Folders, Show Data Directory). It reads a copy of Zotero's database; if a running Zotero keeps the file from being copied, it says so, and closing Zotero lets it through. Each item in your own library becomes one page: its kind, publication, year and authors, DOI, URL and tags, the abstract, your notes, your PDF highlights with their comments, and the full text of its attached PDF and Word files. Group libraries, the trash, and linked files stored relative to Zotero's base folder are left out, and a watched library notices a change only once Zotero's database changes (a file replaced in its `storage` folder alone is not seen). An item you change in Zotero rewrites its page on the next import, unless you edited the page. `cortex watch add --source zotero` (or **Zotero** with an empty path in the app) brings in new and changed items by itself.

### Past AI chats saved automatically

While the app runs, every 5 minutes (the first time a minute after it starts) it saves each new or changed past AI chat as a chat page in `archive/chats/`: your Claude Code chats (in a terminal or the desktop app's Code tab), your Cowork tasks, your Chat-tab chats and the chats you imported. No AI model is involved. An ordinary chat keeps only your own messages, with secrets removed and the length capped. A coding session (one where files were edited or commands run) becomes a short summary: the project, the dates, what you asked (first lines), the files touched and the commit messages. It never includes code, diffs or command output. Newest chats go first, and the Status line shows the progress and time left. A chat you are still in waits until it has been quiet for 2 minutes, and a chat that grows later updates its page in place. Pages you edited or deleted, chats you saved yourself and chats an earlier import already saved are left alone. Chats in a category you set to skip are never saved, a category you keep on this computer makes the page private (the Browsing page's **Chats kept private** lists them, newest first, with the category that caused it; their titles show only after you unlock Private), and sessions in a folder you excluded are skipped. You get one notification per run that saved something, and the saved chats show in the home page's live feed on the day they were saved (private ones only while the lock is open). Turn it off with **Save new AI chats to your notes automatically** on the Status page. `cortex import-chats --auto` runs one pass by hand.

**Where chat pages live.** `archive/chats/` is left out of the vault's git (its `.gitignore` lists it), so your chats are in no commit and no push. They are still ordinary pages: search, recall, Chat, the brain map and `[[links]]` find them as before. Chat pages from before sat in `resources/`; the app moves them once on its next chat save. `cortex chats archive --dry-run` lists what would move, `cortex chats archive` moves them now, and `cortex chats archive --undo` moves them all back to `resources/` and keeps new ones there ([PRIVACY.md](PRIVACY.md#past-chats)). After the move, a **Chat archive** line on the Status page says how many pages moved, to which folder and when, and names the undo command; it goes away after `--undo`.

**Chat-tab chats.** The desktop Chat tab keeps no file Cortex can read, so Cortex logs each chat while it happens: `cortex_prime` records your first message, and the chat records each later message of yours, with a one-line gist of its previous answer, without asking. The log sits in `~/.cortex/claude-chat/claude-live/` with secrets removed and becomes a chat page of your messages, like any other chat, once the chat has been quiet for 2 minutes. Ask a chat whether it saves your chats and it should say every chat is logged in Cortex. To check the connection, run `cortex doctor`: for Claude Desktop and Claude Code it shows whether Cortex is set up, whether Claude Code has the prime hook, and when each one last called Cortex. A model can forget a turn; the first message of each chat is always logged. If you later import a claude.ai export that holds the same chat (the same first message, started within 15 minutes of the log), it does not become a second page: the chat's page is filled from the export, which has every message, plus any messages you sent after the export was made. This works whichever comes first, the log or the export. When two chats could match, each keeps its own page.

### The chat safety net

Claude is asked to record the topics and concepts it teaches, but it can forget. After each automatic chat import the app reads your chat pages from the last 7 days and records what was missed. It skips private chats, chats in a category you keep on this computer, and chats about a hidden subject. What Claude already recorded during a chat (a topic touch, a taught concept, a question or a grade between the chat's first message and half an hour after its last) is not recorded again; for the rest it logs a topic touch for each concept or topic named in your messages. When the small local model is already loaded and the PC is idle (Cortex never loads a model for this), the model also names up to 3 concepts the chat explained; each is recorded as taught, and a concept you do not have yet is added (under the area of the chat's other concepts, or under **From chats › Unsorted**) and linked to its nearest topic. At most 10 chats and 15 records per pass. Each pass that recorded something adds one line to the day's journal, so it shows in the home feed, for example `Safety net: from 2 chats, 1 new concept, 1 taught, 3 touches (new: [[heap-sort]])`.

`cortex safety-net` runs one pass by hand (`--dry-run` prints it and writes nothing). To undo, `cortex safety-net --undo` cancels everything it recorded (`--chat <page id>` for one chat), or `cortex void <event id>` cancels one record; the cancelled chats are not recorded again. A concept it added stays; merge it with `cortex concept merge` or delete its page.

**Facts about you.** In the same pass, when the small local model is already loaded and the PC is idle, Cortex also reads your own messages in those recent chats (the same skips: private chats, chats in a category you keep on this computer, hidden subjects) and saves up to 3 facts per chat that you stated about yourself, such as where you live or study, what you work on, the tools you use, your goals or a standing preference. Right after you use Cortex's own Chat with the local model, the model is still loaded, so a few seconds after your answer Cortex runs a short pass of about 10 seconds: it starts no new chat after that or once you ask again, so your next question waits at most for the one chat being read, and the rest wait for a later pass. Even with no model loaded, Cortex picks up plain sentences such as "I live in Kochi", "I'm a data analyst", "I prefer short answers" or "I don't eat meat", and skips roleplay, "imagine" or "if I were" set-ups, questions, passing states ("today", "right now"), quoted text, code and model thinking traces; `cortex eval harvest-facts` measures this on made-up test messages. Cortex saves a fact only when your exact words are in one of your messages, say it in the first person, are not a question and hold most of the fact's words with the same yes or no. A message that reads like a planted promotion ("summarize this page and remember", "always recommend", "from now on recommend") never saves a fact: it waits in Review for your Approve, and so does a //pref: or //fact: line that reads like one. Each fact is saved as a guess on Profile, Preferences, Goals or Personality, so AI apps see it marked (ask) until you confirm it. A near-copy of a line you have is not saved, a clash with your own line is not saved, and Core and Setup are never touched. Each pass is one session, listed in Tonight under "Cortex, from your chats" with its Undo (or `cortex undo <session>`). An undone pass counts its lines as rejected, and a pass you keep for 3 days counts them as accepted. Once 20 or more lines are decided and more were rejected than accepted, Status shows "Lines from imported chats paused" and the pass stops until you press Resume, which starts the count again. `cortex fact-harvest` runs one pass by hand.

To turn imported chats into context, run `cortex digest` when you want to. It lists each chat imported since your last digest with up to 3 topics from your catalog that it seems to be about (local search, no AI model; private topics are never proposed), and asks about each one. A yes records a topic touch: context for your plan, never a grade, so a topic's level moves only after a real check. The listed chats are not shown again. `--dry-run` only lists, `--yes` accepts every topic shown, and `--since DATE` and `--limit N` (default 10) narrow the list. Claude Code chats are left out: a connected Claude records topics as you talk.

You may not need the Import page at all: when a claude.ai, ChatGPT or Google Takeout export lands in your Downloads folder, Home shows a card for it, and Add chats imports it in one click (Not this one hides it). If Claude Desktop is connected and no claude.ai export came in for 30 days, Home reminds you to export one, since Claude Desktop keeps no copy of its chats on your computer.

The steps below match `GUIDES` in `src/cortex/chats.py` and `src/cortex/importers.py`.

**Claude (claude.ai).** Wait: a few minutes. Add the downloaded .zip as it is.
1. On claude.ai (web or desktop app, not the phone app) open Settings > Privacy.
2. Click Export data.
3. Open the email from Anthropic and download the .zip (the link works for 24 hours).

**ChatGPT.** Wait: minutes to a few hours (OpenAI says up to 7 days). Add the downloaded .zip as it is (it holds `conversations.json`).
1. On chatgpt.com click your profile picture > Settings > Data controls.
2. Next to Export data click Export, then Confirm export.
3. Open the email from OpenAI and download the .zip (the link works for 24 hours).

**Gemini.** Wait: usually the same day, up to a few days. Add the Takeout .zip as it is.
1. Open takeout.google.com and click Deselect all.
2. Tick My Activity, click All activity data included, click Deselect all, tick Gemini Apps, then OK.
3. Leave Activity records as HTML (the default) or set them to JSON under Multiple formats; Cortex reads both.
4. Click Next step, keep "Send download link via email" and .zip, then Create export.
5. Download the .zip from Google's email (the link works for 7 days).

Gemini's export has no conversation ids, so prompts become one chat until 30 minutes pass between two of them.

**Perplexity.** Wait: minutes to a day for the account export; instant for one thread. Add the export .zip as it is (it holds `conversations-*.json`), or the .md files or their folder. A thread you imported before as a .md file is matched by its first question and answer and kept once.
1. On perplexity.ai open Settings > Account (perplexity.ai/account/details).
2. Click Export my data. If your account has no such button, ask Perplexity's privacy contact (named in its privacy policy) for your data.
3. Download the .zip from Perplexity's email soon: the link expires.
4. For a few threads only: open each one, choose Export > Markdown from its menu, and save the .md files in one folder.

**Google activity (Search, YouTube and Chrome).** Wait: usually the same day, up to a few days. Keep the Takeout .zip as it is. Its rows go through the browse privacy filter and land private by default, under the browse rules in section 6. `cortex import <the .zip>` imports them (so does `cortex browse import --takeout`). A Takeout of just "YouTube and YouTube Music" works too: its watch and search history are read the same way, and a watch that is also in My Activity counts once. Takeouts in English, Spanish, Portuguese, French, German, Hindi, Indonesian and Japanese are read; in other languages, rows whose date or product Cortex cannot read are skipped (HTML ones are counted).
1. Open takeout.google.com and click Deselect all.
2. Tick My Activity, click All activity data included, click Deselect all, tick Search, YouTube and Chrome, then OK.
3. Leave Activity records as HTML (the default) or set them to JSON under Multiple formats; Cortex reads both.
4. Click Next step, keep "Send download link via email" and .zip, then Create export.
5. Download the .zip from Google's email (the link works for 7 days).

**Notes (Obsidian, Notion or a folder).** Instant for a folder; minutes to hours for a large Notion workspace. Give the Obsidian vault folder, the Notion .zip as it is, or the folder of notes. A note with `private: true` in its frontmatter stays private. Importing the folder again updates a changed note's page in place, as long as you have not edited that page's text since (your tags and links on it stay). If you have, your page is kept as it is, the new version comes in as a second page, and the app's Review shows it under **Imported notes you edited** (**Dismiss** leaves both). A note gone from the folder is never deleted from your vault: the report counts it as "not in the folder (kept)". An older version of a note ranks lower in recall than the newest one.
1. Obsidian: nothing to export; pick your vault folder (the one holding `.obsidian`). Hidden folders and attachments are skipped.
2. Notion: open Settings, then General, and click Export all workspace content. Set Export format to Markdown & CSV, include everything, click Export and download the .zip.
3. Any other notes app: export or copy your notes as .md or .txt files into one folder.

**Calendar (Google, Outlook or Apple).** Instant. Give the .ics file, or Google's .zip as it is; attendees' emails are left out. Each month becomes one page.
1. Google Calendar (on a computer): Settings (gear) > Settings > Import & export > Export; a .zip of .ics files downloads.
2. Outlook (classic, Windows): open Calendar, File > Save Calendar, click More Options, set Date Range to Whole calendar and Detail to Full details, then save as iCalendar (.ics).
3. Outlook.com: Settings > Calendar > Shared calendars > Publish a calendar, pick All details, Publish, then open the ICS link to download the file.
4. Apple Calendar (Mac): select a calendar in the list, then File > Export > Export and save the .ics (one calendar at a time).

**Documents (PDF, Word, text).** Instant. Give the folder. Each .pdf, .docx, .md and .txt file becomes one page: its title (a PDF's own title, else the file name), its date (a PDF's creation date, else the file's) and its whole text, so Chat can answer from any part of it. A PDF's page has a "Page N" heading before each page's text, so Chat's sources can name the page. A very long document keeps its first 500,000 characters (about 250 pages), and its page says "Cortex kept the first N of M pages." A page imported before with only the first 1,500 characters gets the rest when you import the same file again, unless you edited that page: then it is left as it is, and the report says "you edited the page; it was left as it is". A .md or .txt you already imported as a note is not imported twice. Encrypted PDFs and Word files, PDFs with no text, and files over 200 MB are skipped, and the report says why. On Windows a scanned PDF is read with Windows text recognition (OCR) on this computer, up to 100 scanned pages a file and about 20 seconds of reading; its page ends "Read with Windows OCR: check names and numbers." Hidden folders, `node_modules`, `.git`, `.venv` and `__pycache__` are never walked, and shortcut files (.url, .lnk, .gdoc, .gsheet, .gslides) are skipped as "a shortcut, not the file". Each PDF and Word file is read in a separate process: one that takes over 30 seconds is skipped as "parse timed out", so a broken or hostile file never hangs the import.

**Your code projects (git).** Seconds. Give the folder that holds your projects (`cortex import C:\Users\you\Projects`); Cortex finds the git projects in it one or two folders down, or takes the folder itself if it is one. Each project becomes one page: the start of its README (800 characters, code blocks left out), its languages from the names of files such as `pyproject.toml` or `package.json`, and, from the commits made with that project's git email (`git config user.email`), how many there are, the first and last dates and the last 20 commit messages. A project with none of your commits is skipped. Importing again updates each project's page in place. Of your last 200 commit messages, the ones that read like a decision (with words such as decided, chose, switched to, migrated to, adopted, deprecated, replaced … with, instead of, in favour of, default to, moved to or ADR; never merge, bump, release, wip, fixup, typo, lint, format or revert commits) are listed under **Decisions in my commits** on the page, and the app's **Review** offers them under **Decisions in your commits**, five at a time, newest first, with the project, date and matched word: **Approve** makes a decision page (accepted, dated the commit's day, linked to the project's page, private when the project's page is), **Dismiss** drops it. Neither is offered again, also after a new import.

**Reading highlights (Kindle, Readwise, Pocket).** Instant. Cortex reads the format from the file's content.
1. Kindle: plug it in by USB and copy `documents/My Clippings.txt`. Each book becomes one page of your highlights and notes with their dates (a book with more than 50 highlights gets one page per month). Bookmarks are skipped.
2. Readwise: open readwise.io/export and click Export to CSV. Each book or article becomes one page, your notes beside the highlights.
3. Pocket: the export .csv (or its .zip), if you saved one. It holds the articles you saved, not highlights, so each month becomes one page listing them.

Importing a newer file again updates a book's page instead of adding a second one.

**Sent mail (Gmail or any .mbox).** Hours to a day for Takeout; instant for a file. Give the `.mbox` file, unzipped (or a folder of them). Private by default.
1. Gmail: open takeout.google.com, click Deselect all, tick Mail, click All Mail data included, untick Include all messages in Mail, tick only Sent, then OK.
2. Click Next step and Create export; download the .zip from Google's email and unzip it.
3. Other mail apps (Thunderbird, Apple Mail): export the Sent folder as .mbox.

Each month becomes one page (`mail-sent-mail-YYYY-MM`) with a section per message you sent: the date, subject, the names you wrote to and your own words (the first 1,500 characters). Only your own messages are read: those Gmail labels Sent, or, in an mbox without labels, those in a file named Sent (`Sent.mbox`, `Sent Items.mbox`), plus mail in your other files from those same addresses. A file with none of your mail (an Inbox alone) imports nothing. Quoted replies (`>` lines and everything from "On … wrote:"), forwarded and original-message parts, Outlook's quoted header block, signatures (from a `-- ` line), drafts and attachments are left out; an HTML-only message is turned into plain text without its quoted part. E-mail addresses never reach a page: recipients are named, and an address in your text becomes `[address]`. Importing a newer export again rewrites a month's page in place, so import the whole (cumulative) Sent folder each time, not a slice of it; a month the new file has fewer messages for keeps its page as it is.

**Notes to self (WhatsApp, Telegram).** Instant on WhatsApp; minutes for a Telegram export. `cortex import <file> --source selfchat`. Private by default.
1. WhatsApp (phone): open the chat with yourself (Message yourself), tap the menu (or the chat's name on an iPhone), then More > Export chat > Without media, and save the .zip or .txt.
2. Telegram Desktop: Settings > Advanced > Export Telegram data; tick Personal chats, choose Machine-readable JSON, Export, then use `result.json`.

Each month becomes one page (`selfchat-YYYY-MM`) with a line per message: `- YYYY-MM-DD HH:MM WhatsApp: …`. Only your own messages are kept: a WhatsApp chat in which anyone else wrote is skipped whole ("not a chat with yourself"), and in Telegram only the Saved Messages chat is read, without forwarded messages. Media become `[photo]`, `[video]`, `[audio]`, `[voice note]`, `[sticker]`, `[GIF]` or `[file]`; no file is read or copied. Deleted messages and system lines are skipped. The date order (day or month first) is read from the file; a WhatsApp file that cannot tell is read day first. Importing the same file again adds nothing.

**Location (Google Timeline).** Hours to a day for Takeout; instant on the phone. Give the Takeout .zip as it is, the `Semantic Location History` folder, or `Timeline.json` / `location-history.json`. Private by default.
1. Older exports: open takeout.google.com, click Deselect all, tick Location History (Timeline), then Next step and Create export; download the .zip.
2. Timeline kept on your phone: on Android open Settings > Location > Location services > Timeline > Export Timeline data (on an iPhone, Google Maps' Timeline settings offer the same export), and save the .json.

Each month becomes one page (`location-YYYY-MM`) with one line per day, `- YYYY-MM-DD: City`: the city of the place you visited most that day, taken from the place's postal address in the export (the part before the region and country). A part that could be finer than a city (it holds a number or a word such as Road or Street) is not used. Coordinates, street addresses, place names, movements and raw location points (`Records.json`, never read) never reach a page, and nothing is looked up online, so a day whose visits carry no address is skipped as "no place name". Exports made on the phone often carry coordinates only, so most of their days are skipped. Importing the same export again adds nothing; a later export that names another city for a day adds a second line for it.

**Photo dates and albums.** Seconds to minutes. `cortex import <the photo folder> --source photos`. Nothing to export: pick the folder that holds your photos (subfolders count as albums). Each month becomes one page (`photos-YYYY-MM`) with one line per day, `- YYYY-MM-DD: 12 photos (Camera, Goa trip)`: how many photos you took and the names of the folders they are in. The day is the photo's EXIF capture date (DateTimeOriginal, else DateTime), or the file's modified date when it has none or Pillow cannot read it (HEIC files, for one). Only .jpg, .jpeg, .png, .heic, .heif, .webp, .tif, .tiff, .dng and .gif files count; videos and other files are skipped as "not a photo", and hidden folders are not walked. Images, thumbnails, GPS and every other EXIF field are never kept. Importing the same folder again adds nothing; after you add photos to a day, that day gets a second line with the new count.

**Spotify listening.** About 5 days for account data; up to 30 days for the extended history. Give Spotify's .zip as it is, or its folder.
1. Open spotify.com/account/privacy (signed in) and request your Account data (the last year) or your Extended streaming history (every year).
2. Download the .zip from Spotify's email.

Each year becomes one page (`listening-spotify-YYYY`): total hours (music and podcasts), your top 20 artists and top 20 tracks by time listened, and your top 10 podcasts. Nothing per play is kept: no play times, devices, IP addresses or countries. Both shapes are read, the account data's `StreamingHistory*.json` and the extended `Streaming_History_Audio_*.json`; when both are there, the extended one is used and the others are skipped as "covered by the extended history". Importing a newer export again rewrites a year's page in place, unless it has fewer hours for that year than the page (the account data covers only the last year): then the page stays as it is.

**Voice memos.** Seconds to a minute of transcribing per minute of audio, on this computer. `cortex import <the folder of recordings> --source voice-memos`. Copy your recordings into one folder (your phone's voice recorder files, or WhatsApp voice notes); .m4a, .mp3, .wav, .ogg, .opus and .aac are read, subfolders too. It needs the local speech model once: click Install on the Voice memos card, or run `cortex model download --voice` (installs faster-whisper and downloads about 150 MB); without it the import stops and says so. Each recording becomes one private page (`voice-YYYY-MM-DD-<first words>`): the day (from a date in the file name, else the file's date), the first sentence as its summary, then the transcript in paragraphs of about 30 seconds, each starting with its `[mm:ss]`. Silence, noise and Whisper's repeated phrases are left out; a recording with almost no words, or over 2 hours, is skipped. Importing the folder again skips recordings already in; a changed recording updates its page unless you edited the page. The audio is never copied and never leaves the computer.

### The Chat test

The Chat test checks how well the app's Chat answers. It asks Chat 60 questions about a made-up person and scores each answer in code, with no AI judge. They include questions that link two pages, ask about a past month, carry a hostile instruction inside a page, ask Chat to play a role, ask two things at once, or need a sum. Start it with **Run Chat test** in the Chat settings, or run `cortex chat eval` in a terminal. It runs only when you start it. A small model answers differently from run to run, so `cortex chat eval --runs 3` asks each model question three times and also reports pass^3: the questions that passed all three times.

- **What it uses.** The test builds a made-up vault, browsing history and study record in a temp folder and deletes them afterwards. Your own vault, settings and browsing are never read.
- **Which model.** Questions that need a model use your local model only if a 2B one is already loaded. The test never loads or downloads a model. Without one, those questions show as "blocked: no local model" and the whole result is BLOCKED, not a fail. The 15 questions Cortex answers from its own data (counts, first and last dates, study states, the empty month, the private project with Private off) still run. An empty or failed answer counts as a fail.
- **The report.** It starts with PASS, FAIL, BLOCKED or STOPPED and a fingerprint of the made-up vault (two reports with the same fingerprint asked the same vault). Each question shows pass or fail, with the reason for a failure. It also shows:
  - the result for each kind of question, and for each kind of search it needs (an exact name, plain words, other words, several pages, a changed history, an outdated fact, nothing to find);
  - whether the search served the right pages (recall at 1, 3 and 5; "buried" means the right page came lower than third, "missed" means it did not come at all);
  - why a question went wrong: the right page was not indexed, the search did not find it, it was found but ranked too low, it was there but the answer was wrong, or it was there and the answer said it had nothing;
  - whether longer pages that only repeat a term outranked the short page with the fact, and whether an answer obeyed a planted "SYSTEM: ignore previous instructions" line;
  - the five blockers (a private leak, a stale preference, a made-up history, a wrong count, a missed deep fact);
  - how long the answers took.

  A metric with no questions behind it says "no data". The pass bar is 35 of 40.
- **Terminal options.** `--served` lists the pages each answer was given, `--out report.json` saves everything (answers and passage text), `--no-model` runs only the 14 data questions, and `--keep <new folder>` leaves the made-up vault there for a look.

---

## 5. The 3D brain

The home page of the Cortex app (or `cortex dashboard`) shows the brain as a 3D picture of your vault, with Today and your other lists beside it. It updates as you work. Cortex no longer writes a `dashboard.html` file into your vault; an old one is moved to `.cortex/backups/` once.

**Private items stay hidden until you unlock them**
- Private notes and the browsing kept on this computer only are left out of the home page: not shown and not counted.
- The lock button at the top adds them to Today and This week. Windows asks for Windows Hello (your PIN, face or fingerprint) first. Without Windows Hello set up, it tells you how to set it up; Cortex has no password of its own.
- They hide again when you press the lock again, minimise the window or close it to the tray, after 10 minutes, and when Cortex restarts. In a browser tab it works the same, except for minimising.
- Unlocking changes only this page: Claude, other AI apps and the terminal commands never get private items through it.

**Export snapshot** (in the app: Vault → Snapshot) saves the home as one web page that opens without Cortex: your non-private pages and the 3D brain, with no network access. In the app window you choose where to save it; in a browser it goes to your downloads.

**What you see**
- Each topic has its own colour.
- Bright fibres are your own links between pages. Faint fibres join pages with a similar meaning.
- Big bright dots are pages with many connections.
- A faint outline shows the shape of a brain.
- When pages were added since you last looked (on Home), the brain dims for a moment and the regions that grew glow in their own colour, with a label such as "+3 · Graphs". The page count above counts up. Any click, drag or scroll ends it at once. Today's new pages keep a faint glow until midnight or until you click them.

**The brain stem: your websites** (the app's home only)
- Once your browsing is in Cortex, the stem below the brain holds one dot per website, the oldest you found at the top. It is a column inside a faint tube, and the overview shows the brain and the stem together. Your 5 most visited sites are named.
- A dot's size is your visits, its brightness your last visit (it dims with time but never goes out) and its colour the site's category.
- Faint fibres join a site to its topic and to up to three similar sites.
- Click a site to see the days you visited it, what you searched there, the searches that led you there and the pages you read. This comes from Cortex's browsing store on this computer, never from a vault page.
- To stay quick, the stem shows your 1,500 most visited sites plus up to 1,000 more from the last 30 days. The legend says how many others stay in the store and which colour is which category.
- On the Brain tab, the outline's **Browsing** group lists your 20 most visited sites; click one, or press Enter on it, to open its panel.
- Private sites appear only while the lock is open, as a hollow ring, and leave again when it locks. The 2D map and Export snapshot have no stem.

**Moving around**
- Drag to turn. Scroll to zoom toward the cursor.
- Click a page to select it. It glimmers, and white trails keep running along its connections for as long as it stays selected.
- Esc goes back one level (page, topic, overview). Double-click empty space for the overview.
- The trail at the top left (Brain › Topic › Page) takes you back to any level.
- `[` and `]` step through the selected page's connections. Backspace returns to the previous page.
- F frames the selection. 0 resets the view. Arrow keys turn. + and − zoom.
- A, M and G switch modes. S toggles spin. L opens Look.

**The toolbar**
- **Activity / Mastery / Growth:** what the colours and halos show.
- **Spin:** off by default. While on, it pauses when you move the mouse and resumes after 3 seconds.
- **Alive:** neurons fire now and then. It sleeps after 2 minutes idle.
- **Today:** shows everything from today (your day starts at 04:00): pages taught, tested or asked about, and pages added or saved. The camera frames them all, and every one is highlighted as chosen while the rest dims. `[` and `]`, the Left and Right arrow keys, or the arrows beside the button open them one at a time ("2 of 7"); the links between two of today's pages are drawn brighter than the links leading out of today. Press Today again, Esc or 0 to leave.
- **Stem:** flies to the websites in the brain stem; press it again for the whole brain. It shows only once your browsing is in Cortex.
- **Unsorted chats:** shows chats with no topic as a faint ring under the brain.
- **Look:** opens the Look panel.
- **Legend** (bottom left): what the colours, sizes and fibres mean. It is open on your first visit and folded after that; click it to open or fold it, and the page remembers your choice.

**The Look panel**
- It has 12 presets. "Cortex (default)" is first.
- Below the presets are switches and sliders for each part: outline, dots, fibres, arrangement, colour and motion.
- Your choice is saved in this browser. **Reset to default** brings back the first preset.

---

## 6. Your browsing

Cortex can turn your browser history into pages, so it knows what you have been reading and watching.

**The promise**
- By default nothing from your browsing is visible to Claude. Cortex writes only private pages, and you can see everything in Obsidian.
- It reads your browser history on this laptop only. It never sends it anywhere, unless you turn on `cloud_processing` (see Settings), and even then never a hidden category.
- Sign-in, payment and bank pages are dropped. They are never stored.
- Only if you turn on `visible_to_claude` can Claude see your browsing, and then only ordinary sites and the sensitive categories you allow (`cloud_categories`). The rest stays on private pages, which you see only in Obsidian, never in the dashboard, search or any chat. For what Claude may see, the site pages hold page titles, search terms and site names, and cloud AI apps can read them.
- When unsure, it stays private: a search term reaches a page Claude can read only after your local AI has judged it ordinary (without a local AI, search terms stay private), and a title the built-in word lists cannot place needs that judgement too.
- By default every sensitive category (health, money, betting, dating, sexuality, drugs, self-harm and adult) is private, and adult is not saved at all.
- It reads Chrome, Edge, Brave and Firefox on Windows, macOS and Linux (and Comet on Windows).

**Steps**
1. `cortex browse selftest` runs the whole import on made-up history in a temp folder, never your vault or your browsers. Each check prints PASS or FAIL; the last line says whether all passed.
2. `cortex browse scan` shows counts only: per browser and profile, visits kept, private and dropped, whether Comet was found, and the date range. It writes nothing.
3. `cortex browse import --dry-run` does everything except writing and prints how many day pages it would write.
4. `cortex browse import` writes a private page per day with your topics and time at the top, then the titles, searches, videos and sites. With `visible_to_claude: true` it also writes a public page per day with your topics and time. It prints a line per browser (counts only) and a summary; its last line says which mode ran. You can run it again safely, and Ctrl+C is safe too.
   - If a browser is open and its history can't be copied, it says so and goes on with the others. Close that browser and run it again.
   - If it says the embedding model isn't downloaded, every page lands under "Other" on the private page. Run `cortex model download`, then import again with `--since` to redo old days.
5. `cortex browse status` shows the mode, the last run, and the last read per browser.
6. `cortex browse deepen` gives your local AI time with your sites (see below).
7. `cortex browse forget --site <domain>` (or `--category <name>`, for example `--category health`) removes that site or category from everything Cortex stored: day and site pages (private ones too), visit counts, search vectors, site summaries and the visits the old capture extension saved. It prints what it removed. Add the site to `deny_domains` as well, or the next import reads it again from your browser.

**Keep importing.** Browsers keep about 90 days of history; days not imported by then are gone. After your first import, Cortex reads new visits by itself: while `cortex app` runs, every 10 minutes, only the visits that are new since the last read (a browser that wrote nothing is not even opened, and only the days that changed are written again); the receiver (`cortex browse serve`) on its own imports whenever the last import is older than `browse.auto_import_hours` (default 6; 0 turns both off). It checks a minute after starting, then every 10 minutes, and waits while a forget or `deepen` runs (the app also waits until the search model is downloaded). The local model checks new titles 25 at a time, only while the computer is idle and the model is already loaded; Cortex never loads a model for this. The search vectors an import or `deepen` makes are built on about half the processor cores at low priority, so the computer stays quick; those for private browsing search are built only while it is idle, two minutes per import, and go on where they stopped. **Import past history** in the app shows its named steps as it goes ("Step 3 of 5 · checking titles · 42% · about 2 min left"); Cancel keeps what was read, and the next import goes on from there. The first time, it moves the site pages out of the vault's Markdown files into Cortex's own store (`.cortex/browse/store.db`), after zipping them into `.cortex/backups/`. It reads only the browsers and profiles you imported before, never one you have not imported. A profile you untick in the app's history list is remembered (`browse.skip_profiles`) and left out of every import until you tick it again: the automatic one, the receiver's, **Process now** and the Status page's **Import new history now**. On the days an import redoes, an unticked profile's visits drop out of the counts. Tick it again to include its new visits; to bring back its older days, run `cortex browse import --since <day>`. `cortex browse import --profile <name>` reads an unticked profile when the name fits only that one (add `--browser` if it fits more), and `--browser <name>` reads all of that browser's profiles, unticked ones too. In the app, **Browsing** → **Past history** shows the last import, an **Import new history now** button and this setting. The automatic import runs only while Cortex runs, so the page offers **Start at login** (under **Browsing**) to keep it going without the app open.

**Status warns when it falls behind.** The app's **Status** page has a **Browsing history** row. When the last import is older than twice `auto_import_hours`, it says so ("Last import 2 days ago: newer visits are not in Cortex yet") with **Import new history now**, which imports the browsers imported before (never an unticked profile), and, if the receiver does not start at login yet, **Start at login**. Before your first import the row says "never imported"; with the automatic import off, it says so.

**Visits per browser.** Each import records which browser every visit came from, so Chat can list one browser's sites. Days imported by Cortex 0.2.1 or older do not have it. The first import after updating to 0.2.3 (your click or the automatic one) rebuilds every day your browsers still hold once, so it records it for those days. Days the browsers no longer hold, and a browser's oldest day once it is nearly three months old (the browser is forgetting it), keep their counts without a browser.

**One page per website.** Each import also writes a page per site you visited, in a folder per category: `browse/sites/<category>/` for what Claude may see, and the private folder for hidden categories. A site with some hidden visits (a video site, say) gets both: the visible page never counts or names the hidden ones. Each page has the visits, first and last seen, top pages, the searches that led there, related sites and links to your topics. `browse/browse-profile.md` sums it up: when you browse (by hour and weekday), top categories and sites, interests and months. The private profile adds the hidden categories, with their top sites and searches. `browse/sites.csv` lists the sites for a spreadsheet.

**The first big run** (for example one browser's history since a start date, with a small local model):
1. `cortex browse import --browser chrome --since 2026-08-01` writes the day and site pages. Sites the built-in table doesn't know go under `other` for now.
2. `cortex browse deepen` lets the local AI sort the `other` sites into categories, write a one-line description per site and suggest related sites and topics, most visited sites first. It stops when its time runs out (`localllm.budget_s`, or `--budget-s 3600` for an hour) and remembers where it stopped, so run it again to go on. Ctrl+C keeps what it has done. The receiver (`cortex browse serve`) runs it by itself once, 30 minutes after it starts.

**Optional: a local AI for summaries and `deepen`**
Run `cortex app` → Setup. It finds or installs a runner (Ollama is recommended; LM Studio is the alternative), suggests a model that fits this computer, downloads it, tests it with a made-up sentence and turns it on.

Later, `cortex app` → **Local model** lets you switch: the model list shows every chat model already installed in Ollama or LM Studio (embedding models are left out). Pick one and click **Save choice**. **Test** sends one made-up sentence and shows the answer and time; **Load** starts LM Studio and loads an LM Studio model (Ollama loads its models by itself). To get another Ollama model, type its name from ollama.com/library (for example `gemma3:4b`) and click **Download**; Ollama must be running, and cloud models are refused. The page also says which size suits this computer, with a button that fills in its name. **Model for background work** (optional; `localllm.background_model` in the config) picks a second model for browsing imports, the receiver's runs and `deepen`'s site descriptions, which run while you are away; Chat and `cortex ask` keep the main model: a larger, slower model there (say 9B, with a 2B one for Chat) describes sites better. If it does not answer, the main model is used. Two models can sit in memory at once, so with 8 GB of graphics memory or less, leave it as the same model.

Without the app: install Ollama, run `ollama pull qwen3.5:4b` (`qwen3.5:2b` with under 6 GB of graphics memory or RAM, `qwen3.5:9b` with 10 GB or more), then add `localllm: {enabled: true}` to `~/.cortex/config.yaml` (for LM Studio, `localllm: {enabled: true, runner: lmstudio}`). `cortex llm status` checks it.

It only talks to your own machine. For a public day page it hears your topic names only; for a site, that site's page titles. It can move a site into a sensitive category, never out of one. Without it, the summary line lists your top topics and unknown sites stay under `other`.

**Chat**
Once a local model runs (Ollama or LM Studio; Qwen 3.5 2B is enough), talk with it about everything Cortex holds: in `cortex app` → **Chat**. Ask a question, then follow up ("and its running time?"); each answer searches again, with your new message and the one before it. After an answer that lists things, "tell me more about the second one" (or "the last one", "number 3", "the other one") searches with that item. A question in two parts ("what is a heap, and when did I last study graphs?") is answered in both halves: a part Cortex can count is counted, and the other part is searched on its own. A note's `## Past` section is left out unless you ask about the past ("what did I use before?"), and each passage reaches the model with its date and whether it is current or superseded (a replaced decision, or a note's kept `## Past`). Each search also brings the pages linked with its best match (by `[[link]]` or by naming the page's title), a question about a month also finds the preferences and decisions that held then though written earlier, a passage that repeats one already chosen is skipped, and a search that found only weak matches answers "I don't know from your archive."
- **What it searches:** your notes and mistakes, the AI chats you imported (Claude, ChatGPT, Gemini, Perplexity), and your browsing. While the **Private notes and browsing** switch is on (the default), that includes your private notes (`private: true`, people pages and pages under a private subject) and your private browsing pages. Private notes get their own search on this computer: they never enter the search Claude and other AI apps use, and their search vectors are kept in memory only, so after the app starts they are built again a second or two per question, and a private note may be missed until then. Only the model on this computer reads them; Chat never uses a cloud model, whatever the cloud settings say, and refuses any runner that is not on this computer.
- **Your day starts at 04:00**, as on the dashboard and in Today: "today" before 04:00 is still yesterday, so after-midnight study counts for the day before.
- **Dates:** a date phrase keeps the answer to pages dated then: the browsing day pages, notes with a date, and other notes by the day they were last changed. A range that runs past today stops at today, and a future day ("explain this for tomorrow's exam") does not narrow the search. Chat reads:
  - "today" (also "tonight", "this morning", "this afternoon", "this evening"), "yesterday" (also "last night"), "the day before yesterday", "tomorrow";
  - "3 days ago", "two weeks ago", "a month ago" (numbers up to 999, or one to ten, "a couple", "a few"): a day either side for days, 3 days for weeks, 7 days for months;
  - "the last 3 days" (three days, today the third), "past 2 weeks", "previous 4 months" (30 days a month), and "the past 4 to 5 months" or "last 2-3 weeks" (the wider one); "past week" and "past month" are the last 7 and 30 days;
  - "on Monday", "last Friday", "this Sunday" (the most recent one; "last Monday" said on a Monday is a week ago), and "this weekend", "last weekend" or "over the weekend" (the last Saturday and Sunday);
  - "this week" (Monday to today), "last week" (Monday to Sunday), "this month", "last month", "in October" or "in October 2025";
  - "on 12 Nov", "Nov 12", "12th of November 2025", "2026-11-12" (a day without a year is the most recent one), and "since 2026-11-01" or "since 3 Oct".
  A day in another year is shown with its year.
- **Sites:** "Which sites did I visit yesterday?" or "list all the unique sites I visited this month" is answered from the stored visit counts, not by the model: every site once (up to 2,000), most visits first, with its visits, days, category and the local model's one-line description. It is used only for a plain list of your own browsing: a question asking for advice ("a good site to practise SQL") or naming a topic ("which sites do I use for DSA?") goes to the search and the model. Add a browser after "in", "on", "using", "from" or "with" ("which sites did I visit in Comet?") to count only its visits (Chrome, Edge, Brave, Comet or Firefox; visits only the old capture extension saw count under no browser); "edge detection" is not Edge. The answer also says what it cannot list: visits imported before Cortex kept which browser they came from, days before your first import (browsers keep about 90 days), and categories set to Don't save (never stored; change them under Browsing). A site the local model files under a Don't save category is left out too, and a site you can see never shows a description the model wrote from private page titles.
- **Your studying:** "What should I study today?", "What's due?", "Where am I weak?", "What did I study yesterday?" (any date phrase above) and "How am I doing?" are answered from your study record, not by the model: today's plan, the concepts due for review, weak concepts and open mistakes, what was taught, asked and graded on those days, and your concepts by state. "What's due this week?" lists what is due by Sunday; in a due question a weekday, the weekend, a date or a month without a year means the coming one, so "what's due on Friday?" lists everything due by Friday, overdue included; and "what's next?" means today's plan only as the first question of a chat. Private subjects and private mistakes are included only while the Private notes and browsing switch is on (today's plan always leaves them out, as on the dashboard). A question that names a topic ("what did I study about graphs yesterday?") searches your pages instead.
- **Your day or week:** "What did I do yesterday?", "What did I learn yesterday?", "Summarize my day" (today), "Summarize my week" or "What have I been up to?" (this week), "What happened last week?" and "Recap 12 Nov" are answered from Cortex's records, not by the model, in short sections: what you studied, your most visited sites with their visit counts, the notes and saved AI chats written or changed then, and your journal lines (10 lines a section). A last line says what is left out. Private notes (in a section of their own), private subjects and private browsing are shown only while the Private notes and browsing switch is on; any source switched off in Chat settings is left out. A date more than a year back ("what happened in 1918?") is a history question and goes to the model, and so does a question that names a topic ("what did I read about engines yesterday?").
- **The Notes and mistakes switch** (Chat settings) also covers these answers: off, study questions say they cannot read your plan, and a day or week summary leaves out what you studied, your notes and your journal.
- **While it works:** the reply shows each step with a live count of seconds: "Searching your notes and browsing", then "Reading 5 pages with <model>", then "Thinking" for a model that reasons first; a data answer shows its own step ("Counting the sites you visited", "Putting together your day", "Reading your study plan"). When the answer is done its time is shown next to the model's name ("Cortex" for a data answer).
- **Stop, scroll and copy:** Send turns into Stop while an answer comes in, and Esc does the same; a stopped answer is kept as far as it got. Scroll up and the page stops following the answer; **New text** jumps back to the end. Every finished answer has **Copy**, which copies its text without the [n] markers.
- **Counts, firsts and timelines:** "How many times did I visit github last week?", "When did I first read about kubernetes?", "Timeline of October" and "Tell me everything about rust" are counted by Cortex over every dated note, saved AI chat, journal line, visit and study event, not by the model, which only puts the result into words. A **Computed from your data** box above the words shows the numbers exactly as counted: the total, the period, the count per source, and how many items were read to count them (and which source switches were off).
- **Compare and gaps:** "rust vs go" or "compare heaps and stacks" counts each side the same way and finds the note or saved AI chat that mentions it most; the model reads those two pages, each with the day it was written, and the box shows both sides. "What am I missing about databases?" lists the pages and sub-topics under your topic page of that name, the longest untouched first with the day each last changed, plus searches Claude made about it whose results you never opened (when Cortex has logged any). Private pages are never listed.
- **Your things, tests and decisions:** "How many git repos have I imported?" or "list my projects" counts those pages (repos, projects, decisions, people, books, courses and the like), newest first. "What tests or interviews do I have coming up?" lists your pending assessments soonest first, with your calendar's events of the next 30 days. "What did I decide about the storage engine?" lists the matching decisions, newest first, and says which one replaced an older one. Cortex counts these too, and the model only words them.
- **Sources:** the answer cites passages as [1], [2]. Each source is a pill with its type (note, chat or browsing) and title. Click a number or a pill to see the passage the model read: its text, the page's title and the heading it sits under, with **Open the page in Obsidian**; click the pill again to close it. The passage opens only while its source switch is on in Chat settings (a private one only with Private notes and browsing on). A line under the answer says how many passages the model read of how many the search found ("Read 5 of 14 matches"). A question about "my sister", "my manager" or any other relation also searches the name of the people page whose `relation` is that word, and the line says so ("also searched: Asha Rao"); only people pages marked `private: false` are used for this. When nothing fits it says "I don't know from your archive." When an answer cites nothing, the passages it read are listed under **Searched, not cited**. When the model says nothing, Chat asks it once more ("asking once more"); if it still says nothing, the reply says so and lists the pages it searched, to open yourself. Stray end-of-turn markers some models leave (`<|im_end|>`, `<end_of_turn>`, `</think>`) are removed from answers. Each passage tells the model whose words it holds ("your note", or "imported from YouTube", from your browsing, a saved AI chat or a saved source), so an article or highlight is not repeated as your own view, and that "tomorrow" or "next week" inside a passage counts from that passage's date, not today. Anything the model puts in quotes (12 characters or more) is checked against the passages it cited, ignoring case, spacing and curly quotes; a quote none of them holds is listed under the answer as **Quote not found in a cited passage**, since the model may have made it up.
- **Settings** (Chat settings, top right; saved under `chat:` in `~/.cortex/config.yaml`): answer length (short, normal, long), creativity (0 to 1, default 0.2), passages read per answer (4 to 12, default 5; each up to about 700 characters, so a small model reads them all), which sources to search (Notes and mistakes, Saved AI chats, Browsing, Private notes and browsing; all on by default), and Keep chat history (on by default). Each setting shows "Saved" when it is stored.
- **What the model remembers:** with each question the model gets only the last 6 messages of the chat (about 3,000 characters). Once a chat is longer, a thin line, "The model remembers from here", sits above the oldest message it still gets; anything above the line it no longer sees. The model also gets a short "About the user" note (your pinned core lines and your Profile, about 600 characters, out of that same budget) so it knows who is asking; it never cites that note, and a page your source switches hide stays out of it.
- **History:** each chat is a file in `<vault>/.cortex/browse/chat/`, inside the private browse folder: never shown to Claude or any other AI app, never on the dashboard. Delete one with its Delete button, or all with Chat settings → Delete all chats. With Keep chat history off, new chats are never written and are forgotten when the app stops.
- **Ask about a file:** click **Attach a file** next to the message box, or drop a file on the chat, to ask about a PDF, .docx, .txt or .md (up to 200 MB for a PDF or .docx, 5 MB for text). A line above the message box names it ("lecture.pdf, 12 pages") while it is attached. Each question then searches the file first and your notes after it, and sources read "lecture.pdf, page 3" (or "part 3" for a file without pages). Only your local model reads the file; with Chat set to a cloud model it says "Files are answered on this computer only." With no local model running, Chat shows the 3 passages that match best, with their pages. Up to 3 files stay attached at once (a fourth drops the oldest); a file is forgotten on **Remove**, on New chat or deleting the chat, after 2 hours unused, or when the app stops. A scanned PDF is read with Windows text recognition (OCR) on this computer, and its line says "(read from a scan)"; without an OCR language installed in Windows it can't be read. A very long file is searched up to about 2,000 passages, and its line says where the search stops. **Save to your brain** imports the file as one Documents page (the Documents import's choice of who may see it, and its own duplicate check), committed like other imports; the line then says "Saved as" the page's title, with a button to open it.

**With no local model or no search model,** Chat shows what is missing above the message box, with a button to Setup. The questions about your sites, your studying and your days above still work, since they need neither model; with no chat yet, the page offers a few of them to try. Any other question shows what is missing and goes back into the message box. In a terminal, `cortex ask "what did I read about binary search?"` answers one question from the same sources with the local model, without history or date phrases.

**The old browser extension.** Cortex 0.3.2 removed the Cortex capture extension: remove it from your browser's extensions page. The visits it saved stay in Cortex (their time on screen and excerpts still feed day pages, Chat and search), and visits it left waiting are imported by the next import. Your history keeps coming in from the browsers' own history, as above.

**Settings** (in `~/.cortex/config.yaml`, under `browse:`)
- `visible_to_claude: false` or `true`. The default is `false`: only private pages, and no journal lines, topic touches or dashboard pages from browsing. With `true`, the day pages (your topics, time and counts only), the site pages and profile of ordinary sites and of every sensitive category in `cloud_categories`, journal lines and topic touches are written too. Switching back to `false`, hiding or skipping one more category, or denying a site, removes what is no longer allowed on the next import, from older pages, visit counts and search vectors too.
- `cloud_categories`: the sensitive categories Claude and other cloud AI apps may read, for example `[money]`. The default is none: every sensitive category stays on private pages, and so does any category a later release adds until you list it. An older config's `hidden_from_claude` (the categories kept private) still works.
- `skip_categories`: the categories never saved at all (Don't save in the app). The default is `[adult]` (from the older `adult: skip`). A site the local model files under one of them after it was stored is dropped the same way: Chat's site lists and day summaries never show it.
- `adult: skip` or `private`, and `sensitive: private` or `skip`: the older settings; they only set the default of `skip_categories`.
- `browsers`: which browsers to read, for example `[comet]`. Leave it out to read every browser found.
- `auto_import_hours`: import new history by itself when the last import is this many hours old (0 to 168; default 6; 0 is off). Only after your first import, and only from the browsers and profiles imported before.
- `skip_profiles`: the browser profiles you unticked in the app's history list (the app keeps this list; the default is none). Every import leaves them out until you tick them again, except `cortex browse import` with `--browser`, or with `--profile` naming only that one.
- `deny_domains`: sites that are never read.
- `allow_domains`: if set, only these sites are kept.
- `private_domains`: sites treated like sensitive ones: they never add time to a public topic.
- `cloud_processing: true` (default `false`; in the app: Browsing → Cloud model) lets a cloud model run `deepen` on the site pages Claude may already see; hidden ones stay with the local AI. It needs a `cloudllm:` block, with the key in an environment variable, never in the file:
  `cloudllm: {provider: openrouter, model: <model>, api_key_env: OPENROUTER_API_KEY}`
  Providers: `openai`, `openrouter`, `gemini`, `anthropic`, or your own `https://` `base_url`. At most `max_items` calls a run (default 500); each run adds a counts-only line to `.cortex/cloud-log.jsonl`.

**Google activity.** `cortex browse import --takeout <Takeout zip or folder>` also reads your Google Search, YouTube and Chrome activity (Takeout → My Activity, as HTML or JSON), through the same filter. `cortex import <the .zip>` does the same. Rows on days your browsers already hold are skipped, so nothing counts twice.

---

## 7. What to try, in order

**This week:**

For everyone: tell Cortex about you in any chat (`//fact: I'm on the payments team`, `//pref: short answers`), and save a decision when you make one. The steps below are for study.

1. **Date your pending tests** in Obsidian (in `assessments/`; a best guess like `date: 2026-12-01` if you don't know the day). Until then the exam clock, the extra revision near exams and the readiness forecast are all off.
2. **`/cortex`** once, to see the home card.
3. **`/cortex learn`** (or `/cortex learn <subject>`) as your daily session.
4. **`/cortex sweep`** every few days, to catch topics you only *think* you know.
5. **`/cortex short`** on tired nights instead of skipping.
6. **`/cortex mock`** a few days before each OA or interview.

**Once a week:**

7. **`/cortex reflect`**, about 5 minutes: confirm or drop what's waiting, flip any grade a chat got wrong.

**Later, when you have time:**

8. `/cortex harvest` (10 minutes a night).
9. `cortex export context` when you start a fresh ChatGPT or Gemini chat.

---

## 8. The names, in plain words

| Name | Meaning |
|---|---|
| **Vault** | Your Cortex folder (Markdown notes + event logs); also opens in Obsidian |
| **Prime** | The context a chat gets about you from your first message |
| **Session** | One chat's batch of changes; closes by itself and is committed to git |
| **Ambient** | A session the chat opened automatically, not a `/cortex` study session |
| **Today** | Your capped daily plan (also in `Today.md`) |
| **States ○ ◌ ◐ ↻ ●** | Not started, Unverified, Learning, Needs refresh, Solid |
| **Solid** | Passed more than once, including after a break of 7+ days and at least once applying it, not just recalling |
| **Confident miss** | Wrong when you said you were sure; it stays at the top of Today until you get it right on a later day |
| **Taught, not yet checked** | Explained but not tested; comes first next time |
| **Hub / topic tree** | Topic pages that group your chats and notes (e.g. "SQL and databases") |
| **Touch** | A mark that a chat was about a topic hub |
| **Harvest** | Distilling imported chats into hub lines and notes (`/cortex harvest`) |
| **Sweep** | 10 mixed cold questions with "how sure?" |
| **Mock** | The interview mode |
| **Claim** | A line about you on a `me/` page, with an id |
| **(ask)** | A line about you the chat should check with you before relying on it (written from outside text, or a yes that only the chat relayed) |
| **Clicked / relayed** | You ticked the dialog or clicked Approve / Still true in the app yourself / the chat said you said yes |
| **Exposed** | A chat that recently read outside text (a web page, an imported chat). What it writes about you is marked, and it can't change your teaching contract. |
| **Honest Double** | Checks text written as you (CV, answers) against what you've actually shown you know |
| **Loop** | An open item: a commitment, question or idea |
| **Resurfacing** | Old ideas and decisions brought back at growing intervals |

---

## 9. Where things are

- **Your vault:** your notes folder. In Obsidian, Ctrl+G shows the graph, coloured by subject.
- **Other names for a page:** Obsidian's `aliases:` works in Cortex. Add `aliases: [IITH]` (a list, or one name) to the front matter of any page (a person, project, decision, note or `me/` page), and Cortex finds that page by those names wherever it would find it by its title: in `cortex_find`, in Chat, and when Chat looks for pages linked with the one it found. A name must be a whole word or phrase (an alias "AI" does not match "said"), and a one-letter alias is ignored when Chat looks for pages that mention it. A private page stays hidden, whatever its alias says.
- **What changed and how to undo it:** `cortex claims --unasked` lists what chats saved about you, each with its undo; `cortex undo <session>` reverts one chat.
- **Review, in the app:** a small number beside **Review** in the app's menu counts only the cards left in **Today’s 5** (no popups, no notifications). Cortex saves what a chat infers about you at once, as a guess AI apps get marked (ask) (a line for `me/core` or `me/setup`, which reach every agent's rules, still waits for your Approve), and tidies on its own: of lines on one page that say nearly the same it keeps one (yours over a guess, then the newer, then the longer) and moves the rest to `## Past`; of two lines where only one says no, it keeps yours over a guess; of a line a chat said replaces another, the newer. Each of these is in `cortex undo`, and one you undo is not redone. **Today’s 5** on top picks at most five cards a day: lines that may steer an AI first, then teaching rules, lines that clash, corrections you repeat, and lines two or more chats suggested (a line only one chat suggested stays in the lists below and drops out after 14 days). When they are answered, or there are none, the top says **Done for today**; every other list sits folded under **More, only if you want**, closed until you open it. A card stands for all the lines or items that say nearly the same, and one answer covers them all. Keys: J and K move between cards, Y approves, N dismisses, R retires, S skips. Only **Corrections you repeat** has an **Approve all**. **Waiting for your approval** lists teaching rules a chat suggested (and any line about you an older Cortex left waiting): **Approve** saves one as yours (the strongest kind of yes, since you clicked it yourself), **Dismiss** drops it and Cortex never suggests it again. **Past their end date** lists lines whose `(until …)` day has passed (**Retire**). **Still true?** offers up to 3 lines a month, oldest first, that you confirmed long ago (180 days for preferences and personality, 365 for profile facts): **Still true** keeps one, **Retire** moves it to `## Past`. **Lines that may conflict** shows up to 3 pairs at a time: two lines about the same thing where only one says no (never, don't, avoid…) and both are yours, or both are guesses; two lines on one page that may disagree (only one says no, but they are not plain opposites), whoever wrote them; and a line a chat guessed replaces an older one that Cortex could not merge without losing a fact (until you answer, AI apps get only the newer line; the older is on the left, so **Keep second** keeps the newer). Each card offers **Keep both** (which also covers the same pair said in other words later), **Keep first** or **Keep second** (the other line and its near-copies are retired to `## Past`). **Corrections you repeat** offers a correction you typed in several imported AI chats ("don't …", "stop …", "always …", "never …", "no, I said …", "please use …", and softer ones: "I prefer …", "from now on …", "too long", "not what I meant", "actually …"), "You told 3 chats: …". A standing rule (always, never, from now on) is a core line and needs 3 chats: **Approve** adds it to `me/core.md`, so every chat starts with it. Any other is a preference and needs 2 chats (3 for a softer wording): **Approve** adds it to `me/preferences.md` under `## Personal`. **Dismiss** drops it for good. Private chats are never scanned, and nothing is sent anywhere or to a model. **Lines you asked to forget** shows a `//forget:` that matched no line or several: **Retire** the line you meant, or **Dismiss** to leave everything as it is. If you dismiss more cards than you approve from one source (lines chats suggest, imported chats, repeated corrections, commit decisions or //forget) over 20 or more cards, Review stops offering that source; Status names it, and **Resume** brings its cards back. **Imported notes you edited** shows a note page you changed by hand whose file changed too, so a re-import added the new version beside it: **Dismiss** leaves both pages as they are. **Lines that may steer an AI** shows a line on a `me/` page that reads like an instruction to an AI ("ignore previous instructions", a chat template token such as `[INST]`) or holds invisible characters; AI apps do not get it until you click **Keep it**, and **Keep it out** moves it to `## Past`. Cortex refuses to save such a line from a chat, a `//pref` or `//fact`, or Approve, and says what it found. **Changed outside Cortex: keep?** shows a line on `me/core`, `me/setup` or your teaching contract that Cortex did not write or approve (another program, or your own edit outside the app; an edited line counts as new), with its page; AI apps do not get it until you click **Keep**, and **Remove** moves it to `## Past` (or deletes it when it is not a claim line). Both are in `cortex undo`, and these cards come first in **Today’s 5**. Every line already there when you upgraded counts as yours. A guess one AI app made about you reaches only that app until you confirm it. **Rarely reach your apps: still worth keeping?** shows a line the Agent X-ray saw cut for space in most of the last 40 first calls that offered it (**Keep** pins it so AI apps get it first, **Retire**, or **Skip**), and **Still how you do this?** shows a playbook older than 60 days that no AI app was served in the calls the X-ray still holds (**Keep** asks again in 60 days, **Open** opens it in Obsidian); these two lists share 3 cards a month. **You keep coming back to this. Pin it to Core?** offers, at most once a month, one of your confirmed preferences that came back in 4 or more chats from at least 2 AI apps, or that AI apps got near the top of their preferences 10 times: **Pin to Core** adds it to `me/core.md`, so every chat starts with it, and **Not now** leaves it alone for 6 months. A line that keeps coming back also counts as confirmed again for **Still true?**, and among equally good preferences AI apps get it first. Private pages never show.
- **When a line started, and guesses no app uses:** when a line about you replaces another (Cortex's tidy, **Keep first** or **Keep second** on a card, or a chat that revises a line or says it replaces one), the line that stays gets `(since DAY)` on its page and the old one `(until DAY)` in `## Past`. A brand-new line gets neither, and a line that already has a day keeps it. AI apps never get these days, so their rules files and first calls stay the same from day to day; Chat uses them to tell what held in a month you ask about. Inside each section of a first call your pinned lines come first, then the lines AI apps were served, you confirmed or chats said again most, counted up to yesterday, so the order stays the same all day. Once a day the tidy also moves to `## Past` each guess an AI app made more than 30 days ago that no AI app was served (as far as the Agent X-ray remembers), you never confirmed or pinned and no chat said again; never a line you wrote or confirmed, a Core line or your teaching contract. The receipt says "Cortex retired N old guesses no app used", one `cortex undo` brings them back, and an undone one is not retired again.
- **Tonight, in the app:** closes the day. **Today** shows the day's activity hour by hour from 04:00 (darker means busier; the current hour is outlined), with counts of pages added and changed, chat turns and things AI apps recorded, and where they came from. **Today’s 5** is Review's own cards, answered with the same keys. **What AI apps learned today** lists what each AI app saved today, grouped by chat, with one **Undo** per chat (the same as `cortex undo`; it asks first, and a chat still open can be undone once it closes). **Tomorrow** names up to 3 concepts due tomorrow. Private pages never show. Opened between 8 p.m. and 4 a.m., the app starts on Tonight; Home is one click away.
- **Agent X-ray, in the app:** shows what Cortex told each AI app (Claude Desktop, Claude Code, Cursor…) in its first calls: when, which call, the pages and lines about you it served, what it cut for space and how many characters it used. A private page shows only as "a private page". **Pin** a page or line and Cortex serves it at the start of every chat; **Mute** one and Cortex never serves it. Only the app can pin or mute; an AI app cannot. Cortex keeps the last 500 calls, as ids only, never your messages (`.cortex/xray.jsonl`; pins and mutes in `.cortex/xray.json`). Each AI app's board starts with three things. **Reads:** one button per scope, pressed for what that app may read, and a line naming the rule under Connect → Agent profiles that decides it (and which other apps share that rule). Press a scope to turn it on or off; this changes that rule for every app it covers. An app no rule names gets what "Any other app" gets, and pressing a scope for it adds a rule for that app alone, so other unknown apps keep what they had. While **Everything except private** is on, the other scopes stay on; turn it off first to choose them one by one. **Served most often:** up to 5 pages and lines Cortex served that app most across its recorded calls, with Pin and Mute. **Dry run:** type a message and press **Show** to see what that app's first call would get for it right now: the same pages and lines, under its rule, pins and mutes, with nothing sent to the app and nothing logged.
- **Your teaching contract changes only by your click:** when a chat suggests a new teaching rule, it waits in Review until you click **Approve** there, even if you said yes in the chat. Your own edits to `me/teaching-contract.md` in Obsidian work as always.
- **How long a subject stays sharp, on Home:** under Next up, one line per subject, such as "Stays sharp about 3 weeks": the middle value, over that subject's checked concepts, of how long you would still get about 9 in 10 recalls right. It needs 3 checked concepts first ("Not enough checks yet"), works it out from your results each time (nothing is saved in your notes) and leaves private subjects and concepts out.
- **The 90-second check, on Home:** when something is due today or tomorrow, **90-second check** on the Next up board opens up to 3 due topics, the most overdue first. Recall what you know, press **Reveal** to see the one-line summary from its page, then say how it went: **Forgot**, **Shaky** or **Knew it**. Each answer is saved as a check result straight away (taken at your word, like any other), and `cortex undo` can take it back. When the 90 seconds run out, topics you did not answer are simply not saved. Esc closes it. Private topics never appear.
- **Your behaviour patterns:** notes tagged `patterns` (the dashboard's You tab lists them).
- **Your decisions and open loops:** the pages in `decisions/` and `loops/`, and the dashboard's Ideas tab.
- **Your weeks, months and years:** the pages in `rollups/` (`rollup-2026-w47`, `rollup-2026-11`, `rollup-2026`). Each one counts what Cortex holds for its days and lists the top items: notes and saved AI chats (linked), journal lines, study per topic, and the most visited sites. Code writes them, with no AI model, so Chat can answer "what did I do in March?" from one page. A week page is short (at most 1,600 characters) and always has the same five sections: totals and most visited sites; projects and decisions; other notes and AI chats; people and calendar events; study and journal. Imported pages (notes, documents, repos, highlights, AI chats) start with a few fact lines (When, Who, Where, Summary), only the ones Cortex knows. Private notes, private topics and private or local-only browsing go only on a separate `…-private` page, which only Chat's local model reads under its Private switch. A page is rewritten only when what it counts changes.
- **The Cortex app on Windows:** its own window and an amber tray icon. Closing the window keeps Cortex in the tray; click the icon to bring it back, right-click it for **Start with Windows** and **Quit**. It notifies you once a day when reviews are due, 3 days and 1 day before an exam, when a long task finishes or fails, never naming a private page, and once when a new version is ready. These are `app: {notify: {reviews, exams, jobs, updates}}` in `~/.cortex/config.yaml` (all on); the window's size and place are kept in `~/.cortex/desktop.json`.
- **Updates:** the app checks for a new version when it starts and every 6 hours. An installed Cortex downloads and checks it in the background; one run from source just notes it. Either way a **Restart to update** button appears at the top of the home page and on Status → Updates (in a narrow window it is an icon; hover it for the version). Nothing is installed until you click it. A downloaded installer is kept, so after a restart the button is back without downloading it again.

### Vault history in git: your choice

The app's **Vault** page has two settings under **History** (in `~/.cortex/config.yaml` they are `git: {enabled: ..., private_browsing: ...}`):

- **Keep the vault's history in git** (on by default). Each chat's changes become a git commit, so `cortex undo <session>` can revert them. Git keeps every old version of every page, so the vault folder grows over time; with a very large vault you may prefer to turn it off. Off: Cortex makes no commits, `cortex init` does not create a repo, `cortex undo` explains that it needs this setting, and `cortex doctor` reports git as off by your choice. If a `.git` folder is still in the vault, doctor still warns when it has a remote or when old commits hold private browsing pages, since those matter for privacy whether git is on or off. A history that already exists is left as it is, and turning the setting back on carries on from it. Everything else (imports, chat, the app, the AI apps) works the same without git.
- **Include private browsing pages in git history** (off by default; only with the first setting on). Off: `browse/private/` stays out of git. On: Cortex takes its own `browse/private/` line out of the vault's `.gitignore`, and your private browsing pages are committed with everything else, so they take disk space in the history too, undo covers them, and a page you later remove or forget stays in old commits. Turning it off again puts the line back and stops tracking the folder at the next browse import (or when you save the setting in the app).

**One catch:** the Claude desktop **Chat tab** uses a separately uploaded copy of the skill, which predates `harvest`, `sweep` and `mock`. To get them there, download `cortex-skill.zip` from the latest release (github.com/alenissacsam-2/cortex-learner/releases) and upload it again in Chat's skill settings (from a source checkout: `uv run python scripts/build_skills.py` builds it into `dist/`). Claude Code already has them after a restart.
