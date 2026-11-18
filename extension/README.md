# Cortex capture (browser extension)

A small Manifest V3 extension for Perplexity's Comet, Chrome, Edge and Brave (all
Chromium-based; it loads the same way in each). It notes how long you actually
look at a page, a short readable excerpt of pages you read for 20 seconds or more, and the
title and channel of YouTube videos. It sends them **only** to the Cortex receiver on your
own computer (`http://127.0.0.1:<port>`). There is no server, no account and no remote code.

## Install (Load unpacked)

1. Open `comet://extensions` in Comet (`chrome://extensions` works there too; Chrome:
   `chrome://extensions`, Edge: `edge://extensions`, Brave: `brave://extensions`).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and choose the extension folder. If you installed Cortex with the
   installer, the Cortex app's Browsing section opens the extension folder for you; from a
   source checkout, it is this `extension/` folder.
4. Pin "Cortex capture" to the toolbar so you can open its popup.

The extension is set to `"incognito": "not_allowed"`, so it can never run in private windows.

## Pair it with Cortex

1. In a terminal, run `cortex browse pair` (from a source checkout, prefix `uv run`). It
   prints a token and saves it in your vault at `.cortex/browse/token` (git-ignored). Running it again makes a new token and the
   old one stops working. In the Cortex app, **Browsing** → **Set up the extension** does
   steps 1 and 2 in one click and shows the token once.
2. Start the receiver: `cortex browse serve` (port 8799 unless you pass another). It
   listens on 127.0.0.1 only and refuses any other address. It must be running for anything
   to be stored or processed; see "Run the receiver at login" below.
3. Open the popup, enter the port and paste the token, then press **Save and test**. The top
   line should say "Receiver connected".

## When your browsing is processed

The extension records all the time but processes nothing on its own. Each visit is finished
when you switch tab, navigate away or close the tab, and is saved to the extension's local
queue at once, so closing the browser does not lose it. The queue is sent to the receiver
every minute, and the receiver stores it in your vault's inbox. Processing (the same import
as `cortex browse import`: your private browse pages, with your local model if
`localllm.enabled` is set) runs on the receiver when a browsing session ends:

- you close the last browser window (the extension sends what is left, marked as the end);
- you are idle or the screen is locked for 15 minutes;
- the browser starts again after a run that ended without that last send (for example
  Comet was killed, or the computer shut down): the leftovers go first, marked as the end;
- the receiver got records and then nothing for 20 minutes (a fallback, if the end was
  never sent);
- you press **Process now** in the popup.

The receiver waits 10 seconds after an end so several ends make one run, and never runs two
imports at once. The visit on screen at the moment the browser is killed is lost; finished
visits are not.

When the receiver is not running, visits wait in the extension's local queue (at most 2,000;
the oldest go first) and are sent once it is back. It retries every minute, backing off up to
an hour while the receiver is down. A queued visit older than 24 hours is dropped unsent.
Until a token is saved, and after the receiver refuses the token, the extension times, reads
and queues nothing; saving a new token in the popup starts it again.

## The popup

- **Pause / Resume:** while paused nothing is timed, read or queued, and the visit on screen
  is dropped.
- **Never on this site:** adds the current site (and its subdomains) to your deny list. The
  extension then never reads it or sends anything about it. The popup has no way yet to take
  a site off the list; to clear the list, remove and re-add the extension, which also clears
  the queue and the pairing.
- **Sent today** and **Waiting in queue:** counts only.
- **Process now:** sends the queue and asks the receiver to process it. Below it: when the
  receiver last processed, and how many records wait in its inbox.
- **Pairing:** the port and token.

For deny rules that also cover the history import, add domains under `browse:` →
`deny_domains` in `~/.cortex/config.yaml`. The extension fetches that list from the
receiver every minute and applies it too: it never reads or sends a denied site. Your
`private_domains` list never leaves the receiver; it clears the text of every private visit
before storing it.

## What is captured

One record per visit, sent when you leave the page (switch tab, navigate or close):

- the URL with its query string and fragment removed, except a search engine's search term
  and a YouTube video id;
- the page title (for a search: only the search term);
- when the visit started and how long it was on screen (the focused tab of the focused
  window while you are not idle; idle means 60 s without input);
- for pages on screen 20 s or more: up to 2,000 characters of the main text (`article`,
  `main`, or the block with the most paragraph text, without navigation, header, footer and
  sidebars);
- for YouTube videos: the title and channel only.

## What is never captured

- Anything in incognito windows.
- Form fields: `input`, `textarea`, `select`, `[contenteditable]` and everything inside a
  `form` are removed before any text is read. A page with a password field gives no text.
- Sites on the built-in deny list, which mirrors the receiver's filter
  (`src/cortex/browse/lists.py`): sign-in, OAuth, SSO and password pages; payment, checkout
  and bank sites; webmail; local and intranet addresses; URLs carrying tokens, codes, keys or
  sessions. On these pages the extension does not inject anything.
- Sites on your own deny list, and anything while paused.
- Non-web pages (`chrome://`, `file:`, extension pages and so on).

## What happens on the receiver

The receiver checks the token and drops any request from a web page (only
`chrome-extension://` and `moz-extension://` origins are accepted). It runs the same privacy
filter again before storing anything: dropped records are never written. Kept ones go to
`.cortex/browse/inbox-YYYY-MM-DD.jsonl` in your vault (git-ignored), with sensitive
categories (health, money, betting, dating, sexuality, drugs, self-harm and, if you allow it,
adult) and your `private_domains` marked private and their text cleared. For the categories
that cloud AI apps can read (after you turn on `browse.visible_to_claude`), the site pages include the page titles, the search terms and the
site names, not only your topics and time; hidden categories stay on your private browse
pages. The receiver never logs URLs or titles.

## Run the receiver at login (optional)

The Cortex app's **Browsing** section has a **Start at login** switch that does this for you
(a Run entry on Windows, a LaunchAgent on macOS, a user service on Linux). To do it by hand
instead:

To have the receiver start whenever you sign in to Windows, create a Task Scheduler task
once, yourself, in a terminal (find the `cortex` path with `where cortex`):

```
schtasks /Create /SC ONLOGON /TN CortexBrowse /TR "\"C:\path\to\cortex.exe\" browse serve"
```

From a source checkout, point the task at uv instead (find it with `where uv`):
`/TR "\"C:\path\to\uv.exe\" run --project C:\path\to\cortex cortex browse serve"`.

Remove it with `schtasks /Delete /TN CortexBrowse`. The task runs `serve` with your default
vault from `~/.cortex/config.yaml`.

## Files

- `manifest.json`: MV3 manifest (`tabs`, `storage`, `idle`, `scripting`, `alarms`).
- `background.js`: the service worker: time on screen, the queue, the flush and the session
  ends.
- `content.js`: injected on demand to read the excerpt or the video's title and channel.
- `lib.js`: the deny check, URL scrub and session-end rules; `RULES` is checked against the
  Python lists, and the session rules run in node, in `tests/test_browse_serve.py`.
- `popup.html`, `popup.css`, `popup.js`: the popup.
