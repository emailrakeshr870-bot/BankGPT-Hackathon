"""
Build rag/chunks.jsonl: every page of every snapshot, cut into search-sized chunks, de-duplicated
across snapshots and tagged so an n8n / Pinecone workflow can tell current from stale.

    python rag/build_chunks.py

One JSON object per line:
  id             stable id (hash of url + text)
  text           the chunk, starting with a short header (page title, URL, section, dates)
  url, title     where it came from
  page_type      rates | product | promo | news | blog | resource | calculator | pdf | other
  authority      "1.0" (official rates page) ... "0.15" (calculator); higher = more trustworthy
  snap_1..snap_5 "true"/"false": was this exact text on the site in that snapshot?
  snapshots      "1,2,3,4,5" (same information, human-readable)
  first_seen, last_seen   snapshot dates (YYYY-MM-DD) this text was seen
  effective_date "Rates are effective as of ..." date of a rate table, YYYY-MM-DD or ""
  published_date article date (newsroom/blog) or newest date stamp on the page, YYYY-MM-DD or ""
  has_table      "true" if the chunk contains a rate table
All metadata values are strings so they survive n8n unchanged; Pinecone filters use e.g.
{"snap_5": {"$eq": "true"}}.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SNAPS = ROOT / "snapshots"
OUT = ROOT / "rag" / "chunks.jsonl"
MAX_CHARS = 1600          # target chunk size (~400 tokens)
TABLE_MAX = 3200          # longer tables are split by rows (header repeated)
OVERLAP = 250             # prose chunks repeat up to this many characters (whole sentences) of the previous one

MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                          "august", "september", "october", "november", "december"])}


def parse_date(s: str) -> date | None:
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)
    if m:
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            return date(y, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    m = re.search(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})\b", s, re.I)
    if m:
        try:
            return date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            return None
    return None


def page_type(url: str) -> tuple[str, float]:
    p = urlparse(url)
    host, path = p.netloc.lower(), p.path.lower().rstrip("/")
    if host and host not in ("www.dupaco.com", "dupaco.com"):
        return "other", 0.25
    if path in ("/rates", "/business-rates") or path.startswith("/rates/"):
        return "rates", 1.0
    if "calculator" in path:
        return "calculator", 0.15
    if path.endswith("-lp") or any(k in path for k in ("switch-and-earn", "offer", "promo", "earn-more")):
        return "promo", 0.7
    if "/hubfs" in path or path.endswith(".pdf"):
        return "pdf", 0.25
    if "/resources/" in path:
        return "resource", 0.35
    if path.startswith("/newsroom"):
        return "news", 0.35
    if path.startswith("/learn") or "/blog" in path or re.match(r"/20\d\d/", path):
        return "blog", 0.35
    if path in ("", "/contact", "/membership", "/about", "/about/join") or path.startswith(("/personal", "/business", "/about")):
        return "product", 0.85
    return "other", 0.6


def skip_page(url: str) -> bool:
    p = urlparse(url)
    path = p.path.lower()
    return (("privacy-policy" in path and p.query) or bool(re.search(r"/(tag|author|category|page)/", path))
            or path.rstrip("/") in ("/newsroom", "/learn", "/learn/blog"))


def clean(s: str) -> str:
    s = s.replace("\\r\\n", " ").replace("\\n", " ").replace("\\*", "*").replace("\\#", "#").replace("\\$", "$")
    s = s.replace("\\_", "_").replace("\\|", "/")
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)                       # images (incl. data: URIs)

    def link(m):
        text, href = m.group(1).strip(), m.group(2).strip()
        if href.startswith(("tel:", "mailto:", "#")) or not text:
            return text
        href = href.split("?")[0]
        if "dupaco.com" in href and len(text) < 60:
            return f"{text} ({href})"
        return text
    s = re.sub(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)", link, s)
    s = re.sub(r"(\d%)\d{1,2}\b", r"\1", s)                            # 4.20%1 -> 4.20% (footnote marks)
    s = re.sub(r"\b(APR|APY)[¹²³\d]\b", r"\1", s)
    s = s.replace("**", "").replace("¹", "").replace("²", "").replace("³", "")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def blocks(md: str):
    """Yield (kind, heading_path, text) blocks: whole tables (with lead-in + effective line) or prose."""
    lines = [l.rstrip() for l in md.split("\n")]
    n, i = len(lines), 0
    heads: list[tuple[int, str]] = []
    recent: list[str] = []
    prose: list[str] = []

    def hp():
        return " > ".join(h for _, h in heads[-3:])

    def flush():
        if prose:
            t = clean("\n".join(prose))
            if len(re.findall(r"[A-Za-z]{3,}", t)) >= 4:
                yield ("text", hp(), t)
            prose.clear()

    while i < n:
        st = lines[i].strip()
        if st.startswith("|") and i + 1 < n and re.match(r"^\s*\|\s*:?-{2,}", lines[i + 1]):
            yield from flush()
            j = i + 2
            while j < n and lines[j].strip().startswith("|"):
                j += 1
            table = [clean(l) for l in lines[i:j]]
            after, k = [], j
            while k < n and len(after) < 3:
                if lines[k].strip().startswith("|") or lines[k].strip().startswith("#"):
                    break
                if lines[k].strip():
                    after.append(clean(lines[k].strip())[:600])
                k += 1
            lead = [r for r in recent[-2:]]
            eff_lines = [a for a in after if re.search(r"effective|compounded|minimum", a, re.I)]
            yield ("table", hp(), "\n".join(lead + table + eff_lines[:2]))
            recent.clear()
            i = j
            continue
        if st.startswith("#"):
            yield from flush()
            lvl = len(st) - len(st.lstrip("#"))
            h = re.sub(r"\s*\(https?://[^)]*\)", "", clean(st.lstrip("#").strip()))
            if re.match(r"(recent news|related (articles|posts|stories|news|content)|handpicked for you|"
                        r"you (may|might) also like|more (news|stories|articles)|latest news)\b", h, re.I):
                return          # sidebar / teasers for other pages: not this page's content
            heads[:] = [x for x in heads if x[0] < lvl] + [(lvl, h)]
            recent.append(h)
            i += 1
            continue
        if st:
            c = clean(st)
            if 0 < len(c) < 80 and not st.startswith(("[", "!", "|")):
                recent.append(c)
                recent[:] = recent[-4:]
            prose.append(st)
        else:
            if prose and sum(len(x) for x in prose) > MAX_CHARS:
                yield from flush()
            elif prose:
                prose.append("")
        i += 1
    yield from flush()


def tail(text: str, limit: int = OVERLAP) -> str:
    """The last whole sentence(s) of a chunk, up to `limit` characters, for overlap."""
    sents = re.split(r"(?<=[.!?])\s+", text.strip())
    out = ""
    for s_ in reversed(sents):
        if len(out) + len(s_) + 1 > limit:
            break
        out = f"{s_} {out}".strip()
    return out or text.strip()[-limit:]


def split_long(kind: str, text: str):
    if kind == "table":
        if len(text) <= TABLE_MAX:
            return [text]
        rows = text.split("\n")
        hdr_idx = next((k for k, r in enumerate(rows) if r.startswith("|")), 0)
        head = rows[:hdr_idx + 2]
        body = [r for r in rows[hdr_idx + 2:] if r.startswith("|")]
        tail = [r for r in rows[hdr_idx + 2:] if not r.startswith("|")]
        out, cur = [], []
        for r in body:
            if sum(len(x) for x in head + cur + [r]) > TABLE_MAX and cur:
                out.append("\n".join(head + cur + tail))
                cur = []
            cur.append(r)
        if cur:
            out.append("\n".join(head + cur + tail))
        return out
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        while len(p) > MAX_CHARS:                     # very long paragraph: cut at sentence ends
            cut = p.rfind(". ", 0, MAX_CHARS)
            cut = cut + 1 if cut > MAX_CHARS // 3 else MAX_CHARS
            if cur:
                out.append(cur)
                cur = ""
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if len(cur) + len(p) + 2 > MAX_CHARS and cur:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return out


def main():
    snaps = sorted(d for d in SNAPS.iterdir() if (d / "manifest.json").exists())
    if not snaps:
        sys.exit("No snapshots found. Run the scorer once (or unzip snapshots_zip/) first.")
    records: dict[str, dict] = {}
    for si, d in enumerate(snaps, start=1):
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        taken = m["taken_on"][:10]
        for p in m["pages"]:
            url, title = p["url"], (p.get("title") or "").strip()
            if skip_page(url):
                continue
            md = (d / p["file"]).read_text(encoding="utf-8")
            ptype, auth = page_type(url)
            pub = parse_date(md[:600])
            if not pub:
                stamps = [parse_date(x) for x in re.findall(r"(?:effective|updated)\s+(?:as\s+of|on)?\s*(\d{1,2}/\d{1,2}/\d{2,4})", md, re.I)]
                stamps = [x for x in stamps if x]
                pub = max(stamps) if stamps else None
            prev_text, prev_head = None, None
            for kind, heading, text in blocks(md):
                for piece in split_long(kind, text):
                    if kind == "text":
                        # overlap: continue from the end of the previous prose chunk in the same section
                        if prev_text and prev_head == heading:
                            piece = "… " + tail(prev_text) + "\n\n" + piece
                        prev_text, prev_head = piece.split("\n\n", 1)[-1] if piece.startswith("… ") else piece, heading
                    else:
                        prev_text, prev_head = None, None
                    eff = None
                    if kind == "table":
                        em = re.search(r"effective\s+(?:as\s+of|on)?\s*(\d{1,2}/\d{1,2}/\d{2,4})", piece, re.I)
                        eff = parse_date(em.group(1)) if em else None
                    key = hashlib.sha1(f"{url}\n{piece}".encode("utf-8")).hexdigest()[:24]
                    r = records.get(key)
                    if r is None:
                        hdr = [f"Page: {title}", f"URL: {url}", f"Page type: {ptype}"]
                        if heading:
                            hdr.append(f"Section: {heading}")
                        if eff:
                            hdr.append(f"Rates effective: {eff.isoformat()}")
                        if pub and ptype in ("news", "blog", "resource"):
                            hdr.append(f"Published: {pub.isoformat()}")
                        r = records[key] = {
                            "id": key, "text": "\n".join(hdr) + "\n\n" + piece, "url": url, "title": title,
                            "page_type": ptype, "authority": f"{auth:.2f}", "snaps": set(), "dates": set(),
                            "effective_date": eff.isoformat() if eff else "",
                            "published_date": pub.isoformat() if pub else "",
                            "has_table": "true" if kind == "table" else "false",
                        }
                    r["snaps"].add(si)
                    r["dates"].add(taken)
        print(f"  {d.name}: {len(m['pages'])} pages, {len(records)} unique chunks so far", flush=True)

    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for r in records.values():
            snaps_ = sorted(r.pop("snaps"))
            dates = sorted(r.pop("dates"))
            for k in range(1, len(snaps) + 1):
                r[f"snap_{k}"] = "true" if k in snaps_ else "false"
            r["snapshots"] = ",".join(map(str, snaps_))
            r["first_seen"], r["last_seen"] = dates[0], dates[-1]
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(records)} chunks to {OUT}")


if __name__ == "__main__":
    main()
