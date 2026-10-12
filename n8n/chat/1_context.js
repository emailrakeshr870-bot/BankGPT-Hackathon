// ===== BankGPT Chat – Code node "Context" (Run Once for All Items) =====
// Reads the chat message and this conversation's last few turns (kept in workflow static data,
// which n8n saves for runs from the published chat page, not for manual test runs).
const t = $input.first().json;
const sessionId = String(t.sessionId || 'default');
const message = String(t.chatInput || '').trim();
const store = $getWorkflowStaticData('global');
store.sessions = store.sessions || {};
const hist = (store.sessions[sessionId] || []).slice(-3);
const history = hist.map(h => `Member: ${h.q}\nAssistant: ${h.a}`).join('\n\n');
return [{ json: { sessionId, message, question: message, hasHistory: hist.length > 0, history } }];
