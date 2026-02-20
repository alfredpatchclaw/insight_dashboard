import os
import json
import sqlite3
import random
import asyncio
from datetime import datetime
from typing import List, Optional, Dict
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# --- Configuration ---
SESSIONS_DIR = "/home/claw/.openclaw/agents/main/sessions"
DB_PATH = "/home/claw/.openclaw/workspace/tools_dev/insight_dashboard/history.db"
BUTLER_NAMES = ["Alfred", "Jarvis", "Sebastian", "Nestor", "Hudson", "Cadbury", "Geoffrey", "Woodhouse", "Agdor", "Lurch"]

# --- Global Cache (v1.8.0) ---
dashboard_cache = {
    "active": [],
    "totals": {"cost": 0.0, "in": 0, "out": 0},
    "history": [],
    "last_update": None
}

# --- Database Setup ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  timestamp TEXT, 
                  agent_id TEXT, 
                  agent_name TEXT, 
                  task TEXT,
                  duration_ms INTEGER,
                  cost REAL,
                  status TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS agent_aliases
                 (session_id TEXT PRIMARY KEY, 
                  alias TEXT)''')
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
    cost = 0.0
    if isinstance(obj, dict):
        if "total" in obj and isinstance(obj["total"], (int, float)):
            return float(obj["total"])
        if "cost" in obj:
            c = obj["cost"]
            if isinstance(c, dict): return extract_cost(c)
            elif isinstance(c, (int, float)): return float(c)
        for k in ["usage", "message"]:
            if k in obj: cost += extract_cost(obj[k])
    return cost

def extract_usage(obj):
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

# --- Background Task ---
async def update_cache_loop():
    known_sessions = set()
    while True:
        try:
            active_map: Dict[str, dict] = {}
            total_cost = 0.0
            total_tokens_in = 0
            total_tokens_out = 0
            now = datetime.now().timestamp()
            
            if os.path.exists(SESSIONS_DIR):
                files = [f for f in os.listdir(SESSIONS_DIR) if f.endswith(".jsonl")]
                for filename in files:
                    filepath = os.path.join(SESSIONS_DIR, filename)
                    session_id = filename.split('.')[0]
                    try:
                        mtime = os.path.getmtime(filepath)
                        ctime = os.path.getctime(filepath)
                        
                        sess_cost = 0.0
                        last_msg = "Working..."
                        
                        with open(filepath, 'r') as f:
                            for line in f:
                                try:
                                    data = json.loads(line.strip())
                                    c = extract_cost(data)
                                    sess_cost += c
                                    total_cost += c
                                    usage = extract_usage(data)
                                    total_tokens_in += usage["in"]
                                    total_tokens_out += usage["out"]
                                except: continue

                        # Session is "active" if modified in the last 2 minutes
                        if (now - mtime) < 120:
                            alias = get_alias(session_id)
                            active_map[alias] = {
                                "name": alias, "id": session_id[:8], 
                                "mtime": mtime, "cost": sess_cost
                            }
                        
                        # Save to history if session is finished (older than 10 mins and not in DB)
                        elif (now - mtime) > 600 and session_id not in known_sessions:
                            duration = int((mtime - ctime) * 1000)
                            alias = get_alias(session_id)
                            
                            # Extract a one-sentence summary of the task
                            task_summary = "Task completed"
                            try:
                                with open(filepath, 'r') as f:
                                    first_line = f.readline()
                                    if first_line:
                                        first_data = json.loads(first_line.strip())
                                        if first_data.get("type") == "message":
                                            content = first_data["message"].get("content", "")
                                            if isinstance(content, list):
                                                text = next((c.get("text", "") for c in content if c.get("type") == "text"), "")
                                            else:
                                                text = str(content)
                                            # Truncate to one sentence or 80 chars
                                            task_summary = text.split('.')[0].split('\n')[0][:80].strip()
                            except: pass

                            conn = sqlite3.connect(DB_PATH)
                            c_db = conn.cursor()
                            c_db.execute("INSERT INTO history (timestamp, agent_id, agent_name, task, duration_ms, cost, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                      (datetime.fromtimestamp(mtime).isoformat(), session_id, alias, task_summary, duration, sess_cost, "✅"))
                            conn.commit()
                            conn.close()
                            known_sessions.add(session_id)

                    except: continue

            # Update Global Cache
            dashboard_cache["active"] = list(active_map.values())
            dashboard_cache["totals"] = {"cost": round(total_cost, 4), "in": total_tokens_in, "out": total_tokens_out}
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT timestamp, agent_name, task, duration_ms, cost, status FROM history ORDER BY timestamp DESC LIMIT 10")
            dashboard_cache["history"] = [{"time": r[0].split('T')[1][:5], "agent": r[1], "task": r[2], "duration": r[3], "cost": r[4], "status": r[5]} for r in c.fetchall()]
            conn.close()
            
            dashboard_cache["last_update"] = datetime.now().isoformat()
        except Exception as e: print(f"Error: {e}")
        await asyncio.sleep(10)

# --- FastAPI App ---
app = FastAPI(title="OpenClaw Insight Dashboard v1.8.0")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(update_cache_loop())

@app.get("/api/status")
async def get_status(): return dashboard_cache

@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8"><title>OpenClaw Insight v1.8.0</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-[#0b1120] text-slate-300 font-sans min-h-screen">
        <nav class="border-b border-slate-800 bg-[#111827]/90 p-4 sticky top-0 z-50">
            <div class="max-w-5xl mx-auto flex justify-between items-center">
                <div class="flex items-center gap-2 text-white font-bold">🎩 Insight v1.8.0</div>
                <div id="update-ts" class="text-[10px] text-slate-500"></div>
            </div>
        </nav>
        <main class="max-w-5xl mx-auto p-6 space-y-6">
            <section class="grid grid-cols-3 gap-4">
                <div class="bg-slate-800/40 p-4 rounded-xl text-center">
                    <div class="text-[10px] text-blue-400 font-bold uppercase">Cost (24h)</div>
                    <div class="text-2xl font-bold text-white">$<span id="cost">0.00</span></div>
                </div>
                <div class="bg-slate-800/40 p-4 rounded-xl text-center">
                    <div class="text-[10px] text-slate-500 font-bold uppercase">Tokens In</div>
                    <div id="tin" class="text-xl font-bold">0</div>
                </div>
                <div class="bg-slate-800/40 p-4 rounded-xl text-center">
                    <div class="text-[10px] text-slate-500 font-bold uppercase">Tokens Out</div>
                    <div id="tout" class="text-xl font-bold">0</div>
                </div>
            </section>
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                <div class="lg:col-span-2 space-y-4">
                    <h2 class="text-xs font-bold text-slate-500 uppercase">Active Agents</h2>
                    <div id="active" class="grid gap-2"></div>
                </div>
                <div class="space-y-4">
                    <h2 class="text-xs font-bold text-slate-500 uppercase">High-Level History</h2>
                    <div id="history" class="space-y-3 pl-4 border-l border-slate-800"></div>
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
                        <div class="bg-slate-800/30 border border-slate-800 p-3 rounded-lg">
                            <div class="flex justify-between text-xs">
                                <span class="font-bold text-white">${a.name}</span>
                                <span class="text-emerald-500">$${a.cost.toFixed(4)}</span>
                            </div>
                        </div>
                    `).join('') || '<div class="text-slate-700 text-xs py-4 text-center">No active tasks</div>';
                    document.getElementById('history').innerHTML = d.history.map(h => `
                        <div class="text-[11px] border-b border-slate-800/50 pb-2">
                            <div class="flex justify-between font-bold text-slate-400">
                                <span>${h.status} ${h.agent}</span>
                                <span class="text-emerald-500">$${h.cost.toFixed(3)}</span>
                            </div>
                            <div class="text-slate-500">${h.task}</div>
                            <div class="flex justify-between text-[10px] text-slate-600">
                                <span>${h.time}</span>
                                <span>${Math.round(h.duration/60000)} min</span>
                            </div>
                        </div>
                    `).join('');
                } catch(e){}
            }
            setInterval(upd, 5000); upd();
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8050)
