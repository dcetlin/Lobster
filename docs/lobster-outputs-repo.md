# lobster-outputs: Human-Readable Job Output Repo

## What it is

`dcetlin/lobster-outputs` is a private GitHub repo that receives human-readable outputs from Lobster scheduled jobs. It exists so Dan can get real GitHub links to browse sweep docs, digests, and hygiene reports without exposing sensitive runtime data (memory.db, events.jsonl, logs).

Repo: https://github.com/dcetlin/lobster-outputs

## Why it exists

The VPS runtime directory (`~/lobster-workspace/`) contains sensitive files that cannot be committed to any repo. But scheduled job outputs — sweep markdown files, morning briefings, weekly retros — are human-readable and useful to browse on GitHub. This repo is the bridge: structured, browsable, private.

## Directory layout

```
lobster-outputs/
  hygiene/      # Negentropic sweep outputs (YYYY-MM-DD-sweep.md)
  digests/      # Daily deep-work morning briefings (YYYY-MM-DD-deep-work.md)
  .gitignore    # Excludes *.db, *.jsonl, logs/
```

## Which jobs push here

| Job | Output path in repo | When |
|-----|---------------------|------|
| `negentropic-sweep` | `hygiene/YYYY-MM-DD-sweep.md` | Nightly at 02:00 UTC |
| `async-deep-work` | `digests/YYYY-MM-DD-deep-work.md` | Nightly at 05:00 UTC |

Each job:
1. Writes output to `~/lobster-workspace/` (primary location, unchanged)
2. Copies it to `~/lobster-workspace/projects/lobster-outputs/<dir>/`
3. Commits and pushes to `dcetlin/lobster-outputs`
4. Includes the GitHub URL in its `write_task_output` call

## Push pattern

The repo is cloned to `~/lobster-workspace/projects/lobster-outputs/` and uses the `gh` CLI credential helper for auth:

```
git config credential.helper "/usr/bin/gh auth git-credential"
```

This means no hardcoded tokens — auth rotates automatically with the gh PAT.

The push sequence in each task file looks like:

```bash
OUTPUTS_DIR=~/lobster-workspace/projects/lobster-outputs
cp <output-file> ${OUTPUTS_DIR}/<subdir>/<filename>
cd ${OUTPUTS_DIR}
git add <subdir>/<filename>
git commit -m "<subdir>: <filename>"
git push origin main
```

## What never goes in this repo

- `*.db` files (memory.db, etc.)
- `*.jsonl` files (events.jsonl, etc.)
- Log files
- Any credential or config files

The `.gitignore` enforces this, and these files are never written to `~/lobster-workspace/projects/lobster-outputs/` by any job.
