// ===== BankGPT – Code node "Re-rank" =====
// Mode: Run Once for All Items.
// Reads the results of "Search A" (official pages) and "Search B" (everything but calculators),
// merges them, scores each chunk, and passes the best 8 to the answering model.
// Node names below must match your node names exactly.

const P = $('Prepare').first().json;
const matchesA = ($('Search A').first().json.matches) || [];
const matchesB = ($('Search B').first().json.matches) || [];

const TYPE_BOOST = { rates: 0.08, product: 0.06, promo: 0.03, news: 0, blog: 0, resource: 0, other: 0, pdf: -0.03, calculator: -0.10 };
const TOP_N = P.hoursQ ? 10 : 8;

// merge, de-duplicate (same chunk id, or the same text on another page)
const seen = new Map();
for (const m of [...matchesA, ...matchesB]) {
  const md = m.metadata || {};
  const body = String(md.text || '').split('\n\n').slice(1).join('\n\n').trim();
  const key = m.id || body.slice(0, 300);
  if (!seen.has(key) || seen.get(key).score < m.score) seen.set(key, { id: m.id, score: m.score, md, body });
}

const qWords = P.question.toLowerCase().match(/[a-z0-9$%.]{3,}/g) || [];
const STOP = new Set(['what','whats','the','for','and','are','your','you','does','dupaco','how','much','with','can','tell','about','this','that','have','from']);
const content = [...new Set(qWords.filter(w => !STOP.has(w)))];

function dateBoost(d) {            // newer effective / published date => small boost (2018 → 0, 2026 → 0.04)
  if (!d) return 0;
  const y = parseInt(d.slice(0, 4), 10) + (parseInt(d.slice(5, 7), 10) - 1) / 12;
  return Math.max(0, Math.min(1, (y - 2018) / 8.7)) * 0.04;
}
function yearInRange(text, y) {    // "2024-27 Model Year" contains 2025?
  for (const m of text.matchAll(/\b(20\d\d)\s*[-–]\s*(\d{2,4})\b/g)) {
    let a = +m[1], b = +m[2]; if (b < 100) b += 2000;
    if (a > b) [a, b] = [b, a];
    if (y >= a && y <= b) return true;
  }
  for (const m of text.matchAll(/\b(19|20)(\d\d)\s+Model Year \(and older\)/gi)) if (y <= +(m[1] + m[2])) return true;
  return false;
}
function amountInRange(text, amt) {
  const t = text.replace(/[–—]/g, '-');
  for (const m of t.matchAll(/\$\s*([\d,]+(?:\.\d+)?)\s*-\s*\$?\s*([\d,]+(?:\.\d+)?)/g)) {
    const a = parseFloat(m[1].replace(/,/g, '')), b = parseFloat(m[2].replace(/,/g, ''));
    if (amt >= a && amt <= b) return true;
  }
  for (const m of t.matchAll(/\$\s*([\d,]+)\s*(?:\+|and up|or more)/g)) if (amt >= parseFloat(m[1].replace(/,/g, ''))) return true;
  for (const m of t.matchAll(/\$\s*([\d,]+)\s*or less/g)) if (amt <= parseFloat(m[1].replace(/,/g, ''))) return true;
  return false;
}

// newest effective date among OFFICIAL rate tables (rates/product pages) in the results:
// any other table with an older date is probably a stale copy (promo page, PDF, old guide)
let newestOfficial = '';
for (const c of seen.values()) {
  const t = c.md.page_type, d = c.md.effective_date || '';
  if ((t === 'rates' || t === 'product') && d > newestOfficial) newestOfficial = d;
}

const scored = [];
for (const c of seen.values()) {
  const text = c.body;
  const low = text.toLowerCase();
  const type = c.md.page_type || 'other';
  let s = c.score;
  const why = [];
  s += TYPE_BOOST[type] ?? 0;
  const isTable = c.md.has_table === 'true' || /\|\s*---/.test(text);
  if (P.rateQ) {
    if (isTable) { s += 0.04; why.push('table'); }
    if (['news', 'blog', 'resource'].includes(type)) s -= 0.03;
  }
  for (const t of P.terms || []) {
    const num = t.split(/[\s-]/)[0], unit = t.includes('Year') ? 'year' : 'month';
    if (new RegExp(`\\b${num}[\\s-]*${unit}s?\\b`, 'i').test(text)) { s += 0.08; why.push(t); }
  }
  for (const y of P.years || []) if (yearInRange(text, y)) { s += 0.06; why.push(`MY ${y}`); }
  for (const a of P.amounts || []) if (amountInRange(text, a)) { s += 0.05; why.push(`$${a}`); }
  for (const qual of P.quals || []) if (low.includes(qual)) { s += 0.03; why.push(qual); }
  if ((P.family || []).length && P.family.some(w => low.includes(w))) { s += 0.06; why.push('product'); }
  if (P.hoursQ && /lobby|drive-?up|drive-?thr/i.test(text) && /\d\s*(a\.m\.|p\.m\.|am|pm)|noon/i.test(text)) {
    s += 0.15; why.push('hours');
  }
  if (P.joinQ && /count(y|ies)/i.test(text) && /iowa|illinois|wisconsin/i.test(text)) { s += 0.10; why.push('charter area'); }
  if (P.linkQ && type === 'product') { s += 0.04; why.push('product page'); }
  const eff = c.md.effective_date || '';
  if (P.rateQ && isTable && eff && newestOfficial && eff < newestOfficial && !['rates', 'product'].includes(type)) {
    s -= 0.08; why.push('older copy');
  }
  if (content.length) s += 0.10 * content.filter(w => low.includes(w)).length / content.length;
  s += dateBoost(c.md.effective_date || c.md.published_date);
  scored.push({ ...c, final: s, why });
}
scored.sort((a, b) => b.final - a.final || String(a.id).localeCompare(String(b.id)));
const top = scored.slice(0, TOP_N);

const passages = top.map((c, i) => `[${i + 1}] ${String(c.md.text || '').slice(0, 3000)}`).join('\n\n---\n\n');

return [{ json: {
  question: P.question,
  as_of: P.as_of,
  as_of_label: P.as_of_label,
  passages,
  passage_urls: top.map(c => c.md.url),
  debug: top.map(c => ({ score: +c.final.toFixed(3), sim: +c.score.toFixed(3), type: c.md.page_type,
                        date: c.md.effective_date || c.md.published_date || '', url: c.md.url, why: c.why.join(',') })),
} }];
