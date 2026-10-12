# BankGPT: a knowledge system that knows what's true *now*

Our entry for the Interface.ai Knowledge Hackathon. BankGPT answers Dupaco Community Credit Union
member questions from five downloads of the credit union's website (May–September 2026) with the
**current** value, the **right tier**, a **source**, and an honest *"I don't know. Please call one of
our branches."* when the site doesn't say.

| Public practice set (40 questions) | Accuracy | Consistency | Speed (p50 / p95) | **Final** |
|---|---|---|---|---|
| BankGPT on n8n Cloud (submitted) | **0.946** | 1.00 | 4.2 s / 7.5 s | **0.895** |
| Rule-based backup engine alone | 0.856 | 1.00 | < 0.5 s | 0.856 |

No stale-value traps were hit in either run.

## How it works

```
harness ──▶ solution/solution.py ──POST {question, as_of}──▶ n8n "BankGPT – API" webhook
                    │                                              │
                    │ (backup if n8n fails)                        ▼
                    ▼                                  n8n "BankGPT – AnswerFlow"
            local_engine.py                 Prepare → Embed → Search A → Search B
          (rules + conflicts())                   → Re-rank → Answer (LLM) → Format
                                                          │
                                         Pinecone index "bankgpt" (7,283 chunks, all 5 snapshots)
```

1. **Index** (`rag/`): every page of every snapshot is chunked (rate tables kept whole with their
   "effective as of" date), de-duplicated across snapshots, and tagged with which snapshots contain it,
   its page type (rates, product, promo, news, blog, pdf, calculator…) and its dates.
2. **Prepare**: picks the snapshot that was current on the question's date, and reads the qualifiers
   that decide tiers (term, model year, amount, jumbo / IRA / business…).
3. **Two searches** in that snapshot: official rates/product/promo pages, and everything except
   calculators (which carry stale rates).
4. **Re-rank** in code: official pages, exact tier matches and newer dates win; stale table copies sink.
5. **Answer**: the model answers only from the passages, under explicit rules (newest date wins, then
   rates page > product > promo > other; tiers aren't conflicts; never repeat a stale number;
   rate ↔ APY conversion; honest "I don't know").
6. **Format**: clean JSON, sources, and a few deterministic safety nets.

`conflicts()` is computed locally from all five snapshots by the rule-based engine. Full details:
[`solution/NOTES.md`](solution/NOTES.md).

## Repository layout

| Path | What it is |
|---|---|
| `solution/` | **The submission**: `solution.py` (webhook client, cache, fallback), `local_engine.py` (rule-based backup and `conflicts()`), `NOTES.md` |
| `n8n/` | Exported n8n workflows, the code of every Code node, the answer prompt, and the chat workflow. See [`n8n/README.md`](n8n/README.md) |
| `rag/` | Chunking, Pinecone loading and probing scripts |
| `dev/` | Our extra test sets and `quick_check.py` (re-ask chosen questions) |
| `chat.py` | Local browser chat for the rule-based engine (no keys needed) |
| `harness/`, `eval/`, `starter/`, `snapshots_zip/` | The hackathon kit, unchanged ([`KIT_README.md`](KIT_README.md)) |

## Running it

Python 3.9+; no packages needed for the submission.

```
# settings: solution/bankgpt_config.json (copy bankgpt_config.example.json) or environment variables
#   BANKGPT_WEBHOOK_URL, BANKGPT_WEBHOOK_KEY, BANKGPT_MODE (n8n | n8n-only | local)
python harness/run_eval.py --submission solution --eval eval/public_eval.jsonl

# one question
python solution/solution.py "How much is a five-year car loan for a 2025 vehicle?"

# without any keys (rule-based engine only)
set BANKGPT_MODE=local          (Windows)   |   export BANKGPT_MODE=local   (macOS/Linux)
```

To rebuild the system from scratch (your own OpenAI, Pinecone and n8n accounts):

```
pip install openai pinecone
python rag/build_chunks.py                       # -> rag/chunks.jsonl
python rag/load_pinecone.py --index bankgpt      # needs OPENAI_API_KEY and PINECONE_API_KEY
```

then import the workflows as described in [`n8n/README.md`](n8n/README.md).

## Keys and secrets

OpenAI and Pinecone keys live only in n8n credentials, never in this repository. The webhook URL and
secret are in `solution/bankgpt_config.json`, included because this repository is private; the secret
will be changed after grading.
