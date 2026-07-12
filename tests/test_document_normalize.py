#!/usr/bin/env python3
"""Hermetic tests for the local document normalization pipeline."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
NORMALIZER = ROOT / "scripts" / "document-normalize.py"
INSTALLER = ROOT / "scripts" / "install-docling.py"


class Suite:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  OK   {name}")
        else:
            self.failed += 1
            print(f"  FAIL {name}: {detail}")


def run_normalizer(
    source: Path,
    cache: Path,
    env: dict[str, str],
    *extra: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    result = subprocess.run(
        [
            sys.executable,
            str(NORMALIZER),
            "normalize",
            str(source),
            "--cache-root",
            str(cache),
            "--json",
            *extra,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout, "stderr": result.stderr}
    return result, payload


def make_fake_docling(root: Path) -> Path:
    script = root / "fake-docling"
    script.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if os.environ.get('FAKE_DOCLING_MODE') == 'fail':
    print('fixture conversion failure', file=sys.stderr)
    raise SystemExit(9)
output = pathlib.Path(args[args.index('--output') + 1])
source = pathlib.Path(args[-1])
output.mkdir(parents=True, exist_ok=True)
log = os.environ.get('FAKE_DOCLING_LOG')
if log:
    pathlib.Path(log).write_text(json.dumps({
        'args': args,
        'offline': os.environ.get('HF_HUB_OFFLINE'),
        'transformers_offline': os.environ.get('TRANSFORMERS_OFFLINE'),
        'easyocr_module_path': os.environ.get('EASYOCR_MODULE_PATH'),
    }))
mode = os.environ.get('FAKE_DOCLING_MODE', 'ok')
text = 'x' if mode == 'low' else '# Документ\\n\\nРусский and English text from Docling.\\n\\n![](sample_artifacts/image.png)\\n'
(output / f'{source.stem}.md').write_text(text)
pages = 3 if mode == 'pages' else 1
confidence = 0.2 if mode == 'confidence' else 0.95
(output / f'{source.stem}.json').write_text(json.dumps({
    'pages': [{} for _ in range(pages)],
    'items': [{'confidence': confidence}],
}))
assets = output / 'sample_artifacts'
assets.mkdir()
(assets / 'image.png').write_bytes(b'fixture-image')
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def main() -> int:
    suite = Suite()
    with tempfile.TemporaryDirectory(prefix="document-normalize-test-") as raw:
        root = Path(raw)
        cache = root / "cache"
        env = os.environ.copy()
        env["HOME"] = str(root / "home")
        env.pop("LLM_OBSIDIAN_DOCLING_CMD", None)
        env.pop("LLM_OBSIDIAN_DOCLING_VERSION", None)

        markdown = root / "notes.md"
        markdown.write_text("# Пример\n\nТекстовый документ без тяжёлого парсера.\n", encoding="utf-8")
        first, first_payload = run_normalizer(markdown, cache, env)
        suite.check("builtin markdown succeeds", first.returncode == 0, first.stderr)
        suite.check("builtin processor recorded", first_payload.get("processor", {}).get("processor") == "builtin")
        first_artifact = Path(str(first_payload.get("artifacts", {}).get("markdown", "")))
        suite.check("builtin artifact exists", first_artifact.is_file())

        second, second_payload = run_normalizer(markdown, cache, env)
        suite.check("unchanged source uses cache", second.returncode == 0 and second_payload.get("status") == "cached")
        suite.check("cache key is stable", first_payload.get("cache_key") == second_payload.get("cache_key"))

        short_text = root / "short.txt"
        short_text.write_text("ok\n", encoding="utf-8")
        short_result, short_payload = run_normalizer(short_text, cache, env)
        suite.check(
            "short text stays on usable fast path",
            short_result.returncode == 0 and short_payload.get("quality", {}).get("accepted") is True,
        )

        structured = root / "data.json"
        structured.write_text('{"b": 2, "a": "данные"}', encoding="utf-8")
        json_result, json_payload = run_normalizer(structured, cache, env)
        json_text = Path(str(json_payload.get("artifacts", {}).get("markdown", ""))).read_text(encoding="utf-8")
        suite.check("JSON is normalized", json_result.returncode == 0 and '"a": "данные"' in json_text)

        webpage = root / "page.html"
        webpage.write_text("<h1>Заголовок</h1><script>secret()</script><p>Useful text.</p>", encoding="utf-8")
        html_result, html_payload = run_normalizer(webpage, cache, env)
        html_text = Path(str(html_payload.get("artifacts", {}).get("markdown", ""))).read_text(encoding="utf-8")
        suite.check("local HTML is networkless-cleaned", html_result.returncode == 0 and "Useful text" in html_text and "secret" not in html_text)

        unknown = root / "archive.zip"
        unknown.write_bytes(b"zip")
        unsupported, unsupported_payload = run_normalizer(unknown, cache, env)
        suite.check("unknown binary fails closed", unsupported.returncode == 4 and unsupported_payload.get("status") == "unsupported")

        pdf = root / "sample.pdf"
        pdf.write_bytes(b"%PDF-1.4 fixture")
        missing, missing_payload = run_normalizer(pdf, cache, env)
        action = missing_payload.get("action", {})
        suite.check("missing Docling needs user action", missing.returncode == 2 and missing_payload.get("status") == "needs_user_action")
        suite.check("fallback is explicit", action.get("native_model_fallback_requires_confirmation") is True)
        suite.check("repair command is actionable", action.get("install_command") == "python3 scripts/install-docling.py install")

        fake = make_fake_docling(root)
        fake_log = root / "fake-log.json"
        docling_env = env.copy()
        docling_env.update(
            {
                "LLM_OBSIDIAN_DOCLING_CMD": str(fake),
                "LLM_OBSIDIAN_DOCLING_VERSION": "2.112.0",
                "FAKE_DOCLING_LOG": str(fake_log),
            }
        )
        converted, converted_payload = run_normalizer(pdf, cache, docling_env, "--force")
        log = json.loads(fake_log.read_text(encoding="utf-8"))
        suite.check("fake Docling conversion succeeds", converted.returncode == 0 and converted_payload.get("status") == "ok", converted.stderr)
        suite.check("ru/en OCR is explicit", "ru,en" in log["args"])
        suite.check("remote services disabled", "--no-enable-remote-services" in log["args"] and "--no-allow-external-plugins" in log["args"])
        suite.check("Docling process is offline-configured", log["offline"] == "1" and log["transformers_offline"] == "1")
        suite.check(
            "EasyOCR model path matches downloaded artifact layout",
            str(log["easyocr_module_path"]).endswith("/EasyOcr"),
        )
        artifact_root = Path(str(converted_payload.get("artifacts", {}).get("root", "")))
        suite.check("referenced assets are preserved", (artifact_root / "sample_artifacts" / "image.png").is_file())
        suite.check("raw Docling output is not duplicated", not (artifact_root / "docling-output").exists())

        low_env = docling_env.copy()
        low_env["FAKE_DOCLING_MODE"] = "low"
        low, low_payload = run_normalizer(pdf, root / "low-cache", low_env)
        suite.check("short Docling result is low quality", low.returncode == 3 and low_payload.get("status") == "low_quality")
        suite.check("low-quality artifact remains inspectable", Path(str(low_payload.get("artifacts", {}).get("markdown", ""))).is_file())

        confidence_env = docling_env.copy()
        confidence_env["FAKE_DOCLING_MODE"] = "confidence"
        confidence, confidence_payload = run_normalizer(pdf, root / "confidence-cache", confidence_env)
        suite.check("low confidence is rejected", confidence.returncode == 3 and confidence_payload.get("quality", {}).get("accepted") is False)

        pages_env = docling_env.copy()
        pages_env["FAKE_DOCLING_MODE"] = "pages"
        pages, pages_payload = run_normalizer(pdf, root / "pages-cache", pages_env, "--max-pages", "2")
        suite.check("page limit fails without truncation", pages.returncode == 4 and pages_payload.get("status") == "unsupported")

        too_large, too_large_payload = run_normalizer(pdf, root / "size-cache", docling_env, "--max-bytes", "4")
        suite.check("size limit fails before conversion", too_large.returncode == 4 and "configured limit" in str(too_large_payload.get("reason")))

        check = subprocess.run(
            [sys.executable, str(INSTALLER), "check", "--json"],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        check_payload = json.loads(check.stdout)
        suite.check("installer doctor reports missing runtime", check.returncode == 2 and check_payload.get("status") == "dependency_missing")

        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        fake_uv_log = root / "fake-uv.log"
        fake_uv = fake_bin / "uv"
        fake_uv.write_text(
            f"""#!{sys.executable}
import os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ['FAKE_UV_LOG']).open('a') as handle:
    handle.write(' '.join(args) + '\\n')
if args[0] == 'venv':
    target = pathlib.Path(args[-1]); (target / 'bin').mkdir(parents=True, exist_ok=True)
    python = target / 'bin' / 'python'
    python.write_text(\"\"\"#!{sys.executable}
import pathlib, sys
code = sys.argv[2]
if 'sys.version_info' in code:
    print('3.12')
elif 'import easyocr' in code:
    root = pathlib.Path(sys.argv[3]); root.mkdir(parents=True, exist_ok=True)
    (root / 'craft_mlt_25k.pth').write_bytes(b'x')
    (root / 'cyrillic_g2.pth').write_bytes(b'x')
elif (pathlib.Path(__file__).parent / 'docling-installed').exists():
    print('2.112.0')
else:
    raise SystemExit(1)
\"\"\")
    python.chmod(0o755)
elif args[:2] == ['pip', 'install']:
    python = pathlib.Path(args[args.index('--python') + 1])
    (python.parent / 'docling-installed').write_text('yes')
    tools = python.parent / 'docling-tools'
    tools.write_text(\"\"\"#!{sys.executable}
import pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(args[args.index('--output-dir') + 1])
for name in ('layout', 'tableformer', 'EasyOcr'):
    folder = root / name; folder.mkdir(parents=True, exist_ok=True); (folder / 'fixture.bin').write_bytes(b'x')
\"\"\")
    tools.chmod(0o755)
else:
    raise SystemExit(9)
""",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)
        install_env = env.copy()
        install_env.update(
            {
                "PATH": str(fake_bin) + os.pathsep + install_env.get("PATH", ""),
                "FAKE_UV_LOG": str(fake_uv_log),
                "LLM_OBSIDIAN_DOCLING_HOME": str(root / "installed-docling"),
            }
        )
        installed = subprocess.run(
            [sys.executable, str(INSTALLER), "install"],
            text=True,
            capture_output=True,
            check=False,
            env=install_env,
        )
        suite.check("installer provisions isolated runtime", installed.returncode == 0, installed.stdout + installed.stderr)
        installed_check = subprocess.run(
            [sys.executable, str(INSTALLER), "check", "--json"],
            text=True,
            capture_output=True,
            check=False,
            env=install_env,
        )
        installed_payload = json.loads(installed_check.stdout)
        suite.check(
            "installed runtime validates version/models/languages",
            installed_check.returncode == 0
            and installed_payload.get("actual_python_version") == "3.12"
            and installed_payload.get("actual_docling_version") == "2.112.0"
            and installed_payload.get("ocr_languages") == ["ru", "en"],
        )
        calls_before = fake_uv_log.read_text(encoding="utf-8")
        installed_again = subprocess.run(
            [sys.executable, str(INSTALLER), "install"],
            text=True,
            capture_output=True,
            check=False,
            env=install_env,
        )
        calls_after = fake_uv_log.read_text(encoding="utf-8")
        suite.check("installer is idempotent", installed_again.returncode == 0 and calls_before == calls_after)

    print(f"\n{suite.passed} passed, {suite.failed} failed")
    return 1 if suite.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
