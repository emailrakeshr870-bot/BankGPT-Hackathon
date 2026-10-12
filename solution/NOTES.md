# BankGPT: how this solution works

Answers come from a **retrieval-augmented (RAG) workflow in n8n**, backed by a **Pinecone** vector
index of all five website snapshots and an **OpenAI** model. `solution.py` is a thin client: it sends
each question to the n8n webhook and turns the reply into an `Answer`. A rule-based engine
(`local_engine.py`, standard library only) runs alongside it as a backup and computes `conflicts()`.

## What you need to run it

| Item | Where it is used | Needed by the graders? |
|---|---|---|
| n8n webhook URL + secret | `solution.py` → `bankgpt_config.json` (or `BANKGPT_WEBHOOK_URL` / `BANKGPT_WEBHOOK_KEY`) | **Yes** (provided in `bankgpt_config.json`) |
| OpenAI API key | inside n8n (embeddings `text-embedding-3-small`, answer model) | No, it stays in n8n |
| Pinecone API key | inside n8n (index `bankgpt`) | No, it stays in n8n |

No Python packages are needed (`requirements.txt` is empty). Settings, in order of priority:
environment variables, then `solution/bankgpt_config.json`:

```json
{ "webhook_url": "https://<team>.app.n8n.cloud/webhook/bankgpt-answer",
  "webhook_key": "<secret>", "timeout": 15, "mode": "n8n" }
```

`mode`: `n8n` (default; falls back to the local engine on any failure), `local` (never calls n8n),
`n8n-only` (no fallback; for testing).

## Pipeline

```
solution.py ──POST {question, as_of}──▶ n8n "BankGPT – API" (Webhook, header auth)
                                          └─▶ "BankGPT – AnswerFlow"
  Prepare → Embed → Search A → Search B → Re-rank → Answer (LLM, JSON) → Format
```

1. **Index (offline, `rag/build_chunks.py` + `rag/load_pinecone.py`).** Every page of every snapshot
   is cut into chunks: rate tables are kept whole with their heading and "Rates are effective as of"
   line; prose is split by section with a small overlap; sidebars ("Recent News", "Related") are
   dropped. Identical chunks across snapshots are stored once, with flags `snap_1`…`snap_5` saying
   which downloads contained them, plus `page_type` (rates, product, promo, news, blog, resource,
   calculator, pdf, other), `effective_date` and `published_date`. 7,283 chunks, deterministic IDs.
2. **Prepare.** Reads any date in the question ("as of May 2026", "6/30/2026") or the `as_of`
   argument and picks the snapshot that was current on that date. Extracts the qualifiers that
   decide tiers: term ("five-year" = 60 months), model year, amount, jumbo/IRA/business/…, product
   family (auto vs boat/RV/ATV). Recognises question types (rate, hours, link, membership, mobile
   wallet, card list) and adds the words the website itself uses to the search text.
3. **Two searches in Pinecone,** both limited to the chosen snapshot: **A** = official rates,
   product and promo pages only; **B** = everything except calculators (which carry stale rates).
4. **Re-rank** (in code, no model): similarity plus boosts for official pages, rate tables, exact
   term / model-year / amount / qualifier matches, posted branch hours, and newer dates; a table
   copy older than the official table is pushed down. The best 8 passages go to the model.
5. **Answer.** The model answers **only from the passages**, following `n8n/system_prompt.txt`:
   newest date wins, then rates page > product > promo > other; tiers are separate facts; never
   repeat a stale number (it goes in `conflict_note` instead); dividend rate ↔ APY conversion
   (`APY = (1 + r/n)^n − 1`, n = 2 for certificates/IRAs, 12 otherwise); loan APRs never converted;
   "I don't know. Please call one of our branches." when the passages don't contain the answer.
6. **Format.** Parses the JSON reply (and rescues a cut-off reply), adds the product's own page and
   the top passages to the sources, and returns `{answer, sources, confident, conflict_note}`.

## Reliability, consistency, speed

- **Consistency:** answers are cached per (question, as_of), so repeats are identical; the model runs
  with top_p 0 and low reasoning effort.
- **Failures:** n8n retries OpenAI/Pinecone calls once. If the webhook still fails, times out
  (15 s) or returns nothing usable, `solution.py` answers with the local engine; after 3 failures in
  a row it stops calling n8n for the rest of the run.
- **Speed:** about 4 s per answer on n8n Cloud (p95 about 7.5 s): the answering model, plus network time between n8n Cloud, OpenAI and Pinecone.
- **Sources:** the pages the model cited, the product's main page, the top passages, and the pages
  the local engine would cite for the same question.

## `conflicts()`

Computed locally by `local_engine.py` from all five snapshots: the same rate fact (product + tier +
rate type) with different values across pages or snapshots, the same statement with different numbers
(branch counts, Forbes recognitions), and rate/APY pairs that cannot both be true. Each conflict
lists the value trusted and why (newest effective/published date, then page trust). Details in
`LOCAL_ENGINE_NOTES.md`.

## Results on the public practice set (40 questions)

| Version | Accuracy | Final |
|---|---|---|
| Rule-based only (`mode: local`) | 0.856 | 0.856 |
| n8n RAG, local n8n | 0.923 | 0.881 |
| n8n RAG on n8n Cloud (this submission) | 0.946 | 0.895 |

## Files

| File | What it is |
|---|---|
| `solution.py` | webhook client, cache, fallback, `conflicts()` |
| `local_engine.py` | rule-based backup engine and conflict finder |
| `bankgpt_config.json` | webhook URL + secret |
| `../n8n/*.js`, `../n8n/system_prompt.txt` | code of the n8n Code nodes and the answer prompt |
| `../rag/` | chunking, Pinecone loading and probing scripts |
| `../dev/quick_check.py` | ask n8n selected eval questions and score them |
