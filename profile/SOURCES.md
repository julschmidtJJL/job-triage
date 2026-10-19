# Where a good profile comes from

`profile/profile.md` is the one input that decides how good the scores are.
It's worth an hour. Material that helps, none of which belongs in this
repository:

| Source | What it contributes |
|---|---|
| Your CV and LinkedIn profile | What you have built, with dates and evidence |
| Past applications and their outcomes | What to stop surfacing, and which situations got replies |
| Notes on what you want next | Situations, role shapes, anti-signals, constraints |
| A conversation with an agent | The setup skill (`skills/job-triage-setup/SKILL.md`) interviews you section by section |

Update the profile whenever the board shows you the wrong things. On GitHub
Actions, refresh the secret afterwards:

```bash
gh secret set FIT_PROFILE < profile/profile.md
```
