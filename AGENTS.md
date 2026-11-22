# Notes for AI agents

## Git workflow

Commit to the default branch and push; every commit should leave this
checkout. A branch is fine only for a day or for one open pull request:
merge or delete it when done, and delete it when its PR merges. No
long-lived `ai/*`, `codex/*` or topic branches, and live jobs (cron,
services, benchmarks) run from the default branch. Without write access,
push to your own fork and open a PR. Full policy: homelab
`agents/GIT_WORKFLOW.md`.
