# AI Research & Monitoring Agent

A single scheduled AI agent that emails me one filtered daily briefing instead
of me visiting a dozen sites. Built to answer a practical question: how much of
my daily "keeping up with the field" can a small, well-prompted agent do for me?

**What it does each morning:**

- Parses a configurable set of RSS/Atom feeds and collects new items
- Fingerprints monitored web pages (e.g. careers pages) and detects changes
  between runs
- Surfaces personal key dates as escalating reminders (60/30/7 days out)
- Sends everything to the Claude API with a structured prompt built around a
  personal profile, asking for at most 5 ruthlessly filtered items with a
  "why this matters for *you*" line each
- Generates a rotating 5-minute micro-lesson per day from a topic list
- Emails the result as a formatted HTML digest (Gmail SMTP), and archives a
  copy in the repo

**Engineering choices I care about:**

- **Fail-soft everywhere** — a dead feed or redesigned page is reported at the
  bottom of the digest, never crashes the run
- **State between runs** — seen-item hashes and page fingerprints persist in a
  committed `state.json`, so the agent knows what's new
- **Configuration over code** — profile, sources, dates, and lesson topics all
  live in one YAML file; the Python never needs touching to repurpose the
  agent for a completely different field
- **Cheap by design** — runs free on GitHub Actions cron; the LLM step uses a
  small model (~€0.01–0.05/day)

**Stack:** Python · feedparser · requests · PyYAML · Claude API · GitHub
Actions · smtplib

**Honest provenance:** designed and built with AI pair-programming, then
configured, deployed, and maintained by me — which was itself a useful study
in where AI coding tools genuinely help and where human judgement stays
load-bearing.

## Run it yourself

1. Fork/clone into a private repo
2. Edit `config.yaml` — profile, feeds, pages, dates, topics
3. Add repo secrets: `ANTHROPIC_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`
   (Gmail app password; skip the Gmail ones to archive digests in-repo only)
4. Actions tab → "Daily Digest" → Run workflow to test; it then runs on the
   cron schedule in `.github/workflows/daily-digest.yml`
