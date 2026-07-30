#!/usr/bin/env python3
"""Hermetic tests for the local document normalization pipeline."""

from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
NORMALIZER = ROOT / "scripts" / "document-normalize.py"
INSTALLER = ROOT / "scripts" / "install-docling.py"
ADAPTER = ROOT / "scripts" / "docling-adapter.py"


def load_adapter() -> object:
    spec = importlib.util.spec_from_file_location("docling_adapter", ADAPTER)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Docling adapter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    script = root / "fake-docling-adapter"
    script.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if os.environ.get('FAKE_DOCLING_MODE') == 'fail':
    print('fixture conversion failure', file=sys.stderr)
    raise SystemExit(9)
output = pathlib.Path(args[args.index('--output') + 1])
source = pathlib.Path(args[args.index('--source') + 1])
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
image = output / 'sample_artifacts' / 'image.png'
if mode == 'low':
    text = 'x'
elif mode == 'repair':
    text = ('Обычный длинный контекст без дефектов. ' * 20) + '\\n\\nФраза . ТРЕНИРУЕМ ТОРМОЖЕНИЕ Следующий текст для проверки ремонта .\\n'
elif mode == 'defects':
    text = 'Это достаточно длинный текст , который продолжается с высокой\\n\\nскорости .\\n'
elif mode == 'overcap':
    text = ('Обычный длинный контекст без дефектов. ' * 80) + '\\n\\n' + '\\n\\n'.join(
        f'Фраза. ЗАГОЛОВОК НОМЕР {index} Следующий текст.' for index in range(1, 23)
    )
else:
    text = f'# Документ\\n\\nРусский and English text from Docling.\\n\\n![]({image.resolve()})\\n'
(output / 'document.md').write_text(text)
pages = 3 if mode == 'pages' else 1
confidence = 0.2 if mode == 'confidence' else 0.95
(output / 'document.json').write_text(json.dumps({
    'pages': [{} for _ in range(pages)],
    'items': [{'confidence': confidence}],
    'pictures': [{'uri': str(image.resolve())}],
}))
(output / 'adapter.json').write_text(json.dumps({
    'version': 1,
    'kind': 'pdf-text-first',
    'pages': pages,
    'ocr_pages': [],
    'ocr_ranges': [],
    'page_metrics': [],
    'offline': True,
    'remote_services_allowed': False,
    'external_plugins_allowed': False,
    'ocr_languages': ['ru', 'en'],
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
    adapter = load_adapter()
    suite.check(
        "picture coverage uses rectangle union",
        adapter.rectangle_union_area([(0, 0, 8, 10), (2, 0, 10, 10)]) == 100,
    )
    suite.check(
        "selective OCR ranges retain one-based inclusive pages",
        adapter.contiguous_ranges([1, 2, 4, 7, 8, 9]) == [(1, 2), (4, 4), (7, 9)],
    )

    class FakeConversionResult:
        def __init__(self, status: str) -> None:
            self.status = status

    adapter.require_conversion_success(
        FakeConversionResult("success"), "success", "test pass"
    )
    partial_rejected = False
    try:
        adapter.require_conversion_success(
            FakeConversionResult("partial_success"), "success", "test pass"
        )
    except RuntimeError as exc:
        partial_rejected = "refusing partial output" in str(exc)
    suite.check("partial Docling conversion fails closed", partial_rejected)
    normalizer_spec = importlib.util.spec_from_file_location("document_normalizer", NORMALIZER)
    if normalizer_spec is None or normalizer_spec.loader is None:
        raise RuntimeError("could not load document normalizer")
    normalizer = importlib.util.module_from_spec(normalizer_spec)
    normalizer_spec.loader.exec_module(normalizer)
    boundary_fixture = "Высокая\n\nскорость .\n\n![Фото](image.png)\n\nследующая подпись"
    boundary_clean = normalizer.deterministic_cleanup(boundary_fixture)
    suite.check("safe lowercase paragraph continuation joins", "Высокая скорость." in boundary_clean)
    suite.check("cleanup never joins across pictures", "image.png)\n\nследующая" in boundary_clean)
    fenced_fixture = "До блока\n\n```python\nvalue = 1\n\nother = value - 1\n```\n\nпосле блока\n"
    fenced_clean = normalizer.deterministic_cleanup(fenced_fixture)
    suite.check(
        "cleanup preserves fenced code exactly",
        "```python\nvalue = 1\n\nother = value - 1\n```" in fenced_clean
        and "```\n\nпосле" in fenced_clean,
        fenced_clean,
    )
    token_fixture = (
        "Порог -5, команда docling -v и обычное тире - между словами.\n\n"
        "| Метрика | Значение |\n| --- | --- |\n| температура | -0.5 |"
    )
    token_clean = normalizer.deterministic_cleanup(token_fixture)
    suite.check(
        "cleanup preserves negative numbers, flags, and spaced table cells",
        "Порог -5" in token_clean
        and "docling -v" in token_clean
        and "| температура | -0.5 |" in token_clean
        and "тире — между" in token_clean,
        token_clean,
    )
    table_fixture = (
        "| Поле | Шаги |\n"
        "| --- | --- |\n"
        "| запуск | 1. включить 2. прогреть 3. выключить |"
    )
    table_clean = normalizer.deterministic_cleanup(table_fixture)
    suite.check(
        "inline numbered-list repair leaves Markdown tables intact",
        table_clean.strip() == table_fixture,
        table_clean,
    )
    suite.check(
        "technical ru/en prose is not mixed-script noise",
        normalizer.suspicious_mixed_words("LLM модель и Docling pipeline") == [],
    )
    suite.check(
        "hyphenated ru/en technical compounds are not mixed-script noise",
        normalizer.suspicious_mixed_words(
            "PDF-файл, Web-страница, 3D-модель и IT-отдел"
        )
        == [],
    )
    suite.check(
        "adjacent Latin/Cyrillic glyph confusion remains detectable",
        normalizer.suspicious_mixed_words("Cиcтема") == ["Cиcтема"],
    )
    first_paragraph_issues = normalizer.quality_issues(
        "Начало 1. включить 2. прогреть 3. выключить", None
    )
    suite.check(
        "first-paragraph repair context retains its first character",
        bool(first_paragraph_issues)
        and first_paragraph_issues[0]["text"].startswith("Начало"),
        repr(first_paragraph_issues),
    )
    repeated_defects = normalizer.quality_issues(
        "Первый дефект � здесь.\n\nВторой дефект � здесь.", None
    )
    suite.check(
        "repeated replacement characters keep distinct repair sites",
        len(repeated_defects) == 2
        and len({item["segment_id"] for item in repeated_defects}) == 2,
        repr(repeated_defects),
    )
    many_defects = normalizer.quality_issues(
        "\n\n".join(f"Абзац {index} содержит �." for index in range(25)), None
    )
    many_bundle, many_over_cap = normalizer.repair_bundle(
        "source", "clean", "обычный текст " * 5_000, many_defects
    )
    suite.check(
        "repair cap counts every repeated defect occurrence",
        many_over_cap
        and many_bundle is not None
        and len(many_bundle["segments"]) == normalizer.MAX_REPAIR_SEGMENTS
        and normalizer.issue_counts(many_defects) == {"replacement_character": 25},
    )
    with tempfile.TemporaryDirectory(prefix="document-normalize-test-") as raw:
        root = Path(raw)
        cache = root / "cache"
        env = os.environ.copy()
        env["HOME"] = str(root / "home")
        env.pop("LLM_OBSIDIAN_DOCLING_ADAPTER", None)
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
                "LLM_OBSIDIAN_DOCLING_ADAPTER": str(fake),
                "LLM_OBSIDIAN_DOCLING_VERSION": "2.112.0",
                "FAKE_DOCLING_LOG": str(fake_log),
            }
        )
        converted, converted_payload = run_normalizer(pdf, cache, docling_env, "--force")
        log = json.loads(fake_log.read_text(encoding="utf-8"))
        suite.check("fake Docling conversion succeeds", converted.returncode == 0 and converted_payload.get("status") == "ok", converted.stderr)
        suite.check("ru/en OCR is explicit", "ru,en" in log["args"])
        adapter_path = Path(str(converted_payload.get("artifacts", {}).get("adapter_metadata", "")))
        adapter_metadata = json.loads(adapter_path.read_text(encoding="utf-8"))
        suite.check(
            "remote services disabled",
            adapter_metadata["remote_services_allowed"] is False
            and adapter_metadata["external_plugins_allowed"] is False,
        )
        suite.check("Docling process is offline-configured", log["offline"] == "1" and log["transformers_offline"] == "1")
        suite.check(
            "EasyOCR model path matches downloaded artifact layout",
            str(log["easyocr_module_path"]).endswith("/EasyOcr"),
        )
        artifact_root = Path(str(converted_payload.get("artifacts", {}).get("root", "")))
        stable_markdown = (artifact_root / "document.md").read_text(encoding="utf-8")
        stable_json = (artifact_root / "document.docling.json").read_text(encoding="utf-8")
        suite.check("referenced assets are preserved", (artifact_root / "sample_artifacts" / "image.png").is_file())
        suite.check(
            "artifact references survive atomic cache move",
            "![](sample_artifacts/image.png)" in stable_markdown
            and "docling-output" not in stable_markdown
            and "docling-output" not in stable_json,
        )
        suite.check("raw Docling output is not duplicated", not (artifact_root / "docling-output").exists())
        suite.check("raw and cleaned Markdown are explicit", (artifact_root / "document.raw.md").is_file())

        defects_env = docling_env.copy()
        defects_env["FAKE_DOCLING_MODE"] = "defects"
        defects, defects_payload = run_normalizer(pdf, root / "defects-cache", defects_env)
        defects_text = Path(str(defects_payload.get("artifacts", {}).get("markdown", ""))).read_text(encoding="utf-8")
        suite.check(
            "deterministic cleanup avoids model work",
            defects.returncode == 0
            and "текст," in defects_text
            and "высокой скорости." in defects_text,
            defects_text,
        )

        repair_env = docling_env.copy()
        repair_env["FAKE_DOCLING_MODE"] = "repair"
        repair, repair_payload = run_normalizer(pdf, root / "repair-cache", repair_env)
        suite.check(
            "bounded semantic cleanup has a distinct status",
            repair.returncode == 5
            and repair_payload.get("status") == "needs_semantic_cleanup"
            and Path(str(repair_payload.get("artifacts", {}).get("repair_bundle", ""))).is_file(),
            repair.stderr,
        )
        repair_cached, repair_cached_payload = run_normalizer(
            pdf, root / "repair-cache", repair_env
        )
        suite.check(
            "cached semantic cleanup keeps its typed exit",
            repair_cached.returncode == 5
            and repair_cached_payload.get("status") == "needs_semantic_cleanup"
            and repair_cached_payload.get("cached") is True,
        )

        overcap_env = docling_env.copy()
        overcap_env["FAKE_DOCLING_MODE"] = "overcap"
        overcap, overcap_payload = run_normalizer(pdf, root / "overcap-cache", overcap_env)
        suite.check(
            "oversized semantic cleanup requires user action",
            overcap.returncode == 6 and overcap_payload.get("status") == "needs_user_action",
            overcap.stderr,
        )
        overcap_quality = overcap_payload.get("quality", {})
        suite.check(
            "manifest caps issue detail and preserves exact counts",
            overcap_quality.get("issues_total", 0) > normalizer.MAX_REPAIR_SEGMENTS
            and len(overcap_quality.get("issues", []))
            == normalizer.MAX_REPAIR_SEGMENTS
            and overcap_quality.get("issues_truncated") is True
            and overcap_quality.get("issue_counts", {}).get("probable_heading", 0)
            > normalizer.MAX_REPAIR_SEGMENTS,
            repr(overcap_quality),
        )

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
