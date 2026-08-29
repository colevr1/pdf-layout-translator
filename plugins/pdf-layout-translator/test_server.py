import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf
import server
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def line(
    text: str, bbox: tuple[float, float, float, float], size: float, flags: int = 0
) -> dict:
    bold = bool(flags & pymupdf.TEXT_FONT_BOLD)
    italic = bool(flags & pymupdf.TEXT_FONT_ITALIC)
    font = (
        "Test-BoldItalic"
        if bold and italic
        else "Test-Bold"
        if bold
        else "Test-Italic"
        if italic
        else "Test-Regular"
    )
    return {
        "bbox": bbox,
        "spans": [
            {
                "text": text,
                "bbox": bbox,
                "origin": (bbox[0], bbox[3]),
                "size": size,
                "font": font,
                "flags": flags,
                "color": 0,
            }
        ],
    }


def check_mixed_style_block_split() -> None:
    block = {
        "lines": [
            line("TECHNICAL REPORT", (190, 100, 410, 124), 20, pymupdf.TEXT_FONT_BOLD),
            line(" ", (60, 124, 64, 138), 11),
            line("1. INTRODUCTION", (60, 160, 170, 174), 11),
            line("-", (60, 176, 66, 190), 11),
            line("2. REFERENCES", (80, 176, 180, 190), 11),
            line(" ", (60, 190, 64, 204), 11),
            line("DATE: 29/08/2026", (60, 220, 170, 234), 11),
        ]
    }
    segments = server._split_text_block(block, 600)
    assert [segment["source"] for segment in segments] == [
        "TECHNICAL REPORT",
        "1. INTRODUCTION\n- 2. REFERENCES",
        "DATE: 29/08/2026",
    ]
    assert [segment["font_size"] for segment in segments] == [20, 11, 11]
    assert segments[0]["bold"] is True
    assert segments[0]["align"] == "center"
    assert segments[1]["bbox"] == [60, 160, 180, 190]


def check_inline_style_markup() -> None:
    block = {
        "lines": [
            {
                "bbox": (60, 100, 360, 114),
                "spans": [
                    line("Si definisce ", (60, 100, 120, 114), 12)["spans"][0],
                    line(
                        "intelligibilità di un messaggio vocale",
                        (120, 100, 300, 114),
                        12,
                        pymupdf.TEXT_FONT_BOLD,
                    )["spans"][0],
                    line(" la capacità", (300, 100, 360, 114), 12)["spans"][0],
                ],
            }
        ]
    }
    segment = server._split_text_block(block, 600)[0]
    assert segment["source_markup"] == (
        "Si definisce [[B]]intelligibilità di un messaggio vocale[[/B]] la capacità"
    )
    server._validate_style_markup(
        "L’[[B]]intelligibilité d’un message vocal[[/B]] se définit comme la capacité",
        segment["source_markup"],
    )
    try:
        server._validate_style_markup(
            "L’intelligibilité d’un message vocal se définit comme la capacité",
            segment["source_markup"],
        )
    except ValueError:
        pass
    else:
        raise AssertionError("missing style markers must be rejected")

    wrapped = {
        "lines": [
            {
                "bbox": (60, 120, 240, 134),
                "spans": [
                    line("la ", (60, 120, 72, 134), 12)["spans"][0],
                    line("riverberazione", (72, 120, 150, 134), 12, 16)["spans"][0],
                ],
            },
            {
                "bbox": (60, 136, 180, 150),
                "spans": [
                    line("acustica", (60, 136, 110, 150), 12, 16)["spans"][0],
                    line(" aumenta", (110, 136, 180, 150), 12)["spans"][0],
                ],
            },
        ]
    }
    assert server._split_text_block(wrapped, 600)[0]["source_markup"] == (
        "la [[B]]riverberazione\nacustica[[/B]] aumenta"
    )


def check_alignment_inference() -> None:
    left_aligned_page_break = {
        "lines": [
            line("A full first body line", (56.6, 731, 536.5, 743), 12),
            line("A slightly shorter second line", (56.6, 748, 518.2, 760), 12),
            line("A similar third line", (56.6, 765, 524.1, 777), 12),
        ]
    }
    assert (
        server._split_text_block(left_aligned_page_break, 595.32)[0]["align"] == "left"
    )

    centered_heading = {
        "lines": [
            line("A centered heading", (100, 100, 500, 116), 14),
            line("with a shorter line", (160, 118, 440, 134), 14),
        ]
    }
    assert server._split_text_block(centered_heading, 600)[0]["align"] == "center"

    split_body_flow = [
        server._split_text_block(
            {"lines": [line("A nearly full standalone line", (75, 200, 530, 212), 11)]},
            600,
        )[0],
        server._split_text_block(
            {
                "lines": [
                    line("The continuation", (75, 216, 520, 228), 11),
                    line("ends well before the right edge", (75, 232, 360, 244), 11),
                ]
            },
            600,
        )[0],
    ]
    assert split_body_flow[0]["align"] == "center"
    assert split_body_flow[1]["align"] == "left"
    server._refine_page_alignment(split_body_flow, 600)
    assert [segment["align"] for segment in split_body_flow] == ["left", "left"]


def check_structural_layout_helpers() -> None:
    blocks = [
        {
            "type": 0,
            "lines": [line("Synthetic italic", (100, 100, 200, 116), 12)],
        },
    ]
    server._enrich_synthetic_styles(
        blocks,
        [
            {
                "x": 100,
                "y": 116,
                "size": 12,
                "synthetic_bold": False,
                "synthetic_italic": True,
            }
        ],
    )
    assert blocks[0]["lines"][0]["spans"][0]["synthetic_italic"] is True
    assert server._is_sheared([1, 0, 0.3333, 1, 0, 0]) is True
    assert server._is_sheared([0, 1, -1, 0, 0, 0]) is False
    assert (
        server._is_sheared(
            server._combined_linear_matrix([0, 1, -1, 0, 0, 0], [1, 0, 0, 1, 0, 0])
        )
        is False
    )

    offset_blocks = [
        {
            "type": 0,
            "lines": [line("3G2.5 mm", (410.3, 100, 482.2, 118), 15.96)],
        }
    ]
    server._enrich_synthetic_styles(
        offset_blocks,
        [
            {
                "text": " 3G2.5 mm",
                "x": 406.3,
                "y": 118,
                "size": 15.96,
                "synthetic_bold": True,
                "synthetic_italic": False,
            }
        ],
    )
    assert offset_blocks[0]["lines"][0]["spans"][0]["synthetic_bold"] is True

    repeated_blocks = [
        {
            "type": 0,
            "lines": [line("TOTAL", (100, 130, 150, 145), 12)],
        }
    ]
    server._enrich_synthetic_styles(
        repeated_blocks,
        [
            {
                "text": "TOTAL",
                "x": 300,
                "y": 145,
                "size": 12,
                "synthetic_bold": True,
                "synthetic_italic": False,
            }
        ],
    )
    assert "synthetic_bold" not in repeated_blocks[0]["lines"][0]["spans"][0]

    numbered = {
        "lines": [
            line("CONSTRUCTION", (350, 350, 430, 362), 10, pymupdf.TEXT_FONT_BOLD),
            line(
                "CABLE STRUCTURE",
                (350, 363, 440, 375),
                10,
                pymupdf.TEXT_FONT_BOLD | pymupdf.TEXT_FONT_ITALIC,
            ),
            line("1. Primary text", (350, 376, 440, 388), 10),
            line(
                "Parallel translation",
                (350, 389, 460, 401),
                10,
                pymupdf.TEXT_FONT_ITALIC,
            ),
            line("2. Next item", (350, 402, 430, 414), 10),
            line(
                "Parallel translation",
                (350, 415, 460, 427),
                10,
                pymupdf.TEXT_FONT_ITALIC,
            ),
        ]
    }
    segments = server._split_text_block(numbered, 600)
    assert [segment["source"] for segment in segments] == [
        "CONSTRUCTION",
        "CABLE STRUCTURE",
        "1. Primary text",
        "Parallel translation",
        "2. Next item",
        "Parallel translation",
    ]
    assert [segment["italic"] for segment in segments] == [
        False,
        True,
        False,
        True,
        False,
        True,
    ]

    distant_row = {
        "lines": [
            line("APPLICATIONS", (110, 400, 175, 412), 10),
            line("APPLICATIONS", (194, 401, 262, 413), 10),
        ]
    }
    assert len(server._split_text_block(distant_row, 600)) == 2

    list_block = {
        "lines": [
            line("1. First item", (60, 450, 180, 462), 10),
            line("2. Second item", (60, 464, 190, 476), 10),
        ]
    }
    list_segment = server._split_text_block(list_block, 600)[0]
    assert list_segment["preserve_breaks"] is True

    bullet_lines = [
        {
            "text": "• A first deliberately long bullet item that must retain its own row",
            "line": line("first bullet", (60, 480, 500, 492), 10),
        },
        {
            "text": "• A second deliberately long bullet item that must retain its own row",
            "line": line("second bullet", (60, 494, 500, 506), 10),
        },
    ]
    assert server._preserve_line_breaks(bullet_lines) is True

    address = {
        "lines": [
            line("Company Ltd", (60, 500, 130, 512), 10),
            line("Street 1", (60, 514, 120, 526), 10),
            line("info@example.com", (60, 528, 155, 540), 10),
        ]
    }
    assert server._split_text_block(address, 600)[0]["preserve_breaks"] is True

    compact_columns = {
        "lines": [
            line("Revision Date", (40, 550, 100, 562), 10),
            line("06/03/2025", (120, 550, 180, 562), 10),
            line("Issue n.", (200, 550, 245, 562), 10),
        ]
    }
    compact_segments = server._split_text_block(compact_columns, 600)
    assert all(segment["preserve_breaks"] is False for segment in compact_segments)
    assert server._markup_to_html("wrapped\ntext") == "wrapped text"
    assert server._markup_to_html("1. One\n2. Two", preserve_breaks=True) == (
        "1. One<br>2. Two"
    )

    heading = {
        "bbox": [200, 300, 330, 314],
        "source": "SCHEMATIC DRAWINGS",
        "font_size": 12,
        "align": "left",
        "line_count": 1,
    }
    expanded = server._insertion_bbox(heading, [heading], 600)
    assert expanded.x1 == 395
    assert expanded.y1 == 314
    blocked_heading = {**heading, "bbox": [50, 300, 100, 314]}
    blocked = server._insertion_bbox(
        blocked_heading,
        [blocked_heading],
        600,
        [pymupdf.Rect(110, 300, 200, 340)],
    )
    assert blocked.x1 <= 108
    paragraph = {**heading, "source": "first line\nsecond line", "line_count": 2}
    paragraph_box = server._insertion_bbox(paragraph, [paragraph], 600)
    assert paragraph_box.x1 == 330
    assert paragraph_box.y1 == 314


def check_mixed_synthetic_render_modes(directory: Path) -> None:
    def write_pdf(name: str, operators: bytes) -> Path:
        source = directory / name
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=200)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                )
            }
        )
        content = DecodedStreamObject()
        content.set_data(operators)
        page[NameObject("/Contents")] = writer._add_object(content)
        with source.open("wb") as handle:
            writer.write(handle)
        return source

    source = write_pdf(
        "mixed-render-modes.pdf",
        b"BT /F1 12 Tf 50 100 Td 0 Tr (A) Tj 2 Tr (B) Tj ET",
    )

    runs = server._synthetic_style_runs(PdfReader(source).pages[0])
    assert all(not (run["synthetic_bold"] and "A" in run["text"]) for run in runs)
    segments, _ = server._extract_segments(source)
    assert segments[0]["source"] == "AB"
    assert segments[0]["bold"] is False

    quote_source = write_pdf(
        "quote-operator.pdf",
        b"BT /F1 12 Tf 14 TL 50 150 Td 2 Tr (A) Tj (B) ' ET",
    )
    quote_segments, _ = server._extract_segments(quote_source)
    assert any(
        "B" in segment["source"] and segment["bold"] for segment in quote_segments
    )

    spaced_source = write_pdf(
        "spaced-array.pdf",
        b"BT /F1 12 Tf 50 100 Td 2 Tr [(A) -2000 (B)] TJ ET",
    )
    spaced_segments, _ = server._extract_segments(spaced_source)
    assert all(segment["bold"] for segment in spaced_segments)

    bounded_source = write_pdf(
        "bounded-spaced-array.pdf",
        b"BT /F1 12 Tf 50 100 Td 2 Tr [(A) -2000 (B)] TJ "
        b"0 Tr 1 0 .3333 1 200 99 Tm (C) Tj ET",
    )
    bounded_segments, _ = server._extract_segments(bounded_source)
    assert any(
        segment["source"] == "C" and segment["italic"] for segment in bounded_segments
    )

    td_source = write_pdf(
        "td-leading.pdf",
        b"BT /F1 12 Tf 50 150 Td 0 -14 TD 2 Tr (A) Tj (B) ' ET",
    )
    td_segments, _ = server._extract_segments(td_source)
    assert any("B" in segment["source"] and segment["bold"] for segment in td_segments)

    scaled_source = write_pdf(
        "scaled-text.pdf",
        b"q 2 0 0 2 0 0 cm BT /F1 12 Tf 50 50 Td 2 Tr (AB) Tj ET Q",
    )
    scaled_segments, _ = server._extract_segments(scaled_source)
    assert scaled_segments[0]["font_size"] == 24
    assert scaled_segments[0]["bold"] is True

    restored_source = write_pdf(
        "restored-graphics-state.pdf",
        b"BT /F1 12 Tf 2 Tr 50 150 Td (A) Tj ET "
        b"q BT /F1 20 Tf 50 120 Td (X) Tj ET Q "
        b"BT 50 90 Td (B) Tj ET",
    )
    restored_segments, _ = server._extract_segments(restored_source)
    assert any(
        segment["source"] == "B" and segment["font_size"] == 12 and segment["bold"]
        for segment in restored_segments
    )


def main() -> None:
    check_mixed_style_block_split()
    check_inline_style_markup()
    check_alignment_inference()
    check_structural_layout_helpers()
    with TemporaryDirectory() as directory:
        check_mixed_synthetic_render_modes(Path(directory))
        source = Path(directory) / "source.pdf"
        output = Path(directory) / "translated.pdf"
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_htmlbox((72, 50, 220, 90), "Hello <b>world</b>")
            page.draw_rect((60, 50, 180, 90), color=(1, 0, 0))
            document.save(source)

        prepared = server.prepare_translation(str(source), "Spanish", str(output))
        batch = server.get_translation_batch(prepared["job_id"])
        assert batch["segments"][0]["source"] == "Hello [[B]]world[[/B]]"
        try:
            server.submit_translations(
                prepared["job_id"],
                [{"id": batch["segments"][0]["id"], "text": "Hola mundo"}],
            )
        except ValueError:
            pass
        else:
            raise AssertionError("submission without required style markers must fail")
        translations = [
            {"id": item["id"], "text": "Hola [[B]]mundo[[/B]]"}
            for item in batch["segments"]
        ]
        assert (
            server.submit_translations(prepared["job_id"], translations)["remaining"]
            == 0
        )
        result = server.render_translated_pdf(prepared["job_id"])
        assert Path(result["output_path"]).is_file()
        with pymupdf.open(output) as translated:
            assert "Hola mundo" in translated[0].get_text()
            bold = [
                span
                for block in translated[0].get_text("dict")["blocks"]
                if block.get("type") == 0
                for text_line in block.get("lines", [])
                for span in text_line.get("spans", [])
                if span.get("flags", 0) & pymupdf.TEXT_FONT_BOLD
            ]
            assert any("mundo" in span["text"] for span in bold)
            assert translated[0].get_drawings(), "vector graphics should be preserved"


async def check_mcp() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("server.py"))],
    )
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "prepare_translation",
            "get_translation_batch",
            "submit_translations",
            "render_translated_pdf",
        }


if __name__ == "__main__":
    main()
    asyncio.run(check_mcp())
