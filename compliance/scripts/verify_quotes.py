#!/usr/bin/env python3
"""Check that every quoted span really occurs in the archived source text.

    python3 scripts/verify_quotes.py extracted.json
    python3 scripts/verify_quotes.py extracted.json --write-verified obligations.json

Input is a JSON list (or {"items": [...]}) where each record has at least
`source_id` and `quote`.

Why a script and not a reviewer agent: a quote either appears in the file or it
does not. That is decidable, so decide it — asking a model whether a model
quoted correctly reintroduces exactly the failure mode being checked.

Government pages reflow whitespace and mix full-width/half-width punctuation, so
comparison is done on a normalised copy: whitespace removed, full-width brackets
and punctuation folded to a canonical form. Everything else must match exactly —
a paraphrase still fails.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEXT_DIR = REPO / "sources" / "text"

_WS_RE = re.compile(r"\s+")
# Fold the punctuation pairs that vary between rendering and copying.
_FOLD = str.maketrans({
    "（": "(", "）": ")", "，": ",", "。": ".", "；": ";", "：": ":",
    "！": "!", "？": "?", "“": '"', "”": '"', "‘": "'", "’": "'",
    "、": ",", "　": "", "​": "", "﻿": "",
})


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_FOLD)
    return _WS_RE.sub("", text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="JSON file of extracted items")
    parser.add_argument("--write-verified", metavar="PATH", help="write only the verified items")
    parser.add_argument("--min-len", type=int, default=8, help="reject quotes shorter than this")
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    items = payload.get("items", payload) if isinstance(payload, dict) else payload

    cache: dict[str, str] = {}

    def source_text(sid: str) -> str | None:
        if sid not in cache:
            path = TEXT_DIR / f"{sid}.txt"
            if not path.exists():
                return None
            cache[sid] = normalise(path.read_text(encoding="utf-8"))
        return cache[sid]

    verified, failed = [], []
    for item in items:
        sid = (item.get("source_id") or "").strip()
        quote = (item.get("quote") or "").strip()
        if not sid or not quote:
            failed.append((item, "missing source_id or quote"))
            continue
        if len(quote) < args.min_len:
            failed.append((item, f"quote too short ({len(quote)} chars)"))
            continue
        haystack = source_text(sid)
        if haystack is None:
            failed.append((item, f"no archived text for source_id {sid!r}"))
            continue
        if normalise(quote) in haystack:
            verified.append(item)
        else:
            failed.append((item, "quote not found in archived text"))

    total = len(items)
    print(f"quotes checked: {total}")
    print(f"  verified: {len(verified)}")
    print(f"  FAILED:   {len(failed)}")

    if failed:
        print("\nrejected items (these must NOT enter the library):")
        for item, reason in failed:
            print(f"\n  [{reason}]")
            print(f"    source_id : {item.get('source_id')}")
            print(f"    obligation: {(item.get('obligation') or '')[:88]}")
            print(f"    quote     : {(item.get('quote') or '')[:88]}")

    if args.write_verified:
        Path(args.write_verified).write_text(
            json.dumps(verified, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n-> {args.write_verified} ({len(verified)} items)")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
