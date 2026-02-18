import os
import json
import sqlite3
import random
from datetime import datetime
from typing import List, Optional, Dict
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# --- Configuration ---
SESSIONS_DIR = "/home/claw/.openclaw/agents/main/sessions"
DB_PATH = "/home/claw/.openclaw/workspace/tools_dev/insight_dashboard/history.db"

BUTLER_NAMES = ["Alfred", "Jarvis", "Sebastian", "Nestor", "Hudson", "Cadbury", "Geoffrey", "Woodhouse", "Agdor", "Lurch"]

# --- Database Setup ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  timestamp TEXT, 
                  agent_id TEXT, 
                  agent_name TEXT, 
                  task TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS agent_aliases
                 (session_id TEXT PRIMARY KEY, 
                  alias TEXT)''')
    c.execute("INSERT OR IGNORE INTO agent_aliases (session_id, alias) VALUES (?, ?)", 
              ("b6a17b63-f710-444a-9091-74c2ea6238d6", "Alfred"))
    conn.commit()
    conn.close()

init_db()

def get_alias(session_id: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT alias FROM agent_aliases WHERE session_id = ?", (session_id,))
    row = c.fetchone()
    if row:
        alias = row[0]
    else:
        alias = random.choice(BUTLER_NAMES)
        c.execute("INSERT INTO agent_aliases (session_id, alias) VALUES (?, ?)", (session_id, alias))
        conn.commit()
    conn.close()
    return alias

# --- OpenClaw Session Monitor ---
async def get_active_sessions():
    active_map: Dict[str, dict] = {}
    if not os.path.exists(SESSIONS_DIR):
        return []
    
    now = datetime.now().timestamp()
    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith(".jsonl") and not f.endswith(".lock")]
    
    for filename in files:
        filepath = os.path.join(SESSIONS_DIR, filename)
        session_id = filename.split('.')[0]
        try:
            mtime = os.path.getmtime(filepath)
            # v1.3: Narrow window (120s) and filter noise
            diff = now - mtime
            if diff > 120:
                continue
            
            with open(filepath, 'rb') as f:
                try:
                    f.seek(-2, os.SEEK_END)
                    while f.read(1) != b'\n':
                        f.seek(-2, os.SEEK_CUR)
                except OSError:
                    f.seek(0)
                last_line = f.readline().decode()
                data = json.loads(last_line)
                
                last_msg = ""
                if "message" in data:
                    msg = data["message"]
                    if "content" in msg and isinstance(msg["content"], list):
                        for c in msg["content"]:
                            if c.get("type") == "text":
                                last_msg += c.get("text", "")
                    elif "text" in msg:
                        last_msg = msg["text"]
                
                # Filter out Keeper/Maintenance noise
                noise_keywords = ["dashboard on port 8050 is down", "reactivado con éxito", "restaurado exitosamente"]
                if any(kw in last_msg.lower() for kw in noise_keywords):
                    continue

                alias = get_alias(session_id)
                
                if alias in active_map:
                    active_map[alias]["count"] += 1
                    if mtime > active_map[alias]["mtime"]:
                        active_map[alias]["last_message"] = last_msg[:80] + "..."
                        active_map[alias]["mtime"] = mtime
                else:
                    active_map[alias] = {
                        "name": alias,
                        "id_short": session_id[:8],
                        "last_message": last_msg[:80] + "...",
                        "count": 1,
                        "mtime": mtime,
                        "last_seen": int(diff)
                    }
        except:
            continue
            
    # Convert to sorted list (Alfred first)
    result = list(active_map.values())
    result.sort(key=lambda x: (x['name'] != 'Alfred', x['mtime']), reverse=False)
    return result

# --- FastAPI App ---
app = FastAPI(title="OpenClaw Insight Dashboard")

@app.get("/api/status")
async def get_status():
    active = await get_active_sessions()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM history ORDER BY timestamp DESC LIMIT 15")
    history = [{"id": r[0], "time": r[1].split('T')[1].split('.')[0], "agent": r[3], "task": r[4]} for r in c.fetchall()]
    conn.close()
    return {"active": active, "history": history}

@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>OpenClaw Insight v1.3</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            @keyframes pulse-soft { 0%, 100% { opacity: 1; } 50% { opacity: 0.7; } }
            .pulse { animation: pulse-soft 2s infinite; }
            .alfred-glow { box-shadow: 0 0 15px rgba(59, 130, 246, 0.3); border-color: rgba(59, 130, 246, 0.5); }
        </style>
    </head>
    <body class="bg-[#0b1120] text-slate-300 font-sans min-h-screen">
        <nav class="border-b border-slate-800 bg-[#111827]/80 backdrop-blur-md sticky top-0 z-50">
            <div class="max-w-6xl mx-auto px-6 py-4 flex justify-between items-center">
                <div class="flex items-center gap-3">
                    <span class="text-2xl">🎩</span>
                    <h1 class="text-lg font-bold tracking-tight text-white">OpenClaw <span class="text-blue-500">Insight</span> <span class="text-[10px] bg-slate-800 px-1.5 py-0.5 rounded text-slate-400 ml-1">v1.3</span></h1>
                </div>
                <div id="connection-status" class="flex items-center gap-2 text-[10px] font-bold text-emerald-500 bg-emerald-500/10 px-2 py-1 rounded-full border border-emerald-500/20">
                    <span class="w-1.5 h-1.5 bg-emerald-500 rounded-full pulse"></span> MONITORING LIVE
                </div>
            </div>
        </nav>

        <main class="max-w-6xl mx-auto px-6 py-8 grid grid-cols-1 lg:grid-cols-3 gap-8">
            <div class="lg:col-span-2 space-y-8">
                <section>
                    <div class="flex items-center justify-between mb-4">
                        <h2 class="text-sm font-bold uppercase tracking-widest text-slate-500">Agentes Activos</h2>
                        <span id="active-count" class="text-xs font-mono text-blue-400 bg-blue-400/10 px-2 py-0.5 rounded">0</span>
                    </div>
                    <div id="active-list" class="grid gap-3"></div>
                </section>
            </div>

            <aside class="space-y-8">
                <section>
                    <h2 class="text-sm font-bold uppercase tracking-widest text-slate-500 mb-4">Historial Reciente</h2>
                    <div id="history-list" class="space-y-2 border-l border-slate-800 ml-2"></div>
                </section>
            </aside>
        </main>

        <script>
            async function update() {
                try {
                    const res = await fetch('/api/status');
                    const data = await res.json();
                    
                    const activeList = document.getElementById('active-list');
                    document.getElementById('active-count').innerText = data.active.length;
                    
                    if(data.active.length === 0) {
                        activeList.innerHTML = '<div class="py-12 border border-dashed border-slate-800 rounded-2xl text-center text-slate-600 text-sm">Silencio en el sistema...</div>';
                    } else {
                        activeList.innerHTML = data.active.map(a => `
                            <div class="bg-[#1e293b]/40 border border-slate-800 p-4 rounded-xl transition-all ${a.name === 'Alfred' ? 'alfred-glow bg-blue-500/5' : ''}">
                                <div class="flex justify-between items-center mb-1">
                                    <div class="flex items-center gap-2">
                                        <span class="font-bold ${a.name === 'Alfred' ? 'text-blue-400' : 'text-slate-200'}">${a.name}</span>
                                        ${a.count > 1 ? `<span class="text-[9px] bg-slate-800 text-slate-400 px-1.5 py-0.5 rounded-full border border-slate-700">${a.count} tareas</span>` : ''}
                                    </div>
                                    <span class="text-[9px] font-mono text-slate-500 italic">${a.last_seen}s ago</span>
                                </div>
                                <p class="text-xs text-slate-500 truncate font-mono bg-black/20 p-2 rounded mt-2 border border-white/5">
                                    <span class="text-blue-500/50 mr-1">$</span>${a.last_message}
                                </p>
                            </div>
                        `).join('');
                    }

                    const historyList = document.getElementById('history-list');
                    historyList.innerHTML = data.history.length ? data.history.map(h => `
                        <div class="pl-4 pb-4 relative">
                            <div class="absolute w-2 h-2 bg-slate-800 rounded-full -left-[4.5px] top-1.5 border border-[#0b1120]"></div>
                            <div class="text-[9px] text-slate-600 font-mono mb-0.5">${h.time}</div>
                            <div class="text-xs font-semibold text-slate-400">${h.agent}</div>
                            <div class="text-[10px] text-slate-600 truncate">${h.task}</div>
                        </div>
                    `).join('') : '<div class="pl-4 text-slate-700 text-[10px] italic">Esperando eventos...</div>';

                } catch (e) { console.error(e); }
            }
            setInterval(update, 2000);
            update();
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8050)
