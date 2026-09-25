# Branching model

| Branch | What it is for | Deploys to |
|---|---|---|
| `main` | Production code. Changes arrive only as reviewed merges from `develop`. | prod (set up in a later module) |
| `develop` | Integration and testing. Every feature lands here first. | dev (set up in a later module) |
| `feature/NN-name` | One branch per module, always cut from `develop`. | nothing |
| `hotfix/name` | Urgent production fix, cut from `main`, merged back into both `main` and `develop`. | prod |

Every module follows the same path. A feature branch is created from `develop`, the work goes in, and a pull request is opened into `develop`. CI has to pass and a reviewer has to approve before the merge. Once `develop` has been exercised in the dev environment, a separate pull request takes `develop` into `main`, which is what reaches production.

Nothing is ever pushed straight to `main` or `develop`.

## Planned feature branches

1. `feature/00-foundation`
2. `feature/01-telegram-bot`
3. `feature/02-job-sweep`
4. `feature/03-screening`
5. `feature/04-resume-builder`
6. `feature/05-contacts`
7. `feature/06-gmail-drafts`
8. `feature/07-tracking`
9. `feature/08-deploy`

## How to start a new module

- [ ] `git fetch origin && git checkout develop && git pull origin develop`
- [ ] `git checkout -b feature/NN-name`
- [ ] Build only what the module spec lists
- [ ] Route every Notion write, email, paid API call and model choice through `src/jobengine/safety.py`
- [ ] Add tests for the new behaviour
- [ ] Run `ruff check .`, `pytest` and `APP_ENV=local python -m jobengine.main` until all pass
- [ ] Push the branch and open a pull request into `develop`, filling in the template checklist
- [ ] Wait for green CI and a review before merging

## Hotfixes

- [ ] `git checkout -b hotfix/name origin/main`
- [ ] Fix, test, open a pull request into `main`
- [ ] After it merges, open a second pull request (or merge) bringing the fix into `develop`
