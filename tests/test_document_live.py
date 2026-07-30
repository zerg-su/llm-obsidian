#!/usr/bin/env python3
"""Live acceptance for the installed Docling ru/en document pipeline.

This suite is intentionally not part of ``make test``: it loads the real local
models and can take several minutes on a cold machine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib


ROOT = Path(__file__).resolve().parents[1]
NORMALIZER = ROOT / "scripts" / "document-normalize.py"
VERSION = "2.112.0"


def write_pdf(path: Path, pages: int) -> None:
    objects: dict[int, bytes] = {}
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    page_ids = [4 + index * 2 for index in range(pages)]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode()
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for index, page_id in enumerate(page_ids, start=1):
        content_id = page_id + 1
        message = f"Docling English PDF acceptance page {index} of {pages}."
        stream = f"BT /F1 16 Tf 72 720 Td ({message}) Tj ET".encode()
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    max_id = max(objects)
    for object_id in range(1, max_id + 1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode())
        output.extend(objects[object_id])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {max_id + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(bytes(output))


def write_digital_pdf_with_text_image(path: Path) -> None:
    raw_image = path.with_suffix(".rgb")
    code = """from PIL import Image, ImageDraw, ImageFont
import sys
image = Image.new('RGB', (600, 200), 'white')
draw = ImageDraw.Draw(image)
font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', 52)
draw.text((20, 60), 'NEXT HILES 55 MPH', fill='black', font=font)
open(sys.argv[1], 'wb').write(image.tobytes())
"""
    subprocess.run(
        [str(docling_home() / "venv" / "bin" / "python"), "-c", code, str(raw_image)],
        check=True,
    )
    image_stream = zlib.compress(raw_image.read_bytes())
    raw_image.unlink()
    message = "Native digital body text stays authoritative and contains more than forty characters."
    content = (
        f"BT /F1 16 Tf 72 720 Td ({message}) Tj ET\n"
        "q 360 0 0 120 72 500 cm /Im1 Do Q"
    ).encode()
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> /XObject << /Im1 6 0 R >> >> "
            b"/Contents 5 0 R >>"
        ),
        5: b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        6: (
            b"<< /Type /XObject /Subtype /Image /Width 600 /Height 200 "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
            + b"/Length %d >>\nstream\n" % len(image_stream)
            + image_stream
            + b"\nendstream"
        ),
    }
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id in range(1, 7):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode())
        output.extend(objects[object_id])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(b"xref\n0 7\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(output)


def write_docx(path: Path) -> None:
    code = """from docx import Document
import sys
document = Document()
document.add_heading('Русский DOCX и English document', level=1)
document.add_paragraph('Таблица проверяет сохранение структуры.')
table = document.add_table(rows=2, cols=2)
table.cell(0, 0).text = 'Ключ'
table.cell(0, 1).text = 'Значение'
table.cell(1, 0).text = 'language'
table.cell(1, 1).text = 'ru,en'
document.save(sys.argv[1])
"""
    result = subprocess.run(
        [str(docling_home() / "venv" / "bin" / "python"), "-c", code, str(path)],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("could not generate the DOCX fixture")


def write_pptx(path: Path) -> None:
    files = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>
 <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
 <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
 <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
 <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
 <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
</Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>""",
        "ppt/presentation.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst><p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst><p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>""",
        "ppt/_rels/presentation.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>""",
        "ppt/slides/slide1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/><p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="ru-RU"/><a:t>Русский PPTX и English slide</a:t></a:r><a:endParaRPr lang="ru-RU"/></a:p></p:txBody></p:sp></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>""",
        "ppt/slides/_rels/slide1.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>""",
        "ppt/slideMasters/slideMaster1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld><p:clrMap accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" bg1="lt1" bg2="lt2" folHlink="folHlink" hlink="hlink" tx1="dk1" tx2="dk2"/><p:sldLayoutIdLst><p:sldLayoutId id="1" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>""",
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>""",
        "ppt/slideLayouts/slideLayout1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank"><p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>""",
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>""",
        "ppt/theme/theme1.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Acceptance"><a:themeElements><a:clrScheme name="Default"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="000000"/></a:dk2><a:lt2><a:srgbClr val="FFFFFF"/></a:lt2><a:accent1><a:srgbClr val="4472C4"/></a:accent1><a:accent2><a:srgbClr val="ED7D31"/></a:accent2><a:accent3><a:srgbClr val="A5A5A5"/></a:accent3><a:accent4><a:srgbClr val="FFC000"/></a:accent4><a:accent5><a:srgbClr val="5B9BD5"/></a:accent5><a:accent6><a:srgbClr val="70AD47"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme><a:fontScheme name="Default"><a:majorFont><a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Arial"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme><a:fmtScheme name="Default"><a:fillStyleLst/><a:lnStyleLst/><a:effectStyleLst/><a:bgFillStyleLst/></a:fmtScheme></a:themeElements></a:theme>""",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def docling_home() -> Path:
    override = os.environ.get("LLM_OBSIDIAN_DOCLING_HOME", "")
    return Path(override).expanduser() if override else Path.home() / ".local" / "share" / "llm-obsidian" / "docling" / VERSION


def write_scan(path: Path) -> None:
    python = docling_home() / "venv" / "bin" / "python"
    fonts = [
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ]
    font = next((item for item in fonts if item.is_file()), None)
    if font is None:
        raise RuntimeError("no macOS font with Cyrillic support found")
    code = """from PIL import Image, ImageDraw, ImageFont
import sys
image = Image.new('RGB', (1800, 500), 'white')
draw = ImageDraw.Draw(image)
font = ImageFont.truetype(sys.argv[2], 64)
draw.text((60, 80), 'Русский OCR документ', fill='black', font=font)
draw.text((60, 220), 'English OCR document', fill='black', font=font)
image.save(sys.argv[1])
"""
    result = subprocess.run([str(python), "-c", code, str(path), str(font)], check=False)
    if result.returncode != 0:
        raise RuntimeError("could not generate the ru/en scan fixture")


def image_to_pdf(image: Path, pdf: Path) -> None:
    code = """from PIL import Image
import sys
with Image.open(sys.argv[1]) as image:
    image.convert('RGB').save(sys.argv[2], 'PDF', resolution=150)
"""
    result = subprocess.run(
        [str(docling_home() / "venv" / "bin" / "python"), "-c", code, str(image), str(pdf)],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("could not generate the scanned PDF fixture")


def normalize(source: Path, cache: Path) -> tuple[dict[str, object], float]:
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            str(NORMALIZER),
            "normalize",
            str(source),
            "--cache-root",
            str(cache),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    elapsed = time.monotonic() - started
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{source.name}: invalid normalizer output: {result.stdout} {result.stderr}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{source.name}: {json.dumps(payload, ensure_ascii=False)}")
    return payload, elapsed


def artifact_text(payload: dict[str, object]) -> str:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise RuntimeError("missing artifacts")
    return Path(str(artifacts["markdown"])).read_text(encoding="utf-8")


def main() -> int:
    check = subprocess.run(
        [sys.executable, str(NORMALIZER), "check", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if check.returncode != 0:
        print(check.stdout.strip() or check.stderr.strip(), file=sys.stderr)
        print("Run: python3 scripts/install-docling.py install", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="docling-live-") as raw:
        root = Path(raw)
        cache = root / "cache"
        pdf = root / "multi-page.pdf"
        docx = root / "bilingual-table.docx"
        pptx = root / "bilingual-slide.pptx"
        scan = root / "bilingual-scan.png"
        scan_pdf = root / "bilingual-scan.pdf"
        digital_with_image = root / "digital-with-text-image.pdf"
        write_pdf(pdf, 24)
        write_docx(docx)
        write_pptx(pptx)
        write_scan(scan)
        image_to_pdf(scan, scan_pdf)
        write_digital_pdf_with_text_image(digital_with_image)

        checks = [
            (pdf, ["Docling English PDF acceptance page"]),
            (docx, ["Русский DOCX", "language", "ru,en"]),
            (pptx, ["Русский PPTX"]),
            (scan, ["English OCR document"]),
            (scan_pdf, ["English OCR document"]),
            (digital_with_image, ["Native digital body text"]),
        ]
        payloads: dict[str, dict[str, object]] = {}
        for source, expected_values in checks:
            payload, elapsed = normalize(source, cache)
            payloads[source.name] = payload
            text = artifact_text(payload)
            missing = [value for value in expected_values if value not in text]
            if missing:
                raise RuntimeError(
                    f"{source.name}: expected {missing!r} not found in {text[:500]!r}"
                )
            if source == scan and re.search(r"[А-Яа-яЁё]", text) is None:
                raise RuntimeError("scan: Russian OCR output is missing")
            print(f"OK {source.name}: {elapsed:.2f}s, {len(text)} chars")

        pages = payloads[pdf.name].get("quality", {}).get("pages")  # type: ignore[union-attr]
        if pages != 24:
            raise RuntimeError(f"PDF page provenance mismatch: expected 24, got {pages}")

        scan_pdf_artifacts = payloads[scan_pdf.name].get("artifacts")
        if not isinstance(scan_pdf_artifacts, dict):
            raise RuntimeError("scanned PDF adapter metadata is missing")
        scan_pdf_meta = json.loads(
            Path(str(scan_pdf_artifacts["adapter_metadata"])).read_text(encoding="utf-8")
        )
        if scan_pdf_meta.get("ocr_pages") != [1] or scan_pdf_meta.get("ocr_ranges") != [[1, 1]]:
            raise RuntimeError(f"scanned PDF did not use selective OCR: {scan_pdf_meta}")

        image_pdf_text = artifact_text(payloads[digital_with_image.name])
        image_pdf_artifacts = payloads[digital_with_image.name].get("artifacts")
        if not isinstance(image_pdf_artifacts, dict):
            raise RuntimeError("digital image PDF metadata is missing")
        image_pdf_meta = json.loads(
            Path(str(image_pdf_artifacts["adapter_metadata"])).read_text(encoding="utf-8")
        )
        if image_pdf_meta.get("ocr_pages") or re.search(r"NEXT|HILES|MPH", image_pdf_text):
            raise RuntimeError("text inside a digital PDF picture contaminated the body")

        repeated, elapsed = normalize(pdf, cache)
        if repeated.get("status") != "cached":
            raise RuntimeError("repeat conversion did not hit the content-addressed cache")
        print(f"OK cached repeat: {elapsed:.3f}s")

    real_document = os.environ.get("LLM_OBSIDIAN_REAL_DOCUMENT", "").strip()
    if real_document:
        real_source = Path(real_document).expanduser().resolve()
        with tempfile.TemporaryDirectory(prefix="docling-real-") as raw:
            cache = Path(raw) / "cache"
            result = subprocess.run(
                [
                    sys.executable,
                    str(NORMALIZER),
                    "normalize",
                    str(real_source),
                    "--cache-root",
                    str(cache),
                    "--json",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            payload = json.loads(result.stdout)
            if result.returncode not in {0, 5}:
                raise RuntimeError(f"real document failed: {payload}")
            text = artifact_text(payload)
            if payload.get("quality", {}).get("pages") != 68:  # type: ignore[union-attr]
                raise RuntimeError("real document page count changed")
            if re.search(r"NEXT|HILES|M:₽H", text, flags=re.IGNORECASE):
                raise RuntimeError("real document contains picture OCR contamination")
            if re.search(r"[ \t][,.;:!?]", text):
                raise RuntimeError("real document still contains punctuation-spacing defects")
            visible_text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
            visible_text = re.sub(r"\s+", " ", visible_text)
            if "с высокой скорости." not in visible_text:
                raise RuntimeError("real document still contains the known false page break")
            print(f"OK real document: {len(text)} chars, status={payload.get('status')}")

    print("Live Docling acceptance passed: PDF, selective scan OCR, Office, ru/en OCR, offline profile, cache.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
