#!/usr/bin/env python3
"""Enumerate official documents from CAC topical columns and report what is
not yet archived.

    python3 scripts/discover_sources.py                # list candidates
    python3 scripts/discover_sources.py --json out.json  # machine-readable
    python3 scripts/discover_sources.py --hub <url>     # crawl one extra hub

Why this exists: searching for regulations one at a time is slow and misses
things. The CAC publishes topical index pages ("数据专栏") that enumerate every
document it considers part of a topic — crawling those is deterministic,
cheap, and complete in a way that keyword search is not.

Two gotchas this script encodes, both found the hard way:

1. CAC index pages write attributes WITHOUT quotes (`href=//www.cac.gov.cn/...`).
   A regex expecting `href="..."` silently returns zero links and the page
   looks empty. The pattern below tolerates both forms.
2. Index pages paginate as `...index_1.htm`, `...index_2.htm`, and a page past
   the end still answers 200 with an empty list rather than 404, so crawling
   must stop on "no new links", not on HTTP status.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCES_FILE = REPO / "scripts" / "sources.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 30
DELAY_SECONDS = 0.8
MAX_PAGES = 12

# CAC 数据专栏 (wxzw/sjzl) — the topical hubs for data compliance.
CAC_HUBS = {
    "数据专栏总入口": "https://www.cac.gov.cn/wxzw/sjzl/A093708index_1.htm",
    "政策法规": "https://www.cac.gov.cn/wxzw/sjzl/zcfg/A09370805index_1.htm",
    "数据出境安全评估": "https://www.cac.gov.cn/wxzw/sjzl/sjcjaqpg/A09370801index_1.htm",
    "个人信息出境标准合同": "https://www.cac.gov.cn/wxzw/sjzl/grxxcjbzht/A09370802index_1.htm",
    "个人信息出境认证": "https://www.cac.gov.cn/wxzw/sjzl/grxxcjrz/A09370803index_1.htm",
    "个人信息保护": "https://www.cac.gov.cn/wxzw/sjzl/grxxbh/A09370804index_1.htm",
    "数据出境负面清单": "https://www.cac.gov.cn/wxzw/sjzl/sjcjfmqd/A09370806index_1.htm",
    "工作动态": "https://www.cac.gov.cn/wxzw/sjzl/gzdt/A09370807index_1.htm",
}

# Tolerates href="x", href='x' and bare href=x (CAC uses the last form).
LINK_RE = re.compile(
    r"""href=(?:"([^"]+)"|'([^']+)'|([^\s>]+))[^>]*?title=(?:"([^"]*)"|'([^']*)')""",
    re.I,
)
DOC_RE = re.compile(r"/c_\d{10,}\.htm")
DATE_RE = re.compile(r'class="times">\s*(\d{4}-\d{2}-\d{2})')


def fetch(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def normalise(href: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://www.cac.gov.cn" + href
    return href


def extract_documents(html: str) -> list[dict]:
    """Pull (url, title) pairs for document links out of an index page."""
    out: list[dict] = []
    seen: set[str] = set()
    for match in LINK_RE.finditer(html):
        href = match.group(1) or match.group(2) or match.group(3) or ""
        title = (match.group(4) or match.group(5) or "").strip()
        if not href or not DOC_RE.search(href) or not title:
            continue
        url = normalise(href)
        if url in seen:
            continue
        seen.add(url)
        # The publication date sits in a sibling <div class="times">.
        tail = html[match.end() : match.end() + 400]
        date_match = DATE_RE.search(tail)
        out.append({"url": url, "title": title, "date": date_match.group(1) if date_match else ""})
    return out


def crawl_hub(name: str, base_url: str) -> list[dict]:
    """Walk index_1, index_2 … until a page yields no new documents."""
    found: dict[str, dict] = {}
    for page in range(1, MAX_PAGES + 1):
        url = re.sub(r"index_\d+\.htm$", f"index_{page}.htm", base_url)
        html = fetch(url)
        if html is None:
            break
        docs = extract_documents(html)
        fresh = [d for d in docs if d["url"] not in found]
        if not fresh:
            break  # past the end: 200 with nothing new
        for d in fresh:
            d["hub"] = name
            found[d["url"]] = d
        time.sleep(DELAY_SECONDS)
    return list(found.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", metavar="PATH", help="write candidates as JSON")
    parser.add_argument("--hub", metavar="URL", action="append", default=[], help="extra hub URL")
    parser.add_argument("--all", action="store_true", help="list archived ones too")
    args = parser.parse_args()

    spec = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    archived = {s["url"].replace("http://", "https://") for s in spec["sources"]}

    hubs = dict(CAC_HUBS)
    for i, extra in enumerate(args.hub, start=1):
        hubs[f"custom-{i}"] = extra

    all_docs: list[dict] = []
    for name, url in hubs.items():
        docs = crawl_hub(name, url)
        print(f"  {name:<24} {len(docs):>3} 篇", file=sys.stderr)
        all_docs.extend(docs)

    by_url: dict[str, dict] = {}
    for d in all_docs:
        by_url.setdefault(d["url"], d)

    candidates = [
        d for d in by_url.values()
        if args.all or d["url"].replace("http://", "https://") not in archived
    ]
    candidates.sort(key=lambda d: (d.get("date") or "", d["title"]), reverse=True)

    print(f"\n枚举 {len(by_url)} 篇,其中未收录 {len(candidates)} 篇\n", file=sys.stderr)
    for d in candidates:
        print(f"{d.get('date','          '):<12} [{d['hub']}] {d['title']}")
        print(f"             {d['url']}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n-> {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
