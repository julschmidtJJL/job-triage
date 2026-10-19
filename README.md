# job-triage

A daily pipeline that reads job boards so you don't have to, scores every posting against a written profile of you, and puts the few worth a human look on a Notion board. Your verdicts on that board teach the next run.

**It doesn't apply anywhere.** It sends nothing to employers and fills in no forms. It decides what deserves your attention, shows its reasoning, and measures whether it was right.

It runs on GitHub Actions once a day. No server, no database.

It is extracted from the private pipeline that has run my own job search every day since 14 October 2026. That pipeline grew out of an earlier version I built in May and June 2026 for a family member's search. The history stays private because it holds personal profile notes.

## Quick start

Paste this into Claude Code. It works the same in Codex or Cursor.

```
Set up job-triage for me. Clone https://github.com/julschmidtJJL/job-triage,
then read skills/job-triage-setup/SKILL.md in it and follow it step by step.
Interview me for my profile and the roles I'm after, read every profile
section back to me before you write it, and never handle my API keys
yourself. Stay inside the repository and do not change any global agent
configuration.
```

The agent installs the dependencies into a `.venv/` in the repository and interviews you for your fit profile: what you have built, which situations fit you, your anti-signals and hard constraints. It writes the search queries with you, adapts the location if you are not in Hamburg, walks you through the Notion board, and runs a dry run so you see your first scored list before anything goes to Notion. The scheduled run on GitHub Actions comes last, and only if you want it. [`skills/job-triage-setup/SKILL.md`](skills/job-triage-setup/SKILL.md) is the whole procedure, if you would rather do it by hand.

You need an Anthropic API key and a free Notion account. Google Jobs and the Monday email are optional.

## What makes it more than a scraper

**It measures its own search.** Every job remembers which query found it. Every run prints a scoreboard per query: found, new, passed the prefilter, average score, and how many you marked Interesting or Apply. On Mondays it emails you the scoreboard and suggests new queries from titles you liked. It never edits your queries itself; that decision stays yours. In the first week, the scoreboard showed that three of the original German search terms averaged under 30 out of 100 and produced nothing worth applying to.

**It learns from your verdicts, carefully.** Your Notion decisions become a feature index: title words and companies you liked get a boost, the ones you rejected get a penalty. A word rejected three times or more is promoted to a stop-list, but:

- **Shadow mode first.** For 14 days the stop-list only logs what it *would* drop, so you can audit it before it acts.
- **Generic words can't be learned.** In the first week the loop tried to learn "manager", "data" and "engineer" from rejections, which would have dropped 94 postings. Search vocabulary and generic title words are now excluded and pruned on load.
- **Every drop is visible and reversible.** Dropped rows go to the board as Auto-Dropped with the reason. Set one back to Interesting and its word is rescued for good.
- **"Maybe" is neutral on purpose.** It takes a row out of your queue without teaching the loop anything, so lukewarm jobs neither multiply nor vanish.

**It shows its reasoning.** Each scored job carries a one-line reason and a one-line *situation fit* from Claude, the matched skills, and every feedback adjustment that moved its score.

## How it works

```
queries.json ──► scrapers ──► location + aggregator filter ──► dedup (seen URLs, reposts)
                                                                    │
       Notion decisions ──► stop-list + feature index ──────────────┤
                                                                    ▼
                                        Tier 1: role-term prefilter + feedback deltas
                                                                    │
                                        Tier 2: Claude scores against the fit profile
                                                                    │
              score ≥ 40, top N ──► Notion board        rest ──► holdover queue
                                                                    │
                                              query scoreboard + run summary
```

**Two layers, kept apart on purpose.**

- **The profile** (`profile/profile.md`, gitignored; the `FIT_PROFILE` secret on Actions) says who you are and which situations fit. A company's situation (no AI yet, first hire for the job, close to management) can count for more than its sector. Claude reads it for every batch of postings. `profile/profile.example.md` shows the shape.
- **The queries** (`profile/queries.json`) are the plain search strings job boards need, and they are the experiment. The committed file is the author's own search for AI roles in Hamburg; the setup replaces it with yours.

**State without a database.** Everything the pipeline learns lives in `data/` (seen URLs, stop-list, holdover queue, decision cache, query stats). On Actions, `data/` is restored from and saved to the Actions cache with a per-run key and a prefix fallback, so each run starts from the last run's state. A run that couldn't score or deliver saves nothing, so its jobs are retried the next day.

## Sources

| Scraper | What it searches |
|---|---|
| Arbeitsagentur | The Bundesagentur für Arbeit job search API: official, free, and strongest for Mittelstand employers. Temp-work and placement agencies are excluded. Works for any German city. |
| Google Jobs (ScaleSerp) | `site:` queries against German job boards; optional, needs `SERPAPI_KEY` |
| Hamburg Karriere | The city of Hamburg's job API |
| Good Jobs, Green Jobs, Digital Rapids | Hamburg boards |

It is tuned for Hamburg; the setup skill adapts it to another city. LinkedIn isn't included: its terms of service don't allow scraping.

## Setup by hand

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # then fill in the keys
cp profile/profile.example.md profile/profile.md    # then write the real profile
```

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude scoring (default model `claude-haiku-5-5`, override with `SCORING_MODEL`) |
| `NOTION_API_KEY` | Notion internal integration token |
| `NOTION_JOB_BOARD_DB_ID` | ID of the job board database |
| `NOTION_DATA_SOURCE_ID` | Data source ID of that database (`python scripts/get_notion_data_source_id.py` prints it) |
| `SERPAPI_KEY` | ScaleSerp key for Google Jobs (optional) |
| `FIT_PROFILE` | The profile text; overrides `profile/profile.md` |
| `MAIL_USER`, `MAIL_APP_PASSWORD`, `MAIL_TO` | Gmail sender, its app password, and recipient for the Monday report email (optional) |
| `MIN_NOTION_SCORE` | Minimum final score (0-100) for a job to reach Notion; default 40 |

### Notion setup

Create a database with these properties. The pipeline checks the first two before every run and skips the feedback read if they are missing.

| Property | Type | Values |
|---|---|---|
| Decision | Select | New, Interesting, Maybe, Apply, Applied, Not interesting, Declined, Auto-Dropped |
| Stop Reason | Text | |
| Job Title | Title | |
| Company, Location, Sources | Text | |
| URL | URL | |
| Relevance Score | Number | |
| Query, Portal, Status, Scoring Method, Fetch Method, Duplicate Status | Select | |
| Matched Keywords, AI Matched Skills | Multi-select | |

Create an internal integration at notion.so/my-integrations, connect it to the database (••• → Connections), and use its token as `NOTION_API_KEY`.

### GitHub Actions

Set the same variables as repository secrets, for example:

```bash
gh secret set FIT_PROFILE < profile/profile.md
```

Then set the repository variable `JOBHUNT_ENABLED` to `true`. Until you do, the daily schedule (05:17 UTC) skips itself, so a fresh fork doesn't fail every morning. A manual run from the Actions tab always starts.

### The Monday report

Every Monday the run emails the query scoreboard (last 7 and 28 days), your Notion decisions of the past week, queries with no yield, and suggested new queries. It goes out at most once a day, and a mail failure is logged as a warning without failing the run. To send one now: Actions → Job Hunt Pipeline → Run workflow → tick "Send the weekly report email now".

## Usage

```bash
# Full run: scrape, score, sync to Notion
bash run_local.sh

# Everything except the Notion upload
bash run_local.sh --dry-run

# Tests (offline)
.venv/bin/python -m pytest -q
```

Each run writes `data/new_jobs_<date>.json` (all scored jobs), `logs/run_<date>.log`, and one JSON line per run to `logs/feedback_run_<date>.jsonl` with the decisions loaded, stop-list size, query scoreboard and suggestions. A dry run on a fresh clone marks the jobs it saw as seen; delete `data/` before your first real run.

## License

MIT
