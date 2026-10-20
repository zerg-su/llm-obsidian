#!/usr/bin/env python3
"""Contracts for scripts/drilldown.py: spec validation, fetch, render limits."""

from __future__ import annotations

import http.server
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "drilldown.py"
START = 1_759_999_800  # 2025-10-09T08:50:00Z, aligned to 5m
STEP = 300


def spec(**overrides):
    base = {
        "name": "pod-gc",
        "title": "pod-a — GC",
        "subtitle": "test fixture",
        "tz": {"offset_hours": 3, "label": "MSK"},
        "range": {"start": START, "end": START + 11 * STEP, "step": "5m"},
        "source": {"type": "prometheus", "url": "http://127.0.0.1:1/prometheus"},
        "params": {"sel": 'pod="pod-a"'},
        "tiles": [
            {"label": "max pause", "series": "pause", "agg": "max", "unit": "s", "at_max": True},
            {"label": "live heap", "value": "8.7 GiB", "note": "static"},
        ],
        "panels": [
            {
                "title": "GC by generation",
                "type": "stacked-bar",
                "series": [
                    {"id": "gc", "query": "sum by (gen) (increase(gc{${sel}}[5m]))", "split_by": "gen",
                     "order": ["gen0", "gen1", "gen2"]},
                ],
            },
            {
                "title": "STW pause",
                "type": "bar",
                "unit": "s",
                "annotate_max": True,
                "thresholds": [{"value": 10, "label": "request timeout"}],
                "series": [{"id": "pause", "label": "pause", "query": "increase(pause{${sel}}[5m])"}],
            },
            {
                "title": "Heap after GC",
                "type": "line",
                "unit": "GiB",
                "scale": 1 / 2**30,
                "series": [{"id": "heap", "label": "heap", "query": "heap{${sel}}"}],
            },
            {
                "title": "Timeouts per hour",
                "type": "bar",
                "series": [{"id": "timeouts", "label": "timeouts", "source": "data", "bucket": "1h"}],
            },
        ],
        "table": {"bucket": "1h", "columns": [
            {"series": "pause", "agg": "sum", "label": "pause, s"},
            {"series": "timeouts", "agg": "sum", "label": "timeouts"},
        ]},
    }
    base.update(overrides)
    return base


def data_payload():
    ts = [START + i * STEP for i in range(12)]
    return {
        "version": 1,
        "series": {
            "gc:gen0": {"label": "gen0", "points": [[t, 1] for t in ts]},
            "gc:gen1": {"label": "gen1", "points": [[t, 0] for t in ts]},
            "gc:gen2": {"label": "gen2", "points": [[t, 2] for t in ts]},
            "pause": {"label": "pause", "points": [[t, 4.5 if i != 5 else 76.7] for i, t in enumerate(ts)]},
            "heap": {"label": "heap", "points": [[t, 8.7 * 2**30] for t in ts]},
        },
        "errors": {},
    }


def run(*args, env=None):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True, env=env)


class Drilldown(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="drilldown-test."))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, value):
        path = self.tmp / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def render(self, spec_value=None, data_value=None, extra=None, out=None):
        args = ["render", "--spec", str(self.write("spec.json", spec_value or spec())),
                "--data", str(self.write("data.json", data_value or data_payload())),
                "--out", str(out or self.tmp / "out.html")]
        if extra is not None:
            args += ["--extra", str(self.write("extra.json", extra))]
        return run(*args)

    def test_validate_rejects_bad_specs(self):
        cases = [
            (spec(name="bad name!"), "name"),
            (spec(panels=[{"title": "x", "type": "pie", "series": [{"id": "a", "query": "q"}]}]), "type"),
            (spec(panels=[{"title": "x", "type": "bar", "series": [{"id": "a", "query": "q"}, {"id": "a", "query": "q"}]}]), "duplicate"),
            (spec(range={"start": START, "end": START - 1, "step": "5m"}), "range"),
            (spec(source={"type": "prometheus", "url": "file:///etc/passwd"}), "url"),
            (spec(tiles=[{"label": "x", "series": "nope", "agg": "max"}]), "nope"),
        ]
        for value, needle in cases:
            result = run("validate", "--spec", str(self.write("bad.json", value)))
            self.assertEqual(result.returncode, 2, (needle, result.stdout, result.stderr))
            self.assertIn(needle, result.stderr.lower())
        self.assertEqual(run("validate", "--spec", str(self.write("ok.json", spec()))).returncode, 0)

    def test_render_is_self_contained_and_computes_tiles(self):
        iso = "2025-10-09T09:50:00Z"  # START + 3600, ISO form as agents write it
        extra = {"timeouts": {"label": "timeouts", "points": [[START, 122], [iso, 3]]}}
        result = self.render(extra=extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        html = (self.tmp / "out.html").read_text(encoding="utf-8")
        self.assertLess(len(html.encode()), 512 * 1024)
        self.assertNotIn("100vh", html)
        self.assertIsNone(re.search(r"""(?:src|href)\s*=\s*["']https?://""", html))
        self.assertIn("pod-a — GC", html)
        self.assertIn("76.7", html)            # max tile value
        self.assertIn("prefers-color-scheme:dark", html.replace(" ", ""))
        payload = json.loads(re.search(r'<script id="dd-data" type="application/json">(.*?)</script>', html, re.S).group(1))
        ids = [s["id"] for p in payload["panels"] for s in p["series"]]
        self.assertEqual(ids[:3], ["gc:gen0", "gc:gen1", "gc:gen2"])
        self.assertIn("timeouts", ids)
        tile = payload["tiles"][0]
        self.assertEqual(tile["value"], "76.7 s")
        self.assertIn("12:15", tile["note"])  # max at 09:15Z rendered in tz +3
        self.assertEqual(payload["table"]["rows"][0]["cells"][1], 122)
        self.assertEqual(payload["table"]["rows"][1]["cells"][1], 3)  # ISO timestamp accepted

    def test_missing_series_is_reported_not_fatal(self):
        result = self.render()  # no extra → timeouts missing
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("timeouts", result.stderr)

    def test_downsampling_keeps_bar_sums_and_line_max(self):
        n = 2000
        ts = [START + i * 60 for i in range(n)]
        value = spec(range={"start": START, "end": ts[-1], "step": "1m"}, max_points=500)
        data = data_payload()
        data["series"]["pause"]["points"] = [[t, 1.0] for t in ts]
        data["series"]["heap"]["points"] = [[t, float(i)] for i, t in enumerate(ts)]
        result = self.render(value, data)
        self.assertEqual(result.returncode, 0, result.stderr)
        html = (self.tmp / "out.html").read_text(encoding="utf-8")
        payload = json.loads(re.search(r'<script id="dd-data" type="application/json">(.*?)</script>', html, re.S).group(1))
        series = {s["id"]: s for p in payload["panels"] for s in p["series"]}
        self.assertLessEqual(len(series["pause"]["points"]), 500)
        self.assertAlmostEqual(sum(v for _, v in series["pause"]["points"]), n, places=6)
        self.assertAlmostEqual(max(v for _, v in series["heap"]["points"]), (n - 1) / 2**30, places=9)

    def test_rejects_output_name_orca_cannot_show(self):
        result = self.render(out=self.tmp / "bad name.html")
        self.assertEqual(result.returncode, 2)
        self.assertIn("file name", result.stderr.lower())

    def test_render_js_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        self.assertEqual(self.render().returncode, 0)
        html = (self.tmp / "out.html").read_text(encoding="utf-8")
        script = re.search(r"<script>(.*)</script>", html, re.S).group(1)
        js = self.tmp / "check.js"
        js.write_text(script, encoding="utf-8")
        self.assertEqual(subprocess.run([node, "--check", str(js)], capture_output=True).returncode, 0)

    def test_fetch_from_prometheus_api(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                seen.append((self.path, self.headers.get("Authorization")))
                expr = query["query"][0]
                if expr.startswith("heap"):
                    body, code = {"status": "error", "error": "too many samples"}, 422
                elif "by (gen)" in expr:
                    body = {"status": "success", "data": {"resultType": "matrix", "result": [
                        {"metric": {"gen": g}, "values": [[START, "1"], [START + STEP, "2"]]} for g in ("gen2", "gen0")]}}
                    code = 200
                else:
                    body = {"status": "success", "data": {"resultType": "matrix", "result": [
                        {"metric": {}, "values": [[START, "4.5"]]}]}}
                    code = 200
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            value = spec(source={"type": "prometheus", "url": f"http://127.0.0.1:{server.server_port}/prometheus",
                                 "bearer_env": "DD_TEST_TOKEN"})
            env = {"PATH": "/usr/bin:/bin", "DD_TEST_TOKEN": "secret-token"}
            out = self.tmp / "data.json"
            result = run("fetch", "--spec", str(self.write("spec.json", value)), "--out", str(out), env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("secret-token", result.stdout + result.stderr)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(sorted(data["series"]), ["gc:gen0", "gc:gen2", "pause"])
            self.assertIn("heap", data["errors"])
            self.assertTrue(any('pod%3D%22pod-a%22' in p or 'pod="pod-a"' in urllib.parse.unquote(p) for p, _ in seen))
            self.assertTrue(all(auth == "Bearer secret-token" for _, auth in seen))
            self.assertTrue(all("/api/v1/query_range" in p for p, _ in seen))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
