#!/usr/bin/env python
"""Sync this mirror from upstream — but only when the change looks sane.

    python scripts/guarded_sync.py              # fetch, check, apply if safe
    python scripts/guarded_sync.py --dry-run    # report and change nothing
    python scripts/guarded_sync.py --force      # apply even if the guard trips
    python scripts/guarded_sync.py --report-file report.md

A plain mirror is not a backup. `git pull upstream main` faithfully reproduces
whatever happened upstream, including the things a backup exists to survive:
the dataset emptied by a bad build, half the catalog dropped by a refactor, the
repository deleted outright. This script is the difference between the two — it
compares what upstream is offering against the copy already here and **refuses
to apply a change that destroys data**, leaving the last good copy in place and
raising the alarm instead.

What it will do on its own:

  * take additions and edits, however many
  * take removals, up to `MAX_REMOVED_FRACTION` of the catalog
  * download any image the data file references and this mirror lacks

What it will never do on its own:

  * apply anything if upstream is unreachable, unparseable, or has shrunk past
    the floors below
  * **delete an image file.** Images are the part of this dataset that cannot
    be reconstructed, they cost nothing to keep, and a file upstream dropped is
    exactly the file a mirror exists to still have. They are reported as
    retained and left alone.

Nothing is lost even when a change *is* accepted: every accepted sync is a
commit, and the workflow tags it, so any previous state of the data file is a
`git checkout` away.

Exit codes: 0 applied or already current · 1 guard tripped · 2 upstream
unusable. The workflow turns 1 and 2 into an issue, and leaves the mirror as it
was.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The report is markdown destined for a GitHub issue, so it is UTF-8. A Windows
# console defaults to cp1252 and would raise on the first arrow rather than
# print the report it was asked for.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):  # already fine, or not a real stream
        pass

UPSTREAM = os.environ.get('UPSTREAM_REPO', 'yuhonas/free-exercise-db')
UPSTREAM_BRANCH = os.environ.get('UPSTREAM_BRANCH', 'main')
# Overridable so the refusal paths can be exercised against a local fixture,
# and so a move of the source (a rename, a different host) is a variable rather
# than a patch.
RAW_BASE = os.environ.get(
    'UPSTREAM_RAW_BASE',
    f'https://raw.githubusercontent.com/{UPSTREAM}/{UPSTREAM_BRANCH}/')
API_BASE = f'https://api.github.com/repos/{UPSTREAM}'

DATA_PATH = os.path.join(REPO, 'dist', 'exercises.json')
IMAGE_ROOT = os.path.join(REPO, 'exercises')
STATE_PATH = os.path.join(REPO, 'UPSTREAM_STATE.json')

# --- The guard -----------------------------------------------------------
#
# Thresholds are fractions of the catalog *already here*, not of the incoming
# one — the question is "how much of what I have would this destroy?", and
# measuring against the incoming file lets a file with three records in it
# claim that removing 870 is a 0 % change.
#
# The numbers are deliberately loose enough that ordinary upstream maintenance
# passes without a human, and tight enough that the failure modes this exists
# for cannot. A release that removes 40 exercises is unusual and worth a look;
# one that removes 400 is an accident or an attack.

MAX_REMOVED_FRACTION = float(os.environ.get('MAX_REMOVED_FRACTION', '0.05'))
MAX_CHANGED_FRACTION = float(os.environ.get('MAX_CHANGED_FRACTION', '0.25'))
# An absolute floor as well as a relative one: a mirror that has been eroded
# five percent at a time is still an eroded mirror.
MIN_EXERCISE_COUNT = int(os.environ.get('MIN_EXERCISE_COUNT', '700'))

MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_CONTENT_TYPES = ('image/jpeg', 'image/png', 'image/webp')
IMAGE_DELAY_SECONDS = 0.05

# A record without one of these is not a usable exercise, and a file full of
# them is not a dataset worth overwriting a good copy with.
REQUIRED_FIELDS = ('id', 'name')


class UpstreamUnusable(Exception):
    """Upstream could not be read, or what came back was not the dataset."""


# --- Fetching ------------------------------------------------------------

def _get(url: str, *, timeout: int = 60) -> bytes:
    headers = {'User-Agent': 'free-exercise-db-mirror'}
    token = os.environ.get('GITHUB_TOKEN', '')
    if token and url.startswith('https://api.github.com/'):
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def upstream_head() -> dict:
    """`{sha, date}` of upstream's branch head, or empty if the API is coy.

    Not load-bearing: it is recorded so a human can see *which* upstream state
    this mirror is at. The data itself is compared record by record, so a
    missing sha never decides anything.
    """
    try:
        payload = json.loads(_get(f'{API_BASE}/commits/{UPSTREAM_BRANCH}'))
    except Exception:  # noqa: BLE001 - provenance is nice to have, not required
        return {}
    return {'sha': payload.get('sha', ''),
            'date': (payload.get('commit') or {}).get('committer', {}).get('date', '')}


def fetch_upstream_records() -> list[dict]:
    """The upstream dataset, or `UpstreamUnusable` with the reason."""
    try:
        raw = _get(RAW_BASE + 'dist/exercises.json')
    except urllib.error.HTTPError as e:
        raise UpstreamUnusable(
            f'HTTP {e.code} fetching the dataset — upstream may have been '
            f'renamed, made private or deleted') from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpstreamUnusable(f'could not reach upstream: {e}') from e

    try:
        records = json.loads(raw)
    except ValueError as e:
        raise UpstreamUnusable(f'the dataset is not valid JSON: {e}') from e

    if not isinstance(records, list) or not records:
        raise UpstreamUnusable('the dataset is not a non-empty JSON array')
    for record in records:
        if not isinstance(record, dict):
            raise UpstreamUnusable('the dataset contains a non-object entry')
    return records


def load_local_records() -> list[dict]:
    if not os.path.exists(DATA_PATH):
        return []
    with open(DATA_PATH, encoding='utf-8') as fh:
        return json.load(fh)


# --- Comparison ----------------------------------------------------------

def key_of(record: dict) -> str:
    return str(record.get('id') or record.get('name') or '')


def compare(old: list[dict], new: list[dict]) -> dict:
    """What applying `new` over `old` would do."""
    old_by_key = {key_of(r): r for r in old if key_of(r)}
    new_by_key = {key_of(r): r for r in new if key_of(r)}

    removed = sorted(set(old_by_key) - set(new_by_key))
    added = sorted(set(new_by_key) - set(old_by_key))
    changed = sorted(key for key in set(old_by_key) & set(new_by_key)
                     if old_by_key[key] != new_by_key[key])

    incomplete = sorted(key for key, record in new_by_key.items()
                        if any(not record.get(field) for field in REQUIRED_FIELDS))

    old_images = {p for r in old for p in (r.get('images') or [])}
    new_images = {p for r in new for p in (r.get('images') or [])}

    return {
        'old_count': len(old_by_key),
        'new_count': len(new_by_key),
        'removed': removed,
        'added': added,
        'changed': changed,
        'incomplete': incomplete,
        'images_dropped': sorted(old_images - new_images),
        'images_new': sorted(new_images - old_images),
    }


def evaluate(diff: dict) -> list[str]:
    """Reasons to refuse. Empty means the change is safe to apply."""
    reasons = []
    baseline = diff['old_count']

    if diff['new_count'] < MIN_EXERCISE_COUNT:
        reasons.append(
            f"upstream is offering {diff['new_count']} exercises, below the "
            f'floor of {MIN_EXERCISE_COUNT}')

    if baseline:
        removed = len(diff['removed']) / baseline
        if removed > MAX_REMOVED_FRACTION:
            reasons.append(
                f"{len(diff['removed'])} of {baseline} exercises would be "
                f'removed ({removed:.1%}, limit {MAX_REMOVED_FRACTION:.0%})')

        changed = len(diff['changed']) / baseline
        if changed > MAX_CHANGED_FRACTION:
            reasons.append(
                f"{len(diff['changed'])} of {baseline} exercises would change "
                f'({changed:.1%}, limit {MAX_CHANGED_FRACTION:.0%})')

    if diff['incomplete']:
        reasons.append(
            f"{len(diff['incomplete'])} incoming record(s) are missing a "
            f"required field ({', '.join(REQUIRED_FIELDS)}): "
            + ', '.join(diff['incomplete'][:5]))

    return reasons


# --- Applying ------------------------------------------------------------

def write_data(records: list[dict]) -> None:
    """Upstream's own formatting, byte for byte where it can be.

    Two spaces and non-ASCII left as itself, which is how the source publishes
    it — so the diff of an accepted sync is the content that changed and not a
    reformat of the whole file.
    """
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(records, fh, indent=2, ensure_ascii=False)
        fh.write('\n')


def missing_images(records: list[dict]) -> list[str]:
    wanted = []
    seen = set()
    for record in records:
        for path in record.get('images') or ():
            path = str(path).strip()
            if not path or path in seen:
                continue
            seen.add(path)
            if not _is_safe(path):
                continue
            if not os.path.isfile(os.path.join(IMAGE_ROOT, *path.split('/'))):
                wanted.append(path)
    return wanted


def _is_safe(path: str) -> bool:
    """Whether a dataset image path may be joined onto a filesystem path."""
    if not path or path.startswith('/') or '\\' in path:
        return False
    if not path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
        return False
    segments = path.split('/')
    return len(segments) <= 3 and all(
        s and s != '.' and s != '..' and not s.startswith('-') for s in segments)


def fetch_images(paths: list[str], *, limit: int = 0) -> tuple[int, int]:
    """Download the images this mirror does not have. `(written, failed)`."""
    written = failed = 0
    for position, path in enumerate(paths, start=1):
        if limit and written >= limit:
            break
        url = RAW_BASE + 'exercises/' + urllib.parse.quote(path)
        try:
            request = urllib.request.Request(
                url, headers={'User-Agent': 'free-exercise-db-mirror'})
            with urllib.request.urlopen(request, timeout=60) as response:
                content_type = (response.headers.get('Content-Type') or '').split(';')[0]
                if content_type.strip() not in IMAGE_CONTENT_TYPES:
                    print(f'  not an image ({content_type}): {path}', file=sys.stderr)
                    failed += 1
                    continue
                data = response.read(MAX_IMAGE_BYTES + 1)
        except Exception as e:  # noqa: BLE001 - one bad image is not a failed sync
            print(f'  failed: {path} ({e})', file=sys.stderr)
            failed += 1
            continue

        if not data or len(data) > MAX_IMAGE_BYTES:
            print(f'  refused ({len(data)} bytes): {path}', file=sys.stderr)
            failed += 1
            continue

        target = os.path.join(IMAGE_ROOT, *path.split('/'))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        temporary = target + '.part'
        with open(temporary, 'wb') as fh:
            fh.write(data)
        os.replace(temporary, target)
        written += 1
        if position % 100 == 0:
            print(f'  images {position}/{len(paths)} ...')
        time.sleep(IMAGE_DELAY_SECONDS)
    return written, failed


def count_images() -> int:
    total = 0
    for _root, _dirs, files in os.walk(IMAGE_ROOT):
        total += sum(1 for name in files if not name.endswith('.part'))
    return total


def write_state(head: dict, records: list[dict], diff: dict) -> None:
    state = {
        'upstream': UPSTREAM,
        'upstream_branch': UPSTREAM_BRANCH,
        'upstream_sha': head.get('sha', ''),
        'upstream_committed_at': head.get('date', ''),
        'synced_at': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'exercise_count': len(records),
        'image_count': count_images(),
        'last_change': {
            'added': len(diff['added']),
            'changed': len(diff['changed']),
            'removed': len(diff['removed']),
            'images_retained_after_upstream_dropped_them':
                len(diff['images_dropped']),
        },
    }
    with open(STATE_PATH, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(state, fh, indent=2)
        fh.write('\n')


# --- Reporting -----------------------------------------------------------

def report(diff: dict, reasons: list[str], head: dict, *, applied: bool) -> str:
    lines = [
        f'## Upstream sync — {"applied" if applied else "REFUSED"}',
        '',
        f'* upstream: `{UPSTREAM}@{UPSTREAM_BRANCH}`'
        + (f' at `{head["sha"][:12]}`' if head.get('sha') else ''),
        f'* here: {diff["old_count"]} exercises → offered: {diff["new_count"]}',
        f'* added {len(diff["added"])} · changed {len(diff["changed"])} · '
        f'removed {len(diff["removed"])}',
        f'* new images: {len(diff["images_new"])}',
    ]
    if diff['images_dropped']:
        lines.append(f'* images upstream no longer lists: '
                     f'{len(diff["images_dropped"])} — **kept**, as always')
    if reasons:
        lines += ['', '### Why this was refused', '']
        lines += [f'* {reason}' for reason in reasons]
        lines += ['',
                  'The mirror has **not** been modified. Its previous state is '
                  'intact and is still what any consumer reads.',
                  '',
                  'If upstream really did mean it, re-run this workflow with '
                  '`force` set, or raise the thresholds in '
                  '`scripts/guarded_sync.py`.']
    for label, key in (('Removed', 'removed'), ('Added', 'added')):
        if diff[key]:
            shown = ', '.join(f'`{k}`' for k in diff[key][:25])
            more = f' … and {len(diff[key]) - 25} more' if len(diff[key]) > 25 else ''
            lines += ['', f'### {label} ({len(diff[key])})', '', shown + more]
    return '\n'.join(lines) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true',
                        help='report what would happen and write nothing')
    parser.add_argument('--force', action='store_true',
                        help='apply the change even if the guard refuses it')
    parser.add_argument('--report-file',
                        help='write the markdown report here as well')
    parser.add_argument('--image-limit', type=int, default=0,
                        help='cap image downloads this run (0 = no cap)')
    args = parser.parse_args()

    local = load_local_records()
    try:
        upstream = fetch_upstream_records()
    except UpstreamUnusable as e:
        text = (f'## Upstream sync — REFUSED\n\n'
                f'`{UPSTREAM}@{UPSTREAM_BRANCH}` could not be used: {e}\n\n'
                f'The mirror still holds {len(local)} exercises and every image '
                f'it had. Nothing was changed.\n')
        print(text)
        _write_report(args.report_file, text)
        return 2

    head = upstream_head()
    diff = compare(local, upstream)
    reasons = evaluate(diff)

    if reasons and not args.force:
        text = report(diff, reasons, head, applied=False)
        print(text)
        _write_report(args.report_file, text)
        return 1

    text = report(diff, reasons if args.force else [], head, applied=True)
    print(text)
    _write_report(args.report_file, text)

    if args.dry_run:
        print('Dry run: nothing written.')
        return 0

    write_data(upstream)
    wanted = missing_images(upstream)
    if wanted:
        print(f'Fetching {len(wanted)} missing image(s) ...')
        written, failed = fetch_images(wanted, limit=args.image_limit)
        print(f'  wrote {written}, failed {failed}')
    write_state(head, upstream, diff)
    print('Mirror updated.')
    return 0


def _write_report(path, text) -> None:
    if not path:
        return
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)


if __name__ == '__main__':
    raise SystemExit(main())
