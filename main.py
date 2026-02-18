import os
import json
import sqlite3
import asyncio
from datetime import datetime
from typing import List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

# --- Configuration ---
SESSIONS_DIR = "/home/claw/.openclaw/sessions"
DB_PATH = "/home/claw/.openclaw/workspace/tools_dev/insight_dashboard/history.db"

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
    conn.commit()
    conn.close()

init_db()

def log_to_history(agent_id, name, task):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO history (timestamp, agent_id, agent_name, task) VALUES (?, ?, ?, ?)",
              (datetime.now().isoformat(), agent_id, name, task))
    conn.commit()
    conn.close()

# --- OpenClaw Session Monitor ---
async def get_active_sessions():
    sessions = []
    if not os.path.exists(SESSIONS_DIR):
        return []
    
    for filename in os.listdir(SESSIONS_DIR):
        if filename.endswith(".json"):
            try:
                with open(os.path.join(SESSIONS_DIR, filename), 'r') as f:
                    data = json.load(f)
                    # Filter for sessions updated in the last 5 minutes as 'active'
                    mtime = os.path.getmtime(os.path.join(SESSIONS_DIR, filename))
                    is_active = (datetime.now().timestamp() - mtime) < 300
                    
                    if is_active:
                        sessions.append({
                            "id": data.get("key", filename),
                            "name": data.get("label", "Unknown Agent"),
                            "status": "active",
                            "last_message": data.get("history", [{}])[-1].get("text", "No messages yet")[:100] + "..."
                        })
            except:
                continue
    return sessions

# --- FastAPI App ---
app = FastAPI(title="OpenClaw Insight Dashboard")

@app.get("/api/status")
async def get_status():
    active = await get_active_sessions()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM history ORDER BY timestamp DESC LIMIT 10")
    history = [{"id": r[0], "time": r[1], "agent": r[3], "task": r[4]} for r in c.fetchall()]
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
        <title>OpenClaw Insight</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            @keyframes pulse-soft { 0%, 100% { opacity: 1; } 50% { opacity: 0.7; } }
            .pulse { animation: pulse-soft 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
        </style>
    </head>
    <body class="bg-[#0f172a] text-slate-200 font-sans min-h-screen">
        <nav class="border-b border-slate-800 bg-[#1e293b]/50 backdrop-blur-md sticky top-0 z-50">
            <div class="max-w-7xl mx-auto px-6 py-4 flex justify-between items-center">
                <div class="flex items-center gap-3">
                    <span class="text-3xl">🎩</span>
                    <div>
                        <h1 class="text-xl font-bold bg-gradient-to-r from-blue-400 to-emerald-400 bg-clip-text text-transparent">
                            OpenClaw Insight
                        </h1>
                        <p class="text-[10px] uppercase tracking-widest text-slate-500 font-bold">Architect Dashboard</p>
                    </div>
                </div>
                <div class="flex items-center gap-4">
                    <div id="connection-status" class="flex items-center gap-2 px-3 py-1 bg-emerald-500/10 text-emerald-400 rounded-full border border-emerald-500/20 text-xs font-medium">
                        <span class="w-2 h-2 bg-emerald-500 rounded-full pulse"></span>
                        LIVE
                    </div>
                </div>
            </div>
        </nav>

        <main class="max-w-7xl mx-auto px-6 py-8">
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">
                <!-- Active Column -->
                <div class="lg:col-span-2 space-y-6">
                    <section>
                        <div class="flex items-center justify-between mb-4">
                            <h2 class="text-lg font-semibold flex items-center gap-2">
                                <span class="text-blue-400">●</span> Agentes Activos
                            </h2>
                            <span id="active-count" class="text-xs bg-slate-800 px-2 py-0.5 rounded text-slate-400 font-mono">0</span>
                        </div>
                        <div id="active-list" class="grid gap-4">
                            <!-- JS populated -->
                        </div>
                    </section>

                    <section>
                        <h2 class="text-lg font-semibold mb-4 flex items-center gap-2">
                            <span class="text-amber-400">◔</span> Actividad en Tiempo Real
                        </h2>
                        <div class="bg-black/40 border border-slate-800 rounded-xl p-4 font-mono text-sm h-64 overflow-y-auto" id="log-stream">
                            <div class="text-slate-600 font-bold">[SYSTEM] Monitoring /sessions...</div>
                        </div>
                    </section>
                </div>

                <!-- History Column -->
                <div class="space-y-6">
                    <section>
                        <h2 class="text-lg font-semibold mb-4 flex items-center gap-2">
                            <span class="text-purple-400">◈</span> Historial
                        </h2>
                        <div id="history-list" class="space-y-3">
                            <!-- JS populated -->
                        </div>
                    </section>
                </div>
            </div>
        </main>

        <script>
            async function update() {
                try {
                    const res = await fetch('/api/status');
                    const data = await res.json();
                    
                    // Active List
                    const activeList = document.getElementById('active-list');
                    document.getElementById('active-count').innerText = data.active.length;
                    
                    if(data.active.length === 0) {
                        activeList.innerHTML = `
                            <div class="p-8 border-2 border-dashed border-slate-800 rounded-xl text-center text-slate-500">
                                No hay agentes trabajando activamente ahora mismo.
                            </div>
                        `;
                    } else {
                        activeList.innerHTML = data.active.map(a => `
                            <div class="bg-slate-800/50 border border-slate-700 p-4 rounded-xl hover:border-blue-500/50 transition-colors group">
                                <div class="flex justify-between items-start mb-2">
                                    <div class="font-bold text-blue-300 flex items-center gap-2">
                                        ${a.name}
                                        <span class="text-[10px] font-mono bg-blue-500/10 text-blue-400 px-1.5 py-0.5 rounded border border-blue-500/20 uppercase">${a.id.split(':')[0]}</span>
                                    </div>
                                    <span class="text-[10px] text-slate-500 font-mono">${new Date().toLocaleTimeString()}</span>
                                </div>
                                <p class="text-sm text-slate-400 line-clamp-2 italic">"${a.last_message}"</p>
                            </div>
                        `).join('');
                    }

                    // History List
                    const historyList = document.getElementById('history-list');
                    historyList.innerHTML = data.history.map(h => `
                        <div class="text-sm p-3 border-l-2 border-slate-700 bg-slate-800/20 hover:bg-slate-800/40 transition-colors">
                            <div class="text-slate-500 text-[10px] mb-1">${h.time.split('T')[1].split('.')[0]}</div>
                            <div class="font-medium text-slate-300">${h.agent}</div>
                            <div class="text-xs text-slate-500 truncate">${h.task}</div>
                        </div>
                    `).join('');

                } catch (e) {
                    console.error(e);
                }
            }

            setInterval(update, 2000);
            update();
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8050)
