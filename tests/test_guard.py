"""The guard's decisions.

This mirror only earns the word "backup" if it refuses the right things, so the
refusals are the tests. They are pure — `evaluate()` takes a diff and returns
reasons — plus one end-to-end run against a local fixture standing in for
upstream, because "the script exits 1 and writes nothing" is the actual
contract the workflow depends on.

    python -m unittest discover -s tests
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / 'scripts'))

import guarded_sync as guard  # noqa: E402


def catalog(count, *, start=0, note=''):
    return [{'id': f'Ex_{n}', 'name': f'Exercise {n}', 'note': note,
             'images': [f'Ex_{n}/0.jpg']}
            for n in range(start, start + count)]


class EvaluationTests(unittest.TestCase):
    """What passes, and what does not."""

    def allowed(self, old, new):
        return not guard.evaluate(guard.compare(old, new))

    def test_an_unchanged_upstream_is_allowed(self):
        self.assertTrue(self.allowed(catalog(873), catalog(873)))

    def test_additions_are_allowed_however_many(self):
        self.assertTrue(self.allowed(catalog(873), catalog(1400)))

    def test_a_small_removal_is_allowed(self):
        # 20 of 873 is ordinary upstream maintenance and needs no human.
        self.assertTrue(self.allowed(catalog(873), catalog(853)))

    def test_a_large_removal_is_refused(self):
        reasons = guard.evaluate(guard.compare(catalog(873), catalog(800)))
        self.assertTrue(reasons)
        self.assertIn('would be removed', reasons[0])

    def test_an_emptied_upstream_is_refused(self):
        # The failure this whole repository exists for.
        reasons = guard.evaluate(guard.compare(catalog(873), catalog(1)))
        self.assertTrue(reasons)

    def test_a_catalog_below_the_floor_is_refused(self):
        # Even a *gradual* erosion is caught: 699 is under the absolute floor
        # however small each individual step was.
        reasons = guard.evaluate(guard.compare(catalog(700), catalog(699)))
        self.assertTrue(any('floor' in reason for reason in reasons))

    def test_a_wholesale_rewrite_is_refused(self):
        # Same ids, every record different: a reformat, a schema change, or
        # something worse. Either way a human should look at it first.
        reasons = guard.evaluate(
            guard.compare(catalog(873), catalog(873, note='rewritten')))
        self.assertTrue(any('would change' in reason for reason in reasons))

    def test_a_record_missing_required_fields_refuses_the_whole_sync(self):
        broken = catalog(873)
        broken[5] = {'id': 'Ex_5', 'name': ''}
        reasons = guard.evaluate(guard.compare(catalog(873), broken))
        self.assertTrue(any('required field' in reason for reason in reasons))

    def test_dropped_images_are_reported_but_never_a_refusal(self):
        old = catalog(873)
        new = catalog(873)
        for record in new:
            record['images'] = []
        diff = guard.compare(old, new)
        self.assertEqual(len(diff['images_dropped']), 873)
        # Losing every image reference is a change of content, not a reason to
        # refuse — the files themselves are kept regardless.
        self.assertFalse(any('image' in reason for reason in guard.evaluate(diff)))


class ImagePathTests(unittest.TestCase):
    """These paths are joined onto a filesystem path before anything is written."""

    def test_ordinary_paths_are_accepted(self):
        for path in ('Barbell_Squat/0.jpg', '3_4_Sit-Up/1.jpg', 'A/b/c.png'):
            self.assertTrue(guard._is_safe(path), path)

    def test_traversal_and_absolute_paths_are_refused(self):
        for path in ('../../etc/passwd', '/etc/shadow', 'a/../b/0.jpg',
                     'C:\\Windows\\0.jpg', './0.jpg', '', 'notes.txt'):
            self.assertFalse(guard._is_safe(path), repr(path))


class EndToEndRefusalTests(unittest.TestCase):
    """The contract the workflow reads: exit code, and an untouched mirror."""

    def setUp(self):
        self.work = pathlib.Path(tempfile.mkdtemp(prefix='guard-'))
        (self.work / 'dist').mkdir()
        (self.work / 'scripts').mkdir()
        (self.work / 'scripts' / 'guarded_sync.py').write_bytes(
            (_REPO / 'scripts' / 'guarded_sync.py').read_bytes())
        self.data = self.work / 'dist' / 'exercises.json'
        self.data.write_text(json.dumps(catalog(873)), encoding='utf-8')
        self.before = self.data.read_text(encoding='utf-8')

    def run_guard(self, upstream_payload, *extra):
        """Run the script with a local file standing in for upstream."""
        served = self.work / 'served'
        (served / 'dist').mkdir(parents=True, exist_ok=True)
        (served / 'dist' / 'exercises.json').write_text(
            upstream_payload, encoding='utf-8')
        environment = dict(os.environ,
                           UPSTREAM_RAW_BASE=served.as_uri() + '/')
        return subprocess.run(
            [sys.executable, str(self.work / 'scripts' / 'guarded_sync.py'), *extra],
            capture_output=True, text=True, env=environment, cwd=self.work)

    def test_a_safe_change_is_applied_and_exits_zero(self):
        result = self.run_guard(json.dumps(catalog(880)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(json.loads(self.data.read_text(encoding='utf-8'))), 880)

    def test_a_destructive_change_exits_one_and_writes_nothing(self):
        result = self.run_guard(json.dumps(catalog(10)))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.data.read_text(encoding='utf-8'), self.before,
                         'the mirror was modified by a refused sync')
        self.assertIn('REFUSED', result.stdout)

    def test_an_unreachable_upstream_exits_two_and_writes_nothing(self):
        environment = dict(os.environ,
                           UPSTREAM_RAW_BASE=(self.work / 'gone').as_uri() + '/')
        result = subprocess.run(
            [sys.executable, str(self.work / 'scripts' / 'guarded_sync.py')],
            capture_output=True, text=True, env=environment, cwd=self.work)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.data.read_text(encoding='utf-8'), self.before)

    def test_a_non_json_response_exits_two_and_writes_nothing(self):
        # What a hijacked or misconfigured host actually serves: an HTML page.
        result = self.run_guard('<!doctype html><title>404</title>')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.data.read_text(encoding='utf-8'), self.before)

    def test_force_applies_what_the_guard_refused(self):
        result = self.run_guard(json.dumps(catalog(10)), '--force')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(json.loads(self.data.read_text(encoding='utf-8'))), 10)

    def test_a_dry_run_never_writes(self):
        result = self.run_guard(json.dumps(catalog(880)), '--dry-run')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.data.read_text(encoding='utf-8'), self.before)


if __name__ == '__main__':
    unittest.main()
