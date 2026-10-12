"""
Chat with the knowledge system in your browser.

    python chat.py

It loads the five snapshots (about a minute the first time), then opens http://localhost:8765
where you can type questions. Nothing leaves your computer.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
import time
import webbrowser
import zipfile
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "harness"))
from contract import Page, Snapshot  # noqa: E402

PORT = 8765


def load_snapshots() -> list[Snapshot]:
    snap_dir, zip_dir = ROOT / "snapshots", ROOT / "snapshots_zip"
    snap_dir.mkdir(exist_ok=True)
    for z in sorted(zip_dir.glob("*.zip")):
        if not (snap_dir / z.stem / "manifest.json").exists():
            print(f"  unpacking {z.name} ...", flush=True)
            with zipfile.ZipFile(z) as zf:
                zf.extractall(snap_dir)
    snaps = []
    for d in sorted(p for p in snap_dir.iterdir() if (p / "manifest.json").exists()):
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        pages = [Page(url=p["url"], title=p.get("title", ""),
                      markdown=(d / p["file"]).read_text(encoding="utf-8"),
                      fetched_at=p.get("fetched_at", m["taken_on"])) for p in m["pages"]]
        snaps.append(Snapshot(m["snapshot_id"], m["taken_on"], pages))
    return sorted(snaps, key=lambda s: s.taken_on)


def load_system():
    spec = importlib.util.spec_from_file_location("solution", ROOT / "solution" / "solution.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ks = mod.build()
    t = time.time()
    for s in load_snapshots():
        print(f"  reading {s.snapshot_id} ({len(s.pages)} pages)", flush=True)
        ks.ingest(s)
    print(f"  ready in {time.time() - t:.0f}s", flush=True)
    return ks


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Knowledge system test chat</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1c1b19;--mute:#6b6862;--line:#e3e0da;--acc:#0b5d4b;--warn:#a14b00}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#1f1f1c;--ink:#ecebe7;--mute:#a19e97;--line:#34332f;--acc:#5cc4a8;--warn:#f0a35c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:16px;border-bottom:1px solid var(--line);background:var(--card)}h1{margin:0;font-size:17px}
header p{margin:4px 0 0;color:var(--mute);font-size:13px}
main{max-width:820px;margin:0 auto;padding:16px 16px 140px}
.q{margin:18px 0 6px;font-weight:600}.q small{font-weight:400;color:var(--mute)}
.a{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;white-space:pre-line}
.meta,.note,.interp{white-space:normal}.interp{font-size:12.5px;color:var(--mute);margin:0 0 6px}
.a.idk{border-color:var(--warn)}.meta{margin-top:8px;font-size:12.5px;color:var(--mute)}
.meta a{color:var(--acc);word-break:break-all}.note{margin-top:6px;font-size:12.5px;color:var(--warn)}
form{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--line);padding:12px 16px}
.row{max-width:820px;margin:0 auto;display:flex;gap:8px;flex-wrap:wrap}
input{font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
#q{flex:1 1 360px}#d{flex:0 0 160px}button{font:inherit;padding:10px 18px;border:0;border-radius:8px;background:var(--acc);color:#fff;cursor:pointer}
.ex{color:var(--mute);font-size:13px;max-width:820px;margin:6px auto 0}.ex span{cursor:pointer;text-decoration:underline dotted;margin-right:10px}
</style></head><body>
<header><h1>Knowledge system &middot; test chat</h1>
<p>Answers come from the five website snapshots only. Leave the date empty for "today" (newest snapshot), or pick a date for an "as of" answer.</p></header>
<main id="log"></main>
<form id="f"><div class="row"><input id="q" placeholder="Ask a member question..." autocomplete="off" required>
<input id="d" type="date" title="Optional: answer as of this date"><button>Ask</button></div>
<div class="ex">Try: <span>What's the 60-month auto loan rate for a 2025 car?</span><span>What are the drive-up hours?</span><span>What's your 7/1 ARM rate?</span><span>How many branches does Dupaco have?</span></div></form>
<script>
const log=document.getElementById('log'),f=document.getElementById('f'),q=document.getElementById('q'),d=document.getElementById('d');
document.querySelectorAll('.ex span').forEach(s=>s.onclick=()=>{q.value=s.textContent;q.focus()});
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
f.onsubmit=async e=>{e.preventDefault();const question=q.value.trim(),as_of=d.value||null;if(!question)return;
 const qd=document.createElement('div');qd.className='q';qd.innerHTML=esc(question)+(as_of?' <small>(as of '+as_of+')</small>':'');log.appendChild(qd);
 const ad=document.createElement('div');ad.className='a';ad.textContent='...';log.appendChild(ad);q.value='';window.scrollTo(0,document.body.scrollHeight);
 try{const r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question,as_of})});const a=await r.json();
  if(!a.confident)ad.classList.add('idk');
  let h=(a.interpreted?'<div class="interp">Follow-up understood as: '+esc(a.interpreted)+'</div>':'')+esc(a.text);
  const src=(a.sources||[]).map(s=>'<a href="'+esc(s.url)+'" target="_blank" rel="noopener">'+esc(s.url.replace('https://www.',''))+'</a>').join('<br>');
  h+='<div class="meta">'+(src?'Sources:<br>'+src:'No source cited')+' &middot; '+a.seconds+'s'+(a.confident?'':' &middot; marked "I don\'t know"')+'</div>';
  if(a.conflict_note)h+='<div class="note">Conflict note: '+esc(a.conflict_note)+'</div>';
  ad.innerHTML=h}catch(err){ad.textContent='Error: '+err}
 window.scrollTo(0,document.body.scrollHeight)};
</script></body></html>"""


def main():
    print("Loading the website snapshots (about a minute)...", flush=True)
    ks = load_system()
    lock = threading.Lock()
    history = {"last": None}

    def follow_up(q: str):
        """Chat-only convenience: merge a follow-up ('show me APY instead', 'I asked as of June 2026')
        with the previous question. The graded system itself answers each question on its own."""
        prev = history["last"]
        ql = q.lower().strip()
        if not prev:
            return q, None
        if re.match(r"^(and|also|what about|how about|i asked|i meant|i mean|show me|same|instead|as of|in |for |"
                    r"what if|ok|okay|no,|now)\b", ql) or re.search(r"\binstead\b|\b(that|those|them|it)\b", ql) \
                or len(re.findall(r"[a-z]+", ql)) <= 3:
            merged = re.sub(r"^(i asked|i meant|i mean|ok|okay|no,?|now|and|also)\s*", "", q, flags=re.I)
            return f"{prev} {merged}".strip(), f"{prev} + {q}"
        return q, None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/ask":
                self.send_error(404)
                return
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            t = time.time()
            raw = str(data.get("question", ""))
            q, interp = follow_up(raw)
            with lock:
                ans = ks.answer(q, data.get("as_of") or None)
                if interp and not ans.confident:      # merging didn't help: answer the message on its own
                    q, interp = raw, None
                    ans = ks.answer(q, data.get("as_of") or None)
            if not interp:
                history["last"] = raw
            out = asdict(ans) if hasattr(ans, "__dataclass_fields__") else dict(ans)
            out["interpreted"] = interp
            out["seconds"] = round(time.time() - t, 2)
            print(f"Q: {data.get('question')}  ->  {out['text'][:120]}", flush=True)
            body = json.dumps(out).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"\nOpen {url} in your browser. Press Ctrl+C here to stop.\n", flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
