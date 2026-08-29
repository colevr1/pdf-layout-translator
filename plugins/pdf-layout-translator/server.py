from __future__ import annotations

import html
import json
import re
import tempfile
import uuid
from math import hypot, sqrt
from pathlib import Path

import pymupdf
from mcp.server.fastmcp import FastMCP
from pypdf import PdfReader
from pypdf.errors import PdfReadError

JOBS_DIR = Path(tempfile.gettempdir()) / "pdf-layout-translator-mcp"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
STYLE_TOKEN_RE = re.compile(r"\[\[(/?)(B|I)\]\]")
ANY_STYLE_TOKEN_RE = re.compile(r"\[\[/?(?:B|I)[^\]]*\]\]")
NUMBERED_ITEM_RE = re.compile(r"^\s*\d+[.)]\s")
BULLET_ITEM_RE = re.compile(
    r"^\s*(?:[\u2022\u25cf\u25aa\u25e6\u2023\u2043*]|[-\u2013\u2014])\s+"
)

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
    safe_language = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in target_language
    )
    safe_language = safe_language.strip("-") or "translated"
    return input_path.with_name(f"{input_path.stem}-{safe_language}{input_path.suffix}")


def _has_style(span: dict, flag: int, name: str) -> bool:
    aliases = {
        "bold": ("bold", "black", "demi"),
        "italic": ("italic", "oblique"),
    }
    return bool(
        int(span.get("flags", 0)) & flag
        or span.get(f"synthetic_{name}")
        or any(alias in str(span.get("font", "")).casefold() for alias in aliases[name])
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
                if _has_style(
                    previous[-1], pymupdf.TEXT_FONT_BOLD, "bold"
                ) and _has_style(following[0], pymupdf.TEXT_FONT_BOLD, "bold"):
                    flags |= pymupdf.TEXT_FONT_BOLD
                if _has_style(
                    previous[-1], pymupdf.TEXT_FONT_ITALIC, "italic"
                ) and _has_style(following[0], pymupdf.TEXT_FONT_ITALIC, "italic"):
                    flags |= pymupdf.TEXT_FONT_ITALIC
            spans.append({"text": "\n", "flags": flags})
        spans.extend(current)
    return re.sub(r"[ \t]+\n", "\n", _style_markup(spans, base_bold, base_italic))


def _validate_style_markup(translated: str, source_markup: str) -> None:
    expected = [match.group(0) for match in STYLE_TOKEN_RE.finditer(source_markup)]
    actual = [match.group(0) for match in STYLE_TOKEN_RE.finditer(translated)]
    unknown = [
        token for token in ANY_STYLE_TOKEN_RE.findall(translated) if token not in actual
    ]
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


def _markup_to_html(text: str, preserve_breaks: bool = False) -> str:
    separator = "<br>" if preserve_breaks else " "
    rendered = separator.join(
        html.escape(part) for part in re.split(r"[ \t]*\n[ \t]*", text)
    )
    for marker, tag in (
        ("[[B]]", "<b>"),
        ("[[/B]]", "</b>"),
        ("[[I]]", "<i>"),
        ("[[/I]]", "</i>"),
    ):
        rendered = rendered.replace(marker, tag)
    return rendered


def _matrix_origin(tm: list[float], cm: list[float]) -> tuple[float, float]:
    return (
        cm[0] * tm[4] + cm[2] * tm[5] + cm[4],
        cm[1] * tm[4] + cm[3] * tm[5] + cm[5],
    )


def _combined_linear_matrix(tm: list[float], cm: list[float]) -> list[float]:
    return [
        cm[0] * tm[0] + cm[2] * tm[1],
        cm[1] * tm[0] + cm[3] * tm[1],
        cm[0] * tm[2] + cm[2] * tm[3],
        cm[1] * tm[2] + cm[3] * tm[3],
    ]


def _is_sheared(matrix: list[float]) -> bool:
    first_length = hypot(matrix[0], matrix[1])
    second_length = hypot(matrix[2], matrix[3])
    if not first_length or not second_length:
        return False
    normalized_dot = (matrix[0] * matrix[2] + matrix[1] * matrix[3]) / (
        first_length * second_length
    )
    return abs(normalized_dot) > 0.05


def _synthetic_style_runs(reader_page) -> list[dict]:
    if int(reader_page.get("/Rotate", 0)) % 360:
        return []
    runs: list[dict] = []
    state = {"render_mode": 0, "font_size": 0.0, "leading": 0.0}
    state_stack: list[dict] = []
    page_top = float(reader_page.mediabox.top)
    crop_left = float(reader_page.cropbox.left)
    crop_top_margin = page_top - float(reader_page.cropbox.top)

    def before(operator, operands, cm, tm) -> None:
        if operator == b"q":
            state_stack.append(state.copy())
        elif operator == b"Q" and state_stack:
            state.update(state_stack.pop())
        elif operator == b"Tr":
            state["render_mode"] = int(operands[0])
        elif operator == b"Tf":
            state["font_size"] = float(operands[1])
        elif operator == b"TL":
            state["leading"] = float(operands[0])
        elif operator == b"TD":
            state["leading"] = -float(operands[1])
        elif operator in {b"Tj", b"TJ", b"'", b'"'}:
            effective_tm = list(tm)
            if operator in {b"'", b'"'}:
                effective_tm[4] -= effective_tm[2] * state["leading"]
                effective_tm[5] -= effective_tm[3] * state["leading"]
            combined = _combined_linear_matrix(effective_tm, cm)
            x, y = _matrix_origin(effective_tm, cm)
            runs.append(
                {
                    "text": str(operands),
                    "x": x - crop_left,
                    "y": page_top - y - crop_top_margin,
                    "size": state["font_size"]
                    * sqrt(abs(combined[0] * combined[3] - combined[1] * combined[2])),
                    "wide": operator == b"TJ",
                    "synthetic_bold": state["render_mode"] in {1, 2, 5, 6},
                    "synthetic_italic": _is_sheared(combined),
                }
            )

    reader_page.extract_text(visitor_operand_before=before)
    for run in runs:
        colocated = [
            other
            for other in runs
            if abs(other["x"] - run["x"]) <= 0.5
            and abs(other["y"] - run["y"]) <= 0.5
            and abs(other["size"] - run["size"]) <= 0.5
        ]
        run["ambiguous"] = (
            len(
                {
                    (other["synthetic_bold"], other["synthetic_italic"])
                    for other in colocated
                }
            )
            > 1
        )
        following = [
            other["x"]
            for other in runs
            if other["x"] > run["x"] + 0.5
            and abs(other["y"] - run["y"]) <= max(1, run["size"] * 0.25)
            and abs(other["size"] - run["size"]) <= 0.5
        ]
        run["end_x"] = min(following, default=float("inf"))
    return runs


def _enrich_synthetic_styles(blocks: list[dict], runs: list[dict]) -> None:
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                origin = span.get("origin")
                if not origin or not span.get("text", "").strip():
                    continue
                span_size = float(span.get("size", 0))
                candidates = []
                for run in runs:
                    if run.get("ambiguous"):
                        continue
                    same_line = abs(run["y"] - origin[1]) <= max(1, span_size * 0.25)
                    nearby_start = abs(run["x"] - origin[0]) <= max(1, span_size * 0.5)
                    covered_by_wide_run = (
                        run.get("wide")
                        and run["x"] <= origin[0] + 1
                        and origin[0] < run["end_x"] - 1
                    )
                    if (
                        abs(run["size"] - span_size) <= 0.5
                        and same_line
                        and (nearby_start or covered_by_wide_run)
                    ):
                        candidates.append(run)
                if candidates:
                    bold_values = {run["synthetic_bold"] for run in candidates}
                    italic_values = {run["synthetic_italic"] for run in candidates}
                    if len(bold_values) == 1:
                        span["synthetic_bold"] = bold_values.pop()
                    if len(italic_values) == 1:
                        span["synthetic_italic"] = italic_values.pop()


def _line_signature(item: dict) -> tuple[float, bool, bool, int]:
    spans = [
        span for span in item["line"].get("spans", []) if span.get("text", "").strip()
    ]
    first = spans[0]
    return (
        float(first.get("size", 11)),
        all(_has_style(span, pymupdf.TEXT_FONT_BOLD, "bold") for span in spans),
        all(_has_style(span, pymupdf.TEXT_FONT_ITALIC, "italic") for span in spans),
        int(first.get("color", 0)),
    )


def _split_layout_runs(group: list[dict]) -> list[list[dict]]:
    split: list[list[dict]] = []
    current: list[dict] = []
    for item in group:
        if current:
            previous = current[-1]
            previous_bbox = previous["line"]["bbox"]
            bbox = item["line"]["bbox"]
            previous_style = _line_signature(previous)
            style = _line_signature(item)
            overlap = min(previous_bbox[3], bbox[3]) - max(previous_bbox[1], bbox[1])
            same_row = (
                overlap
                > min(previous_bbox[3] - previous_bbox[1], bbox[3] - bbox[1]) / 2
            )
            gap = max(0, bbox[0] - previous_bbox[2], previous_bbox[0] - bbox[2])
            size_changed = abs(previous_style[0] - style[0]) > max(
                0.75, min(previous_style[0], style[0]) * 0.1
            )
            style_changed = previous_style[1:] != style[1:]
            distant_same_row = same_row and gap > max(previous_style[0], style[0]) * 1.5
            if size_changed or style_changed or distant_same_row:
                split.append(current)
                current = []
        current.append(item)
    if current:
        split.append(current)
    return split


def _preserve_line_breaks(group: list[dict]) -> bool:
    numbered_lines = sum(bool(NUMBERED_ITEM_RE.match(item["text"])) for item in group)
    bullet_lines = sum(bool(BULLET_ITEM_RE.match(item["text"])) for item in group)
    bboxes = [item["line"]["bbox"] for item in group]
    height = max(bbox[3] for bbox in bboxes) - min(bbox[1] for bbox in bboxes)
    smallest_font = min(_line_signature(item)[0] for item in group)
    has_vertical_room = height >= len(group) * smallest_font * 0.65
    compact_label = (
        len(group) > 1
        and max(len(item["text"]) for item in group) < 45
        and has_vertical_room
    )
    return numbered_lines > 1 or bullet_lines > 1 or compact_label


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
    groups = [part for group in groups for part in _split_layout_runs(group)]

    segments: list[dict] = []
    for group in groups:
        rows: list[list[dict]] = []
        for item in group:
            bbox = item["line"]["bbox"]
            if rows:
                previous = rows[-1][0]["line"]["bbox"]
                overlap = min(previous[3], bbox[3]) - max(previous[1], bbox[1])
                same_row = (
                    overlap > min(previous[3] - previous[1], bbox[3] - bbox[1]) / 2
                )
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
        italic = all(
            _has_style(span, pymupdf.TEXT_FONT_ITALIC, "italic") for span in spans
        )

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
                "line_count": len(group),
                "preserve_breaks": _preserve_line_breaks(group),
            }
        )
    return segments


def _insertion_bbox(
    segment: dict,
    segments: list[dict],
    page_width: float,
    obstacles: list[pymupdf.Rect] | None = None,
) -> pymupdf.Rect:
    """Give text room to reflow without crossing nearby text columns."""
    rect = pymupdf.Rect(segment["bbox"])
    single_line = segment.get("line_count", segment["source"].count("\n") + 1) == 1
    if not single_line and not segment.get("preserve_breaks", False):
        return rect
    margin = page_width * 0.02
    left_limit = margin
    right_limit = page_width - margin
    padding = max(2, segment["font_size"] * 0.25)
    for other in segments:
        if other is segment:
            continue
        other_rect = pymupdf.Rect(other["bbox"])
        if min(rect.y1, other_rect.y1) - max(rect.y0, other_rect.y0) <= 0:
            continue
        if other_rect.x1 <= rect.x0:
            left_limit = max(left_limit, other_rect.x1)
        elif other_rect.x0 >= rect.x1:
            right_limit = min(right_limit, other_rect.x0)
    for obstacle in obstacles or []:
        vertical_gap = max(0, obstacle.y0 - rect.y1, rect.y0 - obstacle.y1)
        if vertical_gap > segment["font_size"] * 0.5:
            continue
        contains_text = (
            obstacle.x0 <= rect.x0 + padding and obstacle.x1 >= rect.x1 - padding
        )
        if contains_text:
            left_limit = max(left_limit, obstacle.x0)
            right_limit = min(right_limit, obstacle.x1)
        elif obstacle.x1 <= rect.x0:
            left_limit = max(left_limit, obstacle.x1)
        elif obstacle.x0 >= rect.x1:
            right_limit = min(right_limit, obstacle.x0)
    if segment.get("align") == "center":
        growth = max(
            0,
            min(
                rect.width * 0.25,
                rect.x0 - left_limit - padding,
                right_limit - rect.x1 - padding,
            ),
        )
        rect.x0 -= growth
        rect.x1 += growth
    else:
        rect.x1 = max(rect.x1, min(rect.x1 + rect.width * 0.5, right_limit - padding))
    return rect


def _refine_page_alignment(segments: list[dict], page_width: float) -> None:
    """Join single-line fragments back to an adjacent left-aligned text flow."""
    tolerance = max(2, page_width * 0.005)
    for index, segment in enumerate(segments):
        if segment["align"] != "center":
            continue
        neighbors = (
            segments[max(0, index - 1) : index] + segments[index + 1 : index + 2]
        )
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
        try:
            reader = PdfReader(str(input_path))
        except (OSError, ValueError, PdfReadError):
            reader = None
        for page_number, page in enumerate(document):
            page_segments: list[dict] = []
            blocks = page.get_text("dict", sort=True)["blocks"]
            if reader is not None:
                try:
                    style_runs = (
                        _synthetic_style_runs(reader.pages[page_number])
                        if page_number < len(reader.pages)
                        else []
                    )
                except (IndexError, KeyError, TypeError, ValueError, PdfReadError):
                    style_runs = []
                _enrich_synthetic_styles(blocks, style_runs)
            for block in blocks:
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
        segment = next(
            segment for segment in job["segments"] if segment["id"] == segment_id
        )
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
            obstacles = [
                pymupdf.Rect(drawing["rect"])
                for drawing in page.get_drawings()
                if drawing["rect"].width > 2 and drawing["rect"].height > 2
            ] + [
                pymupdf.Rect(image["bbox"])
                for image in page.get_image_info()
                if pymupdf.Rect(image["bbox"]).width > 2
                and pymupdf.Rect(image["bbox"]).height > 2
            ]
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
                translated = _markup_to_html(
                    job["translations"][segment["id"]],
                    preserve_breaks=segment.get("preserve_breaks", False),
                )
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
                    f"line-height: 1; margin: 0; padding: 0; {alignment}}}"
                )
                spare_height, scale = page.insert_htmlbox(
                    _insertion_bbox(segment, segments, page.rect.width, obstacles),
                    translated,
                    css=css,
                    scale_low=0,
                )
                if spare_height < 0:
                    warnings.append(
                        f"Page {page_number + 1}, segment {segment['id']} could not be fitted."
                    )
                elif scale < 0.85:
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
