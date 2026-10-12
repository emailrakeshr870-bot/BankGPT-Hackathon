# The local backup engine (`local_engine.py`)

**No API keys. No LLM. No internet. Python standard library only** (`requirements.txt` is empty).
Everything runs locally inside `local_engine.py`; loading all five snapshots takes about 30 seconds and
a typical answer takes well under half a second.

## 1. Ingest: keep history, not just the latest copy

Every page of every snapshot is broken into small *units*:

| Unit | Example | Used for |
|---|---|---|
| **Table row** | `2024-27 Model Year · 60 Months · 5.39% APR`, plus its section heading and the table's "Rates are effective as of 08/31/2026" date | rates, APYs, APRs |
| **Sentence** | `Routing Number: 273974581`, plus the page's publication date if it is an article | everything else |
| **Hours block** | `Lobby: Monday–Thursday 8:30 a.m.–5 p.m.; Friday …` | branch hours |
| **Link** | anchor text + target URL | "what's the link for …" |

Identical units are stored once, together with the set of snapshots they appeared in. That gives the
full history of every fact, so the system can answer "as of" any date. A page that is missing from
one crawl keeps its last known copy, because each snapshot is "every page we had, newest version".

## 2. Deciding what is "current"

These rules are applied in this order:

1. **Date of the question.** Without `as_of`, the newest snapshot is used. With `as_of`, the system
   uses the website as it stood on that date, and it also reads the date stamps on the pages
   themselves: a rate table whose "effective as of" date shows it was already in force on that day
   counts, even if it was only crawled later, and a table that only took effect afterwards is
   excluded. Dates written inside the question ("as of May 2026") are recognised too.
2. **Newest date stamp wins.** For rate tables this is the "Rates are effective as of …" line. For
   articles it is the publication date. For other pages it is the newest "updated / effective as of"
   stamp on the page. A stale copy of a rate table (a vehicle calculator dated 1/1/2026, a checking
   promo dated 07/01/2025) loses to the official table dated 08/31/2026 or 06/02/2026.
3. **Page trust.** The official rates pages (`/rates`, `/business-rates`) come first, then product
   pages (`/personal/…`, `/business/…`, `/contact`), then promo landing pages (`…-lp`,
   `/switch-and-earn`), then everything else (newsroom, blog, PDFs, calculators). Trust never
   overrides a newer date stamp from rule 2.
4. **Newer article beats older article.** When several sentences say the same thing with different
   numbers ("…and **24** branch office locations" vs "…and **22** branch office locations", "the
   **fourth** time Forbes…" vs "the **fifth** time…"), the most recently dated page wins.
5. **Expired offers are ignored.** Text such as "offer ended 2025-09-30" before the snapshot date is
   pushed to the bottom.

## 3. Respecting qualifiers (tiers are different facts, not conflicts)

The question is parsed for:

- **Term:** "60-month", "five-year" → 60 months, "30-year" mortgage.
- **Model year:** 2025 matches the "2024-27" tier.
- **Balance or loan amount:** $15,000 matches the "$10,000 – $24,999.99" tier.
- **Product variants:** jumbo, IRA/Roth/Traditional, business, refinance, introductory/balance
  transfer, HSA, ARM, construction …

A row must match every qualifier the member named. Rows carrying a variant the member did *not* ask
about (jumbo, business, intro rate) are demoted. "Highest/lowest" picks the extreme tier.

## 4. Answering honestly and briefly

- Answers quote only the matching row(s) or sentence and never paste whole paragraphs, because
  repeating an old number is worse than saying less.
- If the member asks about something the site has no rate for (a 7/1 ARM, a construction loan, a
  60-month *jumbo* certificate), or about hours for a branch with no posted hours, the answer is
  "I don't know. … Please call one of our branches." (`confident=False`).
- **Dividend rate ↔ APY.** When a table shows only one of the two and the member asks for the
  other, the system calculates it with `APY = (1 + r/n)^n − 1` (or the inverse,
  `r = n·((1 + APY)^(1/n) − 1)`) and says it is calculated. `n` is the compounding frequency stated
  in the table's footnote, or else semi-annual for certificates and IRAs (2) and monthly for
  savings, checking and money markets (12). This was checked against the rows that show both
  figures. Loan **APRs** are never converted, because an APR includes fees and is not an
  interest-compounding figure.
- Every answer cites the pages that show that exact value, most trusted first. When other pages
  disagree, `conflict_note` explains what was ignored and why.

## 4b. Kinds of questions it recognises

| Question | How it is answered |
|---|---|
| Rates, APY, APR, dividends ("CD rate", "30-year mortgage for $250k") | From the rate tables, as readable bullet lists. A general question shows the whole product family (special *and* regular certificates). A specific one shows only the matching tier. |
| Dates written any way ("in June of 2026", "June 30, 2026", "6/30/2026", "as of May 2026") | Treated as `as_of`. |
| Hours (in general, for a named branch, in a city or state: "branch hours in IL") | Posted hours. When the website doesn't post hours for that branch, the answer says so instead of guessing. |
| "Who is the CEO?", "Who is Joseph Hearn?" | Names and titles from the most recently dated mention. Nicknames are matched (Joseph → Joe). Retirement and "former" mentions are skipped. |
| "What types of business loans do you have?", "Tell me about cash management" | The product page's heading, intro and list of options, plus the current rate when the product has one. |
| "Any promotions?" | Live promo landing pages (expired ones skipped) and the current certificate specials. |
| Everything else | The best-matching sentence on the most trusted, most recent page. |

## 5. `conflicts()`

The method reports three kinds of contradiction, each with the value it trusts and the reason:

- **Rate facts.** The same product + tier + rate type (APR/APY/rate) with different values on
  different pages or in different snapshots. The winner is the newest effective date on the most
  trusted page.
- **Statements that disagree across pages.** The same sentence wording with different numbers, such
  as branch counts, employee counts or Forbes recognitions. The winner is the most recently dated
  page.
- **Statements that changed on one page** between snapshots. The newest version wins.
- **A dividend rate and APY on the same row that cannot both be true**, i.e. no compounding
  frequency turns one into the other, even allowing for rounding to two decimals.

Tiers inside one table (e.g. 60 vs 72 months) and lists of different items on one page are never
reported as conflicts.

## Running it

```
python harness/run_eval.py --submission solution --eval eval/public_eval.jsonl
```

`dev/paraphrase_eval.jsonl` is a set of our own reworded questions, used to check that the system
isn't tuned to the exact wording of the practice questions. `dev/user_log_eval.jsonl` holds
questions from hands-on testing.

`chat.py` (in the kit's top folder) is an optional local chat page for trying the system by hand:
`python chat.py`, then open http://localhost:8765.
