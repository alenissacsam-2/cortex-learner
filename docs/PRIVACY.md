# Privacy

## Local first

Your vault is plain Markdown and JSONL in a folder you choose. Cortex needs no account and sends no telemetry. Settings live in `~/.cortex/` on your computer.

## When Cortex uses the network

**Downloads.** Only these reach the internet, and each one happens because you asked for it:

- **The search model** (about 150 MB, from Hugging Face, huggingface.co), once, into `~/.cortex/models`. This is the one download Cortex makes for you: `cortex setup` runs it after you say yes, and the app runs it when you click **Download search model**. You can also run `cortex model download` yourself. Nothing else downloads it; searches never do.
- **In the app, only when you click** (Setup, Local model and Status):
  - **Check for updates** asks GitHub (api.github.com) for the latest Cortex release. The request carries nothing about you, not even your Cortex version. If GitHub's API is limiting requests from your network, it reads the latest release from github.com's release page instead. **Update now** then downloads the new `CortexSetup.exe` and its checksum from that GitHub release.
  - Ollama itself (about 1 GB): `winget install Ollama.Ollama` on Windows (from ollama.com) or `brew install ollama` on macOS (from Homebrew). On Linux the app only shows the install command.
  - A local model: `ollama pull` (from Ollama's library) or `lms get` (LM Studio's catalogue), about 2 to 6 GB depending on the model you pick.
  - In Connect AI apps, adding the Claude Code plugin runs `claude plugin marketplace add` and `claude plugin install` on your Cortex folder.
- **Installing and upgrading** (uv, git and the Python packages) download from their usual sources.

**Servers on this computer only.** These listen on 127.0.0.1 (loopback), so no other computer can reach them:

- `cortex app`: the app page. Every request needs a key that changes each launch, and it stops after 30 minutes idle or when you click Quit.
- `cortex browse serve`: the receiver for the browser extension, on port 8799. It accepts only the paired extension's token. Before the app or the extension sends that token or any visit, the receiver must prove it holds the token (a keyed hash of a fresh random value), checked again before every send, so another program listening on the port gets nothing.
- The MCP server (`cortex mcp`) talks to Claude through standard input and output, not the network.

**Local model calls.** When you turn on a local model, Cortex talks to Ollama (127.0.0.1:11434) or LM Studio on this computer only. It refuses any address that is not loopback, ignores proxy settings, and refuses cloud models that a local runner can forward to (Ollama `-cloud` models, for example).

Searches, topic filing (`cortex topics`), the dashboard, the Claude Code hooks and every tool run offline.

## What the AI model sees

Cortex is a tool that Claude (or another MCP client) calls. **Whatever a tool returns goes to the AI model you are chatting with**, which is Anthropic when you use Claude.

Every new chat starts with a short Cortex context (`cortex_prime`): your profile summary, the preference and personality lines that match your first message, the core of your teaching contract, and the titles and summaries of related pages. In Claude Code the plugin's hook adds it to the first message of **every** chat, in every project, before Claude answers. In Chat, Claude calls `cortex_prime` itself. Cortex does not store your message; it only notes the chat's id in the vault so it adds the context once. To stop this in Claude Code, turn the plugin off with `/plugin disable cortex@cortex`, or stop it for one folder and everything under it with `cortex exclude add <folder>`: chats there get no context, and the Cortex server there refuses every tool, reads included, and tells Claude only that Cortex is turned off in that folder.

Cortex limits what it hands over:

- Pages marked `private: true` are left out of search, recall and context. People pages (`people/`) are private unless you mark one `private: false`. Apart from the people-page exception below, only your local model reads private pages, in the app's Chat and `cortex ask` (see below).
- **One exception: people pages.** When Claude asks for a people page by its exact id (`cortex_read`), Cortex returns it even if it is private, with a note asking the model to use it for the current task only. That note is a request, not a lock: the page's text goes to the AI app you are chatting with. If a person's page holds something no cloud AI should read, keep it out of the vault.
- Share tiers decide which of your `me/` pages may be shown for a given audience when Claude writes as you (`cortex_as_me`). Lines from other pages are held back by Cortex itself; the model only learns how many. A read-only (`reader`) or `delegate` Cortex serves only `me/` pages marked `share: work` or `public`, in every tool, never reads past chats (`cortex_chats`) or people pages, and writes nothing when it starts.
- The kill switch (`cortex kill`, undo with `cortex kill --off`) stops writing as you and drafting at once.
- `cortex export context` writes a short snapshot (profile, contract rules, current goals, confirmed preferences) only when you run it. Whatever you paste from it into another assistant goes to that assistant's company.

## Prompt injection

Notes can contain text from the web or other people. Cortex treats note text as data:

- Over MCP it wraps note text in `<untrusted_note>` tags, so the model can tell your notes from instructions.
- In Claude Code, a hook marks the session after Claude reads the web or another tool, and later changes to your `me/` pages are recorded as "exposed" or refused.
- Every write to your pages, events and generated files is scanned for secrets (keys, tokens, passwords) and refused if one is found. The working files in `.cortex/` (below) are copies that never enter git and are not scanned again.
- The dashboard runs with a strict content security policy and no network access.

These measures lower the risk; they do not remove it. Review changes to your `me/` pages now and then: every session is a git commit, and `cortex undo` reverts one.

## Your data

- The vault has its own git history, so every change can be reviewed and undone. You can turn it off on the app's Vault page (see Browsing below).
- A pre-push hook refuses `git push`. **Do not publish your vault**, for example to a public GitHub repository: it holds your profile, private pages and voice samples.
- The vault's `.cortex/` folder holds working files: the search vectors, a log of Claude's recent Cortex searches (the last 5,000), `index-snapshot.json`, a copy of the parsed text of your pages that makes start-up faster, and `dashboard.html`, the dashboard: a copy of your vault's non-private pages (up to 20 KB of text per page), rebuilt each time you open it with `cortex dashboard`. If you import browsing it also holds `browse/vectors-private.npz` (search vectors of your **private** browsing pages, for local models only), `browse/sites.json` (the local or cloud model's sorting and summaries of each site), `browse/site-days.json` (visit counts per day and site, kept after the browser forgets them), `browse/verdicts.json` (the local model's one-word judgement of each page title and search term, stored under a hash of the text, never the text), `browse/inbox-archive/` (the browser extension's visits after the filter, kept so a rebuild does not lose them; no page text for private visits; older than both your browsers' history and 90 days is removed) and `cloud-log.jsonl` (one counts-only line per cloud processing run). They stay on your computer and are left out of the vault's git history; `cortex backup` includes them. You can delete them at any time; Cortex rebuilds what it needs, except the extension's archived visits, which only the extension had.
- Keep the vault out of OneDrive, Dropbox, Google Drive and iCloud folders. Sync can break Cortex's file lock and copies private pages to the cloud. `cortex doctor` warns about this.
- Export: `cortex backup` writes a zip of the whole vault, including its history, `browse/private/` and the browse stores in `.cortex/browse/` listed above (`site-days.json`, `sites.json`, `verdicts.json`, `state.json`, `inbox-archive/`, the extension visits not yet imported, `vectors-private.npz`), which hold browsing your browsers have already forgotten. It leaves out the browser extension's pairing token, your local chat history (`.cortex/browse/chat/`), lock files, older backups and `dashboard.html`. **A backup zip holds your private browsing: keep it as private as the vault itself.**
- Delete everything: delete the vault folder and `~/.cortex`, then remove Cortex from Claude (see [INSTALL.md](INSTALL.md), section 11).

## Browsing

Cortex can turn your browser history into pages. It is off until you run an import or start the receiver, and [GUIDE.md section 6](GUIDE.md#6-your-browsing) explains every step and setting.

**After your first import, new history comes in by itself.** While the app or the receiver runs, Cortex imports new visits every `browse.auto_import_hours` (default 6; 0 turns it off). It reads only the browsers and profiles you imported before; a profile you untick in the app is remembered and left out of every import until you tick it again. Nothing is read before your first import.

**Who may see each category.** Every browsing category (news, video, health, money and so on) has one of three settings:

- **Skip** (Don't save in the app): never saved at all. Adult is skipped by default. A site the local model files under a skipped category after it was stored is dropped the same way, so Chat never lists it.
- **Local models only:** saved on private pages in `browse/private/`. Only a local model on this computer (Ollama or LM Studio) may read them. They never appear in search, the dashboard or any cloud chat. Every sensitive category (health, money, betting, dating, sexuality, drugs and self-harm) starts here.
- **Cloud AI apps can read:** saved on normal pages that Claude, Gemini, ChatGPT, Cursor and other AI apps can read through Cortex. Nothing is in this group until you turn on `browse.visible_to_claude`. For these categories the site pages include the **page titles, the search terms and the site names**, so those apps can read them, not only your topics and time. The setting is stored as an allowlist, `browse.cloud_categories`: a category Cortex adds later starts as local-only until you choose otherwise. (Older configs that list `hidden_from_claude` keep working, and categories they did not know are local.)

**When unsure, it stays local.** A page title or search term on a site page Claude can read must first pass two checks: the built-in sensitive word lists (health, money, sexuality, drugs, self-harm and the rest), and, when a local model is set up, that model's judgement, made on this computer before the page is written. **Search terms reach a readable page only after the local model has called them ordinary**; without a local model they stay on the private pages. A title the word lists cannot place and no local model has judged stays private too. The local model's judgements are kept in `.cortex/browse/verdicts.json` by hash; a cloud model is never asked.

**Changing your mind removes the past too.** When you skip or hide a category, or add a site to `deny_domains` or `private_domains`, the next import takes what the new settings no longer allow out of everything already stored: the day and site pages (private ones included), the visit counts, the search vectors, the site summaries and the extension's archive. To remove one site or category at once, run `cortex browse forget --site <domain>` or `cortex browse forget --category <name>`; it prints what it removed. Add the site to `deny_domains` too, or the next import reads it again from your browser.

**Private pages stay out of git.** The vault's `.gitignore` lists `browse/private/`. A vault made before this release may have committed them: the next browse import stops tracking them (once, keeping the files), and `cortex doctor` warns if older commits still hold them, with the commands to rewrite that history. Cortex never rewrites history itself.

**Your choice: git on or off, private pages in or out.** Two settings on the app's Vault page (`git:` in `~/.cortex/config.yaml`) decide this. *Keep the vault's history in git* is on by default; off, Cortex makes no commits at all, so nothing new enters the history, `cortex undo` is off, and an existing `.git` folder is left untouched (delete it yourself if you want the old history gone; until then `cortex doctor` still warns if it has a remote or private pages in old commits). *Include private browsing pages in git history* is off by default, which is what this section describes. On, Cortex removes its own `browse/private/` line from `.gitignore` and your private pages are committed like any other page: the history then holds every version of them, and `cortex browse forget` removes what it forgets from the files on disk but not from old commits (it never rewrites history, for any page). Both histories use more disk space as the vault grows; that is the trade-off for being able to undo.

**Processing.** Sorting sites into categories and writing their one-line summaries uses your local model, unless you turn on cloud processing (below) for the "Cloud AI apps can read" categories. Sign-in, payment and bank pages are dropped and never stored.

**Also built in this release:**

- **Cloud processing**, which lets a cloud model sort and summarise the "Cloud AI apps can read" categories. It is opt-in and off by default, for everyone. It never sees a skipped or local-only category, and if no local model is available, local-only data is skipped, never sent to the cloud.
- **A local chat** (Chat in the app, and `cortex ask`) that answers questions about your archive, private notes and private browsing included, with your local model only. Private notes are searched apart from the search Claude uses, and their search vectors are kept in memory only, never written to disk. It never uses the cloud model, whatever the cloud settings say. Chat history stays on this computer, in `<vault>/.cortex/browse/chat/` inside the private browse folder: it is never served to Claude or any other AI app, never on the dashboard and never logged. Delete it in Chat settings, or turn Keep chat history off to write nothing. Questions about your sites, your studying and your days are answered from Cortex's own records with no model at all. The Private notes and browsing switch in Chat settings (on by default) decides whether Chat reads private notes, private subjects, private mistakes and private browsing, in its search and in these answers alike. `cortex ask` prints its answer in the terminal, so it reads private notes and private browsing only when you add `--private`. Run that yourself: an AI coding tool such as Claude Code that runs a command reads its output.
- In the app, the cloud settings never take an API key: you name the environment variable that holds it, and the page shows only whether that variable is set.

**Two things to know:**

- The browser extension keeps a queue of visits on disk, in the browser's own extension storage, until the receiver takes them. A queued visit older than 24 hours is dropped.
- `cortex backup` includes `browse/private/` and the browse stores in `.cortex/browse/` (visit counts, site summaries, verdicts, the extension's archived visits, the private search vectors); it leaves out the pairing token and chat history ([Your data](#your-data)). Keep backup zips as private as the vault itself.

## Past chats

`cortex_chats` and `cortex chats` read your Claude Code transcripts in `~/.claude/projects` on this computer. Claude Chat, ChatGPT, Gemini and Perplexity conversations are read only after you import an export yourself with `cortex import` (or `cortex import-chats`). Secrets in transcripts are replaced with `[redacted]` before Claude sees them. Reading a chat saves nothing; what Claude learns about you from it waits as unconfirmed until you agree.

## Imported notes and calendars

`cortex import` turns a notes folder (Obsidian, Notion or any `.md`/`.txt` files) or a calendar (`.ics`) into pages in `resources/`. **These pages are normal pages that Claude and every other connected AI app can read**, unless the note's frontmatter marks it private or you import with `--private`, which makes every imported page private. A note counts as private when its frontmatter sets `private` to any yes value (`true`, `"true"`, `yes`, `on`, `1`), when it has a `private` tag, or when its frontmatter cannot be read. Every import ends with a report line: how many items it read, imported, made private, found already imported, and skipped, with the reason. Attendees' email addresses are left out of calendar pages, and secrets are redacted. `cortex import --dry-run` shows the counts first and writes nothing.

## Reporting a problem

To report a security or privacy problem, use GitHub's private vulnerability reporting, as [SECURITY.md](../SECURITY.md) explains. Never open a public issue for a privacy bug.
