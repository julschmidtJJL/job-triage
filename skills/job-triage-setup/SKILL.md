---
name: job-triage-setup
description: Set up job-triage for a new user — install it, interview them for their fit profile and search queries, adapt the location, set up the board (Notion or a plain spreadsheet), produce a first scored list locally, and optionally set up cover-letter drafts. Use when someone asks to set up, install or configure job-triage.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
---

# Set up job-triage

You are setting up a daily pipeline that scores job postings against a
written profile of the user and puts the best ones on a board: a Notion
database, or a CSV file they open in any spreadsheet app. The
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
4. **Nothing personal goes into git.** `profile/profile.md`,
   `profile/letter_facts.md`, `.env`, `board/`, `data/` and `logs/` are
   gitignored. Do not commit or push anything without asking.
5. **It never applies anywhere.** Do not add code that contacts employers,
   submits forms, sends a letter, or logs into job sites. Do not add a
   scraper or ad fetcher for a site whose terms forbid it (LinkedIn among
   them).

Tell the user up front what it needs, so they can stop early: an **Anthropic
API key** and a job search in **Germany** (the Arbeitsagentur API covers all
of Germany; the other job boards are Hamburg-only). Notion is optional: the
board can be a plain spreadsheet file. GitHub Actions, Google Jobs and the
Monday email are optional too.

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
- **What counts as a fit.** Optional but worth it: three to five questions
  they would ask about any offer (what would I learn, what would I own, who
  would I work with, is the pay floor met). When the section exists, the
  scorer weighs a role's answers to these above its title. Use their
  questions and their words, not the example's.
- **Situations that fit.** Push for situations, not just titles: company
  size, stage, who they'd report to, what problem they'd own.
- **Role shapes.** Three to six, ranked.
- **Anti-signals.** What to score down, and why. Ask about seniority,
  internships, sales-heavy roles, prestige employers, travel or a car.
- **Hard constraints.** Location and commute, languages, employment type,
  hours, salary floor. Ask how many years they have in each field they
  have worked in, and which tools or platforms they won't work with: the
  scorer compares the years an ad asks for with the profile's, and treats a
  tool the profile doesn't list as learnable unless the profile rules it out.
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
console.anthropic.com), and the Notion values later, only if they choose a
Notion board in step 7. `SERPAPI_KEY` is optional; without it Google Jobs is
skipped.

## 6. First dry run

```bash
bash run_local.sh --dry-run
```

Show the user the top ten from `data/new_jobs_<date>.json`: title, company,
score, the one-line reason, and any `unmet_requirements` (a job with one is
capped at 50). Ask whether the ranking looks right. If it doesn't, the fix is
almost always the profile (step 2), not the code. Iterate until they're happy.
If a requirement is listed as unmet that they do meet, the profile doesn't
say so yet: add it there.

`fetch_method` says whether a job was scored on its full ad (`full_jd`) or
only the search snippet. If a source they rely on comes back as snippets,
its full ad needs a fetcher: see "Full ads, per source" in the README.

Then reset the state, because a dry run marks the jobs it saw as seen:

```bash
rm -rf data/
```

## 7. The board: Notion or a plain spreadsheet?

Ask: **"Notion or a plain spreadsheet?"** Explain the difference in two
lines, then follow 7a or 7b.

- **Notion:** a database with a page per job (the assessment and the full ad
  on the page), Decision as a dropdown, usable from the phone. Needs a free
  Notion account and an integration token.
- **Spreadsheet:** `board/jobs.csv` in the repository folder, opened in
  Excel, Numbers or LibreOffice; the full ad of each job is a Markdown file
  in `board/ads/`. No account. On GitHub Actions the board lives in their
  (private) repository, so they pull to see new jobs and push their verdicts.

### 7a. Notion

The user creates an internal integration at notion.so/my-integrations
(they copy its token into `.env` as `NOTION_API_KEY` themselves).

Create the database with the properties listed under "Notion setup" in the
README. The seven properties read from the full ad can be left out: the
first sync adds them. If this session has a Notion connector, offer to create it there
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

### 7b. Spreadsheet

`.env` needs `BOARD=csv` instead of `BOARD=notion`. The user changes the
line, or you change it without printing the file:

```bash
sed -i.bak 's/^BOARD=.*/BOARD=csv/' .env && rm .env.bak
grep -q '^BOARD=csv' .env && echo "BOARD=csv set"
```

The `NOTION_` lines can stay as they are; nothing reads them. Then the first
real run:

```bash
bash run_local.sh
```

It creates `board/jobs.csv`, one row per job, and `board/ads/<Key>.md` with
the assessment, the requirements and the full ad. Explain how to use it:

- Open `board/jobs.csv`, type a Decision (New, Interesting, Maybe, Apply,
  Applied, Not interesting, Declined) and save it as CSV, keeping the format
  when the app asks.
- Each run only appends new jobs; their Decisions, extra columns and column
  order are kept. The run fills in an empty Decided with the date.
- Close the file before a run: Excel on Windows locks it, and the new jobs
  then wait in the holdover queue until the next run.

Ask them to mark a few rows and save: that's how it learns.

## 8. Cover-letter drafts (optional)

Only if they want them. A row they mark **Apply** then gets a draft letter,
from the full ad stored with the row and a facts file they control: a child
page of the row on Notion, `board/letters/<Key>.md` on the spreadsheet
board (named after the row's Key; an existing file is never overwritten).

```bash
cp profile/letter_facts.example.md profile/letter_facts.md
```

Interview them for it the way you did for the profile, and read each
section back before you write it:

- **Letter frame.** The name to sign with and the city for the date line.
- **Facts.** Each role, project and degree with exact dates and what they
  did, in words they would sign. This is the only thing a letter may say
  about them, so nothing goes in that they would not defend in an
  interview. You may draft from their CV; do not add anything it doesn't say.
- **Never write.** Anything that must stay out of every letter. Exact
  phrases go in backticks; every draft is checked for them.
- **Base letter.** If they have one, paste it in for tone and order.

Then mark one row Apply on the board and run:

```bash
.venv/bin/python scripts/draft_letters.py --dry-run
```

Show them the letter and the callout above it (odds, what it covers, what to
check). Explain that the opening sentence on why they want the job and the
start date stay placeholders for them. When they're happy, run it without
`--dry-run` to write the letter to the board.

## 9. Daily on GitHub Actions (optional)

Only if they want it. They need their own copy of the repository on GitHub,
as a **private** repository: Actions logs of a public repository are public,
and they name the jobs on the board. Give them the commands; they run them:

```bash
gh secret set ANTHROPIC_API_KEY
gh secret set FIT_PROFILE < profile/profile.md
gh variable set JOBHUNT_ENABLED --body true
```

For a Notion board:

```bash
gh secret set NOTION_API_KEY
gh secret set NOTION_JOB_BOARD_DB_ID
gh secret set NOTION_DATA_SOURCE_ID
```

For the spreadsheet board:

```bash
gh variable set BOARD --body csv
```

Each run then commits `board/` to their repository (the workflow refuses if
the repository is public). Tell them to `git pull` to see new jobs and to
commit and push `board/jobs.csv` after they type their Decisions. If they
already have a local board from step 7b, they push it once first; `board/`
is gitignored, so it needs `-f`:

```bash
git add -f board && git commit -m "Job board" && git push
```

Optional: `SERPAPI_KEY`, and `MAIL_USER`, `MAIL_APP_PASSWORD`, `MAIL_TO` for
the Monday email (a Gmail address with an app password). For cover letters
on every run (step 8):

```bash
gh secret set LETTER_FACTS < profile/letter_facts.md
gh variable set LETTERS_ENABLED --body true
```
 Start one run by
hand from the Actions tab and check the board afterwards, not just the green
tick.

## Done

Summarise what was set up, where the profile, the letter facts and the
board live, how to change queries, and that the stop-list stays in shadow
mode for 14 days.
