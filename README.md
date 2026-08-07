# free-exercise-db — guarded mirror

A controlled copy of
[yuhonas/free-exercise-db](https://github.com/yuhonas/free-exercise-db):
873 exercises with instructions, muscle groups, equipment and demonstration
photographs, released into the public domain under
[the Unlicense](LICENSE.md).

**All credit for the data belongs upstream.** Nothing here is edited by hand;
this repository exists to hold a copy that cannot be taken away, not to fork
the project. If you are looking for the dataset itself, go upstream — they
maintain it.

## Why this exists

It is the source of truth for [FitQuest](https://github.com/crazynudelsieb/fitquest),
which builds its exercise catalog from `dist/exercises.json` and serves the
images in `exercises/` from its own instance. An app that plans people's
training on top of a dataset should not be one upstream force-push away from
having no dataset.

A plain mirror does not give you that. `git pull upstream main` faithfully
reproduces whatever happened upstream — including a dataset emptied by a bad
build, half the catalog dropped by a refactor, or the repository disappearing.
So the sync here is **guarded**.

## What the guard does

`scripts/guarded_sync.py` runs weekly ([workflow](.github/workflows/sync-upstream.yml)),
compares what upstream is offering against the copy already here, and applies
it **only if the change is not destructive**:

| | |
|---|---|
| Additions and edits | applied, however many |
| Removals | applied up to **5 %** of the catalog |
| Catalog size | must stay at or above **700** exercises |
| Malformed records | any record missing `id` or `name` refuses the whole sync |
| Upstream unreachable, deleted, or serving non-JSON | refused |
| Images upstream stopped listing | **kept** — see below |

On a refusal, **nothing is written**: the mirror keeps serving exactly what it
served yesterday, an issue is opened with the full report, and the workflow run
fails so the owner is emailed. A refusal is never silent and never partial.

Thresholds live at the top of `scripts/guarded_sync.py` and can be overridden
per run with `MAX_REMOVED_FRACTION`, `MAX_CHANGED_FRACTION` and
`MIN_EXERCISE_COUNT`. If upstream genuinely did mean it, re-run the workflow
from the Actions tab with **force** ticked — after reading the report.

### Images are never deleted

The photographs are the part of this dataset that cannot be reconstructed if
they go, they cost nothing to keep, and a file upstream dropped is precisely
the file a mirror exists to still have. The sync downloads anything new and
leaves everything else alone, so `exercises/` only ever grows. Orphans are
counted in the report.

### Everything is recoverable anyway

Every accepted sync is one commit and one tag (`upstream-YYYYMMDD-HHMM`), so
any previous state of the data is a `git checkout` away, whatever upstream does
afterwards.

## Layout

```
dist/exercises.json     the dataset, upstream's own format and formatting
exercises/<Id>/<n>.jpg  demonstration photographs, ~1,750 files
schema.json             upstream's JSON schema for a record
UPSTREAM_STATE.json     which upstream commit this copy is at, and when
scripts/guarded_sync.py the guard
```

## Using it

```bash
# the dataset
curl -sSL https://raw.githubusercontent.com/crazynudelsieb/free-exercise-db-mirror/main/dist/exercises.json

# one image
curl -sSL https://raw.githubusercontent.com/crazynudelsieb/free-exercise-db-mirror/main/exercises/Barbell_Squat/0.jpg
```

Or run the guard yourself against any checkout:

```bash
python scripts/guarded_sync.py --dry-run   # what would change, and whether it would be allowed
```

## Related

- **Upstream:** [yuhonas/free-exercise-db](https://github.com/yuhonas/free-exercise-db) — the actual project
- **Plain fork:** [crazynudelsieb/free-exercise-db](https://github.com/crazynudelsieb/free-exercise-db) — tracks upstream unconditionally, no guard
- **Consumer:** [crazynudelsieb/fitquest](https://github.com/crazynudelsieb/fitquest)

## Licence

The data is upstream's, under [the Unlicense](LICENSE.md) — public domain, no
conditions. The sync tooling in `scripts/` is offered on the same terms.
