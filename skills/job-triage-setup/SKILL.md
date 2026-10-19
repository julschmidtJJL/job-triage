---
name: job-triage-setup
description: Set up job-triage for a new user — install it, interview them for their fit profile and search queries, adapt the location, connect a Notion board, and produce a first scored list locally. Use when someone asks to set up, install or configure job-triage.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
---

# Set up job-triage

You are setting up a daily pipeline that scores job postings against a
written profile of the user and puts the best ones on a Notion board. The
profile is the product: a vague profile gives vague scores. Read the rules,
then work through the steps in order. Stop and ask whenever a step needs a
decision only the user can make.

## Rules

1. **The profile is the user's, in the user's words.** You may draft a
   section from material the user gives you (a CV, a LinkedIn export, notes
   they paste), but read every section back and write it only when they
   confirm it. Do not invent achievements, numbers, skills or preferences.
   Constraints (salary floor, location, hours, languages) are asked, never
   assumed.
2. **Touch only this repository.** Do not install anything globally, do not
   edit any CLAUDE.md, shell profile or agent config. Python packages go into
   `.venv/` inside the repository.
3. **Never handle a secret.** API keys and tokens are typed by the user into
   `.env` or into `gh secret set`, which you do not read. Give them the
   command; do not ask them to paste a key into the chat, and never print
   `.env`.
4. **Nothing personal goes into git.** `profile/profile.md`, `.env`, `data/`
   and `logs/` are gitignored. Do not commit or push anything without asking.
5. **It never applies anywhere.** Do not add code that contacts employers,
   submits forms, or logs into job sites. Do not add a scraper for a site
   whose terms forbid it (LinkedIn among them).

Tell the user up front what it needs, so they can stop early: an **Anthropic
API key**, a free **Notion** account, and a job search in **Germany** (the
Arbeitsagentur API covers all of Germany; the other boards are Hamburg-only).
GitHub Actions, Google Jobs and the Monday email are optional.

## 1. Install

If you are not already inside a clone of this repository, ask the user where
to put it (default: the current directory) and clone it there.

```bash
cd job-triage
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

All tests run offline. If any fail, stop and report.

## 2. The fit profile: `profile/profile.md`

```bash
cp profile/profile.example.md profile/profile.md
```

Interview the user section by section, following the headings of the
example. Ask one section at a time:

- **What I have built and shipped.** Concrete things with evidence: what it
  did, for whom, on what. Ask for links.
- **How I work / strengths.** What people come to them for.
- **Situations that fit.** Push for situations, not just titles: company
  size, stage, who they'd report to, what problem they'd own.
- **Role shapes.** Three to six, ranked.
- **Anti-signals.** What to score down, and why. Ask about seniority,
  internships, sales-heavy roles, prestige employers, travel or a car.
- **Hard constraints.** Location and commute, languages, employment type,
  hours, salary floor.
- **Already applied to / rejected by.** Optional, but it stops the pipeline
  from re-surfacing them.

Read the whole file back at the end and ask for corrections.

## 3. The queries: `profile/queries.json`

The committed file is the author's search (AI roles, Hamburg, German and
English terms). Replace it with the user's:

- Ask for the job titles they would search for themselves, in every language
  employers in their area use. Six to twelve queries is a good start.
- Set `role_terms` to words a relevant posting must contain somewhere in its
  title or description (the Tier-1 prefilter). Keep it broad: it only
  removes postings that are clearly off-topic.
- Explain that the queries are the experiment: the scoreboard will show
  which ones earn their place, and they can toggle `active` later.

## 4. Location

The pipeline is tuned for Hamburg. Ask where they want to work.

- **Hamburg:** nothing to change.
- **Elsewhere in Germany:** in `main.py`, change the `location=` arguments in
  `_scrape_all` (Arbeitsagentur and Google Jobs take any German city), replace
  `HAMBURG_AREA` with the user's city and the towns they can commute from, and
  remove the Hamburg-only scrapers (Hamburg Karriere, Good Jobs, Green Jobs,
  Digital Rapids) from the list. Rename `is_hamburg_location` only if they ask.
  Run the tests again and fix any test that hard-codes Hamburg.
- **Outside Germany:** say plainly that only Google Jobs would still work and
  that this needs new scrapers; ask before writing any.

## 5. Keys: `.env`

```bash
cp .env.example .env
```

Tell the user which lines to fill in themselves: `ANTHROPIC_API_KEY` (from
console.anthropic.com), and for later, the Notion values. `SERPAPI_KEY` is
optional; without it Google Jobs is skipped.

## 6. First dry run

```bash
bash run_local.sh --dry-run
```

Show the user the top ten from `data/new_jobs_<date>.json`: title, company,
score, and the one-line reason. Ask whether the ranking looks right. If it
doesn't, the fix is almost always the profile (step 2), not the code. Iterate
until they're happy.

Then reset the state, because a dry run marks the jobs it saw as seen:

```bash
rm -rf data/
```

## 7. The Notion board

The user creates an internal integration at notion.so/my-integrations
(they copy its token into `.env` as `NOTION_API_KEY` themselves).

Create the database with the properties listed under "Notion setup" in the
README. If this session has a Notion connector, offer to create it there
and confirm the parent page with the user first; otherwise walk them through
it by hand. Then they connect the integration to the database (••• →
Connections) and put the database ID in `.env` as `NOTION_JOB_BOARD_DB_ID`.

```bash
.venv/bin/python scripts/get_notion_data_source_id.py
```

The user copies the printed ID into `.env` as `NOTION_DATA_SOURCE_ID`. Then
the first real run:

```bash
bash run_local.sh
```

Ask them to open the board and mark a few rows: that's how it learns.

## 8. Daily on GitHub Actions (optional)

Only if they want it. They need their own copy of the repository on GitHub
(a fork or a new private repo). Give them the commands; they run them:

```bash
gh secret set ANTHROPIC_API_KEY
gh secret set NOTION_API_KEY
gh secret set NOTION_JOB_BOARD_DB_ID
gh secret set NOTION_DATA_SOURCE_ID
gh secret set FIT_PROFILE < profile/profile.md
gh variable set JOBHUNT_ENABLED --body true
```

Optional: `SERPAPI_KEY`, and `MAIL_USER`, `MAIL_APP_PASSWORD`, `MAIL_TO` for
the Monday email (a Gmail address with an app password). Start one run by
hand from the Actions tab and check the board afterwards, not just the green
tick.

## Done

Summarise what was set up, where the profile lives, how to change queries,
and that the stop-list stays in shadow mode for 14 days.
