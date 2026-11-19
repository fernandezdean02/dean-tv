import os
import json
from urllib.parse import urlparse, unquote
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
import httpx

# ── Create the FastAPI app object (this is the "app = FastAPI()" line) ──
app = FastAPI(title="Dean TV")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Load providers.json (same file you already have) ──
PROVIDERS_FILE = os.path.join(BASE_DIR, "providers.json")
try:
    with open(PROVIDERS_FILE, "r", encoding="utf-8") as f:
        PROVIDERS = json.load(f)
except Exception as e:
    print(f"[providers] failed to load: {e}", flush=True)
    PROVIDERS = {}

# ── Allowed hosts (copy your existing list) ──
ALLOWED_HOSTS = (
    "officialmoviebox.com", ".officialmoviebox.com",
    ".moviebox.co", ".moviebox.ng", ".moviebox.ph", ".movieboxapi",
    ".aoneroom.com", ".aoneroom", ".aonyx",
    ".bcdnxw.com", ".bcdnw.com", ".pbcdnw", ".macdn",
    "hakunaymatata.com", ".hakunaymatata.com",
    ".hdhub4u.cl", ".hdhub4u.ec", ".hdhub4u.kg", ".hdhub4u.ms",
    "hubcloud.foo", ".hubcloud.foo",
    "hubcloud.ist", ".hubcloud.ist",
    "hubcloud.cx", ".hubcloud.cx",
    "gadgetsweb.xyz", ".gadgetsweb.xyz",
    "hubcdn.fans", ".hubcdn.fans",
    "hubdrive.space", ".hubdrive.space",
    "hubdrive.pics", ".hubdrive.pics",
    "hubstream.xyz", ".hubstream.xyz",
    "pixeldrain.com", ".pixeldrain.com",
    "workers.dev", ".workers.dev",
    ".moviesdrive.beer", ".moviesdrive.net",
    "drive.google.com", "drive.usercontent.google.com",
    "docs.google.com", "gdrive.com",
)

def _is_allowed(host: str) -> bool:
    if not host:
        return False
    host = host.lower()
    for h in ALLOWED_HOSTS:
        if h.startswith("."):
            if host == h[1:] or host.endswith(h):
                return True
        else:
            if host == h or host.endswith("." + h):
                return True
    return False

# ── Routes ──

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/providers")
async def list_providers():
    return [
        {"id": pid, "name": info.get("name", pid), "url": info.get("url", "")}
        for pid, info in PROVIDERS.items()
    ]

@app.get("/", response_class=HTMLResponse)
async def index():
    path = os.path.join(BASE_DIR, "index.html")
    if not os.path.exists(path):
        raise HTTPException(404, "index.html not found")
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/{filename}.html", response_class=HTMLResponse)
async def serve_html(filename: str):
    safe = os.path.basename(filename) + ".html"
    path = os.path.join(BASE_DIR, safe)
    if not os.path.exists(path):
        raise HTTPException(404, f"{safe} not found")
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/proxy")
async def proxy(request: Request):
    url = request.query_params.get("url")
    if not url:
        raise HTTPException(400, "missing url")
    url = unquote(url)
    referer = unquote(request.query_params.get("referer", "https://officialmoviebox.com/"))
    direct = request.query_params.get("direct", "0") == "1"

    host = (urlparse(url).hostname or "").lower()
    if not _is_allowed(host):
        raise HTTPException(403, f"host not allowed: {host}")

    if direct:
        return RedirectResponse(url, status_code=302)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": referer,
        "Origin": "https://officialmoviebox.com",
        "Accept": "*/*",
    }
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng

    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120.0, connect=15.0), verify=False)
    try:
        req = client.build_request("GET", url, headers=headers)
        upstream = await client.send(req, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(502, f"upstream error: {e}")

    async def stream():
        try:
            async for chunk in upstream.aiter_bytes(262144):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    out_headers = {}
    for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
        if k in upstream.headers:
            out_headers[k] = upstream.headers[k]
    out_headers["Access-Control-Allow-Origin"] = "*"

    return StreamingResponse(
        stream(),
        status_code=upstream.status_code,
        headers=out_headers,
    )
