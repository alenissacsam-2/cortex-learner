# Cortex: a user guide

Cortex is your second brain. It is a folder of notes (your vault, also an Obsidian vault) plus a small program that every Claude chat can read from and write to.

It has two jobs:
- **Remember you:** what you know, what you get wrong, your projects, decisions and preferences.
- **Plan your study:** what to revise today, what you're forgetting, what an exam needs.

Cortex never does anything on its own. It acts only when a chat (or you, in a terminal) asks it to.

---

## 1. What happens without you doing anything

In every new Claude Code chat:
- **Prime.** Your first message is matched against your vault, and the chat starts already knowing your profile, your teaching rules, the preferences that fit the message, and a few related pages.
- **Recording.** The chat saves durable things as they come up: a fact about you, a source you shared, an insight, a real decision, a quiz answer. It tells you briefly when it wrote something.
- **Asking first.** Anything about who you are (a preference or personality line) is saved only after your yes. In Claude Code you'll get a small "save this line about you?" dialog.
- **Undo.** Each chat's writes are one commit in the vault's git. `cortex undo <session>` reverts it.

The Claude desktop **Chat tab** gets the same tools but no automatic prime, so the chat calls `cortex_prime` itself.

---

## 2. The `/cortex` commands

Type these in a Claude chat. The word after `/cortex` is the mode; anything after that is the topic.

### Study (placement prep)
| Command | What it does | When to use it |
|---|---|---|
| `/cortex` | Home card: your life map, what's due, what's waiting for you. Then it asks what you want. | Any time; the safe starting point |
| `/cortex learn` | Today's plan in order: things taught but not yet checked, confident misses, due refreshes, sprint items, new topics. Retrieve first, teach the gap, re-test. | A normal study session |
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
| `/cortex map` | Opens the offline dashboard (Today, readiness, the graph) |

### Past chats
| Command | What it does |
|---|---|
| `/cortex save` | Saves what's worth keeping from **this** chat (you approve the list first) |
| `/cortex history` | Saves older Claude Code chats from this laptop, a few at a time |
| `/cortex harvest` | Distils your **imported chats** (Gemini, Perplexity, Claude) into a few good pages, see below |

### About you
| Command | What it does |
|---|---|
| `/cortex probe` | "Predict me": the chat guesses your answer to a question about you before you reply, next to what a typical student would say. Over time this shows how well Cortex knows you, per area. |
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
| `cortex brief` | Your brief, teaching contract and Today |
| `cortex dashboard` | Opens the offline dashboard |
| `cortex recall "query"` | Searches your vault by meaning and keywords |
| `cortex ask "question"` | Answers from your notes, saved AI chats and browsing with your local model, with sources |
| `cortex claims --summary` | How well Cortex knows you, per page (and which lines you clicked vs a chat relayed) |
| `cortex claims --unasked` | What chats saved about you without asking, each with its undo |
| `cortex export context` | A paste-ready "about me" file for ChatGPT, Gemini or Cursor |
| `cortex import <file>` | Imports a chat export, notes, a calendar or Google activity (below) |
| `cortex chats` | Your past chats, newest first |
| `cortex harvest status` | Harvest progress |
| `cortex undo <session>` | Reverts one chat's changes |
| `cortex doctor` | Health check |
| `cortex lint` / `cortex relink` | Find vault problems / fix the Obsidian links |
| `cortex eval` | How well search and the study forecasts are doing |
| `cortex backup` | Zips the whole vault |
| `cortex exclude add <folder>` | Turns Cortex off in a folder (e.g. a work repo) |
| `cortex kill` | Emergency stop for anything that acts as you |

`cortex --help` lists everything.

### Importing your history

Get the export from the service, then run `cortex import <the .zip, folder or file>`. It takes chat exports (claude.ai, ChatGPT, Gemini, Perplexity), notes (Obsidian, Notion, any folder of .md or .txt), a calendar (.ics) and Google activity (Takeout), and works out which one it got. If it can't, add `--source` with `claude`, `chatgpt`, `gemini`, `perplexity`, `google-activity`, `obsidian`, `notion`, `notes` or `calendar`. `--dry-run` prints the counts and writes nothing; `--private` makes the imported notes or calendar pages private.

Secrets are redacted on import. Importing a newer chat export again replaces the older copy instead of adding a duplicate; notes and events already imported are skipped. A large ChatGPT or claude.ai export split into several `conversations*.json` files is read whole; a conversation in two of them is kept once, its newest copy. Then run `/cortex` to learn from the chats. Imported notes and calendar months become pages in `resources/` that Claude can read, unless a note's frontmatter marks it private (`private: true`, `yes` or `on`, or a `private` tag) or you used `--private` ([PRIVACY.md](PRIVACY.md)). `cortex import-chats` still imports chat exports only.

Every import ends with a report line, for example `Obsidian: read 10, imported 6, private 3, duplicate 0, skipped 4 (3 not a note, 1 over 5 MB)`: the items read, imported, made private, already imported (or twice in the export), and skipped, with the reason.

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

`cortex dashboard` opens the brain as a 3D picture of your vault.

**What you see**
- Each topic has its own colour.
- Bright fibres are your own links between pages. Faint fibres join pages with a similar meaning.
- Big bright dots are pages with many connections.
- A faint outline shows the shape of a brain.

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
- **Today:** replays the pages you saved today.
- **Unsorted chats:** shows chats with no topic as a faint ring under the brain.
- **Look:** opens the Look panel.

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
7. `cortex browse forget --site <domain>` (or `--category <name>`, for example `--category health`) removes that site or category from everything Cortex stored: day and site pages (private ones too), visit counts, search vectors, site summaries and the extension's archive. It prints what it removed. Add the site to `deny_domains` as well, or the next import reads it again from your browser.

**One page per website.** Each import also writes a page per site you visited, in a folder per category: `browse/sites/<category>/` for what Claude may see, and the private folder for hidden categories. A site with some hidden visits (a video site, say) gets both: the visible page never counts or names the hidden ones. Each page has the visits, first and last seen, top pages, the searches that led there, related sites and links to your topics. `browse/browse-profile.md` sums it up: when you browse (by hour and weekday), top categories and sites, interests and months. The private profile adds the hidden categories, with their top sites and searches. `browse/sites.csv` lists the sites for a spreadsheet.

**The first big run** (for example one browser's history since a start date, with a small local model):
1. `cortex browse import --browser chrome --since 2026-08-01` writes the day and site pages. Sites the built-in table doesn't know go under `other` for now.
2. `cortex browse deepen` lets the local AI sort the `other` sites into categories, write a one-line description per site and suggest related sites and topics, most visited sites first. It stops when its time runs out (`localllm.budget_s`, or `--budget-s 3600` for an hour) and remembers where it stopped, so run it again to go on. Ctrl+C keeps what it has done. The receiver (`cortex browse serve`) runs it by itself after 30 minutes without browsing.

**Optional: a local AI for summaries and `deepen`**
Run `cortex app` → Setup. It finds or installs a runner (Ollama is recommended; LM Studio is the alternative), suggests a model that fits this computer, downloads it, tests it with a made-up sentence and turns it on.

Later, `cortex app` → **Local model** lets you switch: the model list shows every chat model already installed in Ollama or LM Studio (embedding models are left out). Pick one and click **Save choice**. **Test** sends one made-up sentence and shows the answer and time; **Load** starts LM Studio and loads an LM Studio model (Ollama loads its models by itself). To get another Ollama model, type its name from ollama.com/library (for example `gemma3:4b`) and click **Download**; Ollama must be running, and cloud models are refused. The page also says which size suits this computer, with a button that fills in its name.

Without the app: install Ollama, run `ollama pull qwen3.5:4b` (`qwen3.5:2b` with under 6 GB of graphics memory or RAM, `qwen3.5:9b` with 10 GB or more), then add `localllm: {enabled: true}` to `~/.cortex/config.yaml` (for LM Studio, `localllm: {enabled: true, runner: lmstudio}`). `cortex llm status` checks it.

It only talks to your own machine. For a public day page it hears your topic names only; for a site, that site's page titles. It can move a site into a sensitive category, never out of one. Without it, the summary line lists your top topics and unknown sites stay under `other`.

**Chat**
Once a local model runs (Ollama or LM Studio; Qwen 3.5 2B is enough), talk with it about everything Cortex holds: in `cortex app` → **Chat**. Ask a question, then follow up ("and its running time?"); each answer searches again, with your new message and the one before it.
- **What it searches:** your notes and mistakes, the AI chats you imported (Claude, ChatGPT, Gemini, Perplexity), and your browsing, private pages included. Only the model on this computer reads them; Chat never uses a cloud model, whatever the cloud settings say, and refuses any runner that is not on this computer.
- **Dates:** "yesterday", "3 days ago" (a day either side), "last week", "this month", "on 12 Nov", "in October" and "since 2026-11-01" keep the answer to pages dated then: the browsing day pages, notes with a date, and other notes by the day they were last changed. "Which sites did I visit yesterday?" lists that day's pages, newest first.
- **While it works:** the reply shows each step with a live count (searching, reading the pages with your model, thinking), and the answer's time when done.
- **Sources:** the answer cites pages as [1], [2]; click a number to jump to its source, and a source to open it in Obsidian. When nothing fits it says "I don't know from your archive." When an answer cites nothing, the pages it read are listed under **Searched, not cited**.
- **Settings** (Chat settings, top right; saved under `chat:` in `~/.cortex/config.yaml`): answer length (short, normal, long), creativity (0 to 1, default 0.2), pages read per answer (4 to 12, default 8), which sources to search (notes, saved AI chats, browsing, private browsing), and Keep chat history.
- **History:** each chat is a file in `<vault>/.cortex/browse/chat/`, inside the private browse folder: never shown to Claude or any other AI app, never on the dashboard. Delete one with its Delete button, or all with Chat settings → Delete all chats. With Keep chat history off, new chats are never written and are forgotten when the app stops.

With no local model or no search model, Chat shows what is missing with a button to Setup. In a terminal, `cortex ask "what did I read about binary search?"` answers one question from the same sources, without history or date phrases.

**Optional: the browser extension**
It adds time on screen and a short text excerpt of pages you read. It works in Chrome, Edge and Brave, and should work in Comet too (it is built on the same engine; not yet tried there). It never runs in private windows.
1. Follow `extension/README.md` and use Load unpacked.
2. Run `cortex browse pair` and paste the token in the extension's popup. (In `cortex app` → **Browsing**, **Set up the extension** pairs it, starts the receiver and opens the extension folder in one click.)
3. Keep `cortex browse serve` running while you browse, or turn on **Start at login** under **Browsing**.

**Settings** (in `~/.cortex/config.yaml`, under `browse:`)
- `visible_to_claude: false` or `true`. The default is `false`: only private pages, and no journal lines, topic touches or dashboard pages from browsing. With `true`, the day pages (your topics, time and counts only), the site pages and profile of ordinary sites and of every sensitive category in `cloud_categories`, journal lines and topic touches are written too. Switching back to `false`, hiding or skipping one more category, or denying a site, removes what is no longer allowed on the next import, from older pages, visit counts and search vectors too.
- `cloud_categories`: the sensitive categories Claude and other cloud AI apps may read, for example `[money]`. The default is none: every sensitive category stays on private pages, and so does any category a later release adds until you list it. An older config's `hidden_from_claude` (the categories kept private) still works.
- `skip_categories`: the categories never saved at all. The default is `[adult]` (from the older `adult: skip`).
- `adult: skip` or `private`, and `sensitive: private` or `skip`: the older settings; they only set the default of `skip_categories`.
- `browsers`: which browsers to read, for example `[comet]`. Leave it out to read every browser found.
- `deny_domains`: sites that are never read.
- `allow_domains`: if set, only these sites are kept.
- `private_domains`: sites treated like sensitive ones: they never add time to a public topic, and no text the extension reads there is kept.
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

8. `/cortex harvest` (10 minutes a night) and `/cortex probe`.
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
| **Clicked / relayed** | You ticked the dialog yourself / the chat said you said yes |
| **Exposed** | A chat that recently read outside text (a web page, an imported chat). What it writes about you is marked, and it can't change your teaching contract. |
| **Honest Double** | Checks text written as you (CV, answers) against what you've actually shown you know |
| **Need** | A fact about you a chat wished it had; Cortex asks you later, one at a time |
| **Loop** | An open item: a commitment, question or idea |
| **Resurfacing** | Old ideas and decisions brought back at growing intervals |
| **Letters** | Your own advice to a future tired you, shown when the moment matches |
| **Probe** | "Predict me" questions that measure how well Cortex knows you |

---

## 9. Where things are

- **Your vault:** your notes folder. In Obsidian, Ctrl+G shows the graph, coloured by subject.
- **What changed and how to undo it:** `cortex claims --unasked` lists what chats saved about you, each with its undo; `cortex undo <session>` reverts one chat.
- **Your behaviour patterns:** notes tagged `patterns` (the dashboard's You tab lists them).
- **Your decisions and open loops:** the pages in `decisions/` and `loops/`, and the dashboard's Ideas tab.

**One catch:** the Claude desktop **Chat tab** uses a separately uploaded copy of the skill, which predates `harvest`, `sweep` and `mock`. To get them there, run `uv run python scripts/build_skills.py` and upload `dist/cortex-skill.zip` again in Chat's skill settings. Claude Code already has them after a restart.
