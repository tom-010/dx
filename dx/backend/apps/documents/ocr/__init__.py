"""Gemini-vision OCR: a PDF page image in, a validated list of blocks out, and deterministic
Python from there to a snapshot (Agent Brief 03).

Map, then reduce. **Map** (`gemini_client.py`): one request per page image returns a JSON list
of blocks in reading order — kind, heading level, text (tables as HTML), a bounding box, and
a continuation flag informed by the previous page's tail. **Reduce** (`assembly.py`): pure
Python merges cross-page fragments, groups lists, pairs figures with captions, derives the
section tree from the heading stream and hands the result to the snapshot builder as the
extraction tree every other strategy produces (`apps/documents/extraction.py`) — the builder
serializes one sanitized HTML, measures every offset on the stored string, dates the tree and
writes the rows (Brief 01 §4). The model is per-page perception and nothing else; every global
decision is code with fixtures. Its output is untrusted input: validated (`page_schema.py`),
clamped, sanitized.

`manage.py ocr extract|assemble|run` (`management/commands/ocr.py`) is the iteration loop over
the same core without a database; `strategies.GeminiOcrStrategy` is the production path. Both
write the same per-page raw JSON, so `assemble` replays a production run bit for bit.

Confidence: Gemini reports none per word, so `conf_stats` stays NULL everywhere ("no per-word
confidence data available") and the model is never asked to rate itself.
"""
