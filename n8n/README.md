# n8n workflows

| File | Workflow | Purpose |
|---|---|---|
| `BankGPT - AnswerFlow.json` | BankGPT – AnswerFlow | The core: Prepare → Embed → Search A → Search B → Re-rank → Answer → Format |
| `BankGPT - API.json` | BankGPT – API | Webhook `POST /webhook/bankgpt-answer` (header `X-BankGPT-Key`) → AnswerFlow → JSON reply. Used by `solution/solution.py` |
| `BankGPT - Chat.json` | BankGPT – Chat | Public hosted chat with follow-up questions → AnswerFlow |
| `archive/` | | First prototype (n8n QA chain), kept for reference |

Code of the nodes, kept in sync with the workflows:

| File | Node |
|---|---|
| `prepare.js`, `rerank.js`, `format.js` | AnswerFlow Code nodes |
| `system_prompt.txt` | AnswerFlow "Answer" system message |
| `chat/1_context.js`, `chat/3_use_rewrite.js`, `chat/4_reply.js` | Chat Code nodes "Context", "Use rewrite", "Reply" |
| `chat/2_rewrite_prompt.txt` | Chat "Rewrite" model prompt |

## Importing

1. Import **AnswerFlow** first, then **API** and **Chat** (⋯ → Import from File).
2. Create credentials and select them in the nodes:
   - **OpenAI** (Embed, Answer, Rewrite).
   - **Header Auth** `Api-Key: <Pinecone key>` (Search A, Search B; HTTP Request to `https://<index-host>/query`).
   - **Header Auth** `X-BankGPT-Key: <your secret>` (API Webhook).
3. In API and Chat, re-select AnswerFlow in the "Execute Sub-workflow" node (IDs change on import).
4. Answer node: GPT-6-LUNA, reasoning Low, max tokens 1000, Top P 0, output JSON Object.
   Turn on Retry On Fail (2 tries, 500 ms) for Embed, Search A, Search B and Answer.
5. Publish all three. Put the API webhook's Production URL and secret in `solution/bankgpt_config.json`.
