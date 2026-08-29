from __future__ import annotations

import html
import json
import re
import tempfile
import uuid
from pathlib import Path

import pymupdf
from mcp.server.fastmcp import FastMCP

JOBS_DIR = Path(tempfile.gettempdir()) / "pdf-layout-translator-mcp"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
STYLE_TOKEN_RE = re.compile(r"\[\[(/?)(B|I)\]\]")
ANY_STYLE_TOKEN_RE = re.compile(r"\[\[/?(?:B|I)[^\]]*\]\]")

mcp = FastMCP(
    "PDF Layout Translator",
    instructions=(
        "Translate text-based PDFs with the active model. Call prepare_translation, then repeatedly "
        "call get_translation_batch and submit_translations until remaining is zero, then call "
        "render_translated_pdf. Translate every segment exactly once and preserve IDs and inline "
        "style markers."
    ),
)


def _job_path(job_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(job_id))
    except ValueError as exc:
        raise ValueError("Invalid translation job ID.") from exc
    return JOBS_DIR / f"{normalized}.json"


def _load_job(job_id: str) -> dict:
    path = _job_path(job_id)
    if not path.exists():
        raise ValueError(f"Translation job not found: {job_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_job(job: dict) -> None:
    _job_path(job["job_id"]).write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _default_output(input_path: Path, target_language: str) -> Path:
    safe_language = "".join(c if c.isalnum() or c in "-_" else "-" for c in target_language)
    safe_language = safe_language.strip("-") or "translated"
    return input_path.with_name(f"{input_path.stem}-{safe_language}{input_path.suffix}")


def _has_style(span: dict, flag: int, name: str) -> bool:
    return bool(
        int(span.get("flags", 0)) & flag
        or name in str(span.get("font", "")).casefold()
    )


def _style_markup(spans: list[dict], base_bold: bool, base_italic: bool) -> str:
    runs: list[tuple[str, bool, bool]] = []
    for span in spans:
        text = str(span.get("text", ""))
        bold = _has_style(span, pymupdf.TEXT_FONT_BOLD, "bold")
        italic = _has_style(span, pymupdf.TEXT_FONT_ITALIC, "italic")
        if runs and runs[-1][1:] == (bold, italic):
            previous = runs.pop()
            runs.append((previous[0] + text, bold, italic))
        else:
            runs.append((text, bold, italic))

    marked: list[str] = []
    for text, bold, italic in runs:
        core = text.strip()
        if not core:
            marked.append(text)
            continue
        if not any(character.isalnum() for character in core):
            bold = base_bold
            italic = base_italic
        leading = text[: len(text) - len(text.lstrip())]
        trailing = text[len(text.rstrip()) :]
        opened = ("[[B]]" if bold and not base_bold else "") + (
            "[[I]]" if italic and not base_italic else ""
        )
        closed = ("[[/I]]" if italic and not base_italic else "") + (
            "[[/B]]" if bold and not base_bold else ""
        )
        marked.append(f"{leading}{opened}{core}{closed}{trailing}")
    return "".join(marked).strip()


def _group_style_markup(group: list[dict], base_bold: bool, base_italic: bool) -> str:
    spans: list[dict] = []
    for index, item in enumerate(group):
        current = item["line"].get("spans", [])
        if index:
            previous = group[index - 1]["line"].get("spans", [])
            previous = [span for span in previous if span.get("text", "").strip()]
            following = [span for span in current if span.get("text", "").strip()]
            flags = 0
            if previous and following:
                if _has_style(previous[-1], pymupdf.TEXT_FONT_BOLD, "bold") and _has_style(
                    following[0], pymupdf.TEXT_FONT_BOLD, "bold"
                ):
                    flags |= pymupdf.TEXT_FONT_BOLD
                if _has_style(previous[-1], pymupdf.TEXT_FONT_ITALIC, "italic") and _has_style(
                    following[0], pymupdf.TEXT_FONT_ITALIC, "italic"
                ):
                    flags |= pymupdf.TEXT_FONT_ITALIC
            spans.append({"text": "\n", "flags": flags})
        spans.extend(current)
    return re.sub(r"[ \t]+\n", "\n", _style_markup(spans, base_bold, base_italic))


def _validate_style_markup(translated: str, source_markup: str) -> None:
    expected = [match.group(0) for match in STYLE_TOKEN_RE.finditer(source_markup)]
    actual = [match.group(0) for match in STYLE_TOKEN_RE.finditer(translated)]
    unknown = [token for token in ANY_STYLE_TOKEN_RE.findall(translated) if token not in actual]
    if unknown or sorted(actual) != sorted(expected):
        raise ValueError(
            "Translation must preserve every [[B]], [[/B]], [[I]], and [[/I]] marker exactly."
        )
    stack: list[str] = []
    for match in STYLE_TOKEN_RE.finditer(translated):
        closing, style = match.groups()
        if not closing:
            stack.append(style)
        elif not stack or stack.pop() != style:
            raise ValueError("Translation contains incorrectly nested style markers.")
    if stack:
        raise ValueError("Translation contains unclosed style markers.")


def _markup_to_html(text: str) -> str:
    rendered = html.escape(text)
    for marker, tag in (
        ("[[B]]", "<b>"),
        ("[[/B]]", "</b>"),
        ("[[I]]", "<i>"),
        ("[[/I]]", "</i>"),
    ):
        rendered = rendered.replace(marker, tag)
    return rendered.replace("\n", "<br>")


def _split_text_block(block: dict, page_width: float) -> list[dict]:
    """Split a PDF text block at blank lines so unrelated styles do not leak."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    for line in block.get("lines", []):
        text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
        if text:
            current.append({"line": line, "text": text})
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    segments: list[dict] = []
    for group in groups:
        rows: list[list[dict]] = []
        for item in group:
            bbox = item["line"]["bbox"]
            if rows:
                previous = rows[-1][0]["line"]["bbox"]
                overlap = min(previous[3], bbox[3]) - max(previous[1], bbox[1])
                same_row = overlap > min(previous[3] - previous[1], bbox[3] - bbox[1]) / 2
            else:
                same_row = False
            if same_row:
                rows[-1].append(item)
            else:
                rows.append([item])

        merged_group: list[dict] = []
        for row in rows:
            row.sort(key=lambda item: item["line"]["bbox"][0])
            row_bboxes = [item["line"]["bbox"] for item in row]
            merged_group.append(
                {
                    "text": " ".join(item["text"] for item in row),
                    "line": {
                        "bbox": [
                            min(rect[0] for rect in row_bboxes),
                            min(rect[1] for rect in row_bboxes),
                            max(rect[2] for rect in row_bboxes),
                            max(rect[3] for rect in row_bboxes),
                        ],
                        "spans": [
                            span
                            for index, item in enumerate(row)
                            for span in (
                                ([{"text": " "}] if index else [])
                                + item["line"].get("spans", [])
                            )
                        ],
                    },
                }
            )
        group = merged_group

        spans = [
            span
            for item in group
            for span in item["line"].get("spans", [])
            if span.get("text", "").strip()
        ]
        if not spans:
            continue
        first = spans[0]
        bboxes = [item["line"]["bbox"] for item in group]
        bbox = [
            min(rect[0] for rect in bboxes),
            min(rect[1] for rect in bboxes),
            max(rect[2] for rect in bboxes),
            max(rect[3] for rect in bboxes),
        ]
        centers = [(rect[0] + rect[2]) / 2 for rect in bboxes]
        edge_tolerance = max(2, page_width * 0.005)
        fixed_left_edge = (
            len(bboxes) > 1
            and max(rect[0] for rect in bboxes) - min(rect[0] for rect in bboxes)
            <= edge_tolerance
            and max(rect[2] for rect in bboxes) - min(rect[2] for rect in bboxes)
            > edge_tolerance
        )
        centered = not fixed_left_edge and all(
            abs(center - page_width / 2) <= page_width * 0.03 for center in centers
        )
        bold = all(_has_style(span, pymupdf.TEXT_FONT_BOLD, "bold") for span in spans)
        italic = all(_has_style(span, pymupdf.TEXT_FONT_ITALIC, "italic") for span in spans)

        segments.append(
            {
                "bbox": bbox,
                "source": "\n".join(item["text"] for item in group),
                "font_size": float(first.get("size", 11)),
                "color": int(first.get("color", 0)),
                "source_markup": _group_style_markup(group, bold, italic),
                "bold": bold,
                "italic": italic,
                "align": "center" if centered else "left",
            }
        )
    return segments


def _refine_page_alignment(segments: list[dict], page_width: float) -> None:
    """Join single-line fragments back to an adjacent left-aligned text flow."""
    tolerance = max(2, page_width * 0.005)
    for index, segment in enumerate(segments):
        if segment["align"] != "center":
            continue
        neighbors = segments[max(0, index - 1) : index] + segments[index + 1 : index + 2]
        if any(
            neighbor["align"] == "left"
            and abs(neighbor["bbox"][0] - segment["bbox"][0]) <= tolerance
            and abs(neighbor["font_size"] - segment["font_size"]) <= 0.5
            and neighbor["color"] == segment["color"]
            and neighbor["bold"] == segment["bold"]
            and neighbor["italic"] == segment["italic"]
            for neighbor in neighbors
        ):
            segment["align"] = "left"


def _extract_segments(input_path: Path) -> tuple[list[dict], int]:
    segments: list[dict] = []
    with pymupdf.open(input_path) as document:
        if document.needs_pass:
            raise ValueError("Password-protected PDFs are not supported.")
        for page_number, page in enumerate(document):
            page_segments: list[dict] = []
            for block in page.get_text("dict", sort=True)["blocks"]:
                if block.get("type") != 0:
                    continue
                page_segments.extend(_split_text_block(block, page.rect.width))
            _refine_page_alignment(page_segments, page.rect.width)
            for segment in page_segments:
                segment["id"] = str(len(segments))
                segment["page"] = page_number
                segments.append(segment)
        return segments, document.page_count


@mcp.tool()
def prepare_translation(
    input_path: str, target_language: str, output_path: str | None = None
) -> dict:
    """Prepare a text-based PDF for LLM translation while preserving positioned text blocks.

    Use an absolute path to the attached PDF. This creates a temporary translation job but does
    not modify the source file. Scanned/image-only PDFs are rejected.
    """
    source = Path(input_path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError(f"Input must be an existing PDF file: {source}")
    if not target_language.strip():
        raise ValueError("target_language is required.")
    destination = (
        Path(output_path).expanduser().resolve()
        if output_path
        else _default_output(source, target_language)
    )
    if destination == source:
        raise ValueError("Output path must differ from the source PDF.")

    segments, page_count = _extract_segments(source)
    if not segments:
        raise ValueError(
            "No selectable text was found. This first version supports text-based PDFs, not "
            "scanned/image-only PDFs."
        )

    job = {
        "job_id": str(uuid.uuid4()),
        "input_path": str(source),
        "output_path": str(destination),
        "target_language": target_language.strip(),
        "page_count": page_count,
        "segments": segments,
        "translations": {},
    }
    _save_job(job)
    return {
        "job_id": job["job_id"],
        "target_language": job["target_language"],
        "page_count": page_count,
        "segment_count": len(segments),
        "output_path": str(destination),
        "next": "Call get_translation_batch with this job_id.",
    }


@mcp.tool()
def get_translation_batch(job_id: str, limit: int = 20) -> dict:
    """Return the next untranslated PDF text blocks for the active model to translate."""
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50.")
    job = _load_job(job_id)
    pending = [
        {
            "id": segment["id"],
            "page": segment["page"] + 1,
            "source": segment.get("source_markup", segment["source"]),
        }
        for segment in job["segments"]
        if segment["id"] not in job["translations"]
    ]
    batch = pending[:limit]
    return {
        "job_id": job_id,
        "target_language": job["target_language"],
        "segments": batch,
        "remaining_after_this_batch": len(pending) - len(batch),
        "instruction": (
            "Translate each source into the target language. Preserve meaning, numbers, URLs, "
            "names, IDs, and every [[B]], [[/B]], [[I]], and [[/I]] marker exactly. Move a complete "
            "marker pair with the words it styles when target-language grammar reorders the phrase. "
            "Do not translate the markers or summarize. Submit [{id, text}, ...]."
        ),
    }


@mcp.tool()
def submit_translations(job_id: str, translations: list[dict[str, str]]) -> dict:
    """Save one translated batch as objects containing the original segment id and translated text."""
    job = _load_job(job_id)
    valid_ids = {segment["id"] for segment in job["segments"]}
    for item in translations:
        segment_id = str(item.get("id", ""))
        text = str(item.get("text", "")).strip()
        if segment_id not in valid_ids:
            raise ValueError(f"Unknown segment ID: {segment_id}")
        if not text:
            raise ValueError(f"Translation for segment {segment_id} is empty.")
        segment = next(segment for segment in job["segments"] if segment["id"] == segment_id)
        _validate_style_markup(text, segment.get("source_markup", segment["source"]))
        job["translations"][segment_id] = text
    _save_job(job)
    remaining = len(job["segments"]) - len(job["translations"])
    return {
        "job_id": job_id,
        "saved": len(translations),
        "remaining": remaining,
        "next": (
            "Call render_translated_pdf."
            if remaining == 0
            else "Call get_translation_batch again."
        ),
    }


@mcp.tool()
def render_translated_pdf(job_id: str) -> dict:
    """Create the translated PDF after every prepared text segment has a submitted translation."""
    job = _load_job(job_id)
    missing = [
        segment["id"]
        for segment in job["segments"]
        if segment["id"] not in job["translations"]
    ]
    if missing:
        raise ValueError(f"{len(missing)} segments still need translation.")

    output = Path(job["output_path"])
    output.parent.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    by_page: dict[int, list[dict]] = {}
    for segment in job["segments"]:
        by_page.setdefault(segment["page"], []).append(segment)

    with pymupdf.open(job["input_path"]) as document:
        for page_number, segments in by_page.items():
            page = document[page_number]
            for segment in segments:
                page.add_redact_annot(
                    pymupdf.Rect(segment["bbox"]), fill=False, cross_out=False
                )
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )
            for segment in segments:
                translated = _markup_to_html(job["translations"][segment["id"]])
                color = f"#{segment['color'] & 0xFFFFFF:06x}"
                weight = "bold" if segment.get("bold") else "normal"
                style = "italic" if segment.get("italic") else "normal"
                alignment = (
                    "text-align: center; " if segment.get("align") == "center" else ""
                )
                css = (
                    "* { font-family: sans-serif; "
                    f"font-size: {segment['font_size']}pt; color: {color}; "
                    f"font-weight: {weight}; font-style: {style}; "
                    f"{alignment}}}"
                )
                spare_height, scale = page.insert_htmlbox(
                    pymupdf.Rect(segment["bbox"]), translated, css=css, scale_low=0
                )
                if spare_height < 0:
                    warnings.append(
                        f"Page {page_number + 1}, segment {segment['id']} could not be fitted."
                    )
                elif scale < 0.55:
                    warnings.append(
                        f"Page {page_number + 1}, segment {segment['id']} was scaled to {scale:.0%}."
                    )
        document.save(output, garbage=4, deflate=True)

    return {
        "job_id": job_id,
        "output_path": str(output),
        "page_count": job["page_count"],
        "segment_count": len(job["segments"]),
        "warnings": warnings,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
