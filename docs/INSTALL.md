# Installing Keepfold

This guide assumes no programming background. Copy each command into a terminal, one block at a time, and press Enter.

- **Windows:** open **PowerShell** (Start menu, type "PowerShell").
- **macOS:** open **Terminal** (Spotlight, type "Terminal").
- **Linux:** open your terminal app.

In paths below, `~` means your home folder: `C:\Users\<you>` on Windows, `/Users/<you>` on macOS.

## Windows: the installer (easiest)

On Windows you can skip sections 3 and 4: one program installs everything.

1. Download `KeepfoldSetup.exe` from the repository's **Releases** page on GitHub.
2. Double-click it. The file is not signed, so Windows SmartScreen may say it "protected your PC": click **More info**, then **Run anyway**. Some antivirus programs (Microsoft Defender included) can flag or quarantine a new unsigned installer: if the file disappears or setup stops with an access error, check the antivirus's protection history and allow `KeepfoldSetup.exe`. Download it only from the repository's Releases page.
3. Click **Install**. It needs an internet connection this once: it downloads Python and Keepfold's parts (a few minutes). It needs no admin rights and installs for you only.
4. Click **Open Keepfold**. The app's **Setup** screen walks you through the rest: your vault, connecting your AI apps, the search model, your history and a local model. Later, open it with the **Keepfold** shortcut on your desktop or in the Start menu.

Keepfold opens in its own window and puts an amber icon in the taskbar's notification area (the tray; it may sit behind the **^** arrow). Closing the window keeps Keepfold running there: click the icon, or the **Keepfold** shortcut again, to bring the window back. Right-click the icon for **Open Keepfold**, **Start with Windows** (on after the first start; untick it to stop Keepfold starting when you sign in) and **Quit**. From the tray it notifies you once a day when reviews are due, 3 days and 1 day before an exam, and when a long task in the app finishes or fails; it never names a private page. The window needs the Microsoft Edge WebView2 Runtime, which Windows 11 and up-to-date Windows 10 already have; without it Keepfold opens in your browser as before.

What it installs, all in `%LOCALAPPDATA%\Programs\Keepfold`: Keepfold itself (the `keepfold` command, the app, the dashboard and the MCP server), its own copy of Python and uv, the **Keepfold** shortcuts, and an entry in **Settings > Apps > Installed apps**. It adds the `keepfold` command to your PATH, so new terminals know it. To update, see **Updating** below.

To uninstall, close Keepfold (**Quit** in its tray icon's menu), then remove **Keepfold** under **Settings > Apps > Installed apps**. It asks whether to also remove Keepfold from Claude and your other AI apps (the default is yes). It then removes the **Start at login** and **Start with Windows** entries, the program, shortcuts and PATH entry. It deletes only the files setup made: anything else you put in the install folder stays, and the folder stays with it. Your vault and your settings in `~/.keepfold` are kept; delete them yourself if you want them gone. Restart your AI apps afterwards.

For scripts: `KeepfoldSetup.exe --quiet [--dir PATH] [--no-shortcuts] [--no-path] [--no-registry] [--uninstall [--keep-ai-apps]]`. `--keep-ai-apps` leaves Keepfold in your AI apps' settings on uninstall. `--update` updates the existing install without questions and then opens the app (the app's **Update now** runs it). With `--quiet`, setup stops with an error instead of closing programs that are still running from the install folder. It is a windowed program, so in PowerShell run it with `Start-Process -Wait -PassThru` to get its exit code. The log is `%TEMP%\keepfold-setup.log`. Developers build it with `uv run python installer/build.py` (Windows only; the exe lands in `dist/`, which is never committed).

### Updating

While the app runs, it asks GitHub for the newest Keepfold version a minute after it starts and every 6 hours. A Keepfold installed with `KeepfoldSetup.exe` then downloads the new installer (about 35 MB) in the background and checks it. Nothing is installed until you click **Restart to update**. To check only when you click, turn off **Check for updates automatically** under **Status → Updates**.

- **Installed with `KeepfoldSetup.exe`:** open the app, go to **Status**, and click **Check for updates** under **Updates**. When a newer version is out, **Release notes** opens them and **Update now** downloads the new `KeepfoldSetup.exe` from the GitHub release, checks it against the release's SHA-256 checksum, closes Keepfold, updates it in place and opens it again. Status then shows what changed and anything you need to do, once: **Got it** hides it, and **Updates** → **What's new** shows it again. If AI apps (such as Claude) or the browsing receiver are still using Keepfold, the installer names them and asks before closing them. Restart those AI apps afterwards so they reconnect. A receiver that starts at login runs the new version from the next login. If GitHub is limiting update checks from your network (common on campus networks and VPNs), Keepfold reads the latest release from GitHub's release page instead; if that fails too, it says when to try again, and you can download `KeepfoldSetup.exe` from the [latest release page](https://github.com/alenissacsam-2/keepfold/releases/latest).
- **Keepfold 0.1.0** has no update button. Download the newest `KeepfoldSetup.exe` from the **Releases** page once and run it; it updates in place, and from then on the button works.
- **Installed from the repository (sections 3 and 4):** the button shows the command to run instead: `git pull` in your Keepfold-app folder, then `uv tool install --editable . --reinstall` (section 11). A fixed (non-editable) copy installed with `uv tool install --force --reinstall "<folder>"` gets the same command with that line, so it stays a fixed copy.

### Coming from Cortex

Keepfold was called Cortex before 0.6.2. The old names stop working (there are no aliases), and 0.6.2 moves what you had over by itself the first time it runs.

- **Settings:** the old `~/.cortex` folder is moved to `~/.keepfold`, only if `~/.keepfold` doesn't exist yet. If an old copy is still running, Keepfold waits until you close it. Nothing is merged or deleted.
- **Vault:** the vault's `.cortex/` folder is moved to `.keepfold/`, its `.gitignore` lines follow, and the notes' `cortex_status` and `cortex_next` keys become `keepfold_status` and `keepfold_next`.
- **AI apps:** every connection Keepfold made (MCP entries, hooks, rules-file blocks and the Claude Code plugin settings) is pointed at the new names, with a backup of each file first. An entry you wrote by hand is left alone and listed by `keepfold doctor`.
- **Install:** running `KeepfoldSetup.exe` over a Cortex install stops the old app, installs Keepfold, moves your data, and removes the old program folder (only when it is in the default place), its shortcuts, its Start with Windows entries and its uninstall entry.
- **Version 0.6.1 or older** can't find the renamed GitHub repository, so **Check for updates** won't offer 0.6.2. Download `KeepfoldSetup.exe` once from the [latest release page](https://github.com/alenissacsam-2/keepfold/releases/latest) and run it. Updates work again from then on.

## macOS: the app (from Keepfold 0.7.0)

The Mac app is for Apple silicon (M1 or later) on macOS 14 or later. Intel Macs are not supported, because Keepfold's search engine (onnxruntime) has no Intel Mac build.

1. Download `Keepfold-<version>-macos-arm64.dmg` from the repository's **Releases** page on GitHub.
2. Double-click it, and drag **Keepfold** onto the **Applications** folder beside it.
3. Open **Keepfold** from Applications (or Launchpad). The app is signed only by its build, not notarized by Apple, so the first time macOS says it was not opened. Click **Done**. Open **System Settings > Privacy & Security**, scroll down to the line about Keepfold, click **Open Anyway** and confirm with Touch ID or your password. You do this once.
4. If you opened it straight from Downloads or the disk image, macOS runs it from a temporary place. Keepfold then shows **Move Keepfold to Applications**: click **Move**, and it copies itself there and opens again. Until then it does not connect AI apps, because their settings would point at the temporary place. If **Move** fails, quit Keepfold, drag it to Applications in Finder and open it from there.
5. The app's **Setup** screen walks you through the rest, as on Windows.

Keepfold opens in its own window and puts an icon in the menu bar. Closing the window keeps Keepfold running there: click the icon for **Open Keepfold**, **Start at login** (on after the first start) and **Quit**. **Cmd+Q** quits too. The first time Keepfold wants to notify you, macOS asks whether to allow its notifications. AI apps run the `keepfold` command from inside the app, at `/Applications/Keepfold.app/Contents/MacOS/keepfold-cli`; setup also links it as `~/.local/bin/keepfold` for the terminal.

**Updating.** Keepfold checks GitHub as on Windows, but it downloads an update only when you click. Open **Status**, click **Check for updates** under **Updates**, then **Update now**. Keepfold downloads the new app, checks it against the release's SHA-256 checksum, puts it in place of the old one and opens again. If AI apps or the browsing receiver are still running Keepfold, it lists them and asks before closing them. The version you had is kept in `~/Library/Application Support/Keepfold/previous/`. If your Mac account cannot change the Applications folder (a standard account, not an administrator), Keepfold links the release page instead: download the new `.dmg` and drag it over the old app. The Mac file is attached to a release about half an hour after the release appears; until then Keepfold says to check again soon. After an update, macOS may ask again for permissions you gave the old version, such as notifications.

**Uninstalling.** In the app, open **Status** and click **Uninstall Keepfold…** under **Updates**. It removes Keepfold from your AI apps, stops it starting at login, moves the app to the Trash and quits. Or, in Terminal:

```
/Applications/Keepfold.app/Contents/MacOS/keepfold-cli uninstall
```

then quit Keepfold and drag it from Applications to the Trash. Either way, your vault and `~/.keepfold` are kept; delete them yourself if you want them gone. Restart your AI apps afterwards.

## Ubuntu: the app (from Keepfold 0.7.0)

The Ubuntu app is for Ubuntu 24.04 or later on a 64-bit Intel or AMD PC (amd64).

1. Download `keepfold_<version>_amd64.deb` from the repository's **Releases** page on GitHub.
2. Double-click it to open it in App Center, and click **Install**. It asks for your password, as any system install does, and installs what Keepfold needs with it. Or, in a terminal, in the folder you saved it to:

   ```
   sudo apt install ./keepfold_*.deb
   ```

3. Open **Keepfold** from the app menu. The app's **Setup** screen walks you through the rest. In a terminal, the command is `keepfold`.

Keepfold opens in its own window and puts an icon in the top bar. Closing the window keeps Keepfold running there: click the icon for **Open Keepfold**, **Start at login** (on after the first start) and **Quit**. Ubuntu shows the icon through its AppIndicator extension, which is on by default; without it, closing the window quits Keepfold. For reading scanned PDFs, App Center or apt also installs `tesseract-ocr` when it can.

**Updating.** Open **Status**, click **Check for updates** under **Updates**, then **Update now**. Keepfold downloads the new version, checks it against the release's SHA-256 checksum, unpacks it into `~/.local/share/keepfold/app/` and opens again. This needs no password. The `keepfold` command and the app menu always run the newest copy, either that one or the one the `.deb` installed; the version before is kept. Installing a newer `.deb` by hand works too. The Ubuntu file is attached to a release about half an hour after the release appears; until then Keepfold says to check again soon.

**Uninstalling.** In the app, open **Status** and click **Uninstall Keepfold…** under **Updates**. It removes Keepfold from your AI apps, stops it starting at login, deletes the updated copies, asks for your password to remove the package, and quits. Or, in a terminal:

```
keepfold uninstall
sudo apt remove keepfold
```

Either way, your vault and `~/.keepfold` are kept; delete them yourself if you want them gone. Restart your AI apps afterwards.

On other Linux systems, on macOS without the app, or to work on Keepfold's code, use the steps below.

## 1. What you get

Keepfold gives Claude a memory of you: what you work on, read, decide and know, your notes, goals and projects, and, if you study, what you keep getting wrong. Everything lives in a folder of plain Markdown and JSONL files on your own computer (the *vault*). You can open that folder in Obsidian. Keepfold needs no account, and the only servers it runs listen on your own computer. [PRIVACY.md](PRIVACY.md) explains what stays on your machine and what the AI model sees.

## 2. Requirements

- Windows 10 or later, macOS, or Linux.
- **git**. Step 4 downloads Keepfold with git, and git also gives your vault its own history. Check with `git --version`. If it is missing, install it from https://git-scm.com/downloads (on macOS, running `git --version` offers to install it), then open a new terminal.
- An internet connection for the install and for the one-time model download (about 150 MB).
- **Claude Code**, the **Claude desktop app**, or both.

## 3. Install uv

[uv](https://docs.astral.sh/uv/) installs Keepfold and the Python version it needs.

**Windows (PowerShell):**

```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS and Linux:**

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then let uv add its programs to your PATH:

```
uv tool update-shell
```

**Close the terminal and open a new one.** The PATH change only reaches new terminals.

## 4. Install Keepfold

Keepfold installs from a copy of its repository (a *clone*), which also holds the Claude Code plugin and the Chat skill. The last line opens the Keepfold app by its full path, so it works before the terminal knows the `keepfold` command.

**Windows (PowerShell):**

```
git clone https://github.com/alenissacsam-2/keepfold "$HOME\Keepfold-app"
cd "$HOME\Keepfold-app"
uv tool install --editable .
& "$HOME\.local\bin\keepfold.exe" app
```

**macOS and Linux:**

```
git clone https://github.com/alenissacsam-2/keepfold ~/Keepfold-app
cd ~/Keepfold-app
uv tool install --editable .
~/.local/bin/keepfold app
```

Keep the `Keepfold-app` folder: the installed program runs from it. It is the program, not your vault.

In a new terminal, check that the command is found:

```
keepfold --version
```

If the terminal says `keepfold` is not found, run `uv tool update-shell` again and open a new terminal. If the clone fails with a message about `git`, install git (step 2), open a new terminal and try again.

To uninstall the program (your vault stays):

```
uv tool uninstall keepfold
```

## 5. Run the setup

**Run `keepfold app` and open Setup.** The app is a page in your browser that walks through each step with a button: the vault, the search model, an optional local AI runner and model (Ollama is recommended, with a Qwen 3.5 model sized for this computer: `qwen3.5:4b` for 6 to 10 GB of memory), and importing your past history. From a terminal, `keepfold import <file>` imports the same exports, plus notes (Obsidian, Notion, a folder) and calendars (.ics); add `--dry-run` to see the counts first and `--private` to keep imported notes private ([GUIDE.md](GUIDE.md) section 4). **Connect AI apps** adds Keepfold to Claude Code, the Claude desktop app and others. The app runs on this computer only and stops after 30 minutes idle. On Windows, `keepfold app --window` opens it in its own window with a tray icon instead, as the installer's shortcut does; it then runs until **Quit** in the tray.

If you prefer the terminal, `keepfold setup` does the same core steps by asking questions:

```
keepfold setup
```

It asks a few questions. Press Enter to take the suggested answer. You can run it again at any time; it only adds what is missing.

| Step | What it asks | What it changes |
|---|---|---|
| Checks | nothing | Checks Python and SQLite, looks for git and for `keepfold` on your PATH |
| Vault | Where to keep the vault (default `~/Keepfold`) | Creates the folder with its subfolders, starter `me/` pages, a git repository with a push block, and `~/.keepfold/config.yaml` (the pointer to your vault). A folder that already has files is used only after you say yes; your notes are never changed |
| Sample | Add a sample vault? (new vaults only) | Copies 15 pages about a made-up person, Sam (a student), plus a few practice results, so you can try things at once |
| Model | Download the search model? | Downloads about 150 MB once from Hugging Face into `~/.keepfold/models`. Without it, search uses keywords only and new notes are not filed under topics (`keepfold topics`) |
| Claude Code | Install the plugin? | Runs `claude plugin marketplace add` and `claude plugin install`, or prints the two commands to type |
| Claude desktop | Add Keepfold to this config file? | Adds a `keepfold` entry to `claude_desktop_config.json` and keeps everything else. It saves the old file first as `claude_desktop_config.json.keepfold-<date>-<time>.bak` beside it (the same backups as the app's Connect; an older `.bak` is never overwritten). A file it cannot read stays untouched, and setup prints the entry for you to add by hand (section 7) |
| Verify | nothing | Runs `keepfold doctor` and prints three things to try |

Useful options:

- `keepfold setup --dry-run` prints what it would do and changes nothing.
- `keepfold setup --yes` takes every suggested answer without asking.
- `--vault PATH`, `--no-model`, `--no-code`, `--no-desktop` and `--sample` / `--no-sample` skip or answer single questions.

**Optional: voice memos.** Transcribing recordings needs faster-whisper and its speech model, which the core install leaves out. Click Install on the app's Voice memos card, or run `keepfold model download --voice`: it installs faster-whisper (pinned, with `uv pip` into Keepfold's own Python) and downloads the model (about 150 MB) into `~/.keepfold/models`. Nothing else ever downloads them. From a source checkout, `uv sync --extra voice` installs the package. Audio is decoded by PyAV, which bundles FFmpeg, so no ffmpeg install is needed.

To remove the sample later (your own pages stay):

```
keepfold sample remove
```

### Other AI apps: rules files, profiles and context packs

**Rules files.** When **Connect AI apps** adds Keepfold to Cursor, VS Code (Copilot) or Codex, it also writes a short Keepfold block into a rules file that app reads: your pinned core (the `## Core` lines of `me/core.md` you wrote or approved, at most 500 characters) and one line asking the app to call `keepfold_prime` first. The block sits between `<!-- keepfold:start … -->` and `<!-- keepfold:end -->` markers. Keepfold backs the file up first, changes only the block and never the rest, and leaves a file it cannot read (not UTF-8, or markers that do not pair up) untouched with a message. Removing the app takes out only the block; a file that held nothing else is deleted (after its backup). The app checks every 5 minutes and rewrites the block when your core changes; it never adds one you removed. A private `me/core.md` gives an empty block.

| App | File | Why there |
|---|---|---|
| Cursor | `~/.cursor/rules/keepfold.mdc` (`alwaysApply: true`) | Cursor has no documented global rules file: its User Rules live in its settings, and `.cursor/rules` is per project. The file applies when your home folder is the open project; for every project, paste its text into Cursor's **Settings → Rules** |
| VS Code (Copilot) | `prompts/keepfold.instructions.md` in your VS Code user folder (`%APPDATA%\Code\User` on Windows), `applyTo: "**"` | VS Code applies user-level `*.instructions.md` files to Copilot chats in every workspace; `.github/copilot-instructions.md` is per repository |
| Codex | `~/.codex/AGENTS.md` | Codex reads this global AGENTS.md in every project |

**Agent profiles.** Under **Connect AI apps → Agent profiles** you choose what each app may read over MCP, by the name the app gives itself: `all` (everything except private pages), `core` (your pinned lines), a scope per section of your preferences (`coding`, `communication`, `learning`, …; your `## Workflow` section counts as `coding`; `communication` and `learning` also bring the matching personality sections, `learning` the teaching contract, and `environment` your setup page `me/setup.md`), `projects` (also your project pages) and `personal` (your profile, goals, journal, people pages and past chats). The defaults: Claude apps and LM Studio get `all`; Cursor, VS Code (Copilot) and Codex get `core`, `coding`, `projects` and `environment`; any other app gets `core` only. A profiles file you saved keeps your choices; only the defaults changed. Only the app's page changes them (they are kept in `.keepfold/profiles.json` in your vault); no AI app can. A restricted app never sees the other sections, your profile, journal or people pages, and is refused past chats and writing as you. Private pages are never shared with any app.

**Context packs.** For web chats with no MCP (ChatGPT, Gemini), **Connect AI apps → Context packs** shows **General**, **Coding** and **Writing**, and the role packs **Interview** (career and communication), **Research** (learning, projects and communication) and **Review** (coding and communication): your pinned core first, then that pack's preference and personality lines, at 1,500 or 5,000 characters (a switch above each pack), with a **Copy** button (if copying is blocked, the text is selected for Ctrl+C). After you copy a pack, it shows "Changed since your last copy" once its text changes (you edited me/core.md, say), so you know to paste it again. Only lines you wrote or approved go in; never a private page or a line naming a private subject. `keepfold pack general|coding|writing|interview|research|review --chars 1500|5000` prints the same text (1,500 by default).

## 6. Claude Code

[CONNECT.md](CONNECT.md#claude-code-in-a-terminal) walks through Claude Code in a terminal and in the Claude desktop app's Code tab: what to install first, what Keepfold writes, how to check it works and what to do when it doesn't. From a source checkout, if the setup did not install the plugin for you, type `/plugin marketplace add <your Keepfold-app folder>` and then `/plugin install keepfold@keepfold` in Claude Code, and restart it.

Claude Code starts the first `keepfold` on your PATH. If that is a different copy, of a different version from the installed app (for example a `uv tool` one in `~\.local\bin`), **Status** says "Claude Code runs Keepfold 0.3.9, the app is 0.4.1"; its **Fix** puts the app's `bin` folder first on your user PATH, and then you quit Claude completely and open it again. When the other copy's folder is on the system PATH, which Windows reads first, the row names that folder instead: remove it from the system PATH, or update that copy.

The plugin finds your vault through `~/.keepfold/config.yaml`. It calls the `keepfold` command by name, so `keepfold` must be on your PATH (step 3).

Besides the server and the `/keepfold` skill, the plugin adds a few hooks (small commands Claude Code runs by itself). On the first message of each chat, in every project, one adds a short Keepfold context that matches the message: your profile summary, matching preferences and your teaching contract. So the first reply of a chat can take a few seconds longer. The same context comes back when a long chat is compacted, resumed or cleared, and just before a compaction Claude is reminded to save what matters. The other two close Keepfold sessions when Claude Code exits and note when Claude has read the web. [PRIVACY.md](PRIVACY.md) has the details. To turn all of it off, type `/plugin disable keepfold@keepfold`.

To keep Keepfold out of one folder (a client's repository, code you cloned), run `keepfold exclude add <folder>`. New Claude Code chats in that folder or under it get no Keepfold context, and Keepfold's tools there refuse every call, reads included. This stops Keepfold's tools only: an agent with file access can still open your vault's files ([PRIVACY.md](PRIVACY.md#what-the-ai-model-sees)). `keepfold exclude list` shows the folders and `keepfold exclude remove <folder>` lifts one. The Chat tab is not affected.

## 7. Claude desktop app (Chat)

[CONNECT.md](CONNECT.md#claude-desktop-chat) has the steps, the instructions to paste so Claude uses Keepfold in every chat, a check and the fixes for common problems. The rest of this section is for adding the entry by hand.

The setup adds the entry for you when it finds the app's config folder. When Keepfold is installed with KeepfoldSetup.exe, the entry runs that install's `bin\keepfold.exe`, so updating the app also updates what Claude Desktop runs. If the entry points at another copy (for example a `uv tool` one in `~\.local\bin`), **Status** says "Claude Desktop runs a different Keepfold copy"; **Apply** under **Connect** points it back at the install (the old file is backed up beside it). To do it by hand:

1. Open **Settings > Developer > Edit Config**. This opens `claude_desktop_config.json`:
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`. Some Windows installs (for example from the Microsoft Store) use a file under `%LOCALAPPDATA%\Claude-Data\<profile>\` or `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\` instead. Trust the file that Edit Config opens.
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Linux: `~/.config/Claude/claude_desktop_config.json`
2. Add the `keepfold` entry. Use the **full path** to `keepfold` (find it with `where.exe keepfold` in PowerShell, or `which keepfold` on macOS and Linux), because the app does not see your terminal's PATH. On Windows, double every backslash.

   If the file is empty or has no `"mcpServers"`, replace everything in it with:
   ```json
   {
     "mcpServers": {
       "keepfold": {
         "command": "C:\\Users\\<you>\\.local\\bin\\keepfold.exe",
         "args": ["mcp", "--vault", "C:\\Users\\<you>\\Keepfold"]
       }
     }
   }
   ```
   If it already has `"mcpServers"`, add only the `"keepfold": { ... }` part inside it, with a comma between it and the entry before it. Keep the rest of the file as it is.
3. Fully quit Claude (tray icon or menu bar > **Quit**, not just closing the window) and open it again.
4. In **Settings > Developer**, check that `keepfold` shows as **running**.
5. For the `/keepfold` skill, use `keepfold-skill.zip`: with the Windows installer, download it from the release (the app's **Connect** page links it); from a source checkout, use `dist/keepfold-skill.zip` in your Keepfold-app folder (if it is missing, run `uv run python scripts/build_skills.py` there to build it). Upload it under **Settings > Capabilities > Skills**. Skills need **Code execution and file creation** turned on, on the same page. Upload the zip as it is; do not unpack it.

The Chat tab has no hooks. Instead, Keepfold asks Claude to call `keepfold_prime` with your first message of each chat, which loads the same short context as the Claude Code hook. So that Claude does it in every chat, paste the instructions from [CONNECT.md](CONNECT.md#the-instructions-to-paste) into Claude's settings (**Copy instructions** on the Claude Desktop row in Connect AI apps copies them).

If `keepfold` shows as failed, read the MCP logs. On Windows they are in `%APPDATA%\Claude\logs\` or `%LOCALAPPDATA%\Claude-Data\<profile>\logs\`; on macOS in `~/Library/Logs/Claude/`. Look for `mcp-server-keepfold.log`.

## 8. Your first conversation

- **With the sample vault:** ask "what do you know about me?" Claude reads Sam's profile and goals. Then type `/keepfold` for a short study session on loops or recursion.
- **With an empty vault:** tell Claude about yourself ("I'm on the payments team" or "I'm studying for a statistics exam in May"), or say "remember that I prefer short answers". Claude records what you tell it. What it only guesses about you waits in `inbox.md` as unconfirmed until you agree.

## 9. Using it with Obsidian

The vault is a normal Obsidian vault. In Obsidian choose **Open folder as vault** and pick your vault folder. Graph colours and a few saved views come ready-made. You can edit notes in Obsidian while Claude uses them.

## 10. Troubleshooting

Start with:

```
keepfold doctor
```

| Problem | What to do |
|---|---|
| `keepfold` not found | Run `uv tool update-shell`, then open a new terminal |
| Claude Code shows the Keepfold server as failed, or a hook error naming `keepfold` | Check that `keepfold --version` works in a new terminal, then restart Claude Code |
| The first reply of a Claude Code chat is slow | The plugin's hook is loading your context. It waits at most 3 seconds; if your context is not ready by then, it asks Claude to load it instead |
| Chat shows `keepfold` as failed | Use the full path to `keepfold` in the config (section 7), then fully quit and reopen |
| The model download failed | Search still works with keywords. Retry later with `keepfold model download` |
| Doctor warns about two Keepfold servers | Normal if you use both the plugin and the desktop entry. The desktop app's Code tab then shows two copies |
| Doctor warns about cloud sync | Move the vault out of OneDrive, Dropbox, Google Drive or iCloud. Sync can break Keepfold's file lock and copies private pages off your machine |
| `git push` is refused | On purpose: the vault's pre-push hook keeps it local |
| A day ends late | Keepfold starts a new day at 04:00 in your computer's time zone |

## 11. Updating and uninstalling

Update the program by running `git pull` in your Keepfold-app folder. If the update adds a dependency or a command, also run `uv tool install --editable . --reinstall` there. In Claude Code, run `/plugin update keepfold@keepfold` and restart. If the receiver starts at login, restart the computer (or log out and in) so it runs the new version.

To remove Keepfold completely:

1. In Claude Code, run `/plugin uninstall keepfold@keepfold`.
2. Remove the `keepfold` entry from `claude_desktop_config.json`.
3. Run `uv tool uninstall keepfold`.
4. If you turned on **Start at login** under Browsing, turn it off there first (Windows: a "Keepfold browse receiver" value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`; the installer's uninstall removes it; macOS: `~/Library/LaunchAgents/keepfold-browse-receiver.plist`; Linux: `~/.config/systemd/user/keepfold-browse-receiver.service`, or `~/.config/autostart/keepfold-browse-receiver.desktop` without systemd).
5. Delete the `~/.keepfold` folder (the vault pointer, settings, models, logs, chat copies and any downloaded update).
6. Delete the Keepfold-app folder.
7. If you connected other AI apps, remove Keepfold from them in the app (**Connect AI apps** → **Remove**) before step 3. Then delete the `*.keepfold-*.bak` files Keepfold left beside other AI apps' config and rules files: they hold copies of your pinned lines. The installer's log is `%TEMP%\keepfold-setup.log`.

Your vault folder is yours. Keep it, back it up with `keepfold backup`, or delete it.

## 12. Notes for developers

**The editable install.** Section 4 installs with `uv tool install --editable .`, so code changes in the clone apply without reinstalling. After pulling changes that add commands (such as `keepfold taint`) or dependencies, reinstall.

**Always on.** Keepfold needs no `/keepfold` trigger. Read tools (`keepfold_context`, `keepfold_find`, `keepfold_read`, ...) work at any time and open no session. The first write opens an `ambient` session; it closes through `keepfold_end`, the 2 h idle close, the plugin's `SessionEnd` sweep, or the next `keepfold_start`. The plugin's `UserPromptSubmit` hook (`keepfold prime-hook`) adds the light part of `keepfold_prime` (profile, preferences, contract core; about 1 s) as context on the first prompt of each chat and asks Claude to fetch related pages with `keepfold_prime(related_only=true)`; it tracks primed chat ids in `<vault>/.keepfold/primed` and waits at most 3 s (after a timeout or error it only nudges Claude to call `keepfold_prime`). The plugin also has a `PostToolUse` hook (`keepfold taint`): after a web, browser or other non-Keepfold MCP tool it marks the vault so the next `me/*` write counts as `exposed` (docs/tools.md, Claims). The Chat tab has no hooks; the server instructions ask Claude to call `keepfold_prime` itself. The index keeps a parse cache in `<vault>/.keepfold/index-snapshot.json` (local, gitignored, safe to delete). `/keepfold [mode] [topic]` starts a deliberate session; modes are `home` (default), `learn`, `review`, `short`, `log`, `about`, `week`, `capture`, `reflect`, `project`, `save`, `history`, `sweep` and `mock` (docs/tools.md).

**Building the skill.** `skills/keepfold/` (`SKILL.md` and `modes/*.md`) is the only copy you edit. After changing it, run `uv run python scripts/build_skills.py`. It rewrites `plugin/skills/keepfold/` and `dist/keepfold-skill.zip`. Then run `/plugin update keepfold@keepfold` in Claude Code and restart; in Chat, remove the old `keepfold` skill and upload the new zip.
