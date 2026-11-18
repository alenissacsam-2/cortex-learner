# Installing Cortex

This guide assumes no programming background. Copy each command into a terminal, one block at a time, and press Enter.

- **Windows:** open **PowerShell** (Start menu, type "PowerShell").
- **macOS:** open **Terminal** (Spotlight, type "Terminal").
- **Linux:** open your terminal app.

In paths below, `~` means your home folder: `C:\Users\<you>` on Windows, `/Users/<you>` on macOS.

## Windows: the installer (easiest)

On Windows you can skip sections 3 and 4: one program installs everything.

1. Download `CortexSetup.exe` from the repository's **Releases** page on GitHub.
2. Double-click it. The file is not signed, so Windows SmartScreen may say it "protected your PC": click **More info**, then **Run anyway**. Some antivirus programs (Microsoft Defender included) can flag or quarantine a new unsigned installer: if the file disappears or setup stops with an access error, check the antivirus's protection history and allow `CortexSetup.exe`. Download it only from the repository's Releases page.
3. Click **Install**. It needs an internet connection this once: it downloads Python and Cortex's parts (a few minutes). It needs no admin rights and installs for you only.
4. Click **Open Cortex**. The app's **Setup** screen walks you through the rest: your vault, connecting your AI apps, the search model, a local model and imports. Later, open it with the **Cortex** shortcut on your desktop or in the Start menu.

What it installs, all in `%LOCALAPPDATA%\Programs\Cortex`: Cortex itself (the `cortex` command, the app, the dashboard and the MCP server), its own copy of Python and uv, the **Cortex** shortcuts, and an entry in **Settings > Apps > Installed apps**. It adds the `cortex` command to your PATH, so new terminals know it. To update, see **Updating** below.

To uninstall, close Cortex (Quit on its page), then remove **Cortex** under **Settings > Apps > Installed apps**. It asks whether to also remove Cortex from Claude and your other AI apps (the default is yes). It then removes the **Start at login** entry, the program, shortcuts and PATH entry. It deletes only the files setup made: anything else you put in the install folder stays, and the folder stays with it. Your vault and your settings in `~/.cortex` are kept; delete them yourself if you want them gone. Restart your AI apps afterwards.

For scripts: `CortexSetup.exe --quiet [--dir PATH] [--no-shortcuts] [--no-path] [--no-registry] [--uninstall [--keep-ai-apps]]`. `--keep-ai-apps` leaves Cortex in your AI apps' settings on uninstall. `--update` updates the existing install without questions and then opens the app (the app's **Update now** runs it). With `--quiet`, setup stops with an error instead of closing programs that are still running from the install folder. It is a windowed program, so in PowerShell run it with `Start-Process -Wait -PassThru` to get its exit code. The log is `%TEMP%\cortex-setup.log`. Developers build it with `uv run python installer/build.py` (Windows only; the exe lands in `dist/`, which is never committed).

### Updating

Cortex looks for updates only when you click; it never checks in the background.

- **Installed with `CortexSetup.exe`:** open the app, go to **Status**, and click **Check for updates** under **Updates**. When a newer version is out, **What's new** opens its release notes and **Update now** downloads the new `CortexSetup.exe` from the GitHub release, checks it against the release's SHA-256 checksum, closes Cortex, updates it in place and opens it again. If AI apps (such as Claude) or the browsing receiver are still using Cortex, the installer names them and asks before closing them. Restart those AI apps afterwards so they reconnect.
- **Cortex 0.1.0** has no update button. Download the newest `CortexSetup.exe` from the **Releases** page once and run it; it updates in place, and from then on the button works.
- **Installed from the repository (sections 3 and 4):** the button shows the command to run instead: `git pull` in your Cortex-app folder, then `uv tool install --editable . --reinstall` (section 11).

On macOS or Linux, or to work on Cortex's code, use the steps below.

## 1. What you get

Cortex gives Claude a memory of you as a learner: what you know, what you keep getting wrong, your notes, goals and projects. Everything lives in a folder of plain Markdown and JSONL files on your own computer (the *vault*). You can open that folder in Obsidian. Cortex needs no account, and the only servers it runs listen on your own computer. [PRIVACY.md](PRIVACY.md) explains what stays on your machine and what the AI model sees.

## 2. Requirements

- Windows 10 or later, macOS, or Linux.
- **git**. Step 4 downloads Cortex with git, and git also gives your vault its own history. Check with `git --version`. If it is missing, install it from https://git-scm.com/downloads (on macOS, running `git --version` offers to install it), then open a new terminal.
- An internet connection for the install and for the one-time model download (about 150 MB).
- **Claude Code**, the **Claude desktop app**, or both.

## 3. Install uv

[uv](https://docs.astral.sh/uv/) installs Cortex and the Python version it needs.

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

## 4. Install Cortex

Cortex installs from a copy of its repository (a *clone*), which also holds the Claude Code plugin, the Chat skill and the browser extension. The last line opens the Cortex app by its full path, so it works before the terminal knows the `cortex` command.

**Windows (PowerShell):**

```
git clone https://github.com/alenissacsam-2/cortex-learner "$HOME\Cortex-app"
cd "$HOME\Cortex-app"
uv tool install --editable .
& "$HOME\.local\bin\cortex.exe" app
```

**macOS and Linux:**

```
git clone https://github.com/alenissacsam-2/cortex-learner ~/Cortex-app
cd ~/Cortex-app
uv tool install --editable .
~/.local/bin/cortex app
```

Keep the `Cortex-app` folder: the installed program runs from it. It is the program, not your vault.

In a new terminal, check that the command is found:

```
cortex --version
```

If the terminal says `cortex` is not found, run `uv tool update-shell` again and open a new terminal. If the clone fails with a message about `git`, install git (step 2), open a new terminal and try again.

To uninstall the program (your vault stays):

```
uv tool uninstall cortex-learner
```

## 5. Run the setup

**Run `cortex app` and open Setup.** The app is a page in your browser that walks through each step with a button: the vault, the search model, an optional local AI runner and model (Ollama is recommended, with a Qwen 3.5 model sized for this computer: `qwen3.5:4b` for 6 to 10 GB of memory), and importing your past history. From a terminal, `cortex import <file>` imports the same exports, plus notes (Obsidian, Notion, a folder) and calendars (.ics); add `--dry-run` to see the counts first and `--private` to keep imported notes private ([GUIDE.md](GUIDE.md) section 4). **Connect AI apps** adds Cortex to Claude Code, the Claude desktop app and others. The app runs on this computer only and stops after 30 minutes idle.

If you prefer the terminal, `cortex setup` does the same core steps by asking questions:

```
cortex setup
```

It asks a few questions. Press Enter to take the suggested answer. You can run it again at any time; it only adds what is missing.

| Step | What it asks | What it changes |
|---|---|---|
| Checks | nothing | Checks Python and SQLite, looks for git and for `cortex` on your PATH |
| Vault | Where to keep the vault (default `~/Cortex`) | Creates the folder with its subfolders, starter `me/` pages, a git repository with a push block, and `~/.cortex/config.yaml` (the pointer to your vault). A folder that already has files is used only after you say yes; your notes are never changed |
| Sample | Add a sample vault? (new vaults only) | Copies 15 pages about a made-up learner, Sam, plus a few practice results, so you can try things at once |
| Model | Download the search model? | Downloads about 150 MB once from Hugging Face into `~/.cortex/models`. Without it, search uses keywords only and new notes are not filed under topics (`cortex topics`) |
| Claude Code | Install the plugin? | Runs `claude plugin marketplace add` and `claude plugin install`, or prints the two commands to type |
| Claude desktop | Add Cortex to this config file? | Adds a `cortex` entry to `claude_desktop_config.json` and keeps everything else. It saves the old file first as `claude_desktop_config.json.cortex-<date>-<time>.bak` beside it (the same backups as the app's Connect; an older `.bak` is never overwritten). A file it cannot read stays untouched, and setup prints the entry for you to add by hand (section 7) |
| Verify | nothing | Runs `cortex doctor` and prints three things to try |

Useful options:

- `cortex setup --dry-run` prints what it would do and changes nothing.
- `cortex setup --yes` takes every suggested answer without asking.
- `--vault PATH`, `--no-model`, `--no-code`, `--no-desktop` and `--sample` / `--no-sample` skip or answer single questions.

To remove the sample later (your own pages stay):

```
cortex sample remove
```

## 6. Claude Code

If the setup did not install the plugin for you, type these in Claude Code:

```
/plugin marketplace add <your Cortex-app folder>
```

```
/plugin install cortex@cortex
```

Restart Claude Code. Type `/mcp` to check that `cortex` is connected, then type `/cortex` or just talk.

The plugin finds your vault through `~/.cortex/config.yaml`. It calls the `cortex` command by name, so `cortex` must be on your PATH (step 3).

Besides the server and the `/cortex` skill, the plugin adds three hooks (small commands Claude Code runs by itself). On the first message of each chat, in every project, one adds a short Cortex context that matches the message: your profile summary, matching preferences and your teaching contract. So the first reply of a chat can take a few seconds longer. The other two close Cortex sessions when Claude Code exits and note when Claude has read the web. [PRIVACY.md](PRIVACY.md) has the details. To turn all of it off, type `/plugin disable cortex@cortex`.

To keep Cortex out of one folder (a client's repository, code you cloned), run `cortex exclude add <folder>`. New Claude Code chats in that folder or under it get no Cortex context, and Claude can read your vault there but not write to it. `cortex exclude list` shows the folders and `cortex exclude remove <folder>` lifts one. The Chat tab is not affected.

## 7. Claude desktop app (Chat)

The setup adds the entry for you when it finds the app's config folder. To do it by hand:

1. Open **Settings > Developer > Edit Config**. This opens `claude_desktop_config.json`:
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`. Some Windows installs (for example from the Microsoft Store) use a file under `%LOCALAPPDATA%\Claude-Data\<profile>\` or `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\` instead. Trust the file that Edit Config opens.
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Linux: `~/.config/Claude/claude_desktop_config.json`
2. Add the `cortex` entry. Use the **full path** to `cortex` (find it with `where.exe cortex` in PowerShell, or `which cortex` on macOS and Linux), because the app does not see your terminal's PATH. On Windows, double every backslash.

   If the file is empty or has no `"mcpServers"`, replace everything in it with:
   ```json
   {
     "mcpServers": {
       "cortex": {
         "command": "C:\\Users\\<you>\\.local\\bin\\cortex.exe",
         "args": ["mcp", "--vault", "C:\\Users\\<you>\\Cortex"]
       }
     }
   }
   ```
   If it already has `"mcpServers"`, add only the `"cortex": { ... }` part inside it, with a comma between it and the entry before it. Keep the rest of the file as it is.
3. Fully quit Claude (tray icon or menu bar > **Quit**, not just closing the window) and open it again.
4. In **Settings > Developer**, check that `cortex` shows as **running**.
5. For the `/cortex` study skill, use `cortex-skill.zip`: with the Windows installer, download it from the release (the app's **Connect** page links it); from a source checkout, use `dist/cortex-skill.zip` in your Cortex-app folder (if it is missing, run `uv run python scripts/build_skills.py` there to build it). Upload it under **Settings > Capabilities > Skills**. Skills need **Code execution and file creation** turned on, on the same page. Upload the zip as it is; do not unpack it.

The Chat tab has no hooks. Instead, Cortex asks Claude to call `cortex_prime` with your first message of each chat, which loads the same short context as the Claude Code hook.

If `cortex` shows as failed, read the MCP logs. On Windows they are in `%APPDATA%\Claude\logs\` or `%LOCALAPPDATA%\Claude-Data\<profile>\logs\`; on macOS in `~/Library/Logs/Claude/`. Look for `mcp-server-cortex.log`.

## 8. Your first conversation

- **With the sample vault:** ask "what do you know about me?" Claude reads Sam's profile and goals. Then type `/cortex` for a short study session on loops or recursion.
- **With an empty vault:** tell Claude about yourself ("I'm studying for a statistics exam in May"), or say "remember that I prefer short answers". Claude records what you tell it. What it only guesses about you waits in `inbox.md` as unconfirmed until you agree.

## 9. Using it with Obsidian

The vault is a normal Obsidian vault. In Obsidian choose **Open folder as vault** and pick your vault folder. Graph colours and a few saved views come ready-made. You can edit notes in Obsidian while Claude uses them.

## 10. Troubleshooting

Start with:

```
cortex doctor
```

| Problem | What to do |
|---|---|
| `cortex` not found | Run `uv tool update-shell`, then open a new terminal |
| Claude Code shows the Cortex server as failed, or a hook error naming `cortex` | Check that `cortex --version` works in a new terminal, then restart Claude Code |
| The first reply of a Claude Code chat is slow | The plugin's hook is loading your context. It waits at most 3 seconds; if your context is not ready by then, it asks Claude to load it instead |
| Chat shows `cortex` as failed | Use the full path to `cortex` in the config (section 7), then fully quit and reopen |
| The model download failed | Search still works with keywords. Retry later with `cortex model download` |
| Doctor warns about two Cortex servers | Normal if you use both the plugin and the desktop entry. The desktop app's Code tab then shows two copies |
| Doctor warns about cloud sync | Move the vault out of OneDrive, Dropbox, Google Drive or iCloud. Sync can break Cortex's file lock and copies private pages off your machine |
| `git push` is refused | On purpose: the vault's pre-push hook keeps it local |
| A day ends late | Cortex starts a new day at 04:00 in your computer's time zone |

## 11. Updating and uninstalling

Update the program by running `git pull` in your Cortex-app folder. If the update adds a dependency or a command, also run `uv tool install --editable . --reinstall` there. In Claude Code, run `/plugin update cortex@cortex` and restart.

To remove Cortex completely:

1. In Claude Code, run `/plugin uninstall cortex@cortex`.
2. Remove the `cortex` entry from `claude_desktop_config.json`.
3. Run `uv tool uninstall cortex-learner`.
4. If you turned on **Start at login** under Browsing, turn it off there first (Windows: a "Cortex browse receiver" value under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`; the installer's uninstall removes it; macOS: `~/Library/LaunchAgents/cortex-browse-receiver.plist`; Linux: `~/.config/systemd/user/cortex-browse-receiver.service`, or `~/.config/autostart/cortex-browse-receiver.desktop` without systemd).
5. Delete the `~/.cortex` folder (the vault pointer, settings and the search model).
6. Delete the Cortex-app folder.

Your vault folder is yours. Keep it, back it up with `cortex backup`, or delete it.

## 12. Notes for developers

**The editable install.** Section 4 installs with `uv tool install --editable .`, so code changes in the clone apply without reinstalling. After pulling changes that add commands (such as `cortex taint`) or dependencies, reinstall.

**Always on.** Cortex needs no `/cortex` trigger. Read tools (`cortex_context`, `cortex_find`, `cortex_read`, ...) work at any time and open no session. The first write opens an `ambient` session; it closes through `cortex_end`, the 2 h idle close, the plugin's `SessionEnd` sweep, or the next `cortex_start`. The plugin's `UserPromptSubmit` hook (`cortex prime-hook`) adds the light part of `cortex_prime` (profile, preferences, contract core; about 1 s) as context on the first prompt of each chat and asks Claude to fetch related pages with `cortex_prime(related_only=true)`; it tracks primed chat ids in `<vault>/.cortex/primed` and waits at most 3 s (after a timeout or error it only nudges Claude to call `cortex_prime`). The plugin also has a `PostToolUse` hook (`cortex taint`): after a web, browser or other non-Cortex MCP tool it marks the vault so the next `me/*` write counts as `exposed` (docs/tools.md, Claims). The Chat tab has no hooks; the server instructions ask Claude to call `cortex_prime` itself. The index keeps a parse cache in `<vault>/.cortex/index-snapshot.json` (local, gitignored, safe to delete). `/cortex [mode] [topic]` starts a deliberate session; modes are `home` (default), `learn`, `review`, `short`, `log`, `about`, `week`, `capture`, `reflect`, `project`, `save`, `history` and `probe` (docs/tools.md).

**Building the skill.** `skills/cortex/` (`SKILL.md` and `modes/*.md`) is the only copy you edit. After changing it, run `uv run python scripts/build_skills.py`. It rewrites `plugin/skills/cortex/` and `dist/cortex-skill.zip`. Then run `/plugin update cortex@cortex` in Claude Code and restart; in Chat, remove the old `cortex` skill and upload the new zip.
