#!/usr/bin/env python3
"""test_bm25_index.py — tests for scripts/bm25-index.py + hybrid fusion.

Does NOT require ollama: unit tests run via importlib against a sandbox
vault (module path globals monkeypatched); CLI/hybrid degradation tests run
the copied scripts inside the sandbox via subprocess, so the live vault is
never touched. The ollama-dependent dense channel is exercised only through
its FAILURE paths (unreachable endpoint / bogus model), which behave the
same with or without a running ollama.

Usage:
  python3 tests/test_bm25_index.py
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BM25 = ROOT / "scripts" / "bm25-index.py"
SEM = ROOT / "scripts" / "semantic-search.py"
RETRIEVE = ROOT / "scripts" / "retrieve.py"
SCHEMA = ROOT / "scripts" / "vault_schema.py"


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


PAGE_TMPL = """---
type: concept
title: "{title}"
status: draft
created: 2026-01-01
updated: 2026-01-01
tags: [{tags}]
sessions: []
---

# {title}

{body}
"""


def make_sandbox():
    """Sandbox vault with copied scripts and fixture pages."""
    sb = Path(tempfile.mkdtemp(prefix="bm25-test."))
    (sb / "scripts").mkdir()
    (sb / "wiki" / "_templates").mkdir(parents=True)
    (sb / ".vault-meta").mkdir()
    shutil.copy2(BM25, sb / "scripts" / "bm25-index.py")
    shutil.copy2(SEM, sb / "scripts" / "semantic-search.py")
    shutil.copy2(RETRIEVE, sb / "scripts" / "retrieve.py")
    shutil.copy2(ROOT / "scripts" / "pipeline_events.py", sb / "scripts" / "pipeline_events.py")
    shutil.copy2(SCHEMA, sb / "scripts" / "vault_schema.py")

    def page(rel, title, tags, body):
        p = sb / "wiki" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(PAGE_TMPL.format(title=title, tags=tags, body=body), encoding="utf-8")

    page("concepts/karpenter.md", "Karpenter Autoscaling", "karpenter, eks",
         "Karpenter provisions nodes for EKS clusters. Провижининг нод быстрее autoscaler.")
    page("incidents/disk-leak.md", "Buildkit Disk Leak", "buildkit, docker",
         "Orphan buildx buildkit containers filled the disk. Диск переполнен контейнерами buildkit buildkit buildkit.")
    page("meta/session-note.md", "Session Note", "meta",
         "A meta page that the tiling cache would exclude but bm25 must cover.")
    # skipped fixtures
    (sb / "wiki" / "log.md").write_text("# Log\nbuildkit buildkit\n", encoding="utf-8")
    (sb / "wiki" / "_index.md").write_text("# idx\nbuildkit\n", encoding="utf-8")
    (sb / "wiki" / "_templates" / "t.md").write_text("# tmpl\nbuildkit\n", encoding="utf-8")
    # page without frontmatter — must still be indexed (title = stem)
    (sb / "wiki" / "concepts" / "bare-note.md").write_text(
        "Plain note about victoriametrics retention.\n", encoding="utf-8")
    return sb


def patch_paths(mod, sb):
    mod.VAULT_ROOT = sb
    mod.WIKI_DIR = sb / "wiki"
    mod.META_DIR = sb / ".vault-meta"
    mod.BM25_DIR = sb / ".vault-meta" / "bm25"
    mod.INDEX_PATH = mod.BM25_DIR / "index.json"
    mod.LOCK_PATH = mod.META_DIR / ".bm25.lock"


def test_tokenizer(bm):
    toks = bm.tokenize("The user's well-formed query, and running!")
    assert_true("tok basic", "user's" in toks and "well-formed" in toks, str(toks))
    assert_true("tok stopwords dropped", "the" not in toks and "and" not in toks)
    assert_true("tok short dropped", "a" not in toks)
    toks = bm.tokenize("Провижининг нод для кластера и По")
    assert_true("tok cyrillic kept", "провижининг" in toks and "нод" in toks, str(toks))
    assert_true("tok ru stopwords dropped", "для" not in toks)
    assert_eq("tok emoji skipped", [], bm.tokenize("🎉 ✨ !!!"))
    toks = bm.tokenize("naïve café 東京")
    assert_true("tok accents+cjk kept", "naïve" in toks and "café" in toks and "東京" in toks, str(toks))


def test_idf_positive(bm):
    import math
    for df in (996, 997, 998, 999, 1000):
        idf = math.log(1 + (1000 - df + 0.5) / (df + 0.5))
        assert_true(f"idf positive N=1000 df={df}", idf > 0, str(idf))


def test_parse_page(bm):
    title, tags, body = bm.parse_page(PAGE_TMPL.format(
        title="T", tags="a, b-c", body="Body here"))
    assert_eq("parse title", "T", title)
    assert_eq("parse flow tags", ["a", "b-c"], tags)
    assert_true("parse body", "Body here" in body)
    title, tags, body = bm.parse_page("---\ntitle: X\ntags:\n  - one\n  - two\n---\nB\n")
    assert_eq("parse block tags", ["one", "two"], tags)
    title, tags, body = bm.parse_page("no frontmatter at all")
    assert_eq("parse no-fm title empty", "", title)
    assert_eq("parse no-fm body passthrough", "no frontmatter at all", body)


def test_build_and_query(bm, sb):
    idx = bm.build_index()
    assert_true("build returns index", idx is not None)
    assert_true("build records source fingerprint", len(idx["source_fingerprint"]) == 64)
    assert_eq("build doc_count (skips log/_index/_templates)", 4, idx["doc_count"])
    assert_true("build skips log.md", "wiki/log.md" not in idx["docs"])
    assert_true("build skips _index.md", "wiki/_index.md" not in idx["docs"])
    assert_true("build skips _templates", "wiki/_templates/t.md" not in idx["docs"])
    assert_true("build covers meta/", "wiki/meta/session-note.md" in idx["docs"])
    assert_true("build covers no-frontmatter page", "wiki/concepts/bare-note.md" in idx["docs"])

    bm.write_index(idx)
    assert_true("write_index atomic (no tmp strays)",
                not list(bm.BM25_DIR.glob("*.tmp.*")))
    loaded = bm.load_index_or_none()
    assert_true("load_index_or_none roundtrip", loaded is not None)

    hits = bm.score_query(loaded, "buildkit disk leak", top_k=5)
    assert_true("query returns hits", len(hits) >= 1)
    assert_eq("query top hit", "wiki/incidents/disk-leak.md", hits[0][0])

    # tags land in doc_text: query by tag term only
    hits = bm.score_query(loaded, "karpenter", top_k=5)
    assert_eq("tag term finds page", "wiki/concepts/karpenter.md", hits[0][0])

    # cyrillic channel
    hits = bm.score_query(loaded, "провижининг нод", top_k=5)
    assert_eq("cyrillic query hit", "wiki/concepts/karpenter.md", hits[0][0])

    assert_eq("stopword-only query empty", [], bm.score_query(loaded, "the and of и для"))

    # monotonicity: more matching terms should not rank the page lower
    one = dict(bm.score_query(loaded, "buildkit", top_k=5))
    two = dict(bm.score_query(loaded, "buildkit disk", top_k=5))
    assert_true("monotonic score growth",
                two["wiki/incidents/disk-leak.md"] >= one["wiki/incidents/disk-leak.md"])


def test_corrupt_index(bm):
    bm.INDEX_PATH.write_text("garbage{", encoding="utf-8")
    assert_eq("corrupt index -> None", None, bm.load_index_or_none())


def test_rrf(sem):
    fused = sem.rrf_fuse([["a", "b", "c"], ["b", "a"]])
    scores = dict(fused)
    # a: 1/61 + 1/62 ; b: 1/62 + 1/61 — tie; c: 1/63
    assert_eq("rrf tie a==b", round(scores["a"], 10), round(scores["b"], 10))
    assert_true("rrf c last", fused[-1][0] == "c")
    fused = sem.rrf_fuse([["x"], []])
    assert_eq("rrf single list", "x", fused[0][0])
    assert_eq("rrf empty input", [], sem.rrf_fuse([[], []]))


def test_rrf_weighted(sem):
    # Same lists as test_rrf, but favour list 0 (dense). The equal-weight tie
    # between a and b must break in favour of a (rank1 in the up-weighted list).
    fused = sem.rrf_fuse([["a", "b", "c"], ["b", "a"]], weights=[3.0, 1.0])
    scores = dict(fused)
    assert_true("weighted a>b", scores["a"] > scores["b"])
    assert_eq("weighted a first", "a", fused[0][0])
    assert_true("weighted c last", fused[-1][0] == "c")
    # weights=None reproduces classic equal-weight RRF (a==b tie preserved).
    eq = dict(sem.rrf_fuse([["a", "b", "c"], ["b", "a"]], weights=None))
    assert_eq("weights=None == equal", round(eq["a"], 10), round(eq["b"], 10))


def test_hybrid_fuse_scope_aware(sem):
    # dense scope = {a, b, c}; page "m" (meta-like) exists only in bm25.
    scope = {"a", "b", "c"}
    dense = ["a", "b", "c"]
    # in-scope bm25 votes are dropped: bm25 preferring b over a must NOT
    # reorder the dense ranking (the old [3,1] weighting regression).
    fused = sem.hybrid_fuse(dense, ["b", "a"], scope)
    assert_eq("in-scope bm25 ignored", ["a", "b", "c"], [p for p, _ in fused])
    # out-of-scope page injects: bm25#1 lands below dense#3 (2.9/61 < 3/63)
    # but above dense#4 (2.9/61 > 3/64) — never dethrones a correct dense#1.
    fused = sem.hybrid_fuse(dense + ["d"], ["m", "b"], scope | {"d"})
    order = [p for p, _ in fused]
    assert_eq("oos injected 4th", ["a", "b", "c", "m", "d"], order)
    # shipped weights invariant: strict dense > oos (tie-break protection).
    assert_true("dense > oos weight", sem.DENSE_WEIGHT > sem.BM25_OOS_WEIGHT)
    # bm25-only mode unaffected: empty dense list still surfaces oos pages.
    fused = sem.hybrid_fuse([], ["m"], scope)
    assert_eq("oos with empty dense", "m", fused[0][0])


def run_cli(sb, script, *args):
    return subprocess.run(
        [sys.executable, str(sb / "scripts" / script), *args],
        capture_output=True, text=True)


def test_cli_and_degradations(sb):
    # no wiki dir -> exit 4
    empty = Path(tempfile.mkdtemp(prefix="bm25-empty."))
    (empty / "scripts").mkdir()
    (empty / ".vault-meta").mkdir()
    shutil.copy2(BM25, empty / "scripts" / "bm25-index.py")
    r = run_cli(empty, "bm25-index.py", "build")
    assert_eq("cli build no-wiki exit 4", 4, r.returncode)
    shutil.rmtree(empty)

    # query before build -> exit 3
    r = run_cli(sb, "bm25-index.py", "query", "buildkit")
    assert_eq("cli query no-index exit 3", 3, r.returncode)

    r = run_cli(sb, "bm25-index.py", "build", "--quiet")
    assert_eq("cli build exit 0", 0, r.returncode)
    assert_eq("cli build --quiet silent", "", r.stderr.strip())

    before = json.loads((sb / ".vault-meta/bm25/index.json").read_text())
    r = run_cli(sb, "bm25-index.py", "ensure", "--quiet")
    assert_eq("cli ensure fresh exit 0", 0, r.returncode)
    same = json.loads((sb / ".vault-meta/bm25/index.json").read_text())
    assert_eq("cli ensure fresh no rebuild", before["updated_at"], same["updated_at"])

    with (sb / "wiki/concepts/karpenter.md").open("a", encoding="utf-8") as fh:
        fh.write("\nunique-self-heal-token\n")
    r = run_cli(sb, "bm25-index.py", "ensure", "--quiet")
    assert_eq("cli ensure stale rebuild exit 0", 0, r.returncode)
    healed = json.loads((sb / ".vault-meta/bm25/index.json").read_text())
    assert_true(
        "cli ensure fingerprint changed",
        healed["source_fingerprint"] != before["source_fingerprint"],
    )

    r = run_cli(sb, "bm25-index.py", "query", "buildkit disk", "--top", "3")
    assert_eq("cli query exit 0", 0, r.returncode)
    assert_true("cli query finds incident", "wiki/incidents/disk-leak.md" in r.stdout)

    r = run_cli(sb, "bm25-index.py", "stats")
    assert_eq("cli stats exit 0", 0, r.returncode)
    assert_eq("cli stats schema", 2, json.loads(r.stdout)["schema_version"])

    # Compatibility wrapper is sparse-first and degrades explicitly when the
    # optional section-dense cache is absent.
    r = run_cli(sb, "semantic-search.py", "buildkit disk", "--hybrid")
    assert_eq("wrapper sparse fallback exit 0", 0, r.returncode)
    assert_true("wrapper sparse degraded header", "mode=sparse degraded=true" in r.stdout, r.stdout)
    assert_true("wrapper returns best section page", "wiki/incidents/disk-leak.md" in r.stdout)

    # --hybrid is now optional compatibility syntax; default behavior matches.
    r = run_cli(sb, "semantic-search.py", "buildkit disk")
    assert_eq("wrapper default exit 0", 0, r.returncode)
    assert_true("wrapper default section output", "wiki/incidents/disk-leak.md" in r.stdout)

    # Corrupt derived section index self-heals from the wiki.
    section_index = sb / ".vault-meta/retrieval/index.json"
    section_index.write_text("garbage{", encoding="utf-8")
    r = run_cli(sb, "semantic-search.py", "buildkit disk", "--hybrid")
    assert_eq("wrapper corrupt index self-heals", 0, r.returncode)
    assert_true("wrapper self-heal recreates index", section_index.stat().st_size > 20)


def main():
    sb = make_sandbox()
    try:
        bm = load_module(sb / "scripts" / "bm25-index.py", "bm25_index_test")
        patch_paths(bm, sb)
        sem = load_module(sb / "scripts" / "semantic-search.py", "semantic_search_test")

        test_tokenizer(bm)
        test_idf_positive(bm)
        test_parse_page(bm)
        test_build_and_query(bm, sb)
        test_corrupt_index(bm)
        # reset index after corruption for CLI tests
        (bm.INDEX_PATH).unlink()
        test_rrf(sem)
        test_rrf_weighted(sem)
        test_hybrid_fuse_scope_aware(sem)
        test_cli_and_degradations(sb)
    finally:
        shutil.rmtree(sb, ignore_errors=True)
    print("\nAll bm25/hybrid tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
