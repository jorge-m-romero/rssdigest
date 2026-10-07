# rssdigest

Fetch RSS feeds, summarise each item in two sentences, and email a digest.
A port of an n8n flow (RSS Read → summarise → aggregate → send email) into a
single script you trigger by hand when you want the digest.

## How the summarising is billed

The summary is the only step that uses a model. There are two commands:

- **`run-local`** (recommended) — summarises with your **local Claude Code**
  (`claude -p`), which runs on your **Claude subscription**. No API credits.
- **`run`** — summarises via the Anthropic **API** (`ANTHROPIC_API_KEY`), which
  spends **API credits**. Kept for headless/no-subscription setups.

`fetch` and `send` need no model at all.

## How email works

The script **sends** over SMTP and the mail lands in your inbox — nothing
"connects Claude to your mailbox." Send from **Gmail** (Gmail still supports
16-char App Passwords; the same one from your n8n flow works here) and set
`MAIL_TO` to whatever address you want the digest delivered to.

## Install

```bash
cd ~/Projects/rssdigest
python3 -m pip install feedparser            # run-local only needs this
# python3 -m pip install -r requirements.txt # adds anthropic, for the `run` path
cp .env.example .env && chmod 600 .env       # then fill in SMTP + MAIL_TO
```

You also need Claude Code installed and logged in with your subscription
(`claude` on your PATH) for `run-local`.

## Use (manual trigger)

```bash
# Preview without emailing (still summarises on your subscription):
python3 rssdigest.py run-local --dry-run

# Send for real, newest 10 items:
python3 rssdigest.py run-local

# Only items you haven't been sent before (recommended):
python3 rssdigest.py run-local --state ~/.cache/rssdigest/seen.txt
```

```bash
# Save a preview to a file (open it in a browser) instead of emailing:
python3 rssdigest.py run-local --out preview.html

# All items in the feed (like n8n, which has no cap):
python3 rssdigest.py run-local --max-items 0

# A different / extra feed and an explicit recipient:
python3 rssdigest.py run-local --feed https://feeds.feedburner.com/TheHackersNews \
                               --to me@example.com
```

## Commands

| Command     | Summariser            | Billing          | Email |
|-------------|-----------------------|------------------|-------|
| `run-local` | local Claude Code     | subscription     | yes   |
| `run`       | Anthropic API (SDK)   | API credits      | yes   |
| `fetch`     | none (prints JSON)    | none             | no    |
| `send`      | none (takes JSON in)  | none             | yes   |

`fetch` and `send` are the building blocks (`fetch` → add summaries → `send`);
`run-local` and `run` just wire them together with a summariser.

## Options (flags)

| Flag | Commands | Meaning |
|------|----------|---------|
| `--feed URL` | `fetch`, `run-local`, `run` | Feed to read. Repeatable for several feeds. Overrides `RSS_FEEDS` / the default. |
| `--max-items N` | `fetch`, `run-local`, `run` | Cap on items. Default **10**. **`0` = no limit** (every item in the feed, like n8n). |
| `--to ADDR` | `send`, `run-local`, `run` | Recipient. Repeatable. Overrides `MAIL_TO`. |
| `--model ID` | `run-local`, `run` | `run-local`: model the local `claude` uses (default: its own). `run`: API model id (default `claude-opus-5-5`). |
| `--out FILE` | `send`, `run-local`, `run` | Write the rendered HTML to `FILE` instead of emailing (a preview; does not send, does not record state). |
| `--dry-run` | `send`, `run-local`, `run` | Print the rendered HTML to the terminal instead of emailing (preview; no send, no state). |
| `--state PATH` | all | File of already-sent item ids; only new items are used. Overrides `RSS_STATE`. |
| `--no-state` | all | Ignore state: treat every current item as new. |
| `--from FILE` | `send` | Read the items JSON from `FILE` instead of stdin. |
| `--no-ai` | `run` | Skip the API; use a plain text excerpt as the "summary". |
| `--version` | (top level) | Print the version and exit. |

`--out` and `--dry-run` are both previews — they render but never send and never
touch the state file.

## Configuration (environment variables)

Set these in `.env` (loaded automatically) or the real environment. CLI flags win
over env vars. See `.env.example`.

| Variable | Used by | Meaning |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | `run` only | Anthropic API key (the credit-spending path). Not needed for `run-local`. |
| `RSSDIGEST_MODEL` | `run` only | API model override (default `claude-opus-5-5`). |
| `RSS_FEEDS` | fetch/run/run-local | Comma-separated feed URLs (default: BleepingComputer). |
| `RSS_MAX_ITEMS` | fetch/run/run-local | Default item cap (default `10`; `0` = all). Same as `--max-items`. |
| `RSS_STATE` | all | Path to the already-sent ids file. Same as `--state`. |
| `SMTP_HOST` | send/run/run-local | SMTP server (e.g. `smtp.gmail.com`). |
| `SMTP_PORT` | send/run/run-local | SMTP port (default `587`). |
| `SMTP_STARTTLS` | send/run/run-local | `true` (default) to upgrade with STARTTLS; `false` to disable. |
| `SMTP_USER` | send/run/run-local | SMTP login (your Gmail address). |
| `SMTP_PASS` | send/run/run-local | Gmail App Password (not your normal password). |
| `MAIL_FROM` | send/run/run-local | From address. |
| `MAIL_TO` | send/run/run-local | Recipient(s), comma-separated. Overridden by `--to`. |
| `MAIL_SUBJECT` | send/run/run-local | Subject line (default `RSS Summary News`). |

## Only email new items

Point `--state` (or `RSS_STATE` in `.env`) at a file. Item ids are recorded
**after** a successful send, so a failed send won't lose items, and previews
(`--dry-run` / `--out`) never record anything:

```bash
python3 rssdigest.py run-local --state ~/.cache/rssdigest/seen.txt
```

## Tests

Pure-logic tests (no network, API, or SMTP — the `claude` CLI and feed parsing
are mocked), using the standard library `unittest`:

```bash
python3 -m unittest discover -s tests
```

## Item count vs n8n

The n8n flow summarised **every** item in the feed; `rssdigest` defaults to the
newest **10** (`--max-items 10`). To match n8n, pass `--max-items 0` (or set
`RSS_MAX_ITEMS=0`). With `--state` enabled you only ever summarise items you
haven't already been sent, regardless of the cap.
