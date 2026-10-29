# job-triage

A daily pipeline that reads job boards so you don't have to, scores every posting's full ad against a written profile of you, and puts the few worth a human look on a Notion board. Your verdicts on that board teach the next run, and a job you mark Apply gets a draft cover letter.

**It doesn't apply anywhere.** It sends nothing to employers and fills in no forms. It decides what deserves your attention, shows its reasoning, and measures whether it was right. A cover letter stays a draft on your board until you rewrite and send it yourself.

It runs on GitHub Actions once a day. No server, no database.

It is extracted from the private pipeline that has run my own job search every day since 14 October 2026. That pipeline grew out of an earlier version I built in May and June 2026 for a family member's search. The history stays private because it holds personal profile notes.

## Quick start

Paste this into Claude Code. It works the same in Codex or Cursor.

```
Set up job-triage for me. Clone https://github.com/julschmidtJJL/job-triage,
then read skills/job-triage-setup/SKILL.md in it and follow it step by step.
Interview me for my profile, what counts as a fit for me and the roles I'm
after, read every profile section back to me before you write it, and never
handle my API keys yourself. Stay inside the repository and do not change
any global agent configuration.
```

The agent installs the dependencies into a `.venv/` in the repository and interviews you for your fit profile: what you have built, the questions that decide whether a role fits you, which situations fit you, your anti-signals and hard constraints. It writes the search queries with you, adapts the location if you are not in Hamburg, walks you through the Notion board, and runs a dry run so you see your first scored list before anything goes to Notion. Cover-letter drafts and the scheduled run on GitHub Actions come last, and only if you want them. [`skills/job-triage-setup/SKILL.md`](skills/job-triage-setup/SKILL.md) is the whole procedure, if you would rather do it by hand.

You need an Anthropic API key and a free Notion account. Google Jobs and the Monday email are optional.

## What makes it more than a scraper

**It measures its own search.** Every job remembers which query found it. Every run prints a scoreboard per query: found, new, passed the prefilter, average score, and how many you marked Interesting or Apply. On Mondays it emails you the scoreboard and suggests new queries from titles you liked. It never edits your queries itself; that decision stays yours. In the first week, the scoreboard showed that three of the original German search terms averaged under 30 out of 100 and produced nothing worth applying to.

**It learns from your verdicts, carefully.** Your Notion decisions become a feature index: title words and companies you liked get a boost, the ones you rejected get a penalty. A word rejected three times or more is promoted to a stop-list, but:

- **Shadow mode first.** For 14 days the stop-list only logs what it *would* drop, so you can audit it before it acts.
- **Generic words can't be learned.** In the first week the loop tried to learn "manager", "data" and "engineer" from rejections, which would have dropped 94 postings. Search vocabulary and generic title words are now excluded and pruned on load.
- **Every drop is visible and reversible.** Dropped rows go to the board as Auto-Dropped with the reason. Set one back to Interesting and its word is rescued for good.
- **"Maybe" is neutral on purpose.** It takes a row out of your queue without teaching the loop anything, so lukewarm jobs neither multiply nor vanish.

**It reads the whole ad, not the search snippet.** Each job's full ad is fetched once and stored on its Notion page, so the board shows exactly what the scorer read. Claude lists the ad's hard requirements and the ones your profile shows you don't meet:

- **An unmet must-have caps the score at 50.** The job still reaches the board, flagged, but not near the top, unless the ad says nobody needs to tick every box.
- **A missing tool is a tool gap, not a gap in you.** Azure, SAP or Power BI that your profile doesn't list is learnable on the job: it's shown, costs at most one point, and never caps the score.
- **Required years are compared in code.** Claude reads the years the ad asks for and the years your profile shows in that field; the comparison is done in Python, so the same ad and profile give the same answer every run.

**It scores what the role would give you, not its title.** The profile can say in a few questions what counts as a fit for you (what would I learn, what would I own, who would I work with). Claude scores mainly on how a role answers them, so a well-placed role with a plain title can beat a grand title six reporting layers down.

**It drafts the letter, and tells you the odds.** For every row you mark Apply, it writes a base cover letter in the ad's language as a child page of the row, from a facts file you control and nothing else. A callout above the letter gives your realistic odds, which of the ad's requirements the letter covers and which it doesn't, and the sentences to check. Your motive and the start date stay placeholders: those are yours to write.

**It shows its reasoning.** Each scored job carries a one-line reason and a one-line *situation fit* from Claude, the matched skills, the requirements it found, and every feedback adjustment that moved its score.

## How it works

```
queries.json ──► scrapers ──► location + aggregator filter ──► dedup (seen URLs, reposts)
                                                                    │
       Notion decisions ──► stop-list + feature index ──────────────┤
                                                                    ▼
                                        Tier 1: role-term prefilter + feedback deltas
                                                                    │
                                        fetch the full ad (per-source fetcher)
                                                                    │
                                        Tier 2: Claude scores the ad against the fit profile
                                                (hard requirements, years, tool gaps)
                                                                    │
              score ≥ 40, top N ──► Notion board        rest ──► holdover queue
                                         │                          │
            Decision = Apply ──► draft cover letter    query scoreboard + run summary
```

**Two layers, kept apart on purpose.**

- **The profile** (`profile/profile.md`, gitignored; the `FIT_PROFILE` secret on Actions) says who you are, what counts as a fit and which situations fit. A company's situation (no AI yet, first hire for the job, close to management) can count for more than its sector, and the role's answers to your fit questions count for more than its title. Claude reads it for every batch of postings. `profile/profile.example.md` shows the shape.
- **The queries** (`profile/queries.json`) are the plain search strings job boards need, and they are the experiment. The committed file is the author's own search for AI roles in Hamburg; the setup replaces it with yours.

**State without a database.** Everything the pipeline learns lives in `data/` (seen URLs, stop-list, holdover queue, decision cache, query stats). On Actions, `data/` is restored from and saved to the Actions cache with a per-run key and a prefix fallback, so each run starts from the last run's state. A run that couldn't score or deliver saves nothing, so its jobs are retried the next day.

## Sources

| Scraper | What it searches |
|---|---|
| Arbeitsagentur | The Bundesagentur für Arbeit job search API: official, free, and strongest for Mittelstand employers. Temp-work and placement agencies are excluded. Works for any German city. The full ad comes from the API; when it lives at a cooperation partner (jobexport.de, finest-jobs.com...) and the API text is short, the partner page the API links to is read. |
| Google Jobs (ScaleSerp) | `site:` queries against German job boards; optional, needs `SERPAPI_KEY`. Scored on the search snippet, not the full ad |
| Hamburg Karriere | The city of Hamburg's job API |
| Good Jobs, Green Jobs, Digital Rapids | Hamburg boards |

It is tuned for Hamburg; the setup skill adapts it to another city. LinkedIn isn't included: its terms of service don't allow scraping.

**Full ads, per source.** `FETCHERS` in `utils/pipeline.py` says how each source's full ad is read: by default the job page itself (schema.org `JobPosting` data if the page has it, else its main content), the API for the Arbeitsagentur, or nothing but the snippet. A new scraper only needs an entry there, a `"module:function"` that takes the job URL and returns the ad text, or `""` when it can't. A job whose ad couldn't be read is scored on its snippet and its page says so.

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
| `LETTER_FACTS` | The only facts a cover letter may state; overrides `profile/letter_facts.md` (gitignored, shape in `profile/letter_facts.example.md`) |
| `LETTER_MODEL` | Model for cover letters; default `claude-sonnet-5-5` |
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
| Contact Person, Hard Requirements, Unmet Requirements | Text | read from the full ad |
| Ad Language, Seniority | Select | read from the full ad |
| Required Years | Number | read from the full ad |
| Requirements Flexible | Checkbox | the ad says not every listed requirement is a must |

You can leave out the last seven: the first sync that misses them adds them to the data source (rows still sync without them if the integration may not change the schema). Each page's body holds the AI assessment, the requirements, and the full job ad; a row whose ad couldn't be fetched is headed "Job Ad (search snippet only)".

Create an internal integration at notion.so/my-integrations, connect it to the database (••• → Connections), and use its token as `NOTION_API_KEY`.

### GitHub Actions

Set the same variables as repository secrets, for example:

```bash
gh secret set FIT_PROFILE < profile/profile.md
```

Then set the repository variable `JOBHUNT_ENABLED` to `true`. Until you do, the daily schedule (05:17 UTC) skips itself, so a fresh fork doesn't fail every morning. A manual run from the Actions tab always starts; tick "Dry run" for one that uploads nothing and saves no state.

Actions logs of a public repository are public, and they name the jobs on your board. Run your own copy as a private repository.

### Cover letters

`scripts/draft_letters.py` drafts a base cover letter for each row with Decision **Apply** that has no "Anschreiben" child page yet. It reads the full ad stored on the row (the Requirements and Job Ad blocks, Hard and Unmet Requirements, Contact Person), writes in the row's Ad Language against the fit profile and your letter facts, and adds the letter as a child page "Anschreiben – <Company>". Any child page whose title starts with "Anschreiben" counts, so a letter you wrote yourself is never overwritten. A row with only a search snippet is skipped.

The letter facts (`profile/letter_facts.md`, shape in `profile/letter_facts.example.md`) are the only facts a letter may state about you. Their "Letter frame" gives the name and city for the signature and date line, and any phrase you put in backticks under "Never write" is checked in every draft. The letter opens with `[Warum <Company>, in Ihren eigenen Worten.]` (or `[Why <Company>, in your own words.]`) and leaves the start date as `[Datum]` (`[Start date]`): both stay yours. The company website is not checked.

```bash
cp profile/letter_facts.example.md profile/letter_facts.md   # then write your own
.venv/bin/python scripts/draft_letters.py --dry-run           # print the letters, write nothing
.venv/bin/python scripts/draft_letters.py                     # write them to the board
```

On Actions it is opt-in: set the `LETTER_FACTS` secret and the repository variable `LETTERS_ENABLED` to `true`, and every run drafts letters after the pipeline. "Only draft cover letters" in the Run workflow form does just that, without scraping.

```bash
gh secret set LETTER_FACTS < profile/letter_facts.md
gh variable set LETTERS_ENABLED --body true
```

### The Monday report

Every Monday the run emails the query scoreboard (last 7 and 28 days), your Notion decisions of the past week, queries with no yield, and suggested new queries. It goes out at most once a day, and a mail failure is logged as a warning without failing the run. To send one now: Actions → Job Hunt Pipeline → Run workflow → tick "Send the weekly report email now".

## Usage

```bash
# Full run: scrape, score, sync to Notion
bash run_local.sh

# Everything except the Notion upload
bash run_local.sh --dry-run

# Cover letters for the Apply rows (print only)
.venv/bin/python scripts/draft_letters.py --dry-run

# Tests (offline)
.venv/bin/python -m pytest -q
```

Each run writes `data/new_jobs_<date>.json` (all scored jobs, with their full ads and requirements), `data/letters_<date>.json` when it drafted letters, `logs/run_<date>.log`, and one JSON line per run to `logs/feedback_run_<date>.jsonl` with the decisions loaded, stop-list size, query scoreboard and suggestions. A dry run on a fresh clone marks the jobs it saw as seen; delete `data/` before your first real run.

## License

MIT
