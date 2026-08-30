#!/usr/bin/env python3
"""Archive every primary source listed in scripts/sources.json.

Standard library only, so anyone can clone the repo and run it with no setup:

    python3 scripts/fetch_sources.py            # fetch what is missing
    python3 scripts/fetch_sources.py --force    # re-fetch everything
    python3 scripts/fetch_sources.py --only pipl-npc

For each source it stores the raw bytes under sources/raw/, a plain-text
rendering under sources/text/ (HTML only), and one record in
sources/MANIFEST.json containing the sha256 digest, byte count, HTTP status,
declared charset and the <title> actually served.

Failures are recorded, never swallowed: a source that 404s stays in the
manifest with ok=false so the gap is visible instead of silently missing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCES_FILE = REPO / "scripts" / "sources.json"
RAW_DIR = REPO / "sources" / "raw"
TEXT_DIR = REPO / "sources" / "text"
MANIFEST = REPO / "sources" / "MANIFEST.json"

# All timestamps in this repository are Beijing time (Asia/Shanghai, UTC+8).
BEIJING = timezone(timedelta(hours=8))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 30
RETRIES = 3
DELAY_SECONDS = 1.0  # be polite to government hosts


class _TextExtractor(HTMLParser):
    """Minimal HTML -> text renderer (stdlib only, no third-party parser)."""

    _SKIP = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.title_chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4"}:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title_chunks.append(data)
        self.chunks.append(data)

    @property
    def text(self) -> str:
        raw = "".join(self.chunks)
        raw = re.sub(r"[ \t　]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return "\n".join(line.strip() for line in raw.splitlines()).strip()

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_chunks).split())


def detect_charset(headers_charset: str | None, body: bytes) -> str:
    """Header charset wins; otherwise sniff the meta tag; otherwise utf-8."""
    if headers_charset:
        return headers_charset.lower()
    head = body[:4096].decode("ascii", errors="ignore").lower()
    m = re.search(r'charset=["\']?\s*([\w-]+)', head)
    if m:
        return m.group(1).lower()
    return "utf-8"


def decode(body: bytes, charset: str) -> str:
    # Chinese government sites serve a mix of utf-8 and GB-family encodings;
    # gb18030 is a superset of gbk/gb2312 so it is the safest fallback.
    for enc in (charset, "utf-8", "gb18030"):
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def extension_for(content_type: str, url: str) -> str:
    ct = (content_type or "").lower()
    if "pdf" in ct or url.lower().endswith(".pdf"):
        return "pdf"
    if "json" in ct:
        return "json"
    return "html"


def extract_pdf_text(path: Path) -> str | None:
    """Extract PDF text via poppler's pdftotext when it is installed.

    Optional by design: the repo stays runnable with a bare Python install,
    and simply records that the PDF text is unavailable when poppler is not
    present.
    """
    if not shutil.which("pdftotext"):
        return None
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            timeout=120,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", errors="replace")
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def fetch(url: str) -> tuple[bytes, dict]:
    """Return (body, meta). Raises on final failure."""
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read()
                return body, {
                    "http_status": resp.status,
                    "content_type": resp.headers.get("Content-Type", ""),
                    "headers_charset": resp.headers.get_content_charset(),
                    "final_url": resp.url,
                }
        except urllib.error.HTTPError as exc:
            # A 4xx will not improve on retry; record it and move on.
            raise exc
        except Exception as exc:  # noqa: BLE001 - network errors vary widely
            last_error = exc
            if attempt < RETRIES:
                time.sleep(attempt * 2)
    raise last_error  # type: ignore[misc]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-fetch already archived sources")
    parser.add_argument("--only", metavar="ID", help="fetch a single source by id")
    args = parser.parse_args()

    spec = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    sources = spec["sources"]
    if args.only:
        sources = [s for s in sources if s["id"] == args.only]
        if not sources:
            print(f"no source with id={args.only!r}", file=sys.stderr)
            return 2

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)

    previous: dict[str, dict] = {}
    if MANIFEST.exists():
        previous = {r["id"]: r for r in json.loads(MANIFEST.read_text(encoding="utf-8"))["records"]}

    records: list[dict] = []
    ok_count = failed = skipped = 0

    for src in spec["sources"]:
        sid = src["id"]
        if args.only and sid != args.only:
            records.append(previous.get(sid, {"id": sid, "ok": False, "error": "not fetched"}))
            continue

        prior = previous.get(sid)
        if prior and prior.get("ok") and not args.force:
            records.append(prior)
            skipped += 1
            print(f"  skip  {sid}")
            continue

        record = {
            "id": sid,
            "title": src["title"],
            "url": src["url"],
            "category": src["category"],
            "publisher": src["publisher"],
            "official": src.get("official", False),
            "retrieved_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
        }

        try:
            body, meta = fetch(src["url"])
        except urllib.error.HTTPError as exc:
            record.update(ok=False, http_status=exc.code, error=f"HTTP {exc.code} {exc.reason}")
            failed += 1
            print(f"  FAIL  {sid}: HTTP {exc.code}")
            records.append(record)
            time.sleep(DELAY_SECONDS)
            continue
        except Exception as exc:  # noqa: BLE001
            record.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            failed += 1
            print(f"  FAIL  {sid}: {type(exc).__name__}")
            records.append(record)
            time.sleep(DELAY_SECONDS)
            continue

        ext = extension_for(meta["content_type"], meta["final_url"])
        raw_path = RAW_DIR / f"{sid}.{ext}"
        raw_path.write_bytes(body)

        record.update(
            ok=True,
            http_status=meta["http_status"],
            final_url=meta["final_url"],
            content_type=meta["content_type"],
            bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            raw_path=str(raw_path.relative_to(REPO)),
        )

        if ext == "html":
            charset = detect_charset(meta["headers_charset"], body)
            html = decode(body, charset)
            extractor = _TextExtractor()
            try:
                extractor.feed(html)
            except Exception:  # noqa: BLE001 - malformed markup must not abort the run
                pass
            text_path = TEXT_DIR / f"{sid}.txt"
            text_path.write_text(extractor.text, encoding="utf-8")
            record["charset"] = charset
            record["text_path"] = str(text_path.relative_to(REPO))
            record["text_chars"] = len(extractor.text)
            # Quotes are verified against the text layer, so the text layer needs
            # its own digest: hashing only raw/ would leave edits to text/
            # undetected wherever they fall outside a quoted span.
            record["text_sha256"] = hashlib.sha256(
                text_path.read_bytes()
            ).hexdigest()
            # served_title lets a reviewer spot a URL that points at the wrong page.
            record["served_title"] = extractor.title
        elif ext == "pdf":
            text = extract_pdf_text(raw_path)
            if text is None:
                record["note"] = "pdf; install poppler (pdftotext) to extract text"
            else:
                text_path = TEXT_DIR / f"{sid}.txt"
                text_path.write_text(text, encoding="utf-8")
                record["text_path"] = str(text_path.relative_to(REPO))
                record["text_chars"] = len(text)
                record["text_sha256"] = hashlib.sha256(text_path.read_bytes()).hexdigest()
                record["note"] = "text extracted with pdftotext (poppler)"
        else:
            record["note"] = f"binary ({ext}); no text extraction"

        ok_count += 1
        print(f"  ok    {sid}  {len(body):>8,}B  sha256:{record['sha256'][:12]}")
        records.append(record)
        time.sleep(DELAY_SECONDS)

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    if ok_count == 0 and failed == 0 and MANIFEST.exists():
        # Nothing was fetched, so rewriting the manifest would only churn
        # generated_at and leave a dirty tree after a no-op run.
        print(f"\nnothing to fetch (skipped={skipped}); manifest unchanged")
        return 0
    MANIFEST.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(BEIJING).isoformat(timespec="seconds"),
                "timezone": "Asia/Shanghai (UTC+8)",
                "total": len(records),
                "ok": sum(1 for r in records if r.get("ok")),
                "failed": sum(1 for r in records if not r.get("ok")),
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"\nfetched={ok_count} skipped={skipped} failed={failed} -> {MANIFEST.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
