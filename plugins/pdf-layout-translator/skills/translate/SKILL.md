---
name: translate
description: Translate an attached or local text-based PDF into a requested language while preserving page layout, images, and vector graphics. Use for PDF translation requests; do not use for scanned/image-only PDFs.
---

# Translate PDF

Use the `pdf-layout-translator` MCP tools and the active model as the translator. Do not use a separate translation API.

1. Identify the attached PDF's absolute local path and the target language. If either is missing, ask only for the missing item.
2. Call `prepare_translation`. Never overwrite the source PDF.
3. Call `get_translation_batch`. Translate every returned segment faithfully into the target language:
   - Preserve meaning and tone; do not summarize or add commentary.
   - Preserve names, URLs, identifiers, numbers, and placeholders unless translation is clearly appropriate.
   - Preserve every `[[B]]...[[/B]]` and `[[I]]...[[/I]]` pair exactly. Move the complete pair with its corresponding translated phrase when the target language reorders words. Never translate or drop the markers.
   - Keep each segment ID unchanged.
4. Call `submit_translations` with objects shaped as `{id, text}`.
   - If submission reports missing or invalid style markers, correct only those segments and retry.
5. Repeat steps 3–4 until `remaining` is zero, then call `render_translated_pdf`.
6. Open the resulting PDF in Codex when possible and return a clickable absolute file link. Mention fitting warnings briefly.

If preparation reports no selectable text, explain that OCR support is not included yet; do not fabricate a translated output.
