#!/usr/bin/env python3
"""Offline Docling API adapter used by ``document-normalize.py``.

The adapter deliberately has a small file-to-file protocol so the coordinator
can enforce a wall-clock timeout and atomically publish the resulting cache.
It never accepts URLs, remote services, external plugins, or model downloads.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable


PDF_TEXT_THRESHOLD = 40
PDF_RASTER_THRESHOLD = 0.50


def rectangle_union_area(rectangles: Iterable[tuple[float, float, float, float]]) -> float:
    """Return the exact union area of axis-aligned rectangles."""
    rects = [
        (min(left, right), min(bottom, top), max(left, right), max(bottom, top))
        for left, bottom, right, top in rectangles
        if left != right and bottom != top
    ]
    xs = sorted({value for rect in rects for value in (rect[0], rect[2])})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        if right <= left:
            continue
        spans = sorted(
            (bottom, top)
            for x1, bottom, x2, top in rects
            if x1 < right and x2 > left
        )
        if not spans:
            continue
        covered = 0.0
        start, end = spans[0]
        for next_start, next_end in spans[1:]:
            if next_start > end:
                covered += end - start
                start, end = next_start, next_end
            else:
                end = max(end, next_end)
        covered += end - start
        area += (right - left) * covered
    return area


def contiguous_ranges(page_numbers: Iterable[int]) -> list[tuple[int, int]]:
    ordered = sorted(set(page_numbers))
    if not ordered:
        return []
    ranges: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page != previous + 1:
            ranges.append((start, previous))
            start = page
        previous = page
    ranges.append((start, previous))
    return ranges


def configure_offline_environment(models_path: Path) -> None:
    # These variables must be present before importing Docling/Transformers.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["DOCLING_ARTIFACTS_PATH"] = str(models_path)
    os.environ["EASYOCR_MODULE_PATH"] = str(models_path / "EasyOcr")


def docling_types() -> dict[str, Any]:
    from docling.backend.html_backend import HTMLBackendOptions
    from docling.backend.image_backend import ImageDocumentBackend
    from docling.datamodel.backend_options import PdfBackendOptions
    from docling.datamodel.base_models import ConversionStatus, InputFormat
    from docling.datamodel.pipeline_options import (
        ConvertPipelineOptions,
        EasyOcrOptions,
        PdfPipelineOptions,
        TableFormerMode,
    )
    from docling.document_converter import (
        DocumentConverter,
        EpubFormatOption,
        ExcelFormatOption,
        HTMLFormatOption,
        OdpFormatOption,
        OdsFormatOption,
        OdtFormatOption,
        PdfFormatOption,
        PowerpointFormatOption,
        WordFormatOption,
    )
    from docling_core.types.doc import ImageRefMode

    return {
        "HTMLBackendOptions": HTMLBackendOptions,
        "ImageDocumentBackend": ImageDocumentBackend,
        "PdfBackendOptions": PdfBackendOptions,
        "ConversionStatus": ConversionStatus,
        "InputFormat": InputFormat,
        "ConvertPipelineOptions": ConvertPipelineOptions,
        "EasyOcrOptions": EasyOcrOptions,
        "PdfPipelineOptions": PdfPipelineOptions,
        "TableFormerMode": TableFormerMode,
        "DocumentConverter": DocumentConverter,
        "EpubFormatOption": EpubFormatOption,
        "ExcelFormatOption": ExcelFormatOption,
        "HTMLFormatOption": HTMLFormatOption,
        "OdpFormatOption": OdpFormatOption,
        "OdsFormatOption": OdsFormatOption,
        "OdtFormatOption": OdtFormatOption,
        "PdfFormatOption": PdfFormatOption,
        "PowerpointFormatOption": PowerpointFormatOption,
        "WordFormatOption": WordFormatOption,
        "ImageRefMode": ImageRefMode,
    }


def pdf_options(types: dict[str, Any], args: argparse.Namespace, *, do_ocr: bool) -> Any:
    easyocr = types["EasyOcrOptions"](
        lang=args.ocr_languages.split(","),
        force_full_page_ocr=False,
        bitmap_area_threshold=0.05,
        download_enabled=False,
        model_storage_directory=str(args.models_path / "EasyOcr"),
    )
    options = types["PdfPipelineOptions"](
        allow_external_plugins=False,
        enable_remote_services=False,
        artifacts_path=args.models_path,
        do_ocr=do_ocr,
        ocr_options=easyocr,
        do_table_structure=True,
        document_timeout=args.timeout,
        generate_page_images=True,
        generate_picture_images=True,
        images_scale=2,
    )
    options.table_structure_options.mode = types["TableFormerMode"].ACCURATE
    options.table_structure_options.do_cell_matching = True
    return options


def converter(types: dict[str, Any], args: argparse.Namespace, *, do_ocr: bool) -> Any:
    simple = types["ConvertPipelineOptions"](
        artifacts_path=args.models_path,
        enable_remote_services=False,
        allow_external_plugins=False,
    )
    pdf = pdf_options(types, args, do_ocr=do_ocr)
    pdf_format = types["PdfFormatOption"](pipeline_options=pdf)
    image_format = types["PdfFormatOption"](
        pipeline_options=pdf,
        backend=types["ImageDocumentBackend"],
        backend_options=types["PdfBackendOptions"](),
    )
    formats = {
        types["InputFormat"].PDF: pdf_format,
        types["InputFormat"].IMAGE: image_format,
        types["InputFormat"].DOCX: types["WordFormatOption"](pipeline_options=simple),
        types["InputFormat"].PPTX: types["PowerpointFormatOption"](pipeline_options=simple),
        types["InputFormat"].XLSX: types["ExcelFormatOption"](pipeline_options=simple),
        types["InputFormat"].ODT: types["OdtFormatOption"](pipeline_options=simple),
        types["InputFormat"].ODP: types["OdpFormatOption"](pipeline_options=simple),
        types["InputFormat"].ODS: types["OdsFormatOption"](pipeline_options=simple),
        types["InputFormat"].HTML: types["HTMLFormatOption"](
            pipeline_options=simple,
            backend_options=types["HTMLBackendOptions"](
                fetch_images=False,
                enable_local_fetch=False,
                enable_remote_fetch=False,
            ),
        ),
        types["InputFormat"].EPUB: types["EpubFormatOption"](pipeline_options=simple),
    }
    return types["DocumentConverter"](format_options=formats)


def page_text_characters(document: Any) -> dict[int, int]:
    counts = {int(page): 0 for page in document.pages}
    for item in document.texts:
        text = "".join(str(item.text).split())
        for prov in item.prov:
            counts[int(prov.page_no)] = counts.get(int(prov.page_no), 0) + len(text)
            break
    return counts


def page_picture_coverage(document: Any) -> dict[int, float]:
    rectangles: dict[int, list[tuple[float, float, float, float]]] = {
        int(page): [] for page in document.pages
    }
    for picture in document.pictures:
        for prov in picture.prov:
            box = prov.bbox
            rectangles.setdefault(int(prov.page_no), []).append(
                (float(box.l), float(box.b), float(box.r), float(box.t))
            )
    coverage: dict[int, float] = {}
    for page_no, page in document.pages.items():
        page_area = float(page.size.width) * float(page.size.height)
        union = rectangle_union_area(rectangles.get(int(page_no), []))
        coverage[int(page_no)] = min(1.0, union / page_area) if page_area else 0.0
    return coverage


def page_ink_fraction(document: Any) -> dict[int, float]:
    """Estimate whether a textless rendered page is visually non-blank."""
    fractions: dict[int, float] = {}
    for page_no, page in document.pages.items():
        image_ref = getattr(page, "image", None)
        image = getattr(image_ref, "pil_image", None)
        if image is None:
            fractions[int(page_no)] = 0.0
            continue
        sample = image.convert("L")
        sample.thumbnail((256, 256))
        pixels = list(sample.getdata())
        fractions[int(page_no)] = (
            sum(1 for value in pixels if value < 245) / len(pixels) if pixels else 0.0
        )
    return fractions


def export_page(document: Any, page_no: int, output: Path, image_mode: Any) -> str:
    page_file = output / f".page-{page_no}.md"
    document.filter(page_nrs={page_no}).save_as_markdown(
        page_file,
        artifacts_dir=output / "artifacts",
        image_mode=image_mode,
    )
    text = page_file.read_text(encoding="utf-8").strip()
    page_file.unlink()
    return text


def save_json(document: Any, output: Path, image_mode: Any) -> None:
    # Page images are intentionally omitted; Markdown owns the referenced
    # picture artifacts while JSON preserves structure and coordinates.
    document.save_as_json(output / "document.json", image_mode=image_mode)


def require_conversion_success(result: Any, success_status: Any, stage: str) -> None:
    """Reject partial Docling results instead of publishing truncated output."""
    status = getattr(result, "status", None)
    if status == success_status:
        return
    value = getattr(status, "value", status)
    raise RuntimeError(
        f"Docling {stage} returned conversion status {value!r}; "
        "refusing partial output"
    )


def convert_pdf(types: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    native = converter(types, args, do_ocr=False).convert(
        args.source,
        max_num_pages=args.max_pages,
        max_file_size=args.max_bytes,
    )
    require_conversion_success(
        native,
        types["ConversionStatus"].SUCCESS,
        "native PDF pass",
    )
    native_document = native.document
    characters = page_text_characters(native_document)
    coverage = page_picture_coverage(native_document)
    ink = page_ink_fraction(native_document)
    candidates = sorted(
        page
        for page in native_document.pages
        if characters.get(int(page), 0) < args.pdf_text_threshold
        and (
            coverage.get(int(page), 0.0) >= args.pdf_raster_threshold
            or ink.get(int(page), 0.0) >= 0.01
        )
    )
    ranges = contiguous_ranges(int(page) for page in candidates)
    ocr_pages: dict[int, Any] = {}
    if ranges:
        ocr_converter = converter(types, args, do_ocr=True)
        for start, end in ranges:
            result = ocr_converter.convert(
                args.source,
                max_num_pages=args.max_pages,
                max_file_size=args.max_bytes,
                page_range=(start, end),
            )
            require_conversion_success(
                result,
                types["ConversionStatus"].SUCCESS,
                f"OCR PDF pass for pages {start}-{end}",
            )
            for page in result.document.pages:
                ocr_pages[int(page)] = result.document

    image_mode = types["ImageRefMode"].REFERENCED
    page_markdown: list[str] = []
    page_metrics: list[dict[str, Any]] = []
    for page in sorted(int(value) for value in native_document.pages):
        selected = ocr_pages.get(page, native_document)
        exported = export_page(selected, page, args.output, image_mode)
        page_markdown.append(f"<!-- llm-obsidian-page: {page} -->\n\n{exported}".rstrip())
        chars = characters.get(page, 0)
        raster = coverage.get(page, 0.0)
        ink_fraction = ink.get(page, 0.0)
        mode = "ocr" if page in ocr_pages else "native"
        if chars == 0 and raster == 0.0 and ink_fraction < 0.002:
            mode = "blank"
        elif chars < args.pdf_text_threshold and page not in ocr_pages:
            mode = "low_text"
        page_metrics.append(
            {
                "page": page,
                "mode": mode,
                "native_nonspace_characters": chars,
                "picture_union_ratio": round(raster, 6),
                "rendered_ink_fraction": round(ink_fraction, 6),
            }
        )

    (args.output / "document.md").write_text(
        "\n\n".join(part for part in page_markdown if part).strip() + "\n",
        encoding="utf-8",
    )
    save_json(native_document, args.output, types["ImageRefMode"].PLACEHOLDER)
    return {
        "version": 1,
        "kind": "pdf-text-first",
        "pages": len(native_document.pages),
        "ocr_pages": candidates,
        "ocr_ranges": [list(value) for value in ranges],
        "page_metrics": page_metrics,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def convert_other(types: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    do_ocr = args.source.suffix.lower() in {
        ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"
    }
    result = converter(types, args, do_ocr=do_ocr).convert(
        args.source,
        max_num_pages=args.max_pages,
        max_file_size=args.max_bytes,
    )
    require_conversion_success(
        result,
        types["ConversionStatus"].SUCCESS,
        "document pass",
    )
    result.document.save_as_markdown(
        args.output / "document.md",
        artifacts_dir=args.output / "artifacts",
        image_mode=types["ImageRefMode"].REFERENCED,
    )
    save_json(result.document, args.output, types["ImageRefMode"].PLACEHOLDER)
    return {
        "version": 1,
        "kind": "image-ocr" if do_ocr else "standard",
        "pages": len(result.document.pages),
        "ocr_pages": sorted(int(value) for value in result.document.pages) if do_ocr else [],
        "ocr_ranges": [],
        "page_metrics": [],
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def ratio(value: str) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models-path", type=Path, required=True)
    parser.add_argument("--ocr-languages", default="ru,en")
    parser.add_argument("--timeout", type=positive_int, required=True)
    parser.add_argument("--max-pages", type=positive_int, required=True)
    parser.add_argument("--max-bytes", type=positive_int, required=True)
    parser.add_argument("--pdf-text-threshold", type=positive_int, default=PDF_TEXT_THRESHOLD)
    parser.add_argument("--pdf-raster-threshold", type=ratio, default=PDF_RASTER_THRESHOLD)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.source = args.source.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    args.models_path = args.models_path.expanduser().resolve()
    if not args.source.is_file():
        raise SystemExit("source must be an existing local file")
    args.output.mkdir(parents=True, exist_ok=True)
    configure_offline_environment(args.models_path)
    types = docling_types()
    metadata = (
        convert_pdf(types, args)
        if args.source.suffix.lower() == ".pdf"
        else convert_other(types, args)
    )
    metadata["offline"] = True
    metadata["remote_services_allowed"] = False
    metadata["external_plugins_allowed"] = False
    metadata["ocr_languages"] = args.ocr_languages.split(",")
    (args.output / "adapter.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "ok", "metadata": "adapter.json"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
