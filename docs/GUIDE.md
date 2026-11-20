# Cortex: a user guide

Cortex is your second brain. It is a folder of notes (your vault, also an Obsidian vault) plus a small program that every Claude chat can read from and write to.

It has two jobs:
- **Remember you:** what you know, what you get wrong, your projects, decisions and preferences.
- **Plan your study:** what to revise today, what you're forgetting, what an exam needs.

Cortex acts only when a chat (or you, in the app or a terminal) asks it to. The one exception is browsing: after your first browsing import, the app and the receiver import new history by themselves every few hours (section 6).

---

## 1. What happens without you doing anything

In every new Claude Code chat:
- **Prime.** Your first message is matched against your vault, and the chat starts already knowing your profile, your teaching rules, the preferences that fit the message, and a few related pages. Lines you put in `me/core.md` (one `- ` line each, under its `## Core` heading) are pinned: every chat gets them first and word for word, up to 500 characters in all; a line that does not fit is left out whole. Only lines you write or confirm count there, never ones an assistant added.
- **Recording.** The chat saves durable things as they come up: a fact about you, a source you shared, an insight, a real decision, a quiz answer. It tells you briefly when it wrote something.
- **Asking first.** Anything about who you are (a preference or personality line) is saved only after your yes. In Claude Code you'll get a small "save this line about you?" dialog.
- **Undo.** Each chat's writes are one commit in the vault's git. `cortex undo <session>` reverts it. You can turn git off (see "Vault history in git" in section 9); undo is then off.

The Claude desktop **Chat tab** gets the same tools but no automatic prime, so the chat calls `cortex_prime` itself. That call also starts the chat's log, and the chat logs each later message of yours as it goes, so every Chat-tab chat is saved as well ("Past AI chats saved automatically" below). The desktop **Code tab** runs Claude Code, so it works as above.

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

### Second brain
| Command | What it does |
|---|---|
| `/cortex about <topic>` | Looks up what you know and have about a topic |
| "what do I know about X?" (plain words, no command) | Semantic recall over your whole vault |
| `/cortex capture` | Saves a source you paste or link (article, PDF, video) and the ideas worth keeping |
| `/cortex project <name>` | Works on one project page: status, decisions, next steps |
| `/cortex reflect` | The weekly review: confirm or drop the about-me lines waiting for you, emerging idea clusters, old fleeting notes, 3 spot-checks of how a chat graded you |
| `/cortex map` | Opens the Cortex app's home (Today, readiness, the graph) |

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

---

## 3. Harvest, explained

Chats you import from Gemini, Perplexity and Claude are raw transcripts: searchable, but nothing has been learned from them.

`/cortex harvest` turns them into knowledge, about 10 minutes a night:
1. Cortex gives the chat a **bundle**: whole topic hubs (for example "SQL and databases"), about 12 chats at a time, placement topics first. Only **your own messages** are shown.
2. The chat writes short lines under that hub's **`## What I explored`** section, linked to your concept pages, plus at most one note and any decisions or projects it finds.
3. Anything about you goes on one yes/no card that cites the chat.
4. Harvested chats rank lower in search afterwards, so the distilled pages win.

It never edits or deletes an imported chat, and it never counts an old chat as a revision today. `cortex harvest status` shows progress and the undo command for each run.

Is it worth it now? It's not urgent while a test is close. Try it on an evening when you're too tired to study.

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
| `cortex export context` | A paste-ready "about me" file for ChatGPT, Gemini or Cursor |
| `cortex import <file>` | Imports a chat export, notes, a calendar or Google activity (below) |
| `cortex chats` | Your past chats, newest first |
| `cortex harvest status` | Harvest progress |
| `cortex undo <session>` | Reverts one chat's changes |
| `cortex doctor` | Health check, including which Claude apps are connected and when each last called Cortex |
| `cortex lint` / `cortex relink` | Find vault problems / fix the Obsidian links |
| `cortex rollups [--since YYYY-MM]` | Writes the week, month and year summary pages in `rollups/` (below). The app also does this every 10 minutes |
| `cortex eval` | How well search and the study forecasts are doing |
| `cortex backup` | Zips the whole vault, private browsing and its stored visit history included (not chat history; see [PRIVACY.md](PRIVACY.md#your-data)). Keep the zip as private as the vault |
| `cortex exclude add <folder>` | Turns Cortex off in a folder (e.g. a work repo) |
| `cortex kill` | Emergency stop for anything that acts as you |

`cortex --help` lists everything.

### Importing your history

Get the export from the service, then run `cortex import <the .zip, folder or file>`. It takes chat exports (claude.ai, ChatGPT, Gemini, Perplexity), notes (Obsidian, Notion, any folder of .md or .txt), a calendar (.ics) and Google activity (Takeout), and works out which one it got. If it can't, add `--source` with `claude`, `chatgpt`, `gemini`, `perplexity`, `google-activity`, `obsidian`, `notion`, `notes` or `calendar`. `--dry-run` prints the counts and writes nothing; `--private` makes the imported notes or calendar pages private.

Secrets are redacted on import. Importing a newer chat export again replaces the older copy instead of adding a duplicate; notes and events already imported are skipped. A large ChatGPT or claude.ai export split into several `conversations*.json` files is read whole; a conversation in two of them is kept once, its newest copy. Then run `/cortex` to learn from the chats. Imported notes and calendar months become pages in `resources/` that Claude can read, unless a note's frontmatter marks it private (`private: true`, `yes` or `on`, or a `private` tag) or you used `--private` ([PRIVACY.md](PRIVACY.md)). `cortex import-chats` still imports chat exports only.

Every import ends with a report line, for example `Obsidian: read 10, imported 6, private 3, duplicate 0, skipped 4 (3 not a note, 1 over 5 MB)`: the items read, imported, made private, already imported (or twice in the export), and skipped, with the reason.

### Past AI chats saved automatically

While the app runs, every 5 minutes (the first time a minute after it starts) it saves each new or changed past AI chat as a chat page in `resources/`: your Claude Code chats (in a terminal or the desktop app's Code tab), your Cowork tasks, your Chat-tab chats and the chats you imported. No AI model is involved. An ordinary chat keeps only your own messages, with secrets removed and the length capped. A coding session (one where files were edited or commands run) becomes a short summary: the project, the dates, what you asked (first lines), the files touched and the commit messages. It never includes code, diffs or command output. Newest chats go first, and the Status line shows the progress and time left. A chat you are still in waits until it has been quiet for 2 minutes, and a chat that grows later updates its page in place. Pages you edited or deleted, chats you saved yourself and chats an earlier import already saved are left alone. Chats in a category you set to skip are never saved, a category you keep on this computer makes the page private, and sessions in a folder you excluded are skipped. You get one notification per run that saved something, and the saved chats show in the home page's live feed on the day they were saved (private ones only while the lock is open). Turn it off with **Save new AI chats to your notes automatically** on the Status page. `cortex import-chats --auto` runs one pass by hand.

**Chat-tab chats.** The desktop Chat tab keeps no file Cortex can read, so Cortex logs each chat while it happens: `cortex_prime` records your first message, and the chat records each later message of yours, with a one-line gist of its previous answer, without asking. The log sits in `~/.cortex/claude-chat/claude-live/` with secrets removed and becomes a chat page of your messages, like any other chat, once the chat has been quiet for 2 minutes. Ask a chat whether it saves your chats and it should say every chat is logged in Cortex. To check the connection, run `cortex doctor`: for Claude Desktop and Claude Code it shows whether Cortex is set up, whether Claude Code has the prime hook, and when each one last called Cortex. A model can forget a turn; the first message of each chat is always logged. If you later import a claude.ai export that holds the same chat (the same first message, started within 15 minutes of the log), it does not become a second page: the chat's page is filled from the export, which has every message, plus any messages you sent after the export was made. This works whichever comes first, the log or the export. When two chats could match, each keeps its own page.

To turn imported chats into context, run `cortex digest` when you want to. It lists each chat imported since your last digest with up to 3 topics from your catalog that it seems to be about (local search, no AI model; private topics are never proposed), and asks about each one. A yes records a topic touch: context for your plan, never a grade, so a topic's level moves only after a real check. The listed chats are not shown again. `--dry-run` only lists, `--yes` accepts every topic shown, and `--since DATE` and `--limit N` (default 10) narrow the list. Claude Code chats are left out: a connected Claude records topics as you talk.

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

**Google activity (Search, YouTube and Chrome).** Wait: usually the same day, up to a few days. Keep the Takeout .zip as it is. Its rows go through the browse privacy filter and land private by default, under the browse rules in section 6. `cortex import <the .zip>` imports them (so does `cortex browse import --takeout`). Takeouts in English, Spanish, Portuguese, French, German, Hindi, Indonesian and Japanese are read; in other languages, rows whose date or product Cortex cannot read are skipped (HTML ones are counted).
1. Open takeout.google.com and click Deselect all.
2. Tick My Activity, click All activity data included, click Deselect all, tick Search, YouTube and Chrome, then OK.
3. Leave Activity records as HTML (the default) or set them to JSON under Multiple formats; Cortex reads both.
4. Click Next step, keep "Send download link via email" and .zip, then Create export.
5. Download the .zip from Google's email (the link works for 7 days).

**Notes (Obsidian, Notion or a folder).** Instant for a folder; minutes to hours for a large Notion workspace. Give the Obsidian vault folder, the Notion .zip as it is, or the folder of notes. A note with `private: true` in its frontmatter stays private.
1. Obsidian: nothing to export; pick your vault folder (the one holding `.obsidian`). Hidden folders and attachments are skipped.
2. Notion: open Settings, then General, and click Export all workspace content. Set Export format to Markdown & CSV, include everything, click Export and download the .zip.
3. Any other notes app: export or copy your notes as .md or .txt files into one folder.

**Calendar (Google, Outlook or Apple).** Instant. Give the .ics file, or Google's .zip as it is; attendees' emails are left out. Each month becomes one page.
1. Google Calendar (on a computer): Settings (gear) > Settings > Import & export > Export; a .zip of .ics files downloads.
2. Outlook (classic, Windows): open Calendar, File > Save Calendar, click More Options, set Date Range to Whole calendar and Detail to Full details, then save as iCalendar (.ics).
3. Outlook.com: Settings > Calendar > Shared calendars > Publish a calendar, pick All details, Publish, then open the ICS link to download the file.
4. Apple Calendar (Mac): select a calendar in the list, then File > Export > Export and save the .ics (one calendar at a time).

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
- **Today:** shows everything from today (your day starts at 04:00): pages taught, tested or asked about, and pages added or saved. The camera frames them all, and every one is highlighted as chosen while the rest dims. `[` and `]`, or the arrows beside the button, open them one at a time ("2 of 7"). Press Today again, Esc or 0 to leave.
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
Once a local model runs (Ollama or LM Studio; Qwen 3.5 2B is enough), talk with it about everything Cortex holds: in `cortex app` → **Chat**. Ask a question, then follow up ("and its running time?"); each answer searches again, with your new message and the one before it.
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
- **While it works:** the reply shows each step with a live count of seconds: "Searching your notes and browsing", then "Reading 8 pages with <model>", then "Thinking" for a model that reasons first; a data answer shows its own step ("Counting the sites you visited", "Putting together your day", "Reading your study plan"). When the answer is done its time is shown next to the model's name ("Cortex" for a data answer).
- **Stop, scroll and copy:** Send turns into Stop while an answer comes in, and Esc does the same; a stopped answer is kept as far as it got. Scroll up and the page stops following the answer; **New text** jumps back to the end. Every finished answer has **Copy**, which copies its text without the [n] markers.
- **Sources:** the answer cites pages as [1], [2]; click a number to jump to its source, and a source to open it in Obsidian. When nothing fits it says "I don't know from your archive." When an answer cites nothing, the pages it read are listed under **Searched, not cited**.
- **Settings** (Chat settings, top right; saved under `chat:` in `~/.cortex/config.yaml`): answer length (short, normal, long), creativity (0 to 1, default 0.2), pages read per answer (4 to 12, default 8), which sources to search (Notes and mistakes, Saved AI chats, Browsing, Private notes and browsing; all on by default), and Keep chat history (on by default). Each setting shows "Saved" when it is stored.
- **History:** each chat is a file in `<vault>/.cortex/browse/chat/`, inside the private browse folder: never shown to Claude or any other AI app, never on the dashboard. Delete one with its Delete button, or all with Chat settings → Delete all chats. With Keep chat history off, new chats are never written and are forgotten when the app stops.

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
| **Need** | A fact about you a chat wished it had; Cortex asks you later, one at a time |
| **Loop** | An open item: a commitment, question or idea |
| **Resurfacing** | Old ideas and decisions brought back at growing intervals |

---

## 9. Where things are

- **Your vault:** your notes folder. In Obsidian, Ctrl+G shows the graph, coloured by subject.
- **What changed and how to undo it:** `cortex claims --unasked` lists what chats saved about you, each with its undo; `cortex undo <session>` reverts one chat.
- **Review, in the app:** a small number beside **Review** in the app's menu counts what waits for your own click (no popups, no notifications). **Waiting for your approval** lists lines about you and teaching rules a chat suggested: **Approve** saves one as yours (the strongest kind of yes, since you clicked it yourself), **Dismiss** drops it and Cortex never suggests it again. **Past their end date** lists lines whose `(until …)` day has passed (**Retire**). **Still true?** offers up to 3 lines a month, oldest first, that you confirmed long ago (180 days for preferences and personality, 365 for profile facts): **Still true** keeps one, **Retire** moves it to `## Past`. **Lines that may conflict** shows up to 5 pairs of lines about you that probably clash: a new line a chat saved that looks like an update of an older one, or two lines about the same thing where only one says no (never, don't, avoid…): **Keep both**, **Keep left** or **Keep right** (the other line is retired to `## Past`). **Corrections you repeat** offers a correction you typed in two or more imported AI chats ("don't …", "stop …", "always …", "never …", "no, I said …", "please use …") as a core line, "You told 3 chats: …": **Approve** adds it to `me/core.md` as your own line, so every chat starts with it; **Dismiss** drops it for good. Private chats are never scanned, and nothing is sent anywhere or to a model.
- **Your teaching contract changes only by your click:** when a chat suggests a new teaching rule, it waits in Review until you click **Approve** there, even if you said yes in the chat. Your own edits to `me/teaching-contract.md` in Obsidian work as always.
- **Your behaviour patterns:** notes tagged `patterns` (the dashboard's You tab lists them).
- **Your decisions and open loops:** the pages in `decisions/` and `loops/`, and the dashboard's Ideas tab.
- **Your weeks, months and years:** the pages in `rollups/` (`rollup-2026-w47`, `rollup-2026-11`, `rollup-2026`). Each one counts what Cortex holds for its days and lists the top items: notes and saved AI chats (linked), journal lines, study per topic, and the most visited sites. Code writes them, with no AI model, so Chat can answer "what did I do in March?" from one page. Private notes, private topics and private or local-only browsing go only on a separate `…-private` page, which only Chat's local model reads under its Private switch. A page is rewritten only when what it counts changes.
- **The Cortex app on Windows:** its own window and an amber tray icon. Closing the window keeps Cortex in the tray; click the icon to bring it back, right-click it for **Start with Windows** and **Quit**. It notifies you once a day when reviews are due, 3 days and 1 day before an exam, when a long task finishes or fails, never naming a private page, and once when a new version is ready. These are `app: {notify: {reviews, exams, jobs, updates}}` in `~/.cortex/config.yaml` (all on); the window's size and place are kept in `~/.cortex/desktop.json`.
- **Updates:** the app checks for a new version when it starts and every 6 hours. An installed Cortex downloads and checks it in the background; one run from source just notes it. Either way a **Restart to update** button appears at the top of the home page and on Status → Updates (in a narrow window it is an icon; hover it for the version). Nothing is installed until you click it. A downloaded installer is kept, so after a restart the button is back without downloading it again.

### Vault history in git: your choice

The app's **Vault** page has two settings under **History** (in `~/.cortex/config.yaml` they are `git: {enabled: ..., private_browsing: ...}`):

- **Keep the vault's history in git** (on by default). Each chat's changes become a git commit, so `cortex undo <session>` can revert them. Git keeps every old version of every page, so the vault folder grows over time; with a very large vault you may prefer to turn it off. Off: Cortex makes no commits, `cortex init` does not create a repo, `cortex undo` explains that it needs this setting, and `cortex doctor` reports git as off by your choice. If a `.git` folder is still in the vault, doctor still warns when it has a remote or when old commits hold private browsing pages, since those matter for privacy whether git is on or off. A history that already exists is left as it is, and turning the setting back on carries on from it. Everything else (imports, chat, the app, the AI apps) works the same without git.
- **Include private browsing pages in git history** (off by default; only with the first setting on). Off: `browse/private/` stays out of git. On: Cortex takes its own `browse/private/` line out of the vault's `.gitignore`, and your private browsing pages are committed with everything else, so they take disk space in the history too, undo covers them, and a page you later remove or forget stays in old commits. Turning it off again puts the line back and stops tracking the folder at the next browse import (or when you save the setting in the app).

**One catch:** the Claude desktop **Chat tab** uses a separately uploaded copy of the skill, which predates `harvest`, `sweep` and `mock`. To get them there, download `cortex-skill.zip` from the latest release (github.com/alenissacsam-2/cortex-learner/releases) and upload it again in Chat's skill settings (from a source checkout: `uv run python scripts/build_skills.py` builds it into `dist/`). Claude Code already has them after a restart.
