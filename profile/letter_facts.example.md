# Cover-letter facts (example)

The real file lives in `profile/letter_facts.md` (gitignored) and, on GitHub Actions,
in the `LETTER_FACTS` secret. `scripts/draft_letters.py` gives it to Claude as the only
facts a cover letter may state about you; where the fit profile disagrees, this file
wins. Without it, no letters are drafted.

## Letter frame
- Name: Erika Beispiel
- City: Bremen

## Facts
- Each role, project and degree with exact dates, the grade, and what you did, in the
  words you would sign. No numbers you would not defend in an interview.
- e.g. Operations analyst, Beispiel Logistik GmbH, 2023 to 2026: built the weekly
  capacity forecast the branch managers plan with; Python, SQL, Excel.

## Never write
- Things that must stay out of every letter: an amount, a title you never held, a story
  you can't talk about. Put an exact phrase in backticks and every draft is checked for
  it, e.g. `Senior`, `Head of`.
- Your motive for the job: the letter leaves "[Warum <Company>, in Ihren eigenen Worten.]"
  (German) or "[Why <Company>, in your own words.]" (English) for you.

## Your base letter (German and/or English)
- Your own base letter, for tone and order. Without one, the draft opens with what you
  would bring, then your most relevant work, then your education, then the start date.
