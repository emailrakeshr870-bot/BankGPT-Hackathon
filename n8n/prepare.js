// ===== BankGPT – Code node "Prepare" =====
// Mode: Run Once for All Items. Input: { question, as_of } (as_of = "YYYY-MM-DD" or empty)
// Output: one item with the cleaned question, which snapshot to search, the text to embed,
// the two Pinecone filters, and the qualifiers the re-ranker needs.

const SNAPSHOTS = [            // number in Pinecone metadata (snap_N) -> date the site was downloaded
  { n: 1, date: '2026-05-12' },
  { n: 2, date: '2026-05-19' },
  { n: 3, date: '2026-06-09' },
  { n: 4, date: '2026-08-27' },
  { n: 5, date: '2026-09-28' },
];
const MONTHS = ['january','february','march','april','may','june','july','august','september','october','november','december'];
const MON = 'jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|' + MONTHS.join('|');
const NUMWORDS = { one:1, two:2, three:3, four:4, five:5, six:6, seven:7, eight:8, nine:9, ten:10, eleven:11, twelve:12,
                   fifteen:15, seventeen:17, eighteen:18, twenty:20, thirty:30 };

const inp = $input.first().json;
let q = String(inp.question || inp.chatInput || '').trim();
let asOf = String(inp.as_of || '').trim();

// ---- 1. a date written in the question ("as of June 30, 2026", "in May of 2026", "6/30/2026")
function monthIdx(s) { s = s.toLowerCase().replace('.', ''); return MONTHS.findIndex(m => m.startsWith(s.slice(0, 3))); }
function pad(n) { return String(n).padStart(2, '0'); }
function monthEnd(y, m) { return new Date(Date.UTC(y, m + 1, 0)).toISOString().slice(0, 10); }
const datePatterns = [
  [new RegExp(`\\(?\\b(?:(?:as of|as at|in|on|during|for|of|back in|around|at|by)\\s+)?(?:the\\s+)?(?:end of\\s+)?(${MON})\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?,?\\s+(?:of\\s+)?(\\d{4})\\b\\)?`, 'i'),
   m => `${m[3]}-${pad(monthIdx(m[1]) + 1)}-${pad(m[2])}`],
  [new RegExp(`\\(?\\b(?:(?:as of|as at|in|on|during|for|of|back in|around|at|by)\\s+)?(?:the\\s+)?(?:end of\\s+)?(${MON})\\.?,?\\s+(?:of\\s+)?(\\d{4})\\b\\)?`, 'i'),
   m => monthEnd(+m[2], monthIdx(m[1]))],
  [/\(?\b(?:(?:as of|as at|in|on|during|for|of|at|by)\s+)?(\d{1,2})\/(\d{1,2})\/(\d{4})\b\)?/i, m => `${m[3]}-${pad(m[1])}-${pad(m[2])}`],
  [/\(?\b(?:(?:as of|as at|in|on|during|for|of|at|by)\s+)?(\d{4})-(\d{2})-(\d{2})\b\)?/i, m => `${m[1]}-${m[2]}-${m[3]}`],
];
for (const [re, toIso] of datePatterns) {
  const m = q.match(re);
  if (m) {
    const iso = toIso(m);
    if (!asOf) asOf = iso;
    q = (q.slice(0, m.index) + ' ' + q.slice(m.index + m[0].length)).replace(/\s+/g, ' ').replace(/\s+([?.!,])/g, '$1').trim();
    break;
  }
}

// ---- 2. which snapshot was current on that date
const latest = SNAPSHOTS[SNAPSHOTS.length - 1];
let snap = latest;
if (asOf) {
  const older = SNAPSHOTS.filter(s => s.date <= asOf);
  snap = older.length ? older[older.length - 1] : SNAPSHOTS[0];
}
const asOfLabel = asOf ? asOf : 'today';

// ---- 3. qualifiers the member named (used by the re-ranker)
const ql = q.toLowerCase().replace(/[–—]/g, '-');
const qlNum = ql.replace(new RegExp(`\\b(${Object.keys(NUMWORDS).join('|')})\\b(?=[\\s-]*(?:months?|years?|yrs?|mos?)\\b)`, 'g'), w => NUMWORDS[w]);
const isMortgage = /mortgage|home ?loan|refinanc|heloc|home equity|fixed conventional/.test(ql);
const terms = [];               // as written in rate tables: "60 Months", "30-Year"
for (const m of qlNum.matchAll(/\b(\d{1,3})\s*-?\s*(?:months?|mos?)\b/g)) terms.push(`${+m[1]} Month`);
for (const m of qlNum.matchAll(/\b(\d{1,2})\s*-?\s*(?:years?|yrs?)\b/g)) {
  const n = +m[1];
  if (isMortgage || n >= 10) terms.push(`${n}-Year`); else terms.push(`${n * 12} Month`);
}
const years = [...ql.matchAll(/\b(20[0-3]\d|19\d\d)\b/g)].map(m => +m[1]);
const amounts = [...ql.matchAll(/\$\s*([\d,]+(?:\.\d+)?)\s*(k\b)?/g)].map(m => parseFloat(m[1].replace(/,/g, '')) * (m[2] ? 1000 : 1));
const QUALS = ['jumbo', 'ira', 'roth', 'traditional', 'business', 'refinanc', 'purchase', 'special', 'intro', 'balance transfer',
               'hsa', 'money market', 'high-yield', 'bright', 'launchpad', 'holiday', 'mini max', 'heloc', 'student', 'boat', 'rv',
               'motorcycle', 'atv', 'platinum', 'rewards'];
const quals = QUALS.filter(w => ql.includes(w));
const qlNoWallet = ql.replace(/\b(apple|google|samsung|mobile|zelle|bill) pay\b/g, ' ');
const rateQ = /\b(rates?|apr|apy|dividend|yield|interest|percent)\b|%|how much/.test(qlNoWallet)
  || /\bwhat (does|do|will|would|did)\b.*\b(pay|earn|cost|charge)s?\b/.test(qlNoWallet);
const walletQ = /apple pay|google pay|samsung pay|mobile wallet|digital wallet|tap to pay|contactless/.test(ql);
const cardListQ = /credit cards?|visa cards?|\bcards\b/.test(ql) && !/\b(personal|business)\b/.test(ql) && !walletQ;
const hoursQ = /\b(hours?|what time|are you open|open (on|today|tomorrow|late|saturdays?|sundays?|weekends?)|closed? (on|today|saturdays?|sundays?)|closing time|opening time|lobby|drive-?up|drive-?thr(u|ough))\b/.test(ql);
const linkQ = /\b(link|url|web ?page|website|where (can|do|should) i (see|find|look|check))\b/.test(ql);
const joinQ = /\b(eligib\w*|join|become a member|membership|qualify|field of membership|who can)\b/.test(ql);

// product family named in the question -> words that appear in that product's tables/pages
const isRec = /\brecreational\b|\brvs?\b|camper|motorhome|\bboats?\b|\batvs?\b|snowmobile|jet ?ski/.test(ql);
const FAMILIES = [
  [isRec ? /$^/ : /\b(car|auto|vehicle|truck|suv|motorcycle)s?\b/, ['auto', 'motorcycle']],
  [/mortgage|home ?loan|30-year|15-year|20-year/, ['home loan', 'mortgage', 'fixed conventional']],
  [/\bcds?\b|certificate/, ['certificate']],
  [/heloc|home equity/, ['home equity', 'heloc']],
  [/credit cards?|\bvisa\b/, ['visa']],
  [/personal loan/, ['personal loan']],
  [/student/, ['student']],
  [/\bboats?\b|\brvs?\b|recreational vehicle|camper|motorhome/, ['boat', 'rv', 'recreational']],
  [/\batv|snowmobile|jet ski/, ['atv', 'snowmobile']],
  [/checking/, ['checking']],
  [/money market/, ['money market']],
  [/savings/, ['savings']],
  [/\bira\b|retirement/, ['ira']],
];
const family = [];
for (const [re, words] of FAMILIES) if (re.test(ql)) family.push(...words);

// ---- 4. text to embed: the question plus the words rate tables actually use
const extra = [];
if (!isRec && /\b(car|auto|vehicle|truck|suv)\b/.test(ql)) extra.push('auto loan');
if (isRec) extra.push('recreational loan');
if (/\bcds?\b|certificate/.test(ql)) extra.push('certificate');
if (/mortgage|home ?loan/.test(ql)) extra.push('home loan fixed conventional');
if (/credit cards?|visa/.test(ql)) extra.push('Visa credit card');
for (const t of terms) extra.push(t + (t.includes('Month') ? 's' : ''));
for (const y of years) if (/car|auto|vehicle|model/.test(ql)) extra.push(`${y} Model Year`);
if (rateQ) extra.push(/apy|dividend|savings|certificate|cd|checking|money market|ira/.test(ql) ? 'rate APY table' : 'rate APR table');
if (/savings/.test(ql) && !/health|hsa|money market|\bira\b|club|bright|youth|business|you name it/.test(ql)) extra.push('Share Savings dividend rate APY tiers');
if (walletQ) extra.push('mobile wallet: add your Dupaco debit and credit cards to Apple Pay, Google Pay or Samsung Pay');
if (cardListQ) extra.push('Platinum Visa Rewards Visa Business Platinum Visa Business Rewards Visa APR');
if (hoursQ) extra.push('Lobby hours Monday-Thursday a.m. p.m. Friday Saturday Sunday Closed Drive-up');
if (joinQ) extra.push('live or work in charter area. Dupaco serves residents in counties throughout Iowa, northwest Illinois and southwest Wisconsin');
if (linkQ && rateQ) extra.push('See rates #rates page');
const searchText = [q, ...new Set(extra)].join(' ');

// ---- 5. Pinecone filters: A = official pages only, B = everything except calculators
const snapFilter = { [`snap_${snap.n}`]: { $eq: 'true' } };
const filterA = { $and: [snapFilter, { page_type: { $in: ['rates', 'product', 'promo'] } }] };
const filterB = { $and: [snapFilter, { page_type: { $nin: ['calculator'] } }] };

return [{ json: {
  question: q, as_of: asOf, as_of_label: asOfLabel, snap: snap.n, snap_date: snap.date,
  search_text: searchText, filterA, filterB,
  terms, years, amounts, quals, rateQ, hoursQ, linkQ, joinQ, family,
} }];
