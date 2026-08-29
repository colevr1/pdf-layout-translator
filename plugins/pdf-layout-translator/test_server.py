import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf
import server
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def line(text: str, bbox: tuple[float, float, float, float], size: float, flags: int = 0) -> dict:
    return {
        "bbox": bbox,
        "spans": [
            {
                "text": text,
                "bbox": bbox,
                "size": size,
                "font": "Test-Bold" if flags else "Test-Regular",
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
    assert server._split_text_block(left_aligned_page_break, 595.32)[0]["align"] == "left"

    centered_heading = {
        "lines": [
            line("A centered heading", (100, 100, 500, 116), 14),
            line("with a shorter line", (160, 118, 440, 134), 14),
        ]
    }
    assert server._split_text_block(centered_heading, 600)[0]["align"] == "center"

    split_body_flow = [
        server._split_text_block(
            {"lines": [line("A nearly full standalone line", (75, 200, 530, 212), 11)]}, 600
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


def main() -> None:
    check_mixed_style_block_split()
    check_inline_style_markup()
    check_alignment_inference()
    with TemporaryDirectory() as directory:
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
                prepared["job_id"], [{"id": batch["segments"][0]["id"], "text": "Hola mundo"}]
            )
        except ValueError:
            pass
        else:
            raise AssertionError("submission without required style markers must fail")
        translations = [
            {"id": item["id"], "text": "Hola [[B]]mundo[[/B]]"}
            for item in batch["segments"]
        ]
        assert server.submit_translations(prepared["job_id"], translations)["remaining"] == 0
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
