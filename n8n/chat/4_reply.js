// ===== BankGPT Chat – Code node "Reply" (Run Once for All Items) =====
// Formats AnswerFlow's result for the chat window and remembers this turn for follow-ups.
const C = $('Context').first().json;
let question = C.question;
try { question = $('Use rewrite').first().json.question || question; } catch (e) { /* no rewrite this turn */ }
const r = $input.first().json;
const answer = String(r.answer || "I don't know. Please call one of our branches.").trim();
let text = answer;
const src = (r.sources || []).slice(0, 3);
if (src.length) text += '\n\nSources:\n' + src.map(u => '- ' + u).join('\n');

const store = $getWorkflowStaticData('global');
store.sessions = store.sessions || {};
const hist = store.sessions[C.sessionId] || [];
hist.push({ q: question, a: answer.slice(0, 600) });
store.sessions[C.sessionId] = hist.slice(-6);
const ids = Object.keys(store.sessions);                 // keep at most 300 conversations
for (const id of ids.slice(0, Math.max(0, ids.length - 300))) delete store.sessions[id];

return [{ json: { output: text } }];
