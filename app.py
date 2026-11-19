import os
import sys
import json
import re
import importlib
import time
from urllib.parse import urlparse, parse_qs, unquote, urljoin, quote

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, Response,
)

# ────────────────────────────────────────────────────────────────────────────
# App
# ────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Dean TV")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HTML = "index.html"
CHUNK_SIZE = 262144

# ────────────────────────────────────────────────────────────────────────────
# Allowed hosts
# ────────────────────────────────────────────────────────────────────────────
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

CONTENT_TYPE_OVERRIDES = {
    ".mp4": "video/mp4",
    ".m4s": "video/iso.segment",
    ".m4v": "video/mp4",
    ".ts": "video/mp2t",
    ".webm": "video/webm",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mpd": "application/dash+xml",
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
    ".mkv": "video/x-matroska",
}

M3U8_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
)


def _guess_content_type_from_path(path: str):
    p = urlparse(path).path.lower()
    for ext, ctype in CONTENT_TYPE_OVERRIDES.items():
        if p.endswith(ext):
            return ctype
    return None


def _is_allowed(host: str, provider_id: str | None = None) -> bool:
    if not host:
        return False
    host = host.lower()
    if provider_id:
        prov = PROVIDERS.get(provider_id)
        if not prov:
            return False
        for h in [prov.get("host", "")] + prov.get("cdn_hosts", []):
            if h and (host == h or host.endswith("." + h)):
                return True
        return False
    for h in ALLOWED_HOSTS:
        if h.startswith("."):
            if host == h[1:] or host.endswith(h):
                return True
        else:
            if host == h or host.endswith("." + h):
                return True
    for prov in PROVIDERS.values():
        for h in [prov.get("host", "")] + prov.get("cdn_hosts", []):
            if h and (host == h or host.endswith("." + h)):
                return True
    return False


# ────────────────────────────────────────────────────────────────────────────
# Load providers.json
# ────────────────────────────────────────────────────────────────────────────
PROVIDERS_FILE = os.path.join(BASE_DIR, "providers.json")

def _load_providers():
    if not os.path.exists(PROVIDERS_FILE):
        print(f"[providers] {PROVIDERS_FILE} not found — starting empty", flush=True)
        return {}
    try:
        with open(PROVIDERS_FILE, "r", encoding="utf-8") as f:
            src = json.load(f)
    except Exception as e:
        print(f"[providers] failed to load: {e}", flush=True)
        return {}
    out = {}
    for pid, info in src.items():
        url = (info.get("url") or "").strip()
        try:
            host = (urlparse(url).hostname or "").lower() if url else ""
        except Exception:
            host = ""
        out[pid] = {
            "id": pid,
            "name": info.get("name") or pid,
            "url": url,
            "host": host,
            "cdn_hosts": [h.lower() for h in (info.get("cdn_hosts") or []) if h],
        }
    print(f"[providers] loaded {len(out)} providers", flush=True)
    return out


PROVIDERS = _load_providers()


# ────────────────────────────────────────────────────────────────────────────
# Load provider plugins
# ────────────────────────────────────────────────────────────────────────────
PLUGIN_DIR = os.path.join(BASE_DIR, "providers")
_PLUGINS: dict = {}

def _load_plugins():
    if not os.path.isdir(PLUGIN_DIR):
        print(f"[plugins] {PLUGIN_DIR} not found", flush=True)
        return
    init_py = os.path.join(PLUGIN_DIR, "__init__.py")
    if not os.path.exists(init_py):
        with open(init_py, "w", encoding="utf-8") as f:
            f.write('"""Provider plugins."""\n')
    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    for entry in sorted(os.listdir(PLUGIN_DIR)):
        if not entry.endswith(".py") or entry.startswith("_"):
            continue
        modname = entry[:-3]
        try:
            mod = importlib.import_module(f"providers.{modname}")
            cls = getattr(mod, "Provider", None)
            if cls is not None and getattr(cls, "id", None):
                _PLUGINS[cls.id] = cls
                print(f"[plugins] loaded {modname} -> '{cls.id}'", flush=True)
        except Exception as e:
            print(f"[plugins] failed {modname}: {e}", flush=True)


_load_plugins()


# ────────────────────────────────────────────────────────────────────────────
# HTTP client + CDN headers
# ────────────────────────────────────────────────────────────────────────────
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _build_cdn_headers(referer: str, cookie: str | None = None, range_hdr: str | None = None):
    h = {
        "User-Agent": _BROWSER_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity;q=1, *;q=0",
        "Referer": referer,
        "Origin": "https://officialmoviebox.com",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Priority": "u=1, i",
        "Upgrade-Insecure-Requests": "1",
    }
    if cookie:
        h["Cookie"] = cookie
    if range_hdr:
        h["Range"] = range_hdr
    return h


def _is_good_media_response(r: httpx.Response) -> bool:
    if r is None:
        return False
    if r.status_code >= 400:
        return False
    ct = (r.headers.get("Content-Type") or "").lower()
    return (
        ct.startswith("video/")
        or ct.startswith("audio/")
        or ct.startswith("application/octet-stream")
        or ct.startswith("application/vnd.apple.mpegurl")
        or ct.startswith("application/x-mpegurl")
        or ct.startswith("application/dash+xml")
    )


async def _fetch_media_with_retry(target: str, referer: str, cookie: str | None, range_hdr: str | None):
    """
    Try multiple header strategies until we get real video content back.
    Returns (response, label) on success, (None, error_msg) on failure.
    Note: httpx can't impersonate browser TLS fingerprints like curl_cffi,
    so we rely on header/HTTP-version combos instead.
    """
    strategies = [
        ({}, "default"),
        ({"Range": "bytes=0-"} if not range_hdr else {}, "with-range"),
        ({"Accept-Encoding": "gzip, deflate, br"}, "browser-encoding"),
        ({"Origin": "https://www.google.com"}, "google-origin"),
        ({"Referer": "https://officialmoviebox.com/"}, "no-referer-override"),
    ]

    last_error = None
    for extra, label in strategies:
        try:
            headers = _build_cdn_headers(referer, cookie, range_hdr)
            headers.update(extra)
            client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(120.0, connect=15.0),
                verify=False,
                http2=False,
            )
            req = client.build_request("GET", target, headers=headers)
            r = await client.send(req, stream=True)
            if _is_good_media_response(r):
                print(f"[proxy] CDN OK via {label} for {target[:70]}", flush=True)
                return (r, client, label)
            ct = (r.headers.get("Content-Type") or "")[:40]
            print(f"[proxy] CDN retry {label}: status={r.status_code} ct={ct!r}", flush=True)
            await r.aclose()
            await client.aclose()
        except Exception as e:
            last_error = e
            print(f"[proxy] CDN exception {label}: {e}", flush=True)
            continue

    return (None, None, f"all strategies failed (last_error={last_error})")


# ────────────────────────────────────────────────────────────────────────────
# m3u8 rewriting
# ────────────────────────────────────────────────────────────────────────────
def _rewrite_m3u8(content, base_url: str, referer: str, cookie: str | None = None, provider_id: str | None = None):
    if isinstance(content, (bytes, bytearray)):
        content = content.decode("utf-8", "replace")

    def make_proxy(abs_url):
        q = "url=" + quote(abs_url, safe="") + "&referer=" + quote(referer, safe="")
        if cookie:
            q += "&cookie=" + quote(cookie, safe="")
        if provider_id:
            q += "&provider=" + quote(provider_id, safe="")
        return "/proxy?" + q

    out = []
    for line in content.split("\n"):
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            if 'URI="' in s:
                s = re.sub(
                    r'URI="([^"]*)"',
                    lambda m: 'URI="' + make_proxy(urljoin(base_url, m.group(1))) + '"',
                    s,
                )
                out.append(s)
            else:
                out.append(line)
        else:
            out.append(make_proxy(urljoin(base_url, s)))
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────────────
# Routes — basic
# ────────────────────────────────────────────────────────────────────────────
@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index():
    path = os.path.join(BASE_DIR, DEFAULT_HTML)
    if not os.path.exists(path):
        raise HTTPException(404, f"{DEFAULT_HTML} not found")
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


# ────────────────────────────────────────────────────────────────────────────
# Routes — providers
# ────────────────────────────────────────────────────────────────────────────
@app.get("/providers")
async def list_providers():
    data = sorted(
        (
            {
                "id": pid,
                "name": info.get("name", pid),
                "url": info.get("url", ""),
                "has_plugin": pid in _PLUGINS,
            }
            for pid, info in PROVIDERS.items()
        ),
        key=lambda x: x["name"].lower(),
    )
    return JSONResponse(data)


@app.get("/providers/{pid}")
async def get_provider(pid: str):
    prov = PROVIDERS.get(pid)
    if not prov:
        raise HTTPException(404, f"unknown provider: {pid}")
    out = dict(prov)
    out["has_plugin"] = pid in _PLUGINS
    return JSONResponse(out)


@app.get("/providers/{pid}/search")
async def provider_search(pid: str, q: str = ""):
    prov = PROVIDERS.get(pid)
    if not prov:
        raise HTTPException(404, f"unknown provider: {pid}")
    cls = _PLUGINS.get(pid)
    if cls is None:
        raise HTTPException(501, f"no plugin for provider: {pid}")
    try:
        inst = cls(prov)
        result = inst.search(q)
        return JSONResponse(json.loads(json.dumps(result, default=str)))
    except NotImplementedError as e:
        raise HTTPException(501, f"not implemented: {e}")
    except Exception as e:
        print(f"[plugin] {pid}.search error: {e}", flush=True)
        raise HTTPException(502, f"plugin error: {e}")


@app.get("/providers/{pid}/streams")
async def provider_streams(pid: str, url: str = ""):
    prov = PROVIDERS.get(pid)
    if not prov:
        raise HTTPException(404, f"unknown provider: {pid}")
    cls = _PLUGINS.get(pid)
    if cls is None:
        raise HTTPException(501, f"no plugin for provider: {pid}")
    try:
        inst = cls(prov)
        result = inst.get_streams(url)
        return JSONResponse(json.loads(json.dumps(result, default=str)))
    except NotImplementedError as e:
        raise HTTPException(501, f"not implemented: {e}")
    except Exception as e:
        print(f"[plugin] {pid}.streams error: {e}", flush=True)
        raise HTTPException(502, f"plugin error: {e}")


# ────────────────────────────────────────────────────────────────────────────
# Routes — proxy
# ────────────────────────────────────────────────────────────────────────────
@app.get("/proxy")
async def proxy(request: Request):
    url = request.query_params.get("url")
    if not url:
        raise HTTPException(400, "missing url")

    target = unquote(url)
    referer = unquote(request.query_params.get("referer", "https://officialmoviebox.com/"))
    cookie_param = request.query_params.get("cookie")
    provider_id = request.query_params.get("provider")
    direct_flag = request.query_params.get("direct", "0") == "1"

    try:
        host = (urlparse(target).hostname or "").lower()
    except Exception:
        host = ""

    if not _is_allowed(host, provider_id=provider_id):
        print(f"[proxy] BLOCK host={host} provider={provider_id} target={target}", flush=True)
        raise HTTPException(403, f"host not allowed: {host}")

    # Client-side bypass: 302 to CDN
    if direct_flag:
        print(f"[proxy] DIRECT redirect host={host}", flush=True)
        return RedirectResponse(target, status_code=302)

    is_api = "officialmoviebox.com" in host
    is_media = _guess_content_type_from_path(target) is not None
    rng = request.headers.get("range")

    upstream = None
    client = None

    if is_media:
        upstream, client, strategy = await _fetch_media_with_retry(
            target, referer, cookie_param, rng
        )
        if upstream is None:
            print(f"[proxy] MEDIA FAIL host={host} strategy={strategy}", flush=True)
            raise HTTPException(
                502,
                detail={
                    "error": "upstream returned non-video (all strategies failed)",
                    "target": target,
                    "host": host,
                    "referer_sent": referer,
                    "detail": strategy,
                    "hint": "Append &direct=1 to force a 302 redirect.",
                },
            )
    else:
        if is_api:
            headers = {
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": referer,
                "Origin": "https://officialmoviebox.com",
                "x-client-info": '{"timezone":"Asia/Colombo"}',
                "x-source": "",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
            for h in ("x-client-info", "x-source"):
                v = request.headers.get(h)
                if v:
                    headers[h] = v
        else:
            headers = {
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": referer,
                "Origin": "https://officialmoviebox.com",
            }
            if cookie_param:
                headers["Cookie"] = cookie_param
        if rng:
            headers["Range"] = rng

        try:
            client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(120.0, connect=15.0),
                verify=False,
            )
            req = client.build_request("GET", target, headers=headers)
            upstream = await client.send(req, stream=True)
        except Exception as e:
            if client:
                await client.aclose()
            print(f"[proxy] UPSTREAM FAIL target={target} error={e}", flush=True)
            raise HTTPException(502, f"upstream error: {e}")

    # ── m3u8 rewrite ──
    manifest_ct = (upstream.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    is_manifest = (
        urlparse(target).path.lower().endswith(".m3u8")
        or manifest_ct in M3U8_CONTENT_TYPES
    )
    if is_manifest and upstream.status_code < 400:
        try:
            raw = await upstream.aread()
        except Exception as e:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(502, f"manifest read failed: {e}")
        await upstream.aclose()
        await client.aclose()
        rewritten = _rewrite_m3u8(raw, target, referer, cookie_param, provider_id)
        return Response(
            content=rewritten.encode("utf-8"),
            status_code=upstream.status_code,
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )

    # ── non-video safety net ──
    media_ct = _guess_content_type_from_path(target)
    if media_ct:
        upstream_ct = (upstream.headers.get("Content-Type") or "").lower()
        bad = (
            upstream_ct.startswith("text/html")
            or upstream_ct.startswith("application/json")
            or upstream_ct.startswith("text/plain")
            or upstream_ct.startswith("application/xml")
            or upstream_ct.startswith("text/xml")
        )
        if bad or upstream.status_code >= 400:
            try:
                err_bytes = await upstream.aread()
                err_text = err_bytes[:2048].decode("utf-8", "replace")
            except Exception as e:
                err_text = f"(unreadable: {e})"
            await upstream.aclose()
            await client.aclose()
            print(
                f"[proxy] NON-VIDEO target={target}\n"
                f"        upstream_status={upstream.status_code}\n"
                f"        upstream_ct={upstream_ct}\n"
                f"        body_preview={err_text[:400]!r}",
                flush=True,
            )
            raise HTTPException(
                502,
                detail={
                    "error": "upstream returned non-video",
                    "upstream_status": upstream.status_code,
                    "upstream_ct": upstream_ct,
                    "target": target,
                    "referer_sent": referer,
                    "body_preview": err_text[:400],
                    "hint": "Append &direct=1 to force a 302 redirect.",
                },
            )

    # ── stream response ──
    out_headers = {"Access-Control-Allow-Origin": "*"}
    cdn_ct = (upstream.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if cdn_ct.startswith("video/") or cdn_ct == "application/octet-stream":
        out_headers["Content-Type"] = upstream.headers.get("Content-Type")
    elif media_ct:
        out_headers["Content-Type"] = upstream.headers.get("Content-Type") or media_ct
    elif upstream.headers.get("Content-Type"):
        out_headers["Content-Type"] = upstream.headers.get("Content-Type")

    for k in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
        if k in upstream.headers:
            out_headers[k] = upstream.headers[k]
    if is_api:
        out_headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        out_headers["Pragma"] = "no-cache"
    elif "Cache-Control" in upstream.headers:
        out_headers["Cache-Control"] = upstream.headers["Cache-Control"]

    async def stream_body():
        try:
            async for chunk in upstream.aiter_bytes(CHUNK_SIZE):
                if chunk:
                    yield chunk
        except Exception as e:
            print(f"[stream] write error: {e}", flush=True)
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream.status_code,
        headers=out_headers,
    )


# ────────────────────────────────────────────────────────────────────────────
# Local dev entrypoint
# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    print(f"Starting on http://0.0.0.0:{port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port)
