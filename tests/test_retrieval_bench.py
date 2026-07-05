#!/usr/bin/env python3
"""test_retrieval_bench.py — hermetic tests for scripts/retrieval-bench.py.

No ollama, no live vault: metrics math and goldset parsing are unit-tested
via importlib; degradation paths run the copied script in a mktemp sandbox
via subprocess (dense probe fails against a dead endpoint either way).

Usage:
  python3 tests/test_retrieval_bench.py
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "scripts" / "retrieval-bench.py"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Fail(SystemExit):
    pass


def assert_eq(label, expected, actual):
    if expected != actual:
        raise Fail(f"FAIL {label}: expected {expected!r}, got {actual!r}")
    print(f"OK   {label}")


def assert_true(label, cond, extra=""):
    if not cond:
        raise Fail(f"FAIL {label}{': ' + extra if extra else ''}")
    print(f"OK   {label}")


rb = load_module(BENCH, "retrieval_bench_test")


def test_first_hit_rank():
    assert_eq("rank first", 1, rb.first_hit_rank(["a", "b"], ["a"]))
    assert_eq("rank later", 3, rb.first_hit_rank(["x", "y", "a"], ["a", "z"]))
    assert_eq("rank any-of-expect", 2, rb.first_hit_rank(["x", "b"], ["a", "b"]))
    assert_eq("rank miss", None, rb.first_hit_rank(["x", "y"], ["a"]))
    assert_eq("rank beyond k", None, rb.first_hit_rank(["x"] * 10 + ["a"], ["a"]))
    assert_eq("rank empty ranked", None, rb.first_hit_rank([], ["a"]))


def test_metrics():
    m = rb.Metrics()
    for r in (1, 3, None, 2):
        m.add(r)
    assert_eq("metrics n", 4, m.n)
    assert_eq("hit@1", 0.25, m.hit_at(1))
    assert_eq("hit@5", 0.75, m.hit_at(5))
    # MRR = (1/1 + 1/3 + 0 + 1/2) / 4
    assert_eq("mrr", round((1 + 1 / 3 + 0.5) / 4, 6), round(m.mrr(), 6))
    empty = rb.Metrics()
    assert_eq("empty hit", 0.0, empty.hit_at(5))
    assert_eq("empty mrr", 0.0, empty.mrr())


def make_sandbox():
    sb = Path(tempfile.mkdtemp(prefix="bench-test."))
    (sb / "scripts").mkdir()
    (sb / ".vault-meta").mkdir()
    (sb / "wiki").mkdir()
    for f in ("retrieval-bench.py", "tag-search.py", "bm25-index.py", "semantic-search.py"):
        shutil.copy2(ROOT / "scripts" / f, sb / "scripts" / f)
    return sb


def run_bench(sb, *args):
    return subprocess.run(
        [sys.executable, str(sb / "scripts" / "retrieval-bench.py"), *args],
        capture_output=True, text=True)


def test_degradations():
    sb = make_sandbox()
    try:
        # no goldset -> exit 3
        r = run_bench(sb)
        assert_eq("no goldset exit 3", 3, r.returncode)

        # corrupt goldset -> exit 3
        gs = sb / ".vault-meta" / "retrieval-goldset.jsonl"
        gs.write_text("not-json{\n", encoding="utf-8")
        r = run_bench(sb)
        assert_eq("corrupt goldset exit 3", 3, r.returncode)

        # valid goldset but no bm25 index -> exit 3 with hint
        page = sb / "wiki" / "target.md"
        page.write_text("---\ntitle: T\ntags: [buildkit]\n---\nbuildkit leak page\n",
                        encoding="utf-8")
        gs.write_text(json.dumps(
            {"q": "buildkit leak", "expect": ["wiki/target.md"]}) + "\n", encoding="utf-8")
        r = run_bench(sb)
        assert_eq("no bm25 index exit 3", 3, r.returncode)
        assert_true("no bm25 hint", "bm25-index.py build" in r.stderr, r.stderr)

        # build bm25 -> runs; dense/hybrid SKIPPED (no tiling cache in sandbox)
        subprocess.run([sys.executable, str(sb / "scripts" / "bm25-index.py"),
                        "build", "--quiet"], capture_output=True)
        r = run_bench(sb)
        assert_eq("bm25-only run exit 0", 0, r.returncode)
        assert_true("dense skipped noted", "SKIPPED" in r.stdout + r.stderr,
                    r.stdout + r.stderr)
        assert_true("bm25 measured", "bm25" in r.stdout)
        bm25_line = next((l for l in r.stdout.splitlines() if l.startswith("bm25")), "")
        assert_eq("bm25 perfect hit", ["bm25", "1.00", "1.00", "1.000"], bm25_line.split())

        # dead expect page -> query skipped, zero queries -> still exit 0 with 0 queries
        gs.write_text(json.dumps(
            {"q": "buildkit leak", "expect": ["wiki/gone.md"]}) + "\n", encoding="utf-8")
        r = run_bench(sb)
        assert_eq("dead expect exit 0", 0, r.returncode)
        assert_true("dead expect warned", "skipped" in (r.stdout + r.stderr).lower())

        # --report writes a file with frontmatter
        gs.write_text(json.dumps(
            {"q": "buildkit leak", "expect": ["wiki/target.md"]}) + "\n", encoding="utf-8")
        rep = sb / "report.md"
        r = run_bench(sb, "--report", str(rep))
        assert_eq("report run exit 0", 0, r.returncode)
        assert_true("report file written", rep.is_file())
        assert_true("report has frontmatter", rep.read_text().startswith("---\ntype: meta"))
    finally:
        shutil.rmtree(sb, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_first_hit_rank()
        test_metrics()
        test_degradations()
    except Fail as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    print("\nAll retrieval-bench tests passed.")
