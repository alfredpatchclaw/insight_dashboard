import os
import json
import sqlite3
import random
import asyncio
import ipaddress
from datetime import datetime
from typing import List, Optional, Dict
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
import uvicorn

# --- Configuration ---
SESSIONS_DIR = "/home/claw/.openclaw/agents/main/sessions"
DB_PATH = "/home/claw/.openclaw/workspace/tools_dev/insight_dashboard/history.db"
BUTLER_NAMES = ["Alfred", "Jarvis", "Sebastian", "Nestor", "Hudson", "Cadbury", "Geoffrey", "Woodhouse", "Agdor", "Lurch"]

# Pricing for Gemini 3 Flash Preview (Current Model) - Updated 2026-02-20
# Input: $0.50 per 1M tokens ($0.0000005 per token)
# Output: $3.00 per 1M tokens ($0.000003 per token)
PRICING_INPUT = 0.50 / 1000000
PRICING_OUTPUT = 3.00 / 1000000

# --- Global Cache (v1.9.2) ---
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

def calculate_real_cost(usage: dict) -> float:
    return (usage["in"] * PRICING_INPUT) + (usage["out"] * PRICING_OUTPUT)

# --- Background Task ---
async def update_cache_loop():
    while True:
        try:
            active_map: Dict[str, dict] = {}
            total_real_cost = 0.0
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
                        
                        sess_usage = {"in": 0, "out": 0}
                        with open(filepath, 'r') as f:
                            for line in f:
                                try:
                                    data = json.loads(line.strip())
                                    usage = extract_usage(data)
                                    sess_usage["in"] += usage["in"]
                                    sess_usage["out"] += usage["out"]
                                except: continue

                        sess_cost = calculate_real_cost(sess_usage)
                        total_real_cost += sess_cost
                        total_tokens_in += sess_usage["in"]
                        total_tokens_out += sess_usage["out"]

                        if (now - mtime) < 120:
                            alias = get_alias(session_id)
                            active_map[alias] = {
                                "name": alias, "id": session_id[:8], 
                                "mtime": mtime, "cost": sess_cost
                            }
                        elif (now - mtime) > 60:
                            conn = sqlite3.connect(DB_PATH)
                            c_db = conn.cursor()
                            c_db.execute("SELECT 1 FROM history WHERE agent_id = ?", (session_id,))
                            if not c_db.fetchone():
                                duration = int((mtime - ctime) * 1000)
                                alias = get_alias(session_id)
                                task_summary = "Task completed"
                                try:
                                    with open(filepath, 'r') as f:
                                        first_line = f.readline()
                                        if first_line:
                                            first_data = json.loads(first_line.strip())
                                            if first_data.get("type") == "message":
                                                content = first_data["message"].get("content", "")
                                                text = ""
                                                if isinstance(content, list):
                                                    text = next((c.get("text", "") for c in content if c.get("type") == "text"), "")
                                                else: text = str(content)
                                                task_summary = text.split('.')[0].split('\n')[0][:80].strip()
                                except: pass
                                c_db.execute("INSERT INTO history (timestamp, agent_id, agent_name, task, duration_ms, cost, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                          (datetime.fromtimestamp(mtime).isoformat(), session_id, alias, task_summary, duration, sess_cost, "✅"))
                                conn.commit()
                            conn.close()
                    except: continue

            dashboard_cache["active"] = list(active_map.values())
            dashboard_cache["totals"] = {"cost": round(total_real_cost, 6), "in": total_tokens_in, "out": total_tokens_out}
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT timestamp, agent_name, task, duration_ms, cost, status FROM history ORDER BY timestamp DESC LIMIT 10")
            rows = c.fetchall()
            dashboard_cache["history"] = []
            for r in rows:
                try:
                    time_str = r[0].split('T')[1][:5] if 'T' in r[0] else r[0][:5]
                    dashboard_cache["history"].append({"time": time_str, "agent": r[1], "task": r[2], "duration": r[3], "cost": r[4], "status": r[5]})
                except: continue
            conn.close()
            dashboard_cache["last_update"] = datetime.now().isoformat()
        except Exception as e: print(f"Error: {e}")
        await asyncio.sleep(10)

# --- FastAPI & Security ---
app = FastAPI(title="OpenClaw Insight Dashboard v1.9.2")

@app.middleware("http")
async def secure_local_network(request: Request, call_next):
    client_ip = request.client.host
    if client_ip == "127.0.0.1" or client_ip == "::1":
        return await call_next(request)
    try:
        ip = ipaddress.ip_address(client_ip)
        if not ip.is_private:
            raise HTTPException(status_code=403, detail="Access denied")
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid IP")
    return await call_next(request)

@app.on_event("startup")
async def startup_event(): asyncio.create_task(update_cache_loop())

@app.get("/api/status")
async def get_status(): return dashboard_cache

@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>OpenClaw Insight v1.9.2</title><script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-[#0b1120] text-slate-300 font-sans min-h-screen">
        <nav class="border-b border-slate-800 bg-[#111827]/90 p-4 sticky top-0 z-50">
            <div class="max-w-5xl mx-auto flex justify-between items-center px-4">
                <div class="flex items-center gap-2 text-white font-bold">🎩 Insight v1.9.2</div>
                <div id="update-ts" class="text-[10px] text-slate-500"></div>
            </div>
        </nav>
        <main class="max-w-5xl mx-auto p-4 sm:p-6 space-y-6">
            <section class="grid grid-cols-1 sm:grid-cols-3 gap-4">
                <div class="bg-slate-800/40 p-4 rounded-xl text-center">
                    <div class="text-[10px] text-blue-400 font-bold uppercase">Real Cost (Gemini 3 Flash Preview)</div>
                    <div class="text-2xl font-bold text-white">$<span id="cost">0.000000</span></div>
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
                    <h2 class="text-xs font-bold text-slate-500 uppercase px-1">Active Agents</h2>
                    <div id="active" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-1 gap-2"></div>
                </div>
                <div class="space-y-4">
                    <h2 class="text-xs font-bold text-slate-500 uppercase px-1">High-Level History</h2>
                    <div id="history" class="space-y-3 pl-0 sm:pl-4 border-l-0 sm:border-l border-slate-800"></div>
                </div>
            </div>
        </main>
        <script>
            async function upd() {
                try {
                    const r = await fetch('/api/status');
                    const d = await r.json();
                    document.getElementById('cost').innerText = d.totals.cost.toFixed(2);
                    document.getElementById('tin').innerText = d.totals.in.toLocaleString();
                    document.getElementById('tout').innerText = d.totals.out.toLocaleString();
                    document.getElementById('active').innerHTML = d.active.map(a => `
                        <div class="bg-slate-800/30 border border-slate-800 p-3 rounded-lg"><div class="flex justify-between text-xs"><span class="font-bold text-white">${a.name}</span><span class="text-emerald-500">$${a.cost.toFixed(6)}</span></div></div>
                    `).join('') || '<div class="text-slate-700 text-xs py-4 text-center">No active tasks</div>';
                    document.getElementById('history').innerHTML = d.history.map(h => `
                        <div class="text-[11px] border-b border-slate-800/50 pb-2 px-1">
                            <div class="flex justify-between font-bold text-slate-400"><span>${h.status} ${h.agent}</span><span class="text-emerald-500">$${h.cost.toFixed(4)}</span></div>
                            <div class="text-slate-500 leading-tight my-1">${h.task}</div>
                            <div class="flex justify-between text-[10px] text-slate-600"><span>${h.time}</span><span>${Math.round(h.duration/60000)} min</span></div>
                        </div>
                    `).join('');
                } catch(e){}
            }
            setInterval(upd, 5000); upd();
        </script>
    </body></html>
    """
if __name__ == "__main__": uvicorn.run(app, host="0.0.0.0", port=8050)
