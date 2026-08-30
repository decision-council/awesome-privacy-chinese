#!/usr/bin/env python3
"""Strict validator for the compliance library. Standard library only.

    python3 scripts/validate.py            # validate everything
    python3 scripts/validate.py --quiet    # only print failures

Enforces the rules in CONTRIBUTING.md so that "anyone can contribute" does not
degrade into "anyone can add an unsourced claim":

  1. schema conformance for every record in data/*.json (and data/*.csv headers)
  2. ISO dates only (YYYY-MM-DD), and effective_date >= promulgated_date
  3. every source_id resolves to an archived source with ok=true
  4. every 'disputed' record carries a conflict_note
  5. no duplicate ids, in data or in scripts/sources.json
  6. archived bytes still match the sha256 recorded in the manifest (tamper check)
  7. every URL cited in docs/ that belongs to an archived source is reachable
     through the manifest (catches links drifting away from the archive)

Exit code is non-zero when any check fails, so CI blocks the merge.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
DATA = REPO / "data"
SCHEMA_DIR = DATA / "schema"
DOCS = REPO / "docs"
MANIFEST = REPO / "sources" / "MANIFEST.json"
SOURCES_SPEC = REPO / "scripts" / "sources.json"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERIFICATION_VALUES = {"confirmed", "disputed", "unverified"}

errors: list[str] = []
warnings: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


# --------------------------------------------------------------------------
# Minimal JSON-Schema subset validator.
# Deliberately hand-rolled: keeping the repo dependency-free means a
# contributor with a bare Python install can run the same gate CI runs.
# Supported: type, required, enum, pattern, items, properties, minLength.
# --------------------------------------------------------------------------

TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
}


def validate_schema(value, schema: dict, path: str) -> None:
    expected = schema.get("type")
    if expected:
        py = TYPES.get(expected)
        if py and not isinstance(value, py):
            err(f"{path}: expected {expected}, got {type(value).__name__}")
            return
        if expected == "integer" and isinstance(value, bool):
            err(f"{path}: expected integer, got boolean")
            return

    if "enum" in schema and value not in schema["enum"]:
        err(f"{path}: {value!r} not in {schema['enum']}")

    if isinstance(value, str):
        if "pattern" in schema and not re.match(schema["pattern"], value):
            err(f"{path}: {value!r} does not match {schema['pattern']}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            err(f"{path}: shorter than minLength {schema['minLength']}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value or value[key] in (None, ""):
                err(f"{path}: missing required field {key!r}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value and value[key] not in (None, ""):
                validate_schema(value[key], sub, f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    err(f"{path}: unexpected field {key!r}")

    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            validate_schema(item, schema["items"], f"{path}[{i}]")


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


_PUNCT_RE = re.compile(r"[\s《》（）()「」\[\]、,,。:：·\-—_|]")
# Site-name suffixes every CAC/NPC page appends to its <title>.
_SITE_SUFFIX_RE = re.compile(
    r"(中央网络安全和信息化委员会办公室|中华人民共和国国家互联网信息办公室|中国人大网|中国政府网|新华网)"
)


def titles_agree(declared: str, served: str, prefix: int = 12) -> bool:
    """Does the page we archived look like the document we asked for?

    Compared on a punctuation-stripped prefix, because served titles carry a
    site-name suffix and declared titles may carry our own parenthetical notes
    ("(第3页,含附则)"). Either one containing the other's head is enough.
    """
    d = _PUNCT_RE.sub("", _SITE_SUFFIX_RE.sub("", declared or ""))
    s = _PUNCT_RE.sub("", _SITE_SUFFIX_RE.sub("", served or ""))
    if not d or not s:
        return True
    return d[:prefix] in s or s[:prefix] in d


def load_manifest() -> dict[str, dict]:
    if not MANIFEST.exists():
        err("sources/MANIFEST.json missing — run scripts/fetch_sources.py first")
        return {}
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {r["id"]: r for r in data["records"]}


def check_sources_spec() -> None:
    spec = json.loads(SOURCES_SPEC.read_text(encoding="utf-8"))
    seen: set[str] = set()
    for src in spec["sources"]:
        sid = src["id"]
        if sid in seen:
            err(f"scripts/sources.json: duplicate id {sid!r}")
        seen.add(sid)
        for field in ("id", "title", "url", "category", "publisher"):
            if not src.get(field):
                err(f"scripts/sources.json[{sid}]: missing {field}")
        if not str(src.get("url", "")).startswith(("http://", "https://")):
            err(f"scripts/sources.json[{sid}]: url must be absolute")


def check_archive_integrity(manifest: dict[str, dict]) -> None:
    """Recompute sha256 for every archived file — detects silent edits."""
    for sid, rec in manifest.items():
        if not rec.get("ok"):
            warn(f"source {sid!r} is not archived: {rec.get('error', 'unknown error')}")
            continue
        raw = REPO / rec["raw_path"]
        if not raw.exists():
            err(f"source {sid!r}: archived file missing at {rec['raw_path']}")
            continue
        digest = hashlib.sha256(raw.read_bytes()).hexdigest()
        if digest != rec["sha256"]:
            err(
                f"source {sid!r}: sha256 mismatch — archived file was modified "
                f"(manifest {rec['sha256'][:12]}, file {digest[:12]}). "
                f"Re-fetch with --only {sid} instead of hand-editing."
            )
        # The text layer is what quotes are checked against, so it is tamper-
        # evidence too — an edit outside any quoted span would otherwise pass.
        if rec.get("text_path"):
            text_file = REPO / rec["text_path"]
            if not text_file.exists():
                err(f"source {sid!r}: text layer missing at {rec['text_path']}")
            elif rec.get("text_sha256"):
                tdigest = hashlib.sha256(text_file.read_bytes()).hexdigest()
                if tdigest != rec["text_sha256"]:
                    err(
                        f"source {sid!r}: text_sha256 mismatch — the extracted text was "
                        f"modified (manifest {rec['text_sha256'][:12]}, file {tdigest[:12]})."
                    )
            else:
                warn(f"source {sid!r}: no text_sha256 recorded — re-fetch to add one")
        served = rec.get("served_title", "")
        # A government portal answering 200 with its homepage is the classic trap:
        # the request succeeds, the archive looks fine, and the content is wrong.
        if served and served.strip() in {"首页", "中国人大网", "中国政府网"}:
            err(
                f"source {sid!r}: served_title is {served!r} — the URL resolves to a "
                f"portal index rather than the document. Fix the URL and re-fetch."
            )
        elif served and not titles_agree(rec.get("title", ""), served):
            warn(
                f"source {sid!r}: declared title and served title diverge\n"
                f"      declared: {rec.get('title','')[:60]}\n"
                f"      served:   {served[:60]}"
            )
        if served and rec.get("text_chars", 0) < 400:
            warn(f"source {sid!r}: only {rec.get('text_chars', 0)} chars of text — likely a stub or image-only page")


def check_json_datasets(manifest: dict[str, dict]) -> None:
    for path in sorted(DATA.glob("*.json")):
        schema_path = SCHEMA_DIR / f"{path.stem}.schema.json"
        if not schema_path.exists():
            err(f"{path.relative_to(REPO)}: no schema at data/schema/{path.stem}.schema.json")
            continue
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("records", payload if isinstance(payload, list) else [])
        validate_schema(records, {"type": "array", "items": schema}, path.stem)

        seen: set[str] = set()
        for rec in records:
            rid = rec.get("id", "?")
            if rid in seen:
                err(f"{path.stem}: duplicate id {rid!r}")
            seen.add(rid)
            check_common_rules(rec, f"{path.stem}[{rid}]", manifest)


def check_common_rules(rec: dict, where: str, manifest: dict[str, dict]) -> None:
    for field, value in rec.items():
        if field.endswith("_date") and value:
            if not DATE_RE.match(str(value)):
                err(f"{where}.{field}: {value!r} is not YYYY-MM-DD")

    promulgated = rec.get("promulgated_date")
    effective = rec.get("effective_date")
    if promulgated and effective and DATE_RE.match(str(promulgated)) and DATE_RE.match(str(effective)):
        if str(effective) < str(promulgated):
            err(f"{where}: effective_date {effective} precedes promulgated_date {promulgated}")

    sid = rec.get("source_id")
    if sid:
        for one in [s.strip() for s in str(sid).split(",") if s.strip()]:
            if one not in manifest:
                err(f"{where}.source_id: {one!r} is not in sources/MANIFEST.json")
            elif not manifest[one].get("ok"):
                err(f"{where}.source_id: {one!r} is archived as failed")

    verification = rec.get("verification")
    if verification:
        if verification not in VERIFICATION_VALUES:
            err(f"{where}.verification: {verification!r} not in {sorted(VERIFICATION_VALUES)}")
        if verification == "disputed" and not rec.get("conflict_note"):
            err(f"{where}: verification='disputed' requires a conflict_note")
        if verification == "confirmed" and not rec.get("evidence_quote"):
            err(f"{where}: verification='confirmed' requires an evidence_quote from the archive")


def check_csv_datasets(manifest: dict[str, dict]) -> None:
    for path in sorted(DATA.glob("*.csv")):
        schema_path = SCHEMA_DIR / f"{path.stem}.schema.json"
        if not schema_path.exists():
            err(f"{path.relative_to(REPO)}: no schema at data/schema/{path.stem}.schema.json")
            continue
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        expected_cols = list(schema.get("properties", {}))
        with path.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            actual = reader.fieldnames or []
            if actual != expected_cols:
                err(
                    f"{path.stem}: column mismatch\n"
                    f"    expected: {expected_cols}\n"
                    f"    actual:   {actual}"
                )
                return
            props = schema.get("properties", {})
            for i, row in enumerate(reader, start=2):
                clean = {k: v for k, v in row.items() if v not in (None, "")}
                # CSV yields strings for every cell; coerce the fields the schema
                # declares numeric so type checking means something here.
                for key, value in list(clean.items()):
                    declared = props.get(key, {}).get("type")
                    if declared in ("integer", "number"):
                        try:
                            clean[key] = int(value) if declared == "integer" else float(value)
                        except ValueError:
                            err(f"{path.stem}:line{i}.{key}: {value!r} is not {declared}")
                            del clean[key]
                validate_schema(clean, schema, f"{path.stem}:line{i}")
                check_common_rules(clean, f"{path.stem}:line{i}", manifest)


def check_quotes(manifest: dict[str, dict]) -> None:
    """Every `quote` field must occur verbatim in its archived source text.

    This is the gate that stops a plausible-sounding but invented provision
    from entering the library: a quote either is in the file or it is not.
    """
    try:
        from verify_quotes import normalise  # same folder
    except ImportError:  # pragma: no cover - only if the file is removed
        warn("scripts/verify_quotes.py missing — quote verification skipped")
        return

    text_dir = REPO / "sources" / "text"
    cache: dict[str, str] = {}

    def haystack(sid: str) -> str | None:
        if sid not in cache:
            path = text_dir / f"{sid}.txt"
            if not path.exists():
                return None
            cache[sid] = normalise(path.read_text(encoding="utf-8"))
        return cache[sid]

    checked = 0
    for path in sorted(list(DATA.glob("*.csv")) + list(DATA.glob("*.json"))):
        if path.parent.name == "schema":
            continue
        if path.suffix == ".csv":
            with path.open(encoding="utf-8", newline="") as fh:
                records = list(csv.DictReader(fh))
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = payload.get("records", payload if isinstance(payload, list) else [])

        for i, rec in enumerate(records, start=2):
            for field in ("quote", "evidence_quote"):
                quote = (rec.get(field) or "").strip()
                if not quote:
                    continue
                sids = [s.strip() for s in str(rec.get("source_id", "")).split(",") if s.strip()]
                if not sids:
                    err(f"{path.stem}:{rec.get('id', i)}: has {field} but no source_id")
                    continue
                checked += 1
                # An evidence_quote may stitch two passages with an ellipsis.
                fragments = [f.strip() for f in re.split(r"\s*…+\s*|\s*\.\.\.\s*", quote) if f.strip()]
                for frag in fragments:
                    if len(normalise(frag)) < 6:
                        continue
                    if not any(
                        (h := haystack(s)) is not None and normalise(frag) in h for s in sids
                    ):
                        err(
                            f"{path.stem}:{rec.get('id', i)}.{field}: not found verbatim in "
                            f"{sids} — {frag[:44]!r}"
                        )
    if checked:
        print(f"  quotes verified verbatim against archives: {checked}")

    # A verified quote does not protect the prose beside it: an obligation can
    # summarise "20 个工作日" as "200 个工作日" and the quote still passes.
    # So every quantity stated in the summary must also occur in the source.
    quantity_re = re.compile(
        r"\d+\s*(?:个工作日|个月|年|日内|万人|人以上|%|)(?=[^\d]|$)"
    )
    numeric_checked = 0
    for path in sorted(DATA.glob("*.csv")):
        if path.parent.name == "schema":
            continue
        with path.open(encoding="utf-8", newline="") as fh:
            for i, rec in enumerate(csv.DictReader(fh), start=2):
                summary = (rec.get("obligation") or "").strip()
                if not summary:
                    continue
                sids = [s.strip() for s in str(rec.get("source_id", "")).split(",") if s.strip()]
                for token in {t.strip() for t in quantity_re.findall(summary) if t.strip()}:
                    norm = normalise(token)
                    if len(norm) < 3:
                        continue  # bare digits are too noisy to police
                    numeric_checked += 1
                    if not any(
                        (h := haystack(s)) is not None and norm in h for s in sids
                    ):
                        err(
                            f"{path.stem}:{rec.get('id', i)}.obligation: quantity {token!r} "
                            f"does not occur in {sids} — the summary may have drifted from the source"
                        )
    if numeric_checked:
        print(f"  quantities in obligation summaries checked against archives: {numeric_checked}")


def check_docs_links(manifest: dict[str, dict]) -> None:
    """Every archived URL should be the one docs actually cite."""
    archived_urls = {rec["url"] for rec in manifest.values() if rec.get("ok")}
    cited: set[str] = set()
    for md in sorted(DOCS.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        cited.update(re.findall(r"https?://[^\s<>()\[\]\"]+", text))
    def is_document_url(u: str) -> bool:
        # Bare hostnames in the "稳定来源" sections are navigation links, not
        # citations, and can never be archived — warning on them forever is noise.
        path = re.sub(r"^https?://[^/]+", "", u)
        return path not in ("", "/")

    gov_cited = {
        u.rstrip(".,;)")
        for u in cited
        if re.search(r"(gov\.cn|tc260\.org\.cn|samr\.gov\.cn|npc\.gov\.cn)", u)
        and is_document_url(u)
    }
    unarchived = sorted(gov_cited - archived_urls)
    for url in unarchived:
        warn(f"docs cite an official URL that is not archived: {url}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    manifest = load_manifest()
    check_sources_spec()
    check_archive_integrity(manifest)
    check_json_datasets(manifest)
    check_csv_datasets(manifest)
    check_quotes(manifest)
    check_docs_links(manifest)

    if warnings and not args.quiet:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  ! {w}")

    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors:
            print(f"  x {e}")
        print("\nFAILED")
        return 1

    archived = sum(1 for r in manifest.values() if r.get("ok"))
    print(f"\nOK — {archived} sources archived and intact, all datasets valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
