#!/usr/bin/env python3
"""rssdigest - fetch RSS feeds, summarise each item, email a digest.

Pipeline: fetch feed items -> summarise each -> render -> email. Two ways to
run it:

  run    one shot: fetch -> summarise via the Anthropic API -> email.
         Uses ANTHROPIC_API_KEY, so it spends API credits.

  fetch  print new feed items as JSON. No API key, no model call.
    |    A caller (e.g. a Claude Code routine on your subscription) adds a
    |    "summary" to each item, then pipes them into:
  send   render + email the items it is given, then record them as sent.

The fetch -> (summarise elsewhere) -> send split lets the model work run on a
Claude Code subscription instead of paid API credits. See README.md.

Secrets come from the environment (or a .env file beside this script); nothing
is hardcoded. See .env.example for the full list.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage
from email.utils import formatdate

import feedparser

__version__ = "0.1.0"

DEFAULT_FEEDS = ["https://www.bleepingcomputer.com/feed/"]
DEFAULT_MODEL = "claude-opus-5-5"  # a 2-sentence summary is cheap on any model;
# switch to "claude-haiku-4-5" or "claude-sonnet-5-5" in .env to cut cost further.
DEFAULT_MAX_ITEMS = 10
DEFAULT_SMTP_PORT = 587
SUMMARY_PROMPT = "Summarize this in two sentences.\n\n{content}"
MAX_CONTENT_CHARS = 8000  # trim article bodies before summarising

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def load_dotenv(path):
    """Minimal .env loader (KEY=VALUE lines). Avoids a python-dotenv dependency.
    Existing environment variables always win, so a real export overrides the file."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def env_list(name, default):
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def resolve_state(args):
    """State file path, or None if --no-state or unset."""
    if getattr(args, "no_state", False):
        return None
    return getattr(args, "state", None) or os.environ.get("RSS_STATE")


# --------------------------------------------------------------------------- #
# Fetch + clean
# --------------------------------------------------------------------------- #

def strip_html(text):
    """Flatten HTML to plain text so we don't waste tokens (and don't hand the
    summariser a pile of markup). Feed content is untrusted, so this is also a
    small sanitising step before the text ever reaches the summary / email."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def entry_content(entry):
    """Best available body text for a feed entry: content:encoded, else summary."""
    if entry.get("content"):
        # feedparser returns a list of content dicts
        return entry["content"][0].get("value", "")
    return entry.get("summary", "") or entry.get("description", "")


def fetch_items(feeds, max_items):
    """Return a flat list of normalised items across all feeds, newest first."""
    items = []
    for url in feeds:
        parsed = feedparser.parse(url)
        if parsed.bozo and not parsed.entries:
            print(f"warning: could not parse feed {url}: "
                  f"{getattr(parsed, 'bozo_exception', 'unknown error')}",
                  file=sys.stderr)
            continue
        source = parsed.feed.get("title", url)
        for entry in parsed.entries:
            items.append({
                "id": entry.get("id") or entry.get("link", ""),
                "title": entry.get("title", "(untitled)"),
                "link": entry.get("link", ""),
                "creator": entry.get("author", "") or entry.get("dc_creator", ""),
                "content": strip_html(entry_content(entry)),
                "source": source,
            })
    if max_items > 0:
        items = items[:max_items]
    return items


# --------------------------------------------------------------------------- #
# State (so a daily run only emails items it hasn't seen before)
# --------------------------------------------------------------------------- #

def load_seen(path):
    if not path:
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except OSError:
        return set()


def save_seen(path, seen):
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(seen)))


# --------------------------------------------------------------------------- #
# Summarise (API path, used by `run` only)
# --------------------------------------------------------------------------- #

def summarise(client, model, text):
    """Two-sentence summary via Claude. Minimal request keeps it portable across
    Opus / Sonnet / Haiku. Returns None on failure so the caller can fall back."""
    content = text[:MAX_CONTENT_CHARS]
    if not content:
        return None
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user",
                       "content": SUMMARY_PROMPT.format(content=content)}],
        )
    except Exception as exc:  # anthropic.* errors, network, etc.
        print(f"warning: summary failed ({exc.__class__.__name__}: {exc})",
              file=sys.stderr)
        return None
    return "".join(b.text for b in msg.content if b.type == "text").strip() or None


def excerpt(text, limit=280):
    """Fallback used when no summary is available."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------- #
# Summarise (subscription path, used by `run-local`)
# --------------------------------------------------------------------------- #

def extract_json_array(text):
    """Pull a JSON array out of a model reply that may be fenced or chatty."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def summarise_local(items, model=None):
    """Summarise via the local `claude` CLI (-p). This runs on the user's Claude
    Code subscription, NOT the Anthropic API, so it spends no API credits. One
    batched call for all items. Mutates items in place; returns True on success."""
    if not shutil.which("claude"):
        print("error: 'claude' CLI not found on PATH (is Claude Code installed?)",
              file=sys.stderr)
        return False
    payload = [{"id": it["id"], "title": it["title"],
                "content": it["content"][:MAX_CONTENT_CHARS]} for it in items]
    prompt = (
        "You are a summariser. Input is a JSON array of articles, each with "
        "id, title and content. For each article write a two-sentence summary. "
        "Return ONLY a JSON array of objects {\"id\": <id>, \"summary\": <text>}, "
        "one per input article, with no markdown fences and no other text.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"warning: local summariser failed ({exc})", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"warning: claude exited {proc.returncode}: "
              f"{proc.stderr.strip()[:200]}", file=sys.stderr)
        return False
    # `--output-format json` wraps the reply in an envelope with a `result` field.
    result_text = proc.stdout
    try:
        env = json.loads(proc.stdout)
        if isinstance(env, dict) and "result" in env:
            result_text = env["result"]
    except json.JSONDecodeError:
        pass
    try:
        pairs = extract_json_array(result_text)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"warning: could not parse summaries from claude ({exc})",
              file=sys.stderr)
        return False
    by_id = {p.get("id"): (p.get("summary") or "").strip()
             for p in pairs if isinstance(p, dict)}
    for it in items:
        if by_id.get(it["id"]):
            it["summary"] = by_id[it["id"]]
    return True


# --------------------------------------------------------------------------- #
# Render + send
# --------------------------------------------------------------------------- #

def render_html(items):
    """Build the digest email body. Every field is HTML-escaped because it comes
    from an untrusted feed; the layout is a simple inline-styled digest."""
    blocks = []
    for it in items:
        title = html.escape(it.get("title", "(untitled)"))
        link = html.escape(it.get("link", ""), quote=True)
        creator = html.escape(it.get("creator", "")) or "Unknown"
        summary = html.escape(it.get("summary") or excerpt(it.get("content", "")))
        blocks.append(f"""
  <div style="margin-bottom: 24px; border-bottom: 1px solid #ccc; padding-bottom: 16px;">
    <h2 style="margin: 0 0 8px 0;">
      <a href="{link}" style="color: #1a73e8; text-decoration: none;">{title}</a>
    </h2>
    <p style="margin: 0 0 4px 0; color: #888; font-size: 12px;">By {creator}</p>
    <p style="margin: 0; font-size: 14px;">{summary}</p>
  </div>""")
    return ("<div style=\"font-family: Arial, Helvetica, sans-serif; "
            "max-width: 680px; margin: 0 auto;\">" + "".join(blocks) + "</div>")


def send_email(host, port, user, password, starttls, mail_from, mail_to,
               subject, html_body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(mail_to)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content("This digest is formatted as HTML. Please view it in an "
                    "HTML-capable email client.")
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        if starttls:
            smtp.starttls()
            smtp.ehlo()
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)


def deliver(items, mail_to, dry_run, state_path, out_path=None):
    """Shared tail of `send`/`run`/`run-local`: render, then write to a file
    (--out), print (--dry-run), or email. --out and --dry-run are previews: they
    do not send and do not record state."""
    subject = os.environ.get("MAIL_SUBJECT", "RSS Summary News")
    html_body = render_html(items)

    if out_path:
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html_body)
        except OSError as exc:
            print(f"error: could not write {out_path}: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {len(items)} item(s) to {out_path}")
        return 0

    if dry_run:
        print(html_body)
        return 0

    host = os.environ.get("SMTP_HOST")
    mail_from = os.environ.get("MAIL_FROM")
    if not (host and mail_from and mail_to):
        print("error: SMTP_HOST, MAIL_FROM and MAIL_TO (or --to) are required "
              "to send; use --dry-run to preview without sending", file=sys.stderr)
        return 2

    port = int(os.environ.get("SMTP_PORT", DEFAULT_SMTP_PORT))
    starttls = os.environ.get("SMTP_STARTTLS", "true").lower() != "false"
    try:
        send_email(
            host=host, port=port,
            user=os.environ.get("SMTP_USER"),
            password=os.environ.get("SMTP_PASS"),
            starttls=starttls, mail_from=mail_from, mail_to=mail_to,
            subject=subject, html_body=html_body,
        )
    except (smtplib.SMTPException, OSError) as exc:
        print(f"error: sending failed ({exc.__class__.__name__}: {exc})",
              file=sys.stderr)
        return 1

    print(f"sent '{subject}' to {', '.join(mail_to)} ({len(items)} item(s))")

    if state_path:  # only record as sent after a successful send
        seen = load_seen(state_path)
        seen.update(it.get("id", "") for it in items if it.get("id"))
        save_seen(state_path, seen)
    return 0


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #

def cmd_fetch(args):
    """Fetch feeds, drop already-seen items, print the rest as JSON."""
    feeds = args.feeds or env_list("RSS_FEEDS", DEFAULT_FEEDS)
    max_items = (args.max_items if args.max_items is not None
                 else int(os.environ.get("RSS_MAX_ITEMS", DEFAULT_MAX_ITEMS)))
    state_path = resolve_state(args)

    items = fetch_items(feeds, max_items)
    seen = load_seen(state_path)
    fresh = [it for it in items if it["id"] not in seen] if state_path else items

    json.dump(fresh, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    print(f"fetched {len(fresh)} new item(s)", file=sys.stderr)
    return 0


def cmd_send(args):
    """Read items (with summaries) as JSON, render + email, record state."""
    src = sys.stdin if args.from_file in (None, "-") else open(
        args.from_file, encoding="utf-8")
    try:
        items = json.load(src)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"error: could not read items JSON: {exc}", file=sys.stderr)
        return 2
    finally:
        if src is not sys.stdin:
            src.close()

    if not isinstance(items, list) or not items:
        print("no items to send", file=sys.stderr)
        return 0

    mail_to = args.to or env_list("MAIL_TO", [])
    return deliver(items, mail_to, args.dry_run, resolve_state(args), args.out)


def cmd_run_local(args):
    """Manual one shot on your subscription: fetch -> summarise via the local
    `claude` CLI (no API credits) -> email."""
    feeds = args.feeds or env_list("RSS_FEEDS", DEFAULT_FEEDS)
    max_items = (args.max_items if args.max_items is not None
                 else int(os.environ.get("RSS_MAX_ITEMS", DEFAULT_MAX_ITEMS)))
    mail_to = args.to or env_list("MAIL_TO", [])
    state_path = resolve_state(args)

    items = fetch_items(feeds, max_items)
    if not items:
        print("no feed items found; nothing to do", file=sys.stderr)
        return 0
    seen = load_seen(state_path)
    fresh = [it for it in items if it["id"] not in seen] if state_path else items
    if not fresh:
        print("no new items since last run; nothing to send")
        return 0

    if not summarise_local(fresh, args.model):
        print("note: falling back to plain excerpts for this run", file=sys.stderr)
    for it in fresh:
        if not it.get("summary"):
            it["summary"] = excerpt(it["content"])

    return deliver(fresh, mail_to, args.dry_run, state_path, args.out)


def cmd_run(args):
    """One shot: fetch -> summarise via the Anthropic API -> email."""
    feeds = args.feeds or env_list("RSS_FEEDS", DEFAULT_FEEDS)
    model = args.model or os.environ.get("RSSDIGEST_MODEL", DEFAULT_MODEL)
    max_items = (args.max_items if args.max_items is not None
                 else int(os.environ.get("RSS_MAX_ITEMS", DEFAULT_MAX_ITEMS)))
    mail_to = args.to or env_list("MAIL_TO", [])
    state_path = resolve_state(args)

    items = fetch_items(feeds, max_items)
    if not items:
        print("no feed items found; nothing to do", file=sys.stderr)
        return 0

    seen = load_seen(state_path)
    fresh = [it for it in items if it["id"] not in seen] if state_path else items
    if not fresh:
        print("no new items since last run; nothing to send")
        return 0

    client = None
    if not args.no_ai:
        try:
            import anthropic
        except ImportError:
            print("error: the 'anthropic' package is not installed "
                  "(pip install anthropic), or use --no-ai", file=sys.stderr)
            return 2
        if not (os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            print("error: ANTHROPIC_API_KEY is not set (or use --no-ai)",
                  file=sys.stderr)
            return 2
        client = anthropic.Anthropic()

    for it in fresh:
        summary = summarise(client, model, it["content"]) if client else None
        it["summary"] = summary or excerpt(it["content"])
        if client:
            time.sleep(0.2)  # be gentle on the API

    return deliver(fresh, mail_to, args.dry_run, state_path, args.out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _add_feed_args(p):
    p.add_argument("--feed", action="append", dest="feeds", metavar="URL",
                   help="feed URL (repeatable); overrides RSS_FEEDS / default")
    p.add_argument("--max-items", type=int, default=None,
                   help=f"max items (default {DEFAULT_MAX_ITEMS}; 0 = all)")


def _add_state_args(p):
    p.add_argument("--state", default=None, metavar="PATH",
                   help="file of already-sent item ids; only new items are used")
    p.add_argument("--no-state", action="store_true",
                   help="ignore state: treat every current item as new")


def _add_output_args(p):
    p.add_argument("--out", default=None, metavar="FILE",
                   help="write the rendered HTML to FILE instead of emailing")
    p.add_argument("--dry-run", action="store_true",
                   help="print the HTML instead of emailing")


def build_parser():
    p = argparse.ArgumentParser(
        prog="rssdigest",
        description="Fetch RSS feeds, summarise each item, email a digest.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="print new feed items as JSON (no API key)")
    _add_feed_args(f)
    _add_state_args(f)
    f.set_defaults(func=cmd_fetch)

    s = sub.add_parser("send", help="email items (with summaries) read as JSON")
    s.add_argument("--from", dest="from_file", metavar="FILE", default=None,
                   help="read items JSON from FILE instead of stdin")
    s.add_argument("--to", action="append", dest="to", metavar="ADDR",
                   help="recipient (repeatable); overrides MAIL_TO")
    _add_output_args(s)
    _add_state_args(s)
    s.set_defaults(func=cmd_send)

    rl = sub.add_parser("run-local",
                        help="fetch + summarise via local Claude Code + email "
                             "(subscription, no API credits)")
    _add_feed_args(rl)
    rl.add_argument("--to", action="append", dest="to", metavar="ADDR",
                    help="recipient (repeatable); overrides MAIL_TO")
    rl.add_argument("--model", default=None,
                    help="model for the local `claude` CLI (default: its own default)")
    _add_output_args(rl)
    _add_state_args(rl)
    rl.set_defaults(func=cmd_run_local)

    r = sub.add_parser("run", help="fetch + summarise via the API + email (uses credits)")
    _add_feed_args(r)
    r.add_argument("--to", action="append", dest="to", metavar="ADDR",
                   help="recipient (repeatable); overrides MAIL_TO")
    r.add_argument("--model", default=None,
                   help=f"Claude model id (default {DEFAULT_MODEL})")
    r.add_argument("--no-ai", action="store_true",
                   help="skip the API; use a plain excerpt as the summary")
    _add_output_args(r)
    _add_state_args(r)
    r.set_defaults(func=cmd_run)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
