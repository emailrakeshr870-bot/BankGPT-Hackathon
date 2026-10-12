// ===== BankGPT Chat – Code node "Use rewrite" (Run Once for All Items) =====
// Takes the rewritten question from the "Rewrite" model node; falls back to the original message.
const C = $('Context').first().json;
const SKIP = new Set(['type', 'format', 'id', 'model', 'role', 'status', 'object', 'finish_reason', 'refusal']);
function findText(o) {
  if (typeof o === 'string') return o.includes(' ') ? o : null;
  if (Array.isArray(o)) { for (const x of o) { const t = findText(x); if (t) return t; } return null; }
  if (o && typeof o === 'object') {
    for (const k of ['content', 'text', 'output', 'message', 'response', 'choices']) {
      if (k in o) { const t = findText(o[k]); if (t) return t; }
    }
    for (const [k, v] of Object.entries(o)) { if (SKIP.has(k)) continue; const t = findText(v); if (t) return t; }
  }
  return null;
}
let q = (findText($input.first().json) || '').trim().replace(/^["'“]|["'”]$/g, '').replace(/^Output:\s*/i, '');
if (!q || q.length > 400) q = C.message;
return [{ json: { ...C, question: q } }];
