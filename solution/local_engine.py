"""
Time-aware knowledge system for the credit-union website snapshots.

No LLM, no network, standard library only. The approach:

  ingest()   Every page of every snapshot is broken into small "units":
               - table rows   (rate tables, with their heading and "effective as of" date)
               - sentences    (for general facts: routing number, addresses, programs ...)
               - hours blocks (lobby / drive-up schedules)
               - links        (for "what's the URL for ..." questions)
             Identical units are stored once, with the set of snapshots they appeared in,
             so we keep the full history and can answer "as of" any date.

  answer()   1. pick the snapshot that was current on the requested date (default: newest)
             2. route the question: hours / link / rate table / general sentence search
             3. rank candidates with BM25 keyword search plus trust rules:
                  newest "effective as of" date > official rates & product pages >
                  promo landing pages > blogs/newsroom/calculators; newer articles beat older
             4. respect qualifiers (term, model year, balance tier, jumbo, IRA, business ...)
             5. answer briefly with only the matching row(s) / sentence, and cite the pages,
                or say honestly that the site doesn't list it.

  conflicts() Facts (same product + tier + rate type, or the same sentence template) that
             show different values across pages or snapshots, the value we trust, and why.
"""
from __future__ import annotations

import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(next(d / "harness" for d in Path(__file__).resolve().parents
                            if (d / "harness" / "contract.py").exists())))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

# --------------------------------------------------------------------------------------
# Text normalisation
# --------------------------------------------------------------------------------------

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}
MONTH_ABBR = {k[:3]: v for k, v in MONTHS.items()}

NUMWORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "seventeen": 17,
    "eighteen": 18, "twenty": 20, "twenty-four": 24, "thirty": 30, "thirty-six": 36,
    "forty-eight": 48, "sixty": 60, "seventy-two": 72, "eighty-four": 84,
}

STOP = set("""
a an and are as at be by can could do does did for from get give got has have how i i'd i'm if in
is it its me my of on or our please tell than that the their them there these this those to us was
we what whats what's when where which who why will with would you your yours dupaco dupacos
know like want need right now current currently today todays offer offered provide show let
about any some much many s does also just there's here
""".split())

# Words that pick out a product *variant*. If the member names one, the answer row must have it;
# if the row has one the member didn't ask about, that row is demoted.
HARD_QUALS = {"jumbo", "ira", "roth", "traditional", "hsa", "business", "arm", "construction",
              "fha", "va", "usda", "balloon", "land", "lot", "commercial", "agricultural", "farm",
              "sba", "heloc", "refinance", "intro", "minimax", "launchpad", "kid", "youth"}
SOFT_QUALS = {"relief", "jumbo", "ira", "roth", "traditional", "hsa", "business", "refinance", "intro",
              "minimax", "heloc", "launchpad", "bright", "highyield", "holiday", "youname"}

PHRASES = [
    (r"home\s*\$\s*tart", " homestart "),
    (r"\bhome\s+loans?\b", " mortgage homeloan "),
    (r"\bmortgages?\b", " mortgage homeloan "),
    (r"\bcertificates?\s+of\s+deposits?\b", " certificate "),
    (r"\bshare\s+certificates?\b", " certificate "),
    (r"\bterm\s+share\b", " certificate "),
    (r"\bcds?\b", " certificate "),
    (r"\bcredit\s+cards?\b", " creditcard visa "),
    (r"\bmoney\s+markets?\b", " moneymarket "),
    (r"\bhome\s+equity\s+lines?\s+of\s+credit\b", " heloc "),
    (r"\bdrive[\s-]*(?:up|thru|through)\b", " driveup "),
    (r"\brouting\s+(?:and\s+transit\s+)?numbers?\b", " routing "),
    (r"\baba\b", " routing "),
    (r"\badjustable[\s-]*rate\b", " arm "),
    (r"\bmodel[\s-]*years?\b", " modelyear "),
    (r"\bmini[\s-]*max\b", " minimax "),
    (r"\bintroductory\b", " intro "),
    (r"\bbalance\s+transfers?\b", " intro balancetransfer "),
    (r"\bhigh[\s-]*yield\b", " highyield "),
    (r"\byou[\s-]*name[\s-]*it\b", " youname "),
    (r"\brefi\b", " refinance "),
    (r"\bannual\s+percentage\s+yield\b", " apy "),
    (r"\bannual\s+percentage\s+rate\b", " apr "),
    (r"\bshine(?:\s+online)?\b", " shine onlinebanking "),
    (r"\bonline\s+banking\b", " onlinebanking "),
    (r"\bmobile\s+wallets?\b", " mobilewallet applepay googlepay samsungpay "),
    (r"\bapple\s+pay\b", " applepay "),
    (r"\bgoogle\s+pay\b", " googlepay "),
    (r"\bsamsung\s+pay\b", " samsungpay "),
    (r"\bvolunteer\s+time\s+off\b", " vto "),
    (r"\bvehicles?\b|\bcars?\b|\bautomobiles?\b|\btrucks?\b", " auto "),
]
SYN = {
    "yield": "apy", "interest": "rate", "rates": "rate", "dividends": "dividend",
    "automotive": "auto", "motorcycles": "motorcycle",
    "located": "address", "location": "address", "locations": "address", "where": "address",
    "telephone": "phone", "call": "phone", "hrs": "hours", "hour": "hours",
    "closing": "close", "closed": "close", "closes": "close", "closure": "close",
    "join": "join", "joining": "join", "membership": "member", "members": "member",
    "eligible": "eligibility", "eligibility": "eligibility", "qualify": "eligibility",
    "students": "student", "college": "student", "university": "student",
    "employees": "employee", "staff": "employee",
    "branches": "branch", "offices": "branch",
}

TOKEN_RE = re.compile(r"[a-z0-9]+(?:/[0-9]+)?")


def clean_md(s: str) -> str:
    s = s.replace("\\r\\n", " ").replace("\\n", " ").replace("\\*", "*").replace("\\#", "#")
    s = s.replace("\\$", "$").replace("\\_", "_").replace("\\|", "/")
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)                 # images
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)               # links -> anchor text
    s = re.sub(r"(\d%)\d{1,2}\b", r"\1", s)                      # 4.20%1 -> 4.20% (footnotes)
    s = re.sub(r"\b(APR|APY|\))[¹²³\d]\b", r"\1", s)
    s = s.replace("¹", "").replace("²", "").replace("³", "")
    s = s.replace("**", "").replace("__", "")
    return re.sub(r"[ \t]+", " ", s).strip()


def norm(s: str) -> str:
    s = s.lower().replace("–", "-").replace("—", "-").replace("’", "'")
    s = re.sub(r"\b(" + "|".join(sorted(NUMWORDS, key=len, reverse=True)) + r")\b(?=[\s-]*(?:months?|years?|mo\b))",
               lambda m: str(NUMWORDS[m.group(1)]), s)
    for pat, rep in PHRASES:
        s = re.sub(pat, rep, s)
    # terms: "60-month" -> 60mo ; "30-year" -> 30yr + 360mo ; "5 yr" ...
    s = re.sub(r"\b(\d{1,3})\s*-?\s*(?:months?|mos?)\b\.?", r" \1mo ", s)
    s = re.sub(r"\b(\d{1,2})\s*-?\s*(?:years?|yrs?)\b",
               lambda m: f" {m.group(1)}yr {int(m.group(1)) * 12}mo ", s)
    return s


def stem(t: str) -> str:
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss") and not t[-2].isdigit():
        return t[:-1]
    return t


HEAVY_SUFFIXES = ("ations", "ation", "itions", "ition", "izing", "ized", "izes", "ize", "ings", "ing",
                  "ments", "ment", "ied", "ed", "ers", "er", "ly")


def heavy_stem(t: str) -> str:
    if len(t) <= 5 or not t.isalpha():
        return t
    for suf in HEAVY_SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)]
    return t


def tokens(s: str, keep_stop: bool = False, heavy: bool = False, digits: bool = True) -> list[str]:
    out = []
    for t in TOKEN_RE.findall(norm(s)):
        t = SYN.get(t, t)
        t = stem(t)
        t = SYN.get(t, t)
        if not keep_stop and (t in STOP or len(t) < 2 and not t.isdigit()):
            continue
        if not digits and t.isdigit():
            continue
        if heavy:
            t = heavy_stem(t)
        out.append(t)
    return out


def model_year_tokens(text: str) -> set[str]:
    """'2024-27 Model Year' -> {my2024..my2027}; '2016 Model Year (and older)' -> my2000..my2016."""
    out = set()
    t = text.replace("–", "-")
    for m in re.finditer(r"\b(20\d\d)\s*-\s*(\d{2,4})\b", t):
        a = int(m.group(1))
        b = int(m.group(2))
        b = b + 2000 if b < 100 else b
        for y in range(a, min(b, a + 15) + 1):
            out.add(f"my{y}")
    for m in re.finditer(r"\b((?:19|20)\d\d)\s+model\s*year\s*\(and older\)", t, re.I):
        for y in range(1990, int(m.group(1)) + 1):
            out.add(f"my{y}")
    if not out:
        for m in re.finditer(r"\b((?:19|20)\d\d)\s+model", t, re.I):
            out.add(f"my{m.group(1)}")
    return out


AMT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(k\b)?", re.I)


def parse_amount(s: str) -> float:
    return float(s.replace(",", ""))


def amount_ranges(text: str) -> list[tuple[float, float]]:
    t = text.replace("–", "-").replace("—", "-")
    out = []
    for m in re.finditer(r"\$\s*([\d,]+(?:\.\d+)?)\s*-\s*\$?\s*([\d,]+(?:\.\d+)?)", t):
        out.append((parse_amount(m.group(1)), parse_amount(m.group(2))))
    for m in re.finditer(r"\$\s*([\d,]+(?:\.\d+)?)\s*\+", t):
        out.append((parse_amount(m.group(1)), float("inf")))
    for m in re.finditer(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:or less|and under|or below)", t, re.I):
        out.append((0.0, parse_amount(m.group(1))))
    for m in re.finditer(r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:and up|or more|and above|or greater)", t, re.I):
        out.append((parse_amount(m.group(1)), float("inf")))
    return out


def query_amounts(q: str) -> list[float]:
    out = []
    for m in AMT_RE.finditer(q):
        v = parse_amount(m.group(1))
        if m.group(2):
            v *= 1000
        out.append(v)
    for m in re.finditer(r"\b(\d{1,3}(?:,\d{3})+|\d{4,7})\s*(?:dollars)\b", q, re.I):
        out.append(parse_amount(m.group(1)))
    return out


def parse_date_any(s: str) -> date | None:
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


def iso(d: str) -> date | None:
    try:
        y, m, dd = (int(x) for x in d[:10].split("-"))
        return date(y, m, dd)
    except Exception:
        return None


MONTH_RE = (r"(january|february|march|april|may|june|july|august|september|october|november|december|"
            r"jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\.?")
Q_DATE_PATTERNS = [
    # "as of June 30, 2026", "on June 30th 2026", "June 30 of 2026"
    re.compile(r"\(?\b(?:(?:as of|as at|in|on|during|for|of|back in|around|at|by)\s+)?(?:the\s+)?(?:end of\s+)?"
               + MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(?:of\s+)?(\d{4})\b\)?", re.I),
    # "in June of 2026", "June 2026", "as of June, 2026"
    re.compile(r"\(?\b(?:(?:as of|as at|in|on|during|for|of|back in|around|at|by)\s+)?(?:the\s+)?(?:end of\s+)?"
               + MONTH_RE + r",?\s+(?:of\s+)?(\d{4})\b\)?", re.I),
    # "6/30/2026", "2026-06-30"
    re.compile(r"\(?\b(?:(?:as of|as at|in|on|during|for|of|back in|at|by)\s+)?(\d{1,2})/(\d{1,2})/(\d{4})\b\)?", re.I),
    re.compile(r"\(?\b(?:(?:as of|as at|in|on|during|for|of|back in|at|by)\s+)?(\d{4})-(\d{2})-(\d{2})\b\)?", re.I),
]


def question_date(q: str):
    """Find a date the member wrote into the question. Returns (iso date or None, question without it)."""
    def mon(x):
        x = x.lower().rstrip(".")
        return MONTHS.get(x) or MONTH_ABBR.get(x[:3])
    for i, pat in enumerate(Q_DATE_PATTERNS):
        m = pat.search(q)
        if not m:
            continue
        try:
            if i == 0:
                d = date(int(m.group(3)), mon(m.group(1)), int(m.group(2)))
            elif i == 1:
                d = month_end(int(m.group(2)), mon(m.group(1)))
            elif i == 2:
                d = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            else:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except (TypeError, ValueError):
            continue
        rest = re.sub(r"\s+", " ", (q[:m.start()] + " " + q[m.end():])).strip()
        return d.isoformat(), rest
    return None, q


def month_end(y: int, m: int) -> date:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


PCT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")
NUM_MASK_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
ORDINALS = r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\b"


def mask_numbers(s: str) -> str:
    """Hide the *values* in a sentence but keep what identifies the fact (terms, model years)."""
    s = re.sub(r"\$?\d[\d,]*(?:\.\d+)?%?(?!\s*-?\s*(?:months?|mos?|years?|yrs?|model)\b)", "#", s)
    return re.sub(ORDINALS, "#", s, flags=re.I)


DATE_ONLY = re.compile(r"\b(effective|updated|accurate|as of)\b", re.I)


def statement_key(s: str) -> str:
    t = re.findall(r"[a-z#]+", mask_numbers(s).lower())
    return " ".join(w for w in t if w not in {"it", "this", "that", "was", "is", "the", "a", "an"})


def same_statement(a: str, b: str) -> bool:
    """Same wording apart from numbers / ordinals and a few small words."""
    if a == b:
        return True
    ta = set(re.findall(r"[a-z#]+", mask_numbers(a).lower())) - {"it", "this", "that", "the", "a", "an"}
    tb = set(re.findall(r"[a-z#]+", mask_numbers(b).lower())) - {"it", "this", "that", "the", "a", "an"}
    if "#" not in ta or "#" not in tb:
        return False
    return len(ta & tb) / max(1, len(ta | tb)) >= 0.75


# --------------------------------------------------------------------------------------
# Page trust
# --------------------------------------------------------------------------------------

def page_authority(url: str) -> float:
    p = urlparse(url)
    host, path = p.netloc.lower(), p.path.lower().rstrip("/")
    if host and host not in ("www.dupaco.com", "dupaco.com"):
        return 0.25
    if "privacy-policy" in path and p.query:
        return 0.05
    if path in ("/rates", "/business-rates") or path.startswith("/rates/"):
        return 1.0
    if "/calculators" in path or "calculator" in path:
        return 0.15
    if path.endswith("-lp") or any(k in path for k in ("switch-and-earn", "offer", "promo", "makethemost")):
        return 0.7        # promo landing pages: below rates & product pages, above everything else
    if "/hubfs" in path or path.endswith(".pdf"):
        return 0.25
    if re.search(r"/(tag|author|category|page)/", path) or path in ("/newsroom", "/learn", "/learn/blog"):
        return 0.1
    if "/resources/" in path:
        return 0.35        # advice articles
    if path.startswith("/newsroom") or path.startswith("/learn") or "/blog" in path or re.match(r"/20\d\d/", path):
        return 0.35
    if "guidance" in path:
        return 0.5
    if path in ("", "/contact", "/membership", "/about", "/about/join") or path.startswith(("/personal", "/business", "/about")):
        return 0.85
    return 0.6


def page_date(md: str) -> date | None:
    """When the page's content was published: the article date near the top ('July 18, 2023'),
    otherwise the newest 'effective as of' / 'updated as of' stamp anywhere on the page."""
    d = parse_date_any(md[:600])
    if d:
        return d
    stamps = [parse_date_any(m.group(1)) for m in
              re.finditer(r"(?:effective|updated|accurate)\s+(?:as\s+of|on)?\s*(\d{1,2}/\d{1,2}/\d{2,4})", md, re.I)]
    stamps = [x for x in stamps if x]
    return max(stamps) if stamps else None


# --------------------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------------------

class Unit:
    __slots__ = ("kind", "url", "text", "toks", "snaps", "meta", "uid")

    def __init__(self, kind, url, text, toks, meta):
        self.kind, self.url, self.text, self.toks, self.meta = kind, url, text, toks, meta
        self.snaps: set[int] = set()
        self.uid = -1


class BM25:
    def __init__(self, k1=1.2, b=0.75):
        self.k1, self.b = k1, b
        self.post: dict[str, dict[int, int]] = defaultdict(dict)
        self.dl: list[int] = []
        self.total = 0

    def add(self, toks: list[str]) -> int:
        i = len(self.dl)
        for t, c in Counter(toks).items():
            self.post[t][i] = c
        self.dl.append(len(toks))
        self.total += len(toks)
        return i

    def search(self, qtoks, allowed, k=50, weights=None):
        n = len(self.dl) or 1
        avg = self.total / n if n else 1
        scores: dict[int, float] = defaultdict(float)
        for t in set(qtoks):
            p = self.post.get(t)
            if not p:
                continue
            idf = math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5))
            w = (weights or {}).get(t, 1.0)
            for d, tf in p.items():
                if allowed is not None and not allowed(d):
                    continue
                dl = self.dl[d]
                scores[d] += w * idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * dl / avg))
        return sorted(scores.items(), key=lambda x: (-x[1], x[0]))[:k]

    def idf(self, t):
        n = len(self.dl) or 1
        df = len(self.post.get(t, ()))
        return math.log(1 + (n - df + 0.5) / (df + 0.5))


VALUE_HDR = re.compile(r"rate|apr|apy|yield|dividend", re.I)
DROP_HDR = re.compile(r"payment|points|min\.?\s*to\s*open", re.I)
DAY_LINE = re.compile(
    r"^\s*[-*]?\s*((?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?(?:\s*(?:-|–|through|to|&|and)\s*(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?)?)\s*:?\s*(.*)$",
    re.I)
TIME_HINT = re.compile(r"a\.?m\.?|p\.?m\.?|noon|closed|atm only|\d{1,2}:\d\d", re.I)


def short_hdr(h: str) -> str:
    h0 = h.lower()
    if "annual percentage yield" in h0 or h0.strip() == "apy":
        return "APY"
    if "annual percentage rate" in h0 or h0.startswith("apr"):
        return "APR" + (" (as low as)" if "low" in h0 else "")
    if "dividend" in h0:
        return "dividend rate"
    if "min" in h0 and "earn" in h0:
        return "min. to earn"
    return h.strip().lower()


class Knowledge:
    def __init__(self):
        self.snaps: list[tuple[str, date]] = []
        self.units: list[Unit] = []
        self.by_key: dict[tuple, Unit] = {}
        self.idx = {"row": BM25(), "sent": BM25(), "hours": BM25(), "link": BM25(), "page": BM25()}
        self.branch_dir: dict[int, dict] = {}       # snapshot -> branch directory
        self.idx_units = {k: [] for k in self.idx}
        self.page_info: dict[tuple[str, int], dict] = {}     # (url, snap) -> meta
        self.url_snaps: dict[str, list[int]] = defaultdict(list)
        self._eff_cache: dict[int, dict[str, int]] = {}
        self._cache: dict = {}
        self._conflicts = None

    # ------------------------------------------------------------------ ingest
    def _add(self, kind, url, text, index_text, meta, snap, key_extra=()):
        key = (kind, url, text) + tuple(key_extra)
        u = self.by_key.get(key)
        if u is None:
            if kind == "row":
                toks = tokens(index_text, digits=False) + sorted(model_year_tokens(index_text))
            elif kind == "sent":
                toks = tokens(index_text, heavy=True)
            else:
                toks = tokens(index_text)
            u = Unit(kind, url, text, toks, meta)
            u.uid = self.idx[kind].add(toks)
            self.idx_units[kind].append(u)
            self.by_key[key] = u
        u.snaps.add(snap)
        return u

    def ingest(self, snapshot: Snapshot) -> None:
        si = len(self.snaps)
        self.snaps.append((snapshot.snapshot_id, iso(snapshot.taken_on) or date.min))
        seen_text = {}
        for p in snapshot.pages:
            md = p.markdown or ""
            h = hash(md)
            auth = page_authority(p.url)
            if h in seen_text and auth <= 0.05:
                continue
            seen_text[h] = p.url
            pd = page_date(md)
            self.page_info[(p.url, si)] = {"auth": auth, "date": pd, "title": p.title}
            if not self.url_snaps[p.url] or self.url_snaps[p.url][-1] != si:
                self.url_snaps[p.url].append(si)
            self._ingest_page(p.url, p.title or "", md, si, auth, pd)
        self._cache.clear()
        self._eff_cache.clear()
        self._conflicts = None

    def _ingest_page(self, url, title, md, si, auth, pdate):
        lines = md.split("\n")
        n = len(lines)
        heading = ""
        recent: list[str] = []          # short non-table lines just seen (table context)
        section = ""                    # outer heading that carries over to following tables
        pos_counter = [0]
        i = 0
        para: list[str] = []
        hours_buf: list[str] = []
        hours_kind = ""
        hours_label_lines: list[str] = []
        struct = {"h1": "", "intro": "", "sections": []}

        def flush_para():
            if not para:
                return
            text = clean_md(" ".join(para))
            para.clear()
            if len(text) < 8:
                return
            first = re.split(r"(?<=[.!?])\s+(?=[A-Z\"“(])", text)[0].strip()
            if len(first) >= 25 and not first.startswith(("[", "!")):
                if struct["sections"] and not struct["sections"][-1][2]:
                    struct["sections"][-1][2] = first
                elif struct["h1"] and not struct["intro"] and not struct["sections"]:
                    struct["intro"] = first
            prev = None
            for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"“(])", text):
                s = s.strip()
                if len(s) < 8 or len(s) > 700 or len(re.findall(r"[A-Za-z]{2,}", s)) < 2:
                    continue
                pos_counter[0] += 1
                u = self._add("sent", url, s, f"{s} {heading} {title}",
                              {"auth": auth, "date": pdate, "heading": heading, "title": title, "next": {},
                               "pos": pos_counter[0], "stoks": frozenset(tokens(s, heavy=True))}, si)
                if prev is not None and prev is not u:
                    prev.meta["next"].setdefault(si, u)
                    u.meta.setdefault("prev", {}).setdefault(si, prev)
                prev = u

        def flush_hours():
            nonlocal hours_kind
            if len(hours_buf) >= 2:
                sched = "; ".join(hours_buf)
                self._add("hours", url, sched, f"{hours_kind} hours {heading} {title} {' '.join(hours_label_lines)}",
                          {"auth": auth, "date": pdate, "kind": hours_kind or "general", "title": title,
                           "page_text": md[:4000].lower()}, si)
            hours_buf.clear()

        while i < n:
            line = lines[i].rstrip()
            st = line.strip()
            # ---- tables
            if st.startswith("|") and i + 1 < n and re.match(r"^\s*\|\s*:?-{2,}", lines[i + 1]):
                flush_para()
                flush_hours()
                header = [clean_md(c) for c in st.strip("|").split("|")]
                j = i + 2
                rows = []
                while j < n and lines[j].strip().startswith("|"):
                    rows.append([clean_md(c) for c in lines[j].strip().strip("|").split("|")])
                    j += 1
                # look at the next few *non-blank* lines after the table (crawls pad with blank lines)
                after, k = [], j
                while k < n and len(after) < 10:
                    if lines[k].strip().startswith("|"):
                        break
                    if lines[k].strip():
                        after.append(k)
                    k += 1
                comp = None
                for k in after:
                    cm = re.search(r"compounded\s+(daily|monthly|quarterly|semi-?annually|annually)", lines[k], re.I)
                    if cm:
                        comp = {"daily": 365, "monthly": 12, "quarterly": 4, "annually": 1}.get(
                            cm.group(1).lower().replace("-", ""), 2)
                        break
                self._comp_hint = comp
                eff = None
                for k in after[:6]:
                    m = re.search(r"effective\s+(?:as\s+of|on)?\s*(\d{1,2}/\d{1,2}/\d{2,4})", lines[k], re.I)
                    if m:
                        eff = parse_date_any(m.group(1))
                        break
                if len(recent) >= 2:
                    section = recent[-2]
                    ctx = [recent[-1], section]
                elif recent:
                    ctx = [recent[-1]] + ([section] if section and section != recent[-1] else [])
                else:
                    ctx = [section] if section else []
                self._ingest_table(url, title, header, rows, ctx, eff, si, auth)
                recent.clear()
                i = j
                continue
            # ---- hours lines
            dm = DAY_LINE.match(clean_md(st)) if st else None
            if dm and TIME_HINT.search(dm.group(2) or "") and len(st) < 90:
                flush_para()
                if not hours_buf:
                    hours_label_lines[:] = recent[-2:]
                hours_buf.append(clean_md(st).lstrip("-* ").strip())
                i += 1
                continue
            if st:
                low = clean_md(st).lower().strip(":* ")
                if hours_buf and not (low in ("lobby", "drive-up", "drive up", "drive-thru")):
                    flush_hours()
                if low in ("lobby", "lobby hours", "drive-up", "drive up", "drive-up hours", "drive-thru",
                           "drive thru") or (len(low) < 30 and ("lobby" in low or "drive" in low) and "hour" in low):
                    if hours_buf:
                        flush_hours()
                    hours_kind = "driveup" if "drive" in low else "lobby"
                    i += 1
                    continue
            # ---- text
            if not st:
                flush_para()
                i += 1
                continue
            if st.startswith("#"):
                flush_para()
                heading = clean_md(st.lstrip("#").strip())
                recent.append(heading)
                level = len(st) - len(st.lstrip("#"))
                if level == 1 and not struct["h1"]:
                    struct["h1"] = heading
                elif heading:
                    struct["sections"].append([level, heading, ""])
                i += 1
                continue
            # links
            for m in re.finditer(r"\[([^\]]{2,120})\]\((https?://[^)\s]+)\)", st):
                anchor, target = clean_md(m.group(1)), m.group(2)
                if target.startswith(("mailto:", "tel:")) or "privacy-policy" in target:
                    continue
                ctx_line = clean_md(st)[:300]
                self._add("link", url, target, f"{anchor} {anchor} {ctx_line} {heading}",
                          {"auth": auth, "anchor": anchor, "target": target, "date": pdate}, si,
                          key_extra=(anchor,))
            cl = clean_md(st)
            if len(cl) < 80 and not st.startswith(("[", "|", "!", "*", "-")) and \
                    not re.search(r"effective|subject to change|^\d", cl, re.I):
                recent.append(cl)
                if len(recent) > 6:
                    recent.pop(0)
            para.append(st)
            if st.startswith(("-", "*")) or len(st) > 200:
                flush_para()
            i += 1
        flush_para()
        flush_hours()
        # one "page" unit per page version: title, main heading, intro and the list of sections
        heads = [h for _, h, _ in struct["sections"]]
        path_words = " ".join(re.split(r"[/_\-.?=&]+", urlparse(url).path))
        key = f"{title}|{struct['h1']}|{struct['intro']}|" + "|".join(heads)
        self._add("page", url, key, f"{title} {title} {struct['h1']} {struct['h1']} {path_words} {path_words} {' '.join(heads[:12])}",
                  {"auth": auth, "date": pdate, "title": title, "h1": struct["h1"], "intro": struct["intro"],
                   "sections": [tuple(x) for x in struct["sections"]], "pdate": pdate,
                   "expired": bool(re.search(r"offer has (?:expired|ended)|this offer has ended|offer expired|"
                                             r"offer (?:is )?no longer available|promotion has ended", md[:3000], re.I))},
                  si)
        # branch directory (from the locator listing)
        if "Dupaco Branch" in md or "Select a Branch" in md:
            d = self.branch_dir.setdefault(si, {"addresses": {}, "names": set()})
            for m in re.finditer(r"Dupaco Branch\s*\n+\s*([^\n]*?\d[^\n]*?,\s*([A-Za-z .]+?),\s*(IA|IL|WI),\s*(\d{5}))", md):
                d["addresses"][m.group(1).strip()] = (m.group(2).strip(), m.group(3))
            m = re.search(r"Select a Branch(.*?)(?:What's|\n)", md)
            if m:
                # the dropdown text is run together ("San MarnanWaterloo: W Mullan Ave."): split at
                # lower->Upper boundaries, then glue back pieces that are not "City: Street"
                pieces = re.sub(r"([a-z.\d])([A-Z])", r"\1|\2", m.group(1)).split("|")
                items: list[str] = []
                for pc in pieces:
                    if ":" in pc or not items:
                        items.append(pc)
                    else:
                        items[-1] += pc
                for it in items:
                    if ":" in it:
                        city, street = it.split(":", 1)
                        if city.strip() and street.strip():
                            d["names"].add(f"{city.strip()}: {street.strip()}")

    def _ingest_table(self, url, title, header, rows, ctx, eff, si, auth):
        ncol = len(header)
        val_cols = [k for k, h in enumerate(header) if VALUE_HDR.search(h) and not DROP_HDR.search(h)]
        desc_cols = [k for k, h in enumerate(header) if k not in val_cols and not DROP_HDR.search(h)
                     and not re.search(r"min\.?\s*to", h, re.I)]
        earn_cols = [k for k, h in enumerate(header) if re.search(r"min\.?\s*to\s*earn", h, re.I)]
        if not val_cols:
            # generic table (e.g. hours, fees): still index rows as text rows
            val_cols = [k for k in range(1, ncol)]
            desc_cols = [0]
        # model-year buckets (for cross-page fact keys)
        my_starts = []
        for r in rows:
            m = re.search(r"\b(20\d\d|19\d\d)\b", " ".join(r[k] for k in desc_cols if k < len(r)))
            if m and "model" in " ".join(r).lower():
                my_starts.append(int(m.group(1)))
        buckets = {y: i for i, y in enumerate(sorted(set(my_starts), reverse=True))}
        context = " / ".join(ctx[:2])
        tkey = (url, ctx[0] if ctx else "", tuple(header))
        for ri, r in enumerate(rows):
            if len(r) < ncol:
                r = r + [""] * (ncol - len(r))
            desc = " ".join(r[k] for k in desc_cols if k < len(r) and r[k] and r[k] not in ("—", "-")).strip()
            vals = []
            for k in val_cols:
                v = r[k].strip()
                if v and v not in ("—", "-"):
                    vals.append((short_hdr(header[k]), v))
            if not vals or not any(PCT_RE.search(v) for _, v in vals):
                continue
            earn = [r[k] for k in earn_cols if k < len(r) and r[k].strip() not in ("", "—")]
            label = desc
            lead = ctx[0] if ctx else ""
            if lead and lead.lower() not in desc.lower():
                label = f"{lead} - {desc}" if desc else lead
            show = ", ".join(f"{h} {v}" for h, v in vals)
            text = f"{label}: {show}" + (f" (min. to earn {earn[0]})" if earn and "jumbo" in label.lower() else "")
            m = re.search(r"\b(20\d\d|19\d\d)\b", desc)
            dkey = desc
            if m and "model" in desc.lower() and int(m.group(1)) in buckets:
                dkey = re.sub(r"\b(?:19|20)\d\d(?:\s*[-–]\s*\d{2,4})?\b", f"MY#{buckets[int(m.group(1))]}", desc)
            meta = {"auth": auth, "eff": eff, "tkey": tkey, "ri": ri, "desc": desc, "vals": vals,
                    "earn": earn[0] if earn else None,
                    "comp": getattr(self, "_comp_hint", None),
                    "context": context, "title": title, "label": label, "dkey": dkey,
                    "hdr": [short_hdr(h) for h in header]}
            inferred = ""
            if re.search(r"\b\d+\s*months?\b", desc, re.I) and any(h in ("APY", "dividend rate") for h, _ in vals) \
                    and not re.search(r"certificate|\bira\b|roth|traditional", f"{context} {desc}", re.I):
                inferred = "certificate certificate"      # untitled certificate tables ("Regular", "Special")
            index_text = f"{context} {context} {title} {desc} {desc} {inferred} {' '.join(h for h, _ in vals)}"
            self._add("row", url, text, index_text, meta, si, key_extra=(context, eff))

    # ------------------------------------------------------------------ helpers
    def _snap_for(self, as_of: str | None) -> int:
        if not self.snaps:
            return -1
        if not as_of:
            return len(self.snaps) - 1
        d = iso(as_of)
        if d is None:
            return len(self.snaps) - 1
        best = 0
        for i, (_, t) in enumerate(self.snaps):
            if t <= d:
                best = i
        return best

    def _page_version(self, si):
        """For each URL, the snapshot holding its newest copy as of snapshot `si`.
        A page missing from one crawl is still part of the site: we keep its last known copy."""
        eff = self._eff_cache.get(si)
        if eff is None:
            eff = {}
            for url, ss in self.url_snaps.items():
                v = [x for x in ss if x <= si]
                if v:
                    eff[url] = v[-1]
            self._eff_cache[si] = eff
        return eff

    def _visible(self, kind, si):
        us = self.idx_units[kind]
        eff = self._page_version(si)
        return lambda d: eff.get(us[d].url, -1) in us[d].snaps

    def _vis_unit(self, u, si):
        return self._page_version(si).get(u.url, -1) in u.snaps

    @staticmethod
    def _q_as_of(q: str) -> str | None:
        m = re.search(r"\b(?:as of|in|during|back in)\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b", q, re.I)
        if m:
            return month_end(int(m.group(2)), MONTHS[m.group(1).lower()]).isoformat()
        return None

    def _sources_for_rows(self, rows, si, limit=4):
        """Cite every page that shows the exact same row in this snapshot, most trusted first."""
        urls = []
        for r in rows:
            same = [u for u in self.idx_units["row"] if u.text == r.text and self._vis_unit(u, si)]
            for u in sorted(same, key=lambda u: (-u.meta["auth"], u.url)):
                if u.url not in urls:
                    urls.append(u.url)
        return urls[:limit]

    # ------------------------------------------------------------------ answer
    def answer(self, question: str, as_of: str | None = None) -> Answer:
        key = (question.strip().lower(), as_of)
        if key in self._cache:
            return self._cache[key]
        q_date, q_rest = question_date(question)
        if not as_of and q_date:
            as_of = q_date
        if q_date and q_rest:
            question = q_rest
        si = self._snap_for(as_of)
        if si < 0:
            return Answer(text="I don't know. Please call one of our branches.", confident=False)
        ans = self._route(question, si, as_of)
        self._cache[key] = ans
        return ans

    def _route(self, q, si, as_of):
        q = re.sub(r"\s+", " ", q).strip()
        ql = q.lower()
        prefix = ""
        if as_of:
            d = iso(as_of) or self.snaps[si][1]
            prefix = f"As of {d.strftime('%B %d, %Y').replace(' 0', ' ')}: "
            if d < self.snaps[0][1]:
                prefix += (f"(our earliest copy of the website is from "
                           f"{self.snaps[0][1].strftime('%B %d, %Y').replace(' 0', ' ')}) ")
        if (re.search(r"\bhours?\b|what time|open (?:on|until|till|late)|\bopen (?:on )?(?:saturday|sunday|weekends?)\b", ql)
                or re.search(r"\b(lobby|lobbies|drive[- ]?(?:up|thru|through))\b.*\bopen\b|\bwhen\b.*\bopen\b", ql)) \
                and not re.search(r"\b(rate|apr|apy|account|online)\b", ql):
            return self._answer_hours(q, si, prefix)
        if re.search(r"\b(link|url|web\s?site|web address|web\s?page)\b", ql) or \
                re.search(r"\b(join|apply|enroll|sign up|become a member|open an account)\b.*\bonline\b|\bonline\b.*\b(join|apply|enroll)\b", ql) or \
                re.search(r"\b(where|how)\b.*\b(find|see|view|check|look up)\b.*\bonline\b", ql):
            a = self._answer_link(q, si, prefix)
            if a:
                return a
        if re.search(r"\b(promotions?|promos?|special offers?|current offers?|deals?|bonus(?:es)?|cash back)\b", ql) \
                and not re.search(r"\b(certificates?|cds?|apy|apr)\b", ql):
            a = self._answer_promos(q, si, prefix, as_of)
            if a:
                return a
        if re.match(r"\s*(who|who's|whos)\b", ql):
            a = self._answer_who(q, si, prefix)
            if a:
                return a
        if re.search(r"\b(rates?|aprs?|apys?|dividends?|yields?|interest|percent|earn|earns|pay|pays|paying|charge|charges|cost|costs)\b|%|how much", ql):
            a = self._answer_rate(q, si, prefix, as_of)
            if a:
                return a
        if not re.search(r"\bhow (?:many|much|long|often|old)\b|\bwhen\b|\bwhere\b|\bwhy\b", ql) and \
                re.search(r"\b(what (?:types?|kinds?|sorts?) of|which (?:types?|kinds?)|do you (?:have|offer|provide|guys have)|"
                     r"does dupaco (?:have|offer|provide)|tell me (?:more )?about|describe|information (?:on|about)|"
                     r"what (?:\w+ ){0,3}(?:do you|does dupaco) (?:have|offer))\b", ql):
            a = self._answer_overview(q, si, prefix)
            if a:
                return a
        return self._answer_text(q, si, prefix)

    # ---- rate tables ------------------------------------------------------
    def _answer_rate(self, q, si, prefix, as_of=None):
        qtoks = tokens(q, digits=False)
        qset = set(qtoks)
        q_terms = {t for t in qset if re.fullmatch(r"\d+(mo|yr)", t)}
        q_years = {f"my{y}" for y in re.findall(r"\b(20[0-3]\d|19\d\d)\b", q)}
        if q_years:
            qtoks = qtoks + sorted(q_years)
        q_amts = query_amounts(q)
        q_quals = (qset & HARD_QUALS) | {t for t in qset if re.fullmatch(r"\d+/\d+", t)}
        if re.search(r"\b\d+/\d+\s*(arm)?\b", q.lower()) and "arm" not in q_quals and re.search(r"\barm\b|adjustable", q.lower()):
            q_quals.add("arm")
        superl = None
        if re.search(r"\b(highest|best|top|max(?:imum)?|most)\b", q.lower()):
            superl = "max"
        elif re.search(r"\b(lowest|cheapest|min(?:imum)?|least)\b", q.lower()):
            superl = "min"
        list_mode = bool(re.search(r"\b(rates|aprs|apys|cards|accounts|certificates|loans|tiers|all|list|each|every|options|types|kinds)\b|\bwhat\b.*\boffer\b", q.lower()))
        generic = not (q_terms or q_amts or q_quals or superl or re.search(r"\b(19|20)\d\d\b", q))
        list_mode = list_mode or generic

        us = self.idx_units["row"]
        # "as of" a date: the snapshot current then, plus any table whose own "effective as of" stamp
        # shows it was already in force on that date (even if we only crawled it later)
        as_of_d = iso(as_of) if as_of else None
        vis = self._visible("row", si)
        if as_of_d:
            allowed = lambda d: (vis(d) or (us[d].meta.get("eff") is not None and us[d].meta["eff"] <= as_of_d)) \
                and not (us[d].meta.get("eff") and us[d].meta["eff"] > as_of_d)
        else:
            allowed = vis
        hits = self.idx["row"].search(qtoks, allowed, k=600)
        if not hits:
            return None
        effs = [u.meta["eff"] for u in us if u.meta.get("eff")]
        dmin = min(effs).toordinal() if effs else 0
        dspan = max(1, (max(effs).toordinal() - dmin) if effs else 1)
        content = [t for t in set(qtoks) if t not in {"rate", "apr", "apy", "dividend", "earn", "pay", "charge",
                                                      "loan", "account", "balance", "between", "standard", "tier"}
                   and not t.startswith("my") and not re.fullmatch(r"\d+(mo|yr)?|\d+/\d+", t)
                   and t in self.idx["row"].post]
        ridf = self.idx["row"].idf
        ctotal = sum(ridf(t) for t in content) or 1.0
        scored = []
        u_pen: dict[int, bool] = {}
        dropped_for_qual = 0
        for d, s in hits:
            u = us[d]
            utoks = set(u.toks)
            rtoks = set(tokens(u.meta["desc"])) | model_year_tokens(u.meta["desc"])
            # term must match when both sides name one
            r_terms = {t for t in rtoks if re.fullmatch(r"\d+(mo|yr)", t)}
            if q_terms:
                if r_terms and not (q_terms & r_terms):
                    continue
                if not r_terms:
                    s *= 0.5
            if q_years:
                r_my = {t for t in rtoks if t.startswith("my")}
                if r_my and not (q_years & r_my):
                    continue
            if q_amts:
                rng = amount_ranges(u.meta["desc"])
                if rng:
                    ok = [r for r in rng if r[0] <= q_amts[0] <= r[1]]
                    if not ok:
                        continue
                    s *= 1.0 + 0.2 / (1 + math.log10(1 + (ok[0][1] - ok[0][0] if ok[0][1] != float("inf") else 1e9)))
            missing = [ql for ql in q_quals if ql not in utoks]
            if missing:
                dropped_for_qual += 1
                continue
            penalized = False
            ctx_toks = set(tokens(u.meta["context"]))
            for sq in SOFT_QUALS:
                if (sq in rtoks or sq in ctx_toks) and sq not in qset:
                    s *= 0.5
                    penalized = True
            if "business" not in qset and "/business" in u.url and "business" not in ctx_toks:
                s *= 0.5
                penalized = True
            u_pen[id(u)] = penalized
            cov = sum(ridf(t) for t in content if t in utoks) / ctotal if content else 1.0
            if cov < 0.34:
                continue
            s *= 0.4 + 0.6 * cov
            s *= 0.5 + 0.5 * u.meta["auth"]
            eff = u.meta.get("eff")
            s *= 1.0 + (0.35 * (eff.toordinal() - dmin) / dspan if eff else 0.0)
            scored.append((s, u))
        if not scored:
            if q_quals or dropped_for_qual or q_terms:
                return self._refuse_rate(q, prefix)
            return None
        scored.sort(key=lambda x: (-x[0], x[1].text))
        top_s, top_u = scored[0]

        # group rows by table (within this snapshot)
        tables: dict[tuple, list] = defaultdict(list)
        for s, u in scored:
            tables[(u.meta["tkey"], u.meta.get("eff"))].append((s, u))
        ranked_tables = sorted(tables.items(), key=lambda kv: (-kv[1][0][0], kv[0]))
        chosen = [ranked_tables[0]]
        if list_mode:
            known = {}   # (family, tier signature, header) -> (value, effective date)
            seen_sets = set()

            def sig(u):
                rng = tuple(sorted((round(a), round(b) if b != float("inf") else -1)
                                   for a, b in amount_ranges(u.meta["desc"])))
                terms = tuple(sorted(t for t in tokens(u.meta["desc"]) if re.fullmatch(r"\d+(mo|yr)", t)))
                if rng or terms:
                    return (rng, terms)
                return re.sub(r"[^a-z0-9]+", " ", u.meta["desc"].lower()).strip()

            def remember(rows):
                for _, u in rows:
                    for h, v in u.meta["vals"]:
                        known[(self._family(u), sig(u), h)] = (v, u.meta.get("eff"))
                seen_sets.add(frozenset((sig(u), tuple(u.meta["vals"])) for _, u in rows))
            remember(ranked_tables[0][1])
            for tk, rows in ranked_tables[1:]:
                if len(chosen) >= (4 if generic else 6):
                    break
                top_row = rows[0][1]
                if rows[0][0] < 0.45 * top_s or top_row.meta["auth"] < 0.5:
                    continue
                if generic and self._family(top_row) != self._family(ranked_tables[0][1][0][1]):
                    continue
                if frozenset((sig(u), tuple(u.meta["vals"])) for _, u in rows) in seen_sets:
                    continue          # the same table copied onto another page
                stale = any(k in known and known[k][0] != v
                            for _, u in rows for h, v in u.meta["vals"]
                            for k in [(self._family(u), sig(u), h)])
                if stale:
                    continue          # an older copy of a table we already have (different values)
                remember(rows)
                chosen.append((tk, rows))

        blocks, picked = [], []
        for tk, rows in chosen:
            best = rows[0][0]
            sel = [u for s, u in rows if s >= (0.8 if not list_mode else 0.5) * best]
            plain = [u for u in sel if not u_pen.get(id(u))]
            if plain and len(plain) < len(sel):
                sel = plain
            if superl:
                def val(u):
                    for h, v in u.meta["vals"]:
                        if h in ("APY", "APR", "APR (as low as)", "rate", "dividend rate"):
                            m = re.search(r"\d+(?:\.\d+)?", v)
                            if m:
                                return float(m.group())
                    return -1
                pool = [u for s, u in rows]
                sel = [max(pool, key=val) if superl == "max" else min(pool, key=val)]
            if q_amts and not superl:
                def width(u):
                    rr = [r for r in amount_ranges(u.meta["desc"]) if r[0] <= q_amts[0] <= r[1]]
                    return min((r[1] - r[0] for r in rr), default=float("inf"))
                wmin = min(width(u) for u in sel)
                if wmin != float("inf"):
                    sel = [u for u in sel if width(u) == wmin or not amount_ranges(u.meta["desc"])]
            sel.sort(key=lambda u: u.meta["ri"])
            seen = set()
            sel = [u for u in sel if not (u.text in seen or seen.add(u.text))]
            blocks.append(self._render_table(sel, q))
            picked.extend(sel)
        text = (prefix.rstrip() + "\n" if prefix else "") + "\n\n".join(blocks)
        srcs = self._sources_for_rows(picked, si) or list(dict.fromkeys(u.url for u in picked))[:4]
        note = self._row_conflict_note(picked[0], si)
        return Answer(text=text, sources=[Source(url=u, snapshot_id=self.snaps[si][0],
                                                 quote=picked[0].text[:200]) for u in srcs],
                      confident=True, conflict_note=note)

    def _table_title(self, u):
        lead = self._table_title_raw(u)
        fam = self._family(u)
        if fam == "certificate" and not re.search(r"certificate|cd\b", lead, re.I):
            lead += " Certificates"
        return lead

    @staticmethod
    def _table_title_raw(u):
        ctx = [c for c in u.meta["context"].split(" / ") if c]
        lead = ctx[0] if ctx else (u.meta.get("title") or "Rates").split("|")[0].strip()
        if len(ctx) > 1 and lead.lower() in ("rates", "rate", "loan rates", "home rates", "details"):
            lead = ctx[1]
        return lead

    def _render_table(self, sel, q):
        """Readable block:  Title (effective mm/dd/yyyy):  • row label: values"""
        title = self._table_title(sel[0])
        eff = sel[0].meta.get("eff")
        head = title + (f" (effective {eff.strftime('%m/%d/%Y')})" if eff else "") + ":"
        lines = [head]
        for u in sel:
            desc = u.meta["desc"].strip()
            m = re.match(r"^([^:]{2,40}):\s*(.+)$", desc)
            if m and m.group(1).lower() in title.lower():
                desc = m.group(2)                      # "Purchase: 30-Year ..." under "Home Loans - Purchase"
            if desc.lower().startswith(title.lower()):
                desc = desc[len(title):].strip(" -:")
            vals = []
            for h, v in u.meta["vals"]:
                v = v.strip()
                if re.search(r"\b(APR|APY)\b", v):
                    vals.append(v + (" (as low as)" if "low" in h else ""))
                else:
                    vals.append(f"{v} {h}")
            line = "• " + (f"{desc}: " if desc else "") + ", ".join(vals)
            if u.meta.get("earn") and "jumbo" in (desc + title).lower():
                line += f" (minimum {u.meta['earn']})"
            line += self._converted(u, q)
            lines.append(line)
        return "\n".join(lines)

    def _compounding(self, u):
        if u.meta.get("comp"):
            return u.meta["comp"]
        return 2 if self._family(u) in ("certificate", "ira") else 12

    def _converted(self, u, q):
        """Dividend rate <-> APY:  APY = (1 + r/n)^n - 1,  r = n((1 + APY)^(1/n) - 1),
        n = compounding periods per year (from the page's footnote, else 2 for certificates/IRAs,
        12 for savings, checking and money market). Loan APRs are never converted: APR includes fees."""
        vals = dict(u.meta["vals"])
        ql = q.lower()
        n = self._compounding(u)
        names = {1: "annually", 2: "semi-annually", 4: "quarterly", 12: "monthly", 365: "daily"}
        def num(v):
            m = re.search(r"\d+(?:\.\d+)?", v)
            return float(m.group()) / 100 if m else None
        rate_v = vals.get("dividend rate") or vals.get("rate")
        if "apy" in ql and "APY" not in vals and rate_v and not any(h.startswith("APR") for h in vals):
            r = num(rate_v)
            if r is not None:
                a = (1 + r / n) ** n - 1
                return f" (APY {a * 100:.2f}%, calculated from the {rate_v} rate compounded {names.get(n, '')})"
        if re.search(r"dividend rate|interest rate|\brate\b", ql) and "apy" not in ql and "APY" in vals and not rate_v:
            a = num(vals["APY"])
            if a is not None:
                r = n * ((1 + a) ** (1 / n) - 1)
                return f" (dividend rate {r * 100:.2f}%, calculated from the {vals['APY']} APY compounded {names.get(n, '')})"
        return ""

    def _refuse_rate(self, q, prefix):
        return Answer(text="I don't know. The website doesn't list a rate for that. Please call one of our branches.",
                      sources=[], confident=False)

    def _row_conflict_note(self, row, si):
        dkey = (row.meta["dkey"].lower(), row.meta["vals"][0][0])
        others = set()
        for u in self.idx_units["row"]:
            if u is row or u.meta["dkey"].lower() != dkey[0]:
                continue
            if not self._vis_unit(u, si):
                continue
            if self._family(u) != self._family(row):
                continue
            for h, v in u.meta["vals"]:
                if h == dkey[1] and v != row.meta["vals"][0][1]:
                    others.add((v, u.url))
        if not others:
            return None
        o = "; ".join(f"{v} on {url}" for v, url in sorted(others)[:3])
        return f"Other pages show {o}; chose the most recent effective date / official rate page."

    # ---- hours ------------------------------------------------------------
    STATES = {"IA": "Iowa", "IL": "Illinois", "WI": "Wisconsin"}

    def _directory(self, si):
        keys = [k for k in self.branch_dir if k <= si] or list(self.branch_dir)
        if not keys:
            return {"addresses": {}, "names": set()}
        return self.branch_dir[max(keys)]

    def _answer_hours(self, q, si, prefix, _no_loc=False):
        us = [u for u in self.idx_units["hours"] if self._vis_unit(u, si)]
        if not _no_loc:
            loc = self._hours_by_location(q, si, prefix, us)
            if loc:
                return loc
        m = re.search(r"((?:[A-Z][\w'.-]*\s+){0,3}[A-Z][\w'.-]*)\s+(?:branch|office|location|learning lab)", q)
        name = None
        if m:
            words = [w for w in m.group(1).split() if w.lower() not in ("the", "dupaco", "dupaco's", "your", "what", "are", "can", "give", "me", "for")]
            name = " ".join(words) if words else None
        if name:
            nlow = name.lower().replace("avenue", "ave").replace("street", "st")
            alts = {name.lower(), nlow}
            match = [u for u in us if any(a in u.meta["page_text"].replace("avenue", "ave").replace("street", "st") for a in alts)]
            if not match:
                return Answer(text=f"I don't know. The website doesn't list hours for the {name} branch. "
                                   f"Please call one of our branches.",
                              confident=False)
            us = match
        if not us:
            return self._answer_text(q, si, prefix)
        by_kind: dict[str, Counter] = defaultdict(Counter)
        src: dict[tuple, str] = {}
        newest: dict[tuple, date] = {}
        allsrc: dict[tuple, list] = defaultdict(list)
        for u in us:
            k = u.meta["kind"]
            sched = re.sub(r"\s*[–—-]\s*", "-", u.text)
            sched = re.sub(r"\s+", " ", sched).strip()
            by_kind[k][sched] += 1
            src.setdefault((k, sched), u.url)
            allsrc[(k, sched)].append(u)
            d = u.meta.get("date") or date.min
            if d > newest.get((k, sched), date.min):
                newest[(k, sched)] = d
        order = ["driveup", "lobby", "general"] if "driveup" in tokens(q) else ["lobby", "driveup", "general"]
        label = {"lobby": "Lobby", "driveup": "Drive-up", "general": "Hours"}
        parts, urls = [], []
        for k in order:
            if k not in by_kind:
                continue
            common = sorted(by_kind[k].items(), key=lambda kv: (-kv[1], -(newest[(k, kv[0])].toordinal()), kv[0]))[:2]
            if k == "general" and parts:
                continue
            txt = f"{label[k]}: " + common[0][0]
            if len(common) > 1:
                txt += f" (some branches: {common[1][0]})"
            parts.append(txt)
            for sched, _ in common:
                for v in sorted(allsrc[(k, sched)], key=lambda v: (-(v.meta.get("date") or date.min).toordinal(), v.url)):
                    if v.url not in urls:
                        urls.append(v.url)
        lead = "Hours vary by branch. " if not name else f"{name}: "
        text = prefix + lead + ". ".join(parts) + "." + (" Let me know which branch you mean for its exact hours." if not name else "")
        return Answer(text=text, sources=[Source(url=u, snapshot_id=self.snaps[si][0]) for u in urls[:5]],
                      confident=True)

    def _hours_by_location(self, q, si, prefix, us):
        """'branch hours in IL', 'hours in Cedar Rapids': list the branches there, with posted hours if any."""
        d = self._directory(si)
        ql = q.lower()
        state = None
        m = re.search(r"\b(IA|IL|WI)\b", q)
        if m:
            state = m.group(1)
        for ab, full in self.STATES.items():
            if re.search(rf"\b{full.lower()}\b", ql):
                state = ab
        cities = {c for c, _ in d["addresses"].values()} | {n.split(":")[0].strip() for n in d["names"]}
        city = next((c for c in sorted(cities, key=len, reverse=True)
                     if c and re.search(rf"\b{re.escape(c.lower())}\b", ql)), None)
        if not state and not city:
            return None
        # a specific branch inside the city ("Waterloo Mullan Avenue branch")?
        bm = re.search(r"((?:[A-Z][\w'.-]*\s+){0,3}[A-Z][\w'.-]*)\s+(?:branch|office|location)", q)
        street_words = []
        if bm:
            street_words = [w.lower().rstrip(".") for w in bm.group(1).split()
                            if w.lower() not in ("the", "dupaco", "dupaco's", "your", "what", "are", "for", "avenue",
                                                 "street", "road", "drive", "ave", "st", "rd", "dr")
                            and (not city or w.lower() not in city.lower().split())]
        if city and street_words:
            named = [n for n in d["names"] | set(d["addresses"]) if city.lower() in n.lower()
                     and any(w in n.lower() for w in street_words)]
            label = " ".join(bm.group(1).split())
            posted = [u for u in us if any(w in u.meta["page_text"] for w in street_words)]
            if not posted:
                return Answer(text=f"I don't know. The website doesn't list hours for the {label} branch"
                                   + (f" ({named[0]})" if named and not re.search(r"\d", named[0]) else "")
                                   + ". Please call one of our branches.", confident=False)
            return None
        if not city and state and not any(st == state for _, st in d["addresses"].values()):
            # the locator only lists a few full addresses; learn which state other branch cities are in
            # from the site's own text ("Platteville and Madison, Wis.", "Galena, Ill.")
            st_pat = {"IA": r"Iowa|IA", "IL": r"Ill(?:inois)?\.?|IL", "WI": r"Wis(?:consin)?\.?|WI"}[state]
            names_by_city = defaultdict(list)
            for n in d["names"]:
                names_by_city[n.split(":")[0].strip()].append(n)
            found = []
            for c in names_by_city:
                pat = re.compile(rf"\b{re.escape(c)}(?:,| and [A-Z][a-z]+,)\s*(?:{st_pat})\b")
                if any(pat.search(u.text) for u in self.idx_units["sent"] if c in u.text):
                    found += names_by_city[c]
            if found:
                lines = [f"• {e}: hours not posted on the website" for e in sorted(found)]
                general = self._answer_hours("branch hours", si, "", _no_loc=True)
                text = ((prefix.rstrip() + "\n" if prefix else "") + f"Dupaco branches in {self.STATES[state]}:\n"
                        + "\n".join(lines) + "\nThe website doesn't post hours for these branches. For reference, the "
                        "hours posted for other Dupaco branches are: "
                        + general.text.replace("Hours vary by branch. ", "").split(" Let me know")[0]
                        + "\nPlease call one of our branches to confirm.")
                return Answer(text=text, sources=[Source(url=s_.url) for s_ in general.sources[:2]], confident=True)
        if city:
            entries = [f"{a}" for a, (c, st) in d["addresses"].items() if c.lower() == city.lower()]
            entries += [n for n in sorted(d["names"]) if n.lower().startswith(city.lower() + ":")
                        and not any(n.split(":")[1].strip().split()[0].lower() in a.lower() for a in entries)]
            place = city
        else:
            entries = [a for a, (c, st) in d["addresses"].items() if st == state]
            place = self.STATES[state]
        if not entries:
            return Answer(text=f"I don't know. I couldn't find a Dupaco branch in {place} on the website. "
                               f"Please call one of our branches.", confident=False)
        lines, urls, any_hours = [], [], False
        for e in entries:
            key = (city or e.split(",")[-3] if "," in e else e).strip().lower()
            posted = [u for u in us if key and key in u.meta["page_text"]]
            if posted:
                any_hours = True
                best = max(posted, key=lambda u: (u.meta.get("date") or date.min))
                lines.append(f"• {e}: {best.text}")
                urls.append(best.url)
            else:
                lines.append(f"• {e}: hours not posted on the website")
        general = self._answer_hours("branch hours", si, "", _no_loc=True)
        text = (prefix.rstrip() + "\n" if prefix else "") + f"Dupaco branches in {place}:\n" + "\n".join(lines)
        if not any_hours:
            text += ("\nThe website doesn't post hours for this location. For reference, the hours posted for other "
                     "Dupaco branches are: " + general.text.replace("Hours vary by branch. ", "").split(" Let me know")[0])
        text += "\nPlease call one of our branches to confirm."
        srcs = urls + [s_.url for s_ in general.sources[:2]]
        contact = [u for u in self.page_info if u[0].rstrip("/").endswith("/contact")]
        if contact:
            srcs.insert(0, contact[0][0])
        return Answer(text=text, sources=[Source(url=u) for u in dict.fromkeys(srcs)], confident=True)

    # ---- product overviews -------------------------------------------------
    GENERIC_HEADS = re.compile(r"fine print|faq|frequently|question|handpicked|resource center|get in touch|let's|we're here|"
                               r"here to help|steps|apply|learn more|related|testimonial|review|contact|ready|"
                               r"support for every|calculator|rates|disclosure|why |how it works|details that", re.I)

    def _answer_overview(self, q, si, prefix):
        """'What types of business loans do you have?' -> the product page's heading, intro and its options."""
        drop = {"type", "kind", "sort", "option", "tell", "describe", "explain", "offer", "provide", "have",
                "more", "information", "guy", "which", "do", "does"}
        qt = [t for t in tokens(q) if t not in drop]
        if not qt:
            return None
        us = self.idx_units["page"]
        hits = self.idx["page"].search(qt, self._visible("page", si), k=60)
        best, best_s = None, 0.0
        idf = self.idx["page"].idf
        tot = sum(idf(t) for t in set(qt)) or 1.0
        scored_pages = []
        for d, sc in hits:
            u = us[d]
            if u.meta["auth"] < 0.6 or u.meta.get("expired"):
                continue
            path_words = " ".join(re.split(r"[/_\-.?=&]+", urlparse(u.url).path))
            core = set(tokens(f"{u.meta['title']} {u.meta['h1']} {path_words}"))
            cover = sum(idf(t) for t in set(qt) if t in core) / tot
            if cover < 0.5:
                continue
            depth = urlparse(u.url).path.strip("/").count("/")
            sc = sc * (0.5 + 0.5 * u.meta["auth"]) * (0.3 + 0.7 * cover) * (1.0 - 0.05 * depth)
            if "?" in u.url:
                sc *= 0.5
            if re.search(r"rates?$", urlparse(u.url).path.rstrip("/")) and not re.search(r"\brates?\b", q.lower()):
                sc *= 0.3
            if "/business" in u.url and "business" not in q.lower():
                sc *= 0.4
            scored_pages.append((cover, sc, u))
        if not scored_pages:
            return None
        top_cover = max(c for c, _, _ in scored_pages)
        best = max((x for x in scored_pages if x[0] >= top_cover - 0.01), key=lambda x: (x[1], x[2].url))[2]
        head = best.meta["h1"] or best.meta["title"].split("|")[0].strip()
        intro = best.meta["intro"]
        secs = [(lv, h, dsc) for lv, h, dsc in best.meta["sections"]
                if len(h) < 70 and not self.GENERIC_HEADS.search(h) and not h.isupper()]
        items = []
        for lv in (3, 4, 2):
            cand = [(h, dsc) for l, h, dsc in secs if l == lv]
            if len(cand) >= 2:
                items = cand[:8]
                break
        yes = "Yes. " if re.search(r"\b(do you|does dupaco) (have|offer|provide)\b", q.lower()) else ""
        text = (prefix.rstrip() + "\n" if prefix else "") + yes + head.rstrip(".") + "."
        if intro and len(re.findall(r"\b[a-z]{3,}\b", intro)) >= 4:
            text += " " + intro
        if items:
            text += "\n" + "\n".join(f"• {h}" + (f": {dsc[:170]}" if dsc else "") for h, dsc in items)
        srcs = [best.url]
        # a single product with a published rate: add it
        if re.search(r"account|savings|checking|certificate|loan|card|market", q.lower()):
            ra = self._answer_rate(q + " rate", si, "", None)
            if ra and ra.confident:
                first = ra.text.split("\n\n")[0]
                if first.count("\n") <= 3:
                    text += "\nCurrent rate: " + first.replace("\n", " ")
                srcs += [s_.url for s_ in ra.sources[:1]]
        return Answer(text=text, sources=[Source(url=u, snapshot_id=self.snaps[si][0]) for u in dict.fromkeys(srcs)],
                      confident=True)

    # ---- people -------------------------------------------------------------
    NICK = {"joseph": "joe", "robert": "bob", "william": "bill", "michael": "mike", "richard": "rick",
            "thomas": "tom", "james": "jim", "elizabeth": "liz", "katherine": "kate", "jennifer": "jen",
            "christopher": "chris", "daniel": "dan", "david": "dave", "matthew": "matt", "nicholas": "nick",
            "anthony": "tony", "steven": "steve", "stephen": "steve", "jeffrey": "jeff", "timothy": "tim"}
    ROLE_WORDS = {"ceo": r"(?:president\s*(?:and|&|/)\s*)?(?:ceo|chief executive officer)",
                  "president": r"president(?:\s*(?:and|&|/)\s*ceo)?",
                  "cfo": r"(?:cfo|chief financial officer)", "coo": r"(?:coo|chief operating officer)",
                  "cmo": r"(?:cmo|chief marketing officer)", "cio": r"(?:cio|chief information officer)",
                  "chair": r"(?:board\s+)?chair(?:man|woman|person)?", "chairman": r"(?:board\s+)?chair(?:man|woman|person)?"}
    NAME_RE = r"([A-Z][a-z]+(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-z'’-]+){1,2})"

    def _answer_who(self, q, si, prefix):
        ql = q.lower()
        us = [u for u in self.idx_units["sent"] if self._vis_unit(u, si)]
        roles = dict(self.ROLE_WORDS)
        # any other title the member names: "chief marketing officer", "vice president of lending" ...
        mt = re.search(r"\b(chief [a-z]+(?: [a-z]+)? officer|(?:senior |executive )?vice president(?: of [a-z]+)?|"
                       r"general counsel|treasurer|secretary)\b", ql)
        if mt:
            phrase = mt.group(1)
            pat = r"\s+".join(map(re.escape, phrase.split()))
            if phrase.startswith("chief"):
                pat += r"|\b" + "".join(w[0] for w in phrase.split()).upper() + r"\b"
            roles = {phrase: pat}
        role_key = next((r for r in roles if re.search(rf"\b{r}\b", ql)), None)
        names_in_q = [w for w in re.findall(r"\b[A-Z][a-z'’-]+\b", q) if w.lower() not in ("who", "dupaco", "what", "is", "the")]
        if role_key and not names_in_q:
            role = roles[role_key]
            pats = [re.compile(r"(?i:" + role + r")\s*,?\s+" + self.NAME_RE),
                    re.compile(self.NAME_RE + r"\s*,\s*(?:Dupaco(?:’s|'s)?\s+)?(?:[\w/&’' -]{0,25}\s)?(?i:" + role + r")\b")]
            seen: dict[str, list] = defaultdict(list)
            for u in us:
                if not re.search(role, u.text, re.I):
                    continue
                if re.search(r"\b(retire|retiring|retirement|former|late|honor|memory|predecessor)\b", u.text, re.I):
                    continue
                for pat in pats:
                    for m in pat.finditer(u.text):
                        nm = m.group(1).strip()
                        if nm.split()[0] in ("Dupaco", "The", "Community", "Credit"):
                            continue
                        seen[nm].append(u)
            if not seen:
                return None
            def newest(nm):
                return max(self._undated_date(u) for u in seen[nm])
            nm = max(seen, key=lambda n: (newest(n), len(seen[n])))
            ev = max(seen[nm], key=lambda u: self._undated_date(u))
            title = re.search(role, ev.text, re.I).group(0)
            return Answer(text=f"{prefix}{nm} is Dupaco's {title}.",
                          sources=[Source(url=ev.url, quote=ev.text[:200])], confident=True)
        if names_in_q:
            last = names_in_q[-1]
            firsts = {n.lower() for n in names_in_q[:-1]}
            firsts |= {self.NICK.get(f, f) for f in firsts}
            cand = [u for u in us if re.search(rf"\b{re.escape(last)}\b", u.text)
                    and (not firsts or any(re.search(rf"\b{f}\b", u.text, re.I) for f in firsts))]
            if not cand:
                return None
            full = re.compile(r"((?:[A-Z][a-z]+\.?\s+){1,2}" + re.escape(last) + r")\s*,\s*([^,.;“”\"]{3,70})")
            pre = re.compile(r"((?:[A-Z][\w/&’'-]*\s+){1,6})((?:[A-Z][a-z]+\.?\s+){1,2}" + re.escape(last) + r")\b")
            best = None
            for u in sorted(cand, key=lambda u: self._undated_date(u), reverse=True):
                m = full.search(u.text)
                if m and re.search(r"president|ceo|chief|officer|director|manager|vice|chair|founder|lead|head|"
                                   r"specialist|advisor|planner|lender|officer", m.group(2), re.I):
                    best = (m.group(1).strip(), m.group(2).strip(), u)
                    break
                m = pre.search(u.text)
                if m and re.search(r"president|ceo|chief|officer|director|manager|vice|chair", m.group(1), re.I):
                    t = re.sub(r"^(?:Dupaco(?:’s|'s)?\s+|says\s+|said\s+)", "", m.group(1).strip(), flags=re.I)
                    best = (m.group(2).strip(), t, u)
                    break
            if best:
                nm, title, u = best
                who = title if re.match(r"(?i)dupaco", title) else f"Dupaco's {title}"
                return Answer(text=f"{prefix}{nm} is {who}.",
                              sources=[Source(url=u.url, quote=u.text[:200])], confident=True)
            u = max(cand, key=lambda u: self._undated_date(u))
            return Answer(text=prefix + u.text, sources=[Source(url=u.url, quote=u.text[:200])], confident=True)
        return None

    # ---- promotions ---------------------------------------------------------
    def _answer_promos(self, q, si, prefix, as_of):
        us = [u for u in self.idx_units["page"] if self._vis_unit(u, si)]
        promos = []
        for u in us:
            path = urlparse(u.url).path.lower()
            if "/resources" in path or "/learn" in path or "/newsroom" in path:
                continue
            if not (path.endswith("-lp") or any(k in path for k in ("switch-and-earn", "offer", "promo", "earn-more"))):
                continue
            if u.meta.get("expired") or "expired" in path or re.search(r"expired|has ended", u.meta["title"] + u.meta["h1"], re.I):
                continue
            promos.append(u)
        lines, srcs = [], []
        for u in sorted(promos, key=lambda u: u.url):
            head = (u.meta["h1"] or u.meta["title"].split("|")[0]).strip()
            lines.append(f"• {head}" + (f": {u.meta['intro'][:160]}" if u.meta["intro"] else "") + f" ({u.url})")
            srcs.append(u.url)
        spec = self._answer_rate("special certificate APY", si, "", as_of)
        text = (prefix.rstrip() + "\n" if prefix else "")
        if lines:
            text += "Current promotions on the website:\n" + "\n".join(lines)
        if spec and spec.confident:
            text += ("\n\n" if lines else "") + "Certificate specials:\n" + spec.text.split(":\n", 1)[-1]
            srcs += [s_.url for s_ in spec.sources[:1]]
        if not lines and not (spec and spec.confident):
            return None
        return Answer(text=text, sources=[Source(url=u) for u in dict.fromkeys(srcs)], confident=True)

    # ---- links ------------------------------------------------------------
    def _answer_link(self, q, si, prefix):
        qt = [t for t in tokens(q) if t not in ("link", "url", "website", "site", "web", "page", "online", "use", "see", "find")]
        if not qt:
            return None
        us = self.idx_units["link"]
        hits = self.idx["link"].search(qt, self._visible("link", si), k=300)
        agg: dict[str, float] = defaultdict(float)
        cnt: Counter = Counter()
        best_src: dict[str, str] = {}
        for d, s in hits:
            u = us[d]
            t = u.meta["target"].rstrip("/")
            host = urlparse(t).netloc.lower()
            mult = 1.0
            if host not in ("www.dupaco.com", "dupaco.com", "join.dupaco.com", "shine.dupaco.com"):
                mult = 0.3
            if "/hubfs" in t or t.endswith(".pdf") or "?" in t and "join.dupaco.com" not in t:
                mult *= 0.5
            if "/business" in t and "business" not in q.lower():
                mult *= 0.5
            if re.search(r"\b(join|apply|become a member|open an account|enroll)\b", q.lower()) and \
                    (host.startswith("join.") or "/apply" in t or "application" in t):
                mult *= 1.3        # joining / applying online happens on the application portal
            if s * mult > agg[t]:
                agg[t] = s * mult
                best_src[t] = u.url
            cnt[t] += 1
        if not agg:
            return None
        ranked = sorted(agg, key=lambda t: (-(agg[t] * (1 + 0.15 * math.log(1 + cnt[t]))), t))
        t = ranked[0]
        return Answer(text=f"{prefix}You can use {t}", sources=[Source(url=t), Source(url=best_src[t])],
                      confident=True)

    # ---- general sentences -----------------------------------------------
    def _undated_date(self, u):
        """Articles carry a date; undated product pages count as 'as new as the snapshot'."""
        d = u.meta.get("date")
        if d:
            return d
        if u.meta["auth"] >= 0.6:
            return self.snaps[max(u.snaps)][1]
        return date.min

    def _answer_text(self, q, si, prefix):
        qtoks = tokens(q, heavy=True)
        if not qtoks:
            return Answer(text="I don't know. Please call one of our branches.", confident=False)
        if re.search(r"\bstill (?:open|operating|around|there)\b|\bshut(?:ting)? down\b", q.lower()):
            qtoks = qtoks + ["close"]
        us = self.idx_units["sent"]
        hits = self.idx["sent"].search(qtoks, self._visible("sent", si), k=300)
        if not hits:
            return self._dont_know()
        qset = set(qtoks)
        idf = self.idx["sent"].idf
        total = sum(idf(t) for t in qset) or 1.0
        ql = q.lower()
        how_many = re.search(r"\bhow many\b|\bnumber of\b|\bhow often\b", ql)
        defn = re.match(r"\s*(what is|what's|whats|what are|tell me about|describe|explain)\b", ql)
        if how_many and re.search(r"\bnumber of\b", ql):
            qtoks = [t for t in qtoks if t != "number"] or qtoks
            qset = set(qtoks)
            total = sum(idf(t) for t in qset) or 1.0
            hits = self.idx["sent"].search(qtoks, self._visible("sent", si), k=300)
        count_nouns = [t for t in qset if not t.isdigit()] if how_many else []
        cands = []
        for d, s in hits:
            u = us[d]
            st = set(u.toks)
            sst = u.meta["stoks"]
            pv = u.meta.get("prev", {}).get(self._page_version(si).get(u.url, si))
            pst = pv.meta["stoks"] if pv is not None else frozenset()
            cover = sum(idf(t) * (1.0 if t in sst else 0.7 if t in pst else 0.5) for t in qset if t in st or t in pst) / total
            s = s * (0.4 + 0.6 * cover)
            if not (qset & sst):
                s *= 0.5
            if len(u.text.split()) < 5:
                s *= 0.7
            low = u.text.lower()
            if self._expired(u.text, si):
                s *= 0.2
            if re.search(r"\b(for more information|click here|learn more|visit dupaco\.com|to apply, visit)\b", low):
                s *= 0.6           # call-to-action lines rarely answer anything
            if defn and re.search(r"\b(is an?|are|provides?|offers?|helps?|gives?|allows?|lets)\b", low):
                s *= 1.2
            s *= 0.7 + 0.3 * u.meta["auth"]
            if how_many:
                low = u.text.lower()
                if any(re.search(r"(?:\d[\d,]*\+?|" + ORDINALS[2:-2] + r")\s+(?:[a-z-]+\s+){0,2}" + re.escape(n[:5]), low)
                       for n in count_nouns):
                    s *= 1.6
            if u.text.rstrip().endswith("?"):
                s *= 0.85
            cands.append((s, cover, u))
        cands.sort(key=lambda x: (-x[0], x[2].text))
        top_s, top_cover, top_u = cands[0]
        if top_cover < 0.4:
            return self._dont_know()
        # several sentences answer about equally well (often the same boilerplate with different
        # numbers from different years): trust the most recently dated one
        close = [c for c in cands[:60] if c[0] >= 0.85 * top_s and (how_many or same_statement(c[2].text, top_u.text))]
        best = max(close, key=lambda c: (self._undated_date(c[2]), c[2].meta["auth"], c[0], c[2].text))[2]
        text = best.text
        pver = self._page_version(si).get(best.url, si)
        nxt = best.meta["next"].get(pver)
        nxt_ok = nxt is not None and self._vis_unit(nxt, si) and len(nxt.text) < 350
        pv = best.meta.get("prev", {}).get(pver)
        # the previous sentence supplies the subject ("This location will close ...")
        anaphora = re.match(r"\s*(both|this|that|these|those|they|it|he|she|its|their)\b", best.text, re.I)
        if pv is not None and ((qset & pv.meta["stoks"]) - best.meta["stoks"] or anaphora) and len(pv.text) < 300 \
                and not pv.text.endswith("?"):
            text = f"{pv.text} {text}"
        if best.text.endswith("?") and nxt_ok:
            text = nxt.text          # FAQ: the question line is followed by its answer
        elif nxt_ok and (len(text) < 70 or defn):
            text = f"{text} {nxt.text}"
        urls = []
        for c in cands[:80]:
            if c[2].text == best.text and c[2].url not in urls:
                urls.append(c[2].url)
        urls.sort(key=lambda x: (-page_authority(x), x))
        note = None
        variants = {c[2].text for c in cands[:80] if same_statement(c[2].text, best.text)}
        if len(variants) > 1:
            note = "Older pages state different numbers; used the most recently dated page."
        return Answer(text=prefix + text, sources=[Source(url=u, snapshot_id=self.snaps[si][0], quote=best.text[:200])
                                                   for u in urls[:4]],
                      confident=True, conflict_note=note)

    def _expired(self, text, si):
        """'offer ended 2025-09-30', 'expires 12/31/2024' ... before the snapshot date."""
        m = re.search(r"\b(?:offer\s+)?(?:ended|expired|expires|expiring|ends|valid (?:through|until))\s*(?:on\s*)?"
                      r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|[A-Z][a-z]+ \d{1,2},? \d{4})", text)
        if not m:
            return bool(re.search(r"\b(offer|promotion|promo) (?:has )?(?:ended|expired)\b", text, re.I))
        d = iso(m.group(1)) if re.match(r"\d{4}-", m.group(1)) else parse_date_any(m.group(1))
        return bool(d and d < self.snaps[si][1])

    @staticmethod
    def _dont_know():
        return Answer(text="I don't know. Please call one of our branches.", confident=False)

    @staticmethod
    def _family(u):
        """Product family from the table's own heading first, the page title only as a fallback."""
        fams = ("ira", "hsa", "heloc", "mortgage", "auto", "certificate", "moneymarket", "checking",
                "highyield", "saving", "visa", "personal", "student", "recreational", "business")
        for text in (u.meta["context"], u.meta.get("desc", "")):
            t = set(tokens(text))
            if "improvement" in t:
                return "improvement"
            for f in fams:
                if f in t:
                    return f
        # untitled tables ("Regular", "Special"): terms in months + dividend/APY columns = certificates
        hdrs = {h for h, _ in u.meta["vals"]}
        if re.search(r"\b\d+\s*months?\b", u.meta.get("desc", ""), re.I) and hdrs & {"APY", "dividend rate"}:
            return "certificate"
        t = set(tokens(u.meta["title"]))
        for f in fams:
            if f in t:
                return f
        return "other"

    # ------------------------------------------------------------------ conflicts
    def conflicts(self) -> list[Conflict]:
        if self._conflicts is None:
            self._conflicts = self._build_conflicts()
        return self._conflicts

    def _build_conflicts(self):
        out = []
        last = len(self.snaps) - 1
        # 1) rate-table facts
        facts: dict[tuple, list] = defaultdict(list)
        for u in self.idx_units["row"]:
            fam = self._family(u)
            biz = "business" if ("business" in u.meta["context"].lower() or "business" in u.url) else "personal"
            for h, v in u.meta["vals"]:
                if h == "dividend rate" and any(hh == "APY" for hh, _ in u.meta["vals"]):
                    continue
                if not PCT_RE.search(v):
                    continue
                key = (fam, biz, re.sub(r"\s+", " ", u.meta["dkey"].lower()), h)
                for s in u.snaps:
                    facts[key].append((s, u, v))
        def numkey(v):
            return tuple(re.findall(r"\d+(?:\.\d+)?", v.replace(",", "")))
        for key, obs in facts.items():
            # "7.34%" and "7.34% APR" are the same value
            canon = {}
            for o in obs:
                canon.setdefault(numkey(o[2]), o[2])
            obs = [(sn, u, canon[numkey(v)]) for sn, u, v in obs]
            vals = {o[2] for o in obs}
            if len(vals) < 2:
                continue
            cur = [o for o in obs if o[0] == last]
            if not cur:
                continue
            # the same table must not hold two values for one key (that's a tier, not a conflict)
            per_table = defaultdict(set)
            for s, u, v in obs:
                per_table[(s, u.meta["tkey"])].add(v)
            if any(len(x) > 1 for x in per_table.values()):
                continue
            win = max(cur, key=lambda o: (o[1].meta.get("eff") or date.min, o[1].meta["auth"]))
            wv = win[2]
            others = []
            for s, u, v in sorted(obs, key=lambda o: (-o[0], -o[1].meta["auth"])):
                if v != wv and v not in [x[0] for x in others]:
                    others.append((v, u, s))
            stale_pages = [o for o in others if last in o[1].snaps]
            if stale_pages:
                reason = (f"{win[1].url} (effective {win[1].meta['eff'] or 'n/a'}) is the most recent official "
                          f"source; {stale_pages[0][1].url} still shows an older value")
            else:
                reason = "value changed between snapshots; the newest snapshot's official page is current"
            topic = f"{key[0]} {key[2]} {key[3]}".strip()
            values = [wv] + [o[0] for o in others[:4]]
            srcs = [Source(url=win[1].url, snapshot_id=self.snaps[win[0]][0])] + \
                   [Source(url=o[1].url, snapshot_id=self.snaps[o[2]][0]) for o in others[:4]]
            out.append(Conflict(topic=topic, values=values, sources=srcs, resolution=wv, reason=reason))
        # 1b) rate vs APY on the same row that no compounding frequency can reconcile
        seen_bad = set()
        cur_ver = self._page_version(last)
        for u in self.idx_units["row"]:
            if cur_ver.get(u.url, -1) not in u.snaps:
                continue
            v = dict(u.meta["vals"])
            if "dividend rate" not in v or "APY" not in v:
                continue
            try:
                r = float(re.search(r"\d+(?:\.\d+)?", v["dividend rate"]).group())
                a = float(re.search(r"\d+(?:\.\d+)?", v["APY"]).group())
            except AttributeError:
                continue
            if len(re.findall(r"\d+(?:\.\d+)?%", " ".join(x for _, x in u.meta["vals"]))) > 2:
                continue          # multi-phase promo rows
            def apy_pct(rr, n):
                return ((1 + rr / 100 / n) ** n - 1) * 100
            # rates and APYs are printed to 2 decimals, so allow half a hundredth either way
            fits = any(apy_pct(r - 0.005, n) - 0.005 <= a <= apy_pct(r + 0.005, n) + 0.005
                       for n in (1, 2, 4, 12, 365))
            if fits or (u.meta["desc"], r, a) in seen_bad:
                continue
            seen_bad.add((u.meta["desc"], r, a))
            out.append(Conflict(
                topic=f"{self._family(u)} {u.meta['desc']} dividend rate vs APY",
                values=[v["dividend rate"], v["APY"]],
                sources=[Source(url=u.url, snapshot_id=self.snaps[last][0], quote=u.text[:200])],
                resolution=None,
                reason=(f"a {r:.2f}% dividend rate cannot produce a {a:.2f}% APY under any compounding "
                        f"(APY = (1 + r/n)^n - 1); one of the two figures on {u.url} is wrong")))
        # 2) sentence facts: same wording, different numbers, across pages of the latest snapshot
        groups: dict[str, list] = defaultdict(list)
        cur_ver = self._page_version(last)
        for u in self.idx_units["sent"]:
            if cur_ver.get(u.url, -1) not in u.snaps or len(u.text) > 260:
                continue
            if not (NUM_MASK_RE.search(u.text) or re.search(ORDINALS, u.text, re.I)):
                continue
            if DATE_ONLY.search(u.text) and not PCT_RE.search(u.text):
                continue
            t = statement_key(u.text)
            if len(t.split()) < 5 or t.count("#") > 4:
                continue
            groups[t].append(u)
        per_url: dict[tuple, list] = defaultdict(list)
        for u in self.idx_units["sent"]:
            if len(u.text) > 260 or not (NUM_MASK_RE.search(u.text) or re.search(ORDINALS, u.text, re.I)):
                continue
            if DATE_ONLY.search(u.text) and not PCT_RE.search(u.text):
                continue
            t = statement_key(u.text)
            if len(t.split()) < 3 or t.count("#") > 4:
                continue
            per_url[(u.url, t)].append(u)
        for (url, t), us in per_url.items():
            if len({u.text for u in us}) < 2 or len(us) > 6:
                continue
            if any(len(u.snaps & v.snaps) for u in us for v in us if u is not v):
                continue          # two versions at once on one page = list items, not a change
            us = sorted(us, key=lambda u: -max(u.snaps))
            cur = us[0]
            out.append(Conflict(
                topic=t.replace("#", "N")[:120], values=[cur.text] + [u.text for u in us[1:5]],
                sources=[Source(url=url, snapshot_id=self.snaps[max(u.snaps)][0]) for u in us[:5]],
                resolution=cur.text,
                reason=f"the page changed between snapshots; newest version ({self.snaps[max(cur.snaps)][0]}) is current"))
        for t, us in groups.items():
            texts = {u.text for u in us}
            if len(texts) < 2:
                continue
            per_page = defaultdict(set)
            for u in us:
                per_page[u.url].add(u.text)
            if any(len(v) > 1 for v in per_page.values()):
                continue          # several variants on one page = list of different items
            best = max(us, key=lambda u: (self._undated_date(u), u.meta["auth"]))
            others = sorted(texts - {best.text})
            out.append(Conflict(
                topic=t.replace("#", "N")[:120], values=[best.text] + others[:4],
                sources=[Source(url=best.url)] + [Source(url=next(u.url for u in us if u.text == o)) for o in others[:4]],
                resolution=best.text,
                reason=f"most recently dated page ({self._undated_date(best)}, {best.url}) wins over older pages"))
        return out


def build():
    return Knowledge()
