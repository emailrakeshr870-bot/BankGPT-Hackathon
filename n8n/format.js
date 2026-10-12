// ===== BankGPT – Code node "Format" =====
// Mode: Run Once for All Items.
// Turns the answering model's reply into { answer, sources, confident, conflict_note }.
// Works with the different output shapes of n8n's OpenAI nodes.

const R = $('Re-rank').first().json;
const P = $('Prepare').first().json;
// main product page for each product family (the page members are sent to for that product's rates)
const FAMILY_PAGE = [
  [/\bauto\b|motorcycle/, 'https://www.dupaco.com/personal/loans/auto-motorcycle-loans'],
  [/home loan|mortgage|fixed conventional/, 'https://www.dupaco.com/personal/loans/home-loans'],
  [/home equity|heloc/, 'https://www.dupaco.com/personal/loans/home-equity'],
  [/certificate|savings|money market|\bira\b/, 'https://www.dupaco.com/personal/savings-account'],
  [/checking/, 'https://www.dupaco.com/personal/checking-accounts'],
  [/personal loan/, 'https://www.dupaco.com/personal/loans/personal-loans'],
  [/boat|\brv\b|recreational|atv|snowmobile/, 'https://www.dupaco.com/personal/loans/recreational-loans'],
];
const out = $input.first().json;

const SKIP = new Set(['type', 'format', 'id', 'model', 'role', 'status', 'object', 'finish_reason', 'refusal']);
function findText(o) {             // the model's reply text anywhere in the node output (not settings like "json_object")
  if (typeof o === 'string') return (o.includes('{') || o.includes(' ')) ? o : null;
  if (Array.isArray(o)) { for (const x of o) { const t = findText(x); if (t) return t; } return null; }
  if (o && typeof o === 'object') {
    for (const k of ['content', 'text', 'output', 'message', 'response', 'choices']) {
      if (k in o) { const t = findText(o[k]); if (t) return t; }
    }
    for (const [k, v] of Object.entries(o)) { if (SKIP.has(k)) continue; const t = findText(v); if (t) return t; }
  }
  return null;
}

function findAnswerObj(o) {        // an object with an "answer" field anywhere (JSON already parsed by n8n)
  if (o && typeof o === 'object') {
    if (!Array.isArray(o) && typeof o.answer === 'string') return o;
    for (const v of Object.values(o)) { const r = findAnswerObj(v); if (r) return r; }
  }
  return null;
}

let raw = findAnswerObj(out);
if (!raw) {
  const t = findText(out) || '';
  const m = t.match(/\{[\s\S]*\}/);
  try { raw = m ? JSON.parse(m[0]) : null; } catch (e) { raw = null; }
  if (!raw) {                      // cut-off JSON: rescue the "answer" text if it is there
    const a = t.match(/"answer"\s*:\s*"((?:[^"\\]|\\.)*)/);
    raw = a ? { answer: JSON.parse('"' + a[1].replace(/\\$/, '') + '"'), confident: true }
            : (t && !t.includes('{') ? { answer: t } : { answer: '', failed: true });
  }
}

let answer = String(raw.answer || '').trim();
const dontKnow = /\bi don'?t know\b/i.test(answer) || !answer;
if (!answer) answer = "I don't know. Please call one of our branches.";
let sources = Array.isArray(raw.sources) ? raw.sources.filter(s => typeof s === 'string' && s.startsWith('http')) : [];
if (!dontKnow && sources.length === 0) sources = (R.passage_urls || []).slice(0, 2);   // never answer without a source
if (!dontKnow) {
  // keep the model's first 4 sources, then add the product page for the product asked about,
  // the branch locator for hours questions, and the best passages
  const extra = [];
  const fam = (P.family || []).join(' ');
  for (const [re, url] of FAMILY_PAGE) if (P.rateQ && re.test(fam)) { extra.push(url); break; }
  if (P.hoursQ) extra.push('https://www.dupaco.com/contact');   // branch locator
  for (const u of (R.passage_urls || []).slice(0, 3)) extra.push(u);
  sources = [...new Set([...sources.slice(0, 4), ...extra].map(u => u.replace(/#.*$/, '')))].slice(0, 6);
}
if (dontKnow) sources = [];

// membership questions: make sure the area served is named when a passage states it
if (P.joinQ && !dontKnow && !/iowa|illinois|wisconsin/i.test(answer)) {
  // several press releases may state it; the largest county count is the newest
  const found = [...String(R.passages || '').matchAll(/\b(?:Dupaco|It) serves residents in (\d+)? ?counties[^.\n]*Wisconsin[^.\n]*\./gi)];
  found.sort((a, b) => (+b[1] || 0) - (+a[1] || 0));
  if (found.length) answer += ' ' + found[0][0].trim().replace(/^It /, 'Dupaco ');
}
// milestone questions ("how many times", recognition, awards): keep the newest milestone wording
// ("This was the fifth time ...") when the model paraphrased it as a number
const ORD = ['first','second','third','fourth','fifth','sixth','seventh','eighth','ninth','tenth','eleventh','twelfth'];
if (!dontKnow && /recogni|award|honou?r|rank|named|how many times/i.test(P.question || '')) {
  const hits = [...String(R.passages || '').matchAll(new RegExp(`[^.\\n]*\\bthe (${ORD.join('|')}) (?:consecutive )?(?:time|year)[^.\\n]*\\.`, 'gi'))];
  if (hits.length) {
    hits.sort((a, b) => ORD.indexOf(b[1].toLowerCase()) - ORD.indexOf(a[1].toLowerCase()));
    const best = hits[0];
    if (!new RegExp(`\\b${best[1]}\\b`, 'i').test(answer)) answer += ' ' + best[0].trim().replace(/^\[\d+\]\s*/, '');
  }
}
if (raw.failed) {                  // no usable reply: tell solution.py to use its local backup
  return [{ json: { answer: '', error: 'no usable model reply', sources: [], confident: false, conflict_note: null, as_of: R.as_of || null } }];
}
return [{ json: {
  answer,
  sources: [...new Set(sources)],
  confident: dontKnow ? false : (raw.confident !== false),
  conflict_note: raw.conflict_note || null,
  as_of: R.as_of || null,
} }];
