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

def extract_cost(obj):
    """Deep search for cost values in nested dictionaries."""
    cost = 0.0
    if isinstance(obj, dict):
        # Direct 'total' key
        if "total" in obj and isinstance(obj["total"], (int, float)):
            return float(obj["total"])
        # Check for 'cost' dictionary or value
        if "cost" in obj:
            c = obj["cost"]
            if isinstance(c, dict):
                return extract_cost(c)
            elif isinstance(c, (int, float)):
                return float(c)
        # Recurse into usage or message
        for k in ["usage", "message"]:
            if k in obj:
                cost += extract_cost(obj[k])
    return cost

def extract_usage(obj):
    """Extract input/output tokens."""
    usage = {"in": 0, "out": 0}
    if isinstance(obj, dict):
        if "usage" in obj:
            u = obj["usage"]
            if isinstance(u, dict):
                usage["in"] += u.get("input", 0)
                usage["out"] += u.get("output", 0)
        if "message" in obj:
            res = extract_usage(obj["message"])
            usage["in"] += res["in"]
            usage["out"] += res["out"]
    return usage

# --- OpenClaw Session Monitor ---
async def get_dashboard_data():
    active_map: Dict[str, dict] = {}
    total_cost = 0.0
    total_tokens_in = 0
    total_tokens_out = 0
    
    if not os.path.exists(SESSIONS_DIR):
        return {"active": [], "totals": {"cost": 0, "in": 0, "out": 0}}
    
    now = datetime.now().timestamp()
    files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith(".jsonl") and not f.endswith(".lock")]
    
    for filename in files:
        filepath = os.path.join(SESSIONS_DIR, filename)
        session_id = filename.split('.')[0]
        try:
            mtime = os.path.getmtime(filepath)
            if (now - mtime) > 86400: # 24h
                continue

            with open(filepath, 'r') as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        total_cost += extract_cost(data)
                        usage = extract_usage(data)
                        total_tokens_in += usage["in"]
                        total_tokens_out += usage["out"]
                    except:
                        continue

            if (now - mtime) < 120:
                with open(filepath, 'rb') as f:
                    try:
                        f.seek(-2, os.SEEK_END)
                        while f.read(1) != b'\n':
                            f.seek(-2, os.SEEK_CUR)
                    except:
                        f.seek(0)
                    last_line = f.readline().decode()
                    data = json.loads(last_line)
                    
                    last_msg = "Working..."
                    if "message" in data:
                        m = data["message"]
                        if "content" in m and isinstance(m["content"], list):
                            for c in m["content"]:
                                if c.get("type") == "text": last_msg = c.get("text", "")
                        elif "text" in m: last_msg = m["text"]

                    if not any(kw in last_msg.lower() for kw in ["dashboard on port 8050 is down", "reactivado con éxito"]):
                        alias = get_alias(session_id)
                        if alias not in active_map:
                            active_map[alias] = {"name": alias, "id": session_id[:8], "last": last_msg[:80], "count": 1, "mtime": mtime}
                        else:
                            active_map[alias]["count"] += 1
        except:
            continue
            
    active = list(active_map.values())
    active.sort(key=lambda x: (x['name'] != 'Alfred', x['mtime']))
    
    return {
        "active": active,
        "totals": {
            "cost": round(total_cost, 4),
            "in": total_tokens_in,
            "out": total_tokens_out
        }
    }

# --- FastAPI App ---
app = FastAPI(title="OpenClaw Insight Dashboard v1.4.6")

@app.get("/api/status")
async def get_status():
    data = await get_dashboard_data()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM history ORDER BY timestamp DESC LIMIT 10")
    history = [{"id": r[0], "time": r[1].split('T')[1].split('.')[0] if 'T' in r[1] else r[1], "agent": r[3], "task": r[4]} for r in c.fetchall()]
    conn.close()
    return {**data, "history": history}

@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8"><title>OpenClaw Insight v1.4.6</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style> @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } } .pulse { animation: pulse 2s infinite; } .alfred { border-color: rgba(59,130,246,0.5); box-shadow: 0 0 10px rgba(59,130,246,0.2); } </style>
    </head>
    <body class="bg-[#0b1120] text-slate-300 font-sans min-h-screen">
        <nav class="border-b border-slate-800 bg-[#111827]/90 p-4 sticky top-0 z-50">
            <div class="max-w-5xl mx-auto flex justify-between items-center">
                <div class="flex items-center gap-2 text-white font-bold"><span class="text-xl">🎩</span> Insight <span class="text-xs font-normal text-slate-500">v1.4.6</span></div>
                <div class="text-[10px] text-emerald-500 font-bold bg-emerald-500/10 px-2 py-1 rounded-full"><span class="w-1.5 h-1.5 bg-emerald-500 rounded-full inline-block mr-1 pulse"></span> LIVE</div>
            </div>
        </nav>
        <main class="max-w-5xl mx-auto p-6 space-y-6">
            <section class="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div class="bg-blue-500/5 border border-blue-500/20 p-4 rounded-xl text-center">
                    <div class="text-[10px] text-blue-400 font-bold uppercase mb-1">API Cost (24h)</div>
                    <div class="text-3xl font-bold text-white">$<span id="cost">0.0000</span></div>
                </div>
                <div class="bg-slate-800/40 p-4 rounded-xl text-center">
                    <div class="text-[10px] text-slate-500 font-bold uppercase mb-1">Tokens In</div>
                    <div id="tin" class="text-xl font-bold text-white">0</div>
                </div>
                <div class="bg-slate-800/40 p-4 rounded-xl text-center">
                    <div class="text-[10px] text-slate-500 font-bold uppercase mb-1">Tokens Out</div>
                    <div id="tout" class="text-xl font-bold text-white">0</div>
                </div>
            </section>
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                <div class="lg:col-span-2 space-y-4">
                    <h2 class="text-xs font-bold text-slate-500 uppercase">Active Agents</h2>
                    <div id="active" class="grid gap-2"></div>
                </div>
                <div class="space-y-4">
                    <h2 class="text-xs font-bold text-slate-500 uppercase">History</h2>
                    <div id="history" class="space-y-2 border-l border-slate-800 pl-4"></div>
                </div>
            </div>
        </main>
        <script>
            async function upd() {
                try {
                    const r = await fetch('/api/status');
                    const d = await r.json();
                    document.getElementById('cost').innerText = d.totals.cost.toFixed(4);
                    document.getElementById('tin').innerText = d.totals.in.toLocaleString();
                    document.getElementById('tout').innerText = d.totals.out.toLocaleString();
                    document.getElementById('active').innerHTML = d.active.map(a => `
                        <div class="bg-slate-800/30 border border-slate-800 p-3 rounded-lg ${a.name==='Alfred'?'alfred':''}">
                            <div class="flex justify-between text-xs mb-1">
                                <span class="font-bold ${a.name==='Alfred'?'text-blue-400':'text-white'}">${a.name} ${a.count>1?'('+a.count+')':''}</span>
                                <span class="text-slate-600 font-mono">${a.id}</span>
                            </div>
                            <div class="text-[11px] text-slate-500 italic truncate">$ ${a.last}</div>
                        </div>
                    `).join('') || '<div class="text-slate-700 text-xs py-4 text-center">Quiet...</div>';
                    document.getElementById('history').innerHTML = d.history.map(h => `
                        <div class="text-[10px]"><span class="text-slate-600">${h.time}</span> <span class="text-slate-400 font-bold">${h.agent}</span></div>
                    `).join('');
                } catch(e){}
            }
            setInterval(upd, 2000); upd();
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8050)
