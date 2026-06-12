#!/usr/bin/env python3
"""
GRC Career Digest — a single scheduled agent.

Each run it:
  1. Pulls new items from configured RSS feeds (news, GRC, AI governance)
  2. Checks target-company careers pages for changes
  3. Picks today's 5-minute learning topic from the rotation
  4. Asks Claude to write one filtered, personalised daily digest
  5. Emails it (Gmail SMTP) and/or saves it to digests/ in the repo

Designed to fail soft: any broken feed or page is reported in the digest,
never crashes the run.
"""

import hashlib
import html
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
DIGEST_DIR = ROOT / "digests"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
UA = {"User-Agent": "Mozilla/5.0 (career-digest personal agent; contact: repo owner)"}


# ----------------------------------------------------------------- utilities

def load_config():
    with open(ROOT / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"seen_entries": [], "page_hashes": {}, "topic_index": 0}


def save_state(state):
    # Keep the seen-entries list bounded
    state["seen_entries"] = state["seen_entries"][-2000:]
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def strip_html(text, limit=400):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def page_text(html_doc, limit=6000):
    """Crude visible-text extraction good enough for change detection."""
    doc = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html_doc)
    doc = re.sub(r"<[^>]+>", " ", doc)
    doc = html.unescape(re.sub(r"\s+", " ", doc)).strip()
    return doc[:limit]


# ----------------------------------------------------------------- gathering

def collect_feed_items(cfg, state):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.get("lookback_hours", 26))
    items, broken = [], []
    seen = set(state["seen_entries"])

    for feed in cfg.get("feeds", []):
        try:
            parsed = feedparser.parse(feed["url"], request_headers=UA)
            if parsed.bozo and not parsed.entries:
                raise ValueError(str(parsed.bozo_exception))
            for e in parsed.entries[:15]:
                uid = e.get("id") or e.get("link") or e.get("title", "")
                key = hashlib.sha1((feed["name"] + uid).encode()).hexdigest()
                if key in seen:
                    continue
                published = e.get("published_parsed") or e.get("updated_parsed")
                if published:
                    when = datetime(*published[:6], tzinfo=timezone.utc)
                    if when < cutoff:
                        continue
                items.append({
                    "source": feed["name"],
                    "title": strip_html(e.get("title", ""), 200),
                    "link": e.get("link", ""),
                    "summary": strip_html(e.get("summary", "")),
                })
                state["seen_entries"].append(key)
        except Exception as exc:  # noqa: BLE001 — fail soft by design
            broken.append(f"{feed['name']}: {exc}")
    return items, broken


def check_careers_pages(cfg, state):
    changed, broken = [], []
    for page in cfg.get("careers_pages", []):
        try:
            resp = requests.get(page["url"], headers=UA, timeout=30)
            resp.raise_for_status()
            text = page_text(resp.text)
            digest = hashlib.sha1(text.encode()).hexdigest()
            previous = state["page_hashes"].get(page["name"])
            state["page_hashes"][page["name"]] = digest
            if previous and previous != digest:
                changed.append({"name": page["name"], "url": page["url"],
                                "text": text[:3500]})
            elif previous is None:
                changed.append({"name": page["name"], "url": page["url"],
                                "text": text[:3500], "first_run": True})
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{page['name']}: {exc}")
    return changed, broken


# ----------------------------------------------------------------- the agent

def upcoming_dates(cfg, horizon_days=60):
    """Key dates within the horizon, with days remaining."""
    today = datetime.now().date()
    out = []
    for d in cfg.get("key_dates", []):
        try:
            when = datetime.strptime(d["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        delta = (when - today).days
        if -3 <= delta <= horizon_days:
            out.append({"date": d["date"], "days_away": delta, "note": d["note"]})
    return sorted(out, key=lambda x: x["days_away"])


def build_prompt(cfg, items, pages_changed, topic, broken):
    today = datetime.now().strftime("%A %d %B %Y")
    dates = upcoming_dates(cfg)
    lines = [
        f"Today is {today}. You write a private daily career digest for this person:",
        "", "<profile>", cfg["profile"].strip(), "</profile>", "",
        "Write the digest as clean HTML body content (no <html>/<head>, just "
        "headings, paragraphs, lists, links). Warm but efficient tone. Sections:",
        "",
        "1. <h2>⚡ Act on this</h2> — ALWAYS list any items from <upcoming_dates> "
        "below (these are her own planned deadlines; show date, days remaining, and "
        "the note — escalate urgency under 14 days). Also include anything from the "
        "news/jobs below with a deadline. If neither exists, write one line: "
        "'Nothing needs action today.'",
        "2. <h2>💼 Jobs watch</h2> — from the careers-page extracts below, list any "
        "postings plausibly relevant to her (GRC, security, compliance, awareness, "
        "trainee/intern, junior). Link the careers page. If a page changed but you "
        "see no relevant posting, say so in one line. Ignore pages marked first_run "
        "(baseline only) unless a clearly relevant posting is visible.",
        "3. <h2>📰 What matters today</h2> — pick AT MOST 5 items from the feed "
        "items below. For each: title as a link, then ONE sentence on what happened "
        "and ONE on why it matters *for her path specifically* (GRC, NIS2, GDPR, "
        "AI governance, human-factor security, Finland). Ruthlessly skip everything "
        "else. Fewer, better items beat coverage.",
        f"4. <h2>📚 Five-minute lesson: {topic}</h2> — teach this topic in ~200 "
        "words at the depth expected in ISC2 CC / Security+ / an entry GRC "
        "interview. End with one self-check question (answer hidden in "
        "<details><summary>Answer</summary>...</details>).",
        "5. If any sources failed, end with a one-line <p><small>Sources "
        "skipped: ...</small></p>.",
        "",
        "Do not invent items. Only use what is provided below.",
        "", "<upcoming_dates>",
        json.dumps(dates, ensure_ascii=False, indent=1),
        "</upcoming_dates>", "", "<feed_items>",
        json.dumps(items[:60], ensure_ascii=False, indent=1),
        "</feed_items>", "", "<careers_pages_changed>",
        json.dumps(pages_changed, ensure_ascii=False, indent=1),
        "</careers_pages_changed>", "",
        f"<broken_sources>{json.dumps(broken)}</broken_sources>",
    ]
    return "\n".join(lines)


def call_claude(cfg, prompt):
    api_key = os.environ["ANTHROPIC_API_KEY"]
    resp = requests.post(
        ANTHROPIC_URL,
        headers={"x-api-key": api_key,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": cfg.get("model", "claude-haiku-4-5-20251001"),
              "max_tokens": cfg.get("max_tokens", 3000),
              "messages": [{"role": "user", "content": prompt}]},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")


# ----------------------------------------------------------------- delivery

def send_email(subject, html_body):
    addr = os.environ.get("GMAIL_ADDRESS")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pwd:
        print("Email secrets not set — skipping email, saving to digests/ only.")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = addr
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(addr, pwd)
        server.sendmail(addr, [addr], msg.as_string())
    return True


def save_digest(html_body):
    DIGEST_DIR.mkdir(exist_ok=True)
    path = DIGEST_DIR / f"{datetime.now():%Y-%m-%d}.html"
    path.write_text(html_body, encoding="utf-8")
    return path


# ----------------------------------------------------------------- main

def main():
    cfg = load_config()
    state = load_state()

    items, broken_feeds = collect_feed_items(cfg, state)
    pages_changed, broken_pages = check_careers_pages(cfg, state)
    broken = broken_feeds + broken_pages

    topics = cfg.get("learning_topics", [])
    topic = topics[state.get("topic_index", 0) % len(topics)] if topics else "GRC fundamentals"
    state["topic_index"] = state.get("topic_index", 0) + 1

    print(f"Feed items: {len(items)} | pages changed: {len(pages_changed)} "
          f"| broken sources: {len(broken)} | lesson: {topic}")

    prompt = build_prompt(cfg, items, pages_changed, topic, broken)
    digest_html = call_claude(cfg, prompt)

    body = (f"<div style='font-family:Georgia,serif;max-width:680px;margin:auto;"
            f"line-height:1.55'>{digest_html}</div>")
    subject = f"Your GRC Daily — {datetime.now():%a %d %b %Y}"

    emailed = send_email(subject, body)
    path = save_digest(body)
    save_state(state)
    print(f"Digest saved to {path}" + (" and emailed." if emailed else "."))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
