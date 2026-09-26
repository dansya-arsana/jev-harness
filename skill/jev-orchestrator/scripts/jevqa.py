#!/usr/bin/env python3
"""Browser QA with jev-ultrafast: drive flows in a throwaway Chrome, capture screenshots, run DOM checks.

Run:
  uv run --project ~/Documents/Tools/jev-ultrafast python jevqa.py run <scenario.json> [--out DIR] [--headful]

Chrome is always a dedicated, fresh temp profile (never the user's). TYPE_TEXT values come from a local
OpenAI-compatible shim that answers only from the flow's `values` map. The TypeSafe key is never printed.
"""
import argparse
import base64
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))), ".env")  # <repo>/.env
CHROME = os.environ.get("JEVQA_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
SECRET = re.compile(r"password|card|cvv|token|secret", re.I)
DEFAULT_HOSTS = ["localhost", "127.0.0.1"]
DEFAULT_VIEWPORTS = [{"name": "desktop", "width": 1440, "height": 900}]
NAME_OK = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_WAIT_MS = 15000
WAIT_FOR_TIMEOUT = 15
SHEET_COLS, SHEET_TILE_W, SHEET_GUTTER, SHEET_QUALITY = 3, 480, 6, 70


class QAError(Exception):
    pass


# ---------- keys ----------

def load_key():
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    try:
        sys.path.insert(0, HERE)
        import jevlib  # noqa: E402
        return jevlib.api_key()
    except Exception:
        pass
    try:
        with open(REPO_ENV) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TYPESAFE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    raise QAError("TYPESAFE_API_KEY not found (env var or .env)")


# ---------- pure helpers (tested offline) ----------

def mask(label, value):
    if value is None:
        return None
    return "***" if SECRET.search(label or "") else value


def match_value(values, field):
    """Return (value, pattern) from the values map for a field {label, role}, or (None, None)."""
    for key in ("label", "role"):
        text = (field or {}).get(key) or ""
        if not text:
            continue
        for pattern, value in (values or {}).items():
            if re.search(pattern, text, re.I):
                return value, pattern
    return None, None


def host_allowed(url, allow_hosts):
    host = (urlparse(url).hostname or "").lower()
    return bool(host) and host in {h.lower() for h in allow_hosts}


def validate_scenario(sc):
    """Return (normalized scenario, errors). Flows with disallowed hosts are marked refused, not dropped."""
    errors = []
    if not isinstance(sc, dict):
        return None, ["scenario must be a JSON object"]
    base = sc.get("base_url")
    if not isinstance(base, str) or urlparse(base).scheme not in ("http", "https"):
        errors.append("base_url must be an http(s) URL")
        base = ""
    hosts = sc.get("allow_hosts", DEFAULT_HOSTS)
    if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) and h for h in hosts):
        errors.append("allow_hosts must be a non-empty list of hostnames")
        hosts = DEFAULT_HOSTS
    vps = sc.get("viewports", DEFAULT_VIEWPORTS)
    vp_names = set()
    if not isinstance(vps, list) or not vps:
        errors.append("viewports must be a non-empty list")
        vps = []
    for v in vps:
        if not (isinstance(v, dict) and isinstance(v.get("name"), str) and NAME_OK.match(v["name"])
                and isinstance(v.get("width"), int) and isinstance(v.get("height"), int)
                and v["width"] > 0 and v["height"] > 0):
            errors.append(f"bad viewport: {v!r}")
            continue
        if v["name"] in vp_names:
            errors.append(f"duplicate viewport name {v['name']}")
        vp_names.add(v["name"])
    sc_init = sc.get("init_script")
    if sc_init is not None and not isinstance(sc_init, str):
        errors.append("init_script must be a string")
        sc_init = None
    sc_loader = sc.get("wait_for_loader", True)
    if not isinstance(sc_loader, bool):
        errors.append("wait_for_loader must be a boolean")
        sc_loader = True
    flows = sc.get("flows")
    if not isinstance(flows, list) or not flows:
        errors.append("flows must be a non-empty list")
        flows = []
    seen, out_flows = set(), []
    for i, f in enumerate(flows):
        if not isinstance(f, dict):
            errors.append(f"flow {i} must be an object")
            continue
        name = f.get("name")
        if not isinstance(name, str) or not NAME_OK.match(name):
            errors.append(f"flow {i}: name must match {NAME_OK.pattern}")
            continue
        if name in seen:
            errors.append(f"duplicate flow name {name}")
        seen.add(name)
        if not isinstance(f.get("path"), str):
            errors.append(f"flow {name}: path is required")
            continue
        values = f.get("values", {})
        if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
            errors.append(f"flow {name}: values must map label regex -> string")
            values = {}
        for pat in values:
            try:
                re.compile(pat)
            except re.error:
                errors.append(f"flow {name}: bad regex {pat!r}")
        for key, default in (("max_steps", 25), ("max_slices", 10)):
            n = f.get(key, default)
            if not isinstance(n, int) or n < 1:
                errors.append(f"flow {name}: {key} must be a positive integer")
        want = f.get("viewports")
        if want is not None and (not isinstance(want, list) or any(w not in vp_names for w in want)):
            errors.append(f"flow {name}: unknown viewport in {want!r}")
        if "expect_text" in f and not (isinstance(f["expect_text"], list)
                                       and all(isinstance(t, str) for t in f["expect_text"])):
            errors.append(f"flow {name}: expect_text must be a list of strings")
        if "expect_url" in f and not isinstance(f["expect_url"], str):
            errors.append(f"flow {name}: expect_url must be a string")
        if "goal" in f and not (isinstance(f["goal"], str) and f["goal"].strip()):
            errors.append(f"flow {name}: goal must be a non-empty string")
        wait_ms = f.get("wait_ms", 0)
        if not isinstance(wait_ms, int) or isinstance(wait_ms, bool) or not 0 <= wait_ms <= MAX_WAIT_MS:
            errors.append(f"flow {name}: wait_ms must be an integer 0..{MAX_WAIT_MS}")
            wait_ms = 0
        if "init_script" in f and not isinstance(f["init_script"], str):
            errors.append(f"flow {name}: init_script must be a string")
        if "wait_for" in f and not (isinstance(f["wait_for"], str) and f["wait_for"].strip()):
            errors.append(f"flow {name}: wait_for must be a non-empty CSS selector string")
        wfl = f.get("wait_for_loader", sc_loader)
        if not isinstance(wfl, bool):
            errors.append(f"flow {name}: wait_for_loader must be a boolean")
            wfl = sc_loader
        lto = f.get("loader_timeout_ms", 10000)
        if not isinstance(lto, int) or isinstance(lto, bool) or not 0 <= lto <= MAX_WAIT_MS:
            errors.append(f"flow {name}: loader_timeout_ms must be an integer 0..{MAX_WAIT_MS}")
            lto = 10000
        if "scroll_to" in f and not (isinstance(f["scroll_to"], str) and f["scroll_to"].strip()):
            errors.append(f"flow {name}: scroll_to must be a non-empty CSS selector string")
        url = urljoin(base, f["path"]) if base else f["path"]
        out_flows.append({
            **f, "url": url, "values": values, "wait_ms": wait_ms,
            "init_script": resolve_init_script(sc_init, f), "wait_for": f.get("wait_for") or None,
            "wait_for_loader": wfl, "loader_timeout_ms": lto, "scroll_to": f.get("scroll_to") or None,
            "max_steps": f.get("max_steps", 25), "max_slices": f.get("max_slices", 10),
            "refused": None if host_allowed(url, hosts) else f"host of {url} not in allow_hosts",
        })
    return {"base_url": base, "allow_hosts": hosts, "viewports": vps, "init_script": sc_init,
            "wait_for_loader": sc_loader, "flows": out_flows}, errors


def resolve_init_script(scenario_script, flow):
    """A flow's own init_script (a string, even empty) overrides the scenario-level one."""
    own = flow.get("init_script")
    if isinstance(own, str):
        return own or None
    return scenario_script if isinstance(scenario_script, str) and scenario_script else None


def untouched_selects(selects):
    """Selects whose value still equals their first option: [{label, value}]. Input items: {label, value, first}."""
    return [{"label": s.get("label") or "", "value": s.get("value")}
            for s in selects or [] if s.get("first") is not None and s.get("value") == s.get("first")]


def sheet_geometry(sizes, cols=SHEET_COLS, tile_w=SHEET_TILE_W, gutter=SHEET_GUTTER):
    """Tile layout for a contact sheet. sizes: [(w, h)] in reading order.
    Returns (sheet_w, sheet_h, [(x, y, tile_w, tile_h)]). Gutters sit between tiles and around the edge."""
    if not sizes:
        return 0, 0, []
    cols = max(1, min(cols, len(sizes)))
    tiles = [(tile_w, max(1, round(h * tile_w / w))) for w, h in sizes]
    boxes, y = [], gutter
    for r in range(0, len(tiles), cols):
        row = tiles[r:r + cols]
        for c, (tw, th) in enumerate(row):
            boxes.append((gutter + c * (tile_w + gutter), y, tw, th))
        y += max(th for _, th in row) + gutter
    return gutter + cols * (tile_w + gutter), y, boxes


def make_sheet(paths, out_path):
    """Tile PNG slices into one JPEG. Pillow if available, else macOS sips + a vertical stack."""
    if not paths:
        return None
    try:
        from PIL import Image
    except ImportError:
        return _make_sheet_sips(paths, out_path)
    images = [Image.open(p).convert("RGB") for p in paths]
    w, h, boxes = sheet_geometry([im.size for im in images])
    sheet = Image.new("RGB", (w, h), (0, 0, 0))
    for im, (x, y, tw, th) in zip(images, boxes):
        sheet.paste(im.resize((tw, th), Image.LANCZOS), (x, y))
    sheet.save(out_path, "JPEG", quality=SHEET_QUALITY)
    return out_path


def _make_sheet_sips(paths, out_path):
    """Fallback without Pillow: sips resizes each slice to tile width as BMP, we stack them in one
    column with black gutters into a BMP, and sips converts that to JPEG."""
    import struct
    tmp = tempfile.mkdtemp(prefix="jevqa-sheet-")
    try:
        rows, g = [], SHEET_GUTTER
        for i, p in enumerate(paths):
            bmp = os.path.join(tmp, f"{i}.bmp")
            subprocess.run(["sips", "--resampleWidth", str(SHEET_TILE_W), "-s", "format", "bmp", p, "--out", bmp],
                           check=True, capture_output=True)
            rows.append(_read_bmp(bmp))
        full_w = max(w for w, _, _ in rows) + 2 * g
        black = b"\x00\x00\x00"
        lines = [black * full_w] * g
        for w, h, px in rows:
            for y in range(h):
                lines.append(black * g + px[y * w * 3:(y + 1) * w * 3] + black * (full_w - w - g))
            lines += [black * full_w] * g
        stride = (full_w * 3 + 3) & ~3
        pad = b"\x00" * (stride - full_w * 3)
        body = b"".join(ln + pad for ln in lines)
        header = struct.pack("<2sIHHI", b"BM", 54 + len(body), 0, 0, 54) + struct.pack(
            "<IiiHHIIiiII", 40, full_w, -len(lines), 1, 24, 0, len(body), 2835, 2835, 0, 0)
        sheet_bmp = os.path.join(tmp, "sheet.bmp")
        with open(sheet_bmp, "wb") as f:
            f.write(header + body)
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(SHEET_QUALITY), sheet_bmp,
                        "--out", out_path], check=True, capture_output=True)
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read_bmp(path):
    """Minimal 24/32-bit BMP reader -> (w, h, BGR bytes top-down)."""
    import struct
    with open(path, "rb") as f:
        d = f.read()
    off, = struct.unpack_from("<I", d, 10)
    w, h, _, bpp = struct.unpack_from("<iiHH", d, 18)
    bottom_up, h = h > 0, abs(h)
    step = bpp // 8
    stride = (w * step + 3) & ~3
    out = bytearray()
    for y in range(h):
        row = off + (h - 1 - y if bottom_up else y) * stride
        if step == 3:
            out += d[row:row + w * 3]
        else:
            for x in range(w):
                out += d[row + x * step:row + x * step + 3]
    return w, h, bytes(out)


# ---------- text shim ----------

class Shim:
    """Local OpenAI-compatible /v1/chat/completions that answers only from the current flow's values."""

    def __init__(self):
        self.values = {}
        self.served = []
        shim = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    context = json.loads(body["messages"][-1]["content"])
                    content = shim.answer(context.get("field") or {})
                    code, payload = 200, {"choices": [{"message": {"role": "assistant", "content": content}}]}
                except Exception:
                    code, payload = 400, {"error": "bad request"}
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def answer(self, field):
        value, pattern = match_value(self.values, field)
        label = field.get("label") or field.get("role") or ""
        self.served.append({"label": label, "value": mask(label, value), "matched_pattern": pattern})
        return json.dumps({"text": value})

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ---------- Chrome + daemon ----------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Chrome:
    def __init__(self, headful=False):
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="jevqa-chrome-")
        args = [CHROME, f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
                "about:blank"]
        if not headful:
            args.insert(1, "--headless=new")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=1).read()
                return
            except Exception:
                time.sleep(0.1)
        self.stop()
        raise QAError("dedicated Chrome did not start")

    def stop(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception:
                pass
        shutil.rmtree(self.profile, ignore_errors=True)


# ---------- page work ----------

DOM_CHECKS = r"""(() => {
  const sel = e => { let s = e.tagName.toLowerCase(); if (e.id) s += '#' + e.id;
    else if (typeof e.className === 'string' && e.className.trim()) s += '.' + e.className.trim().split(/\s+/).slice(0,2).join('.');
    return s; };
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 &&
    e.checkVisibility({checkOpacity:true, checkVisibilityCSS:true}); };
  const W = innerWidth;
  const overflow = document.documentElement.scrollWidth > W + 1;
  const wide = [];
  if (overflow) for (const e of document.body.querySelectorAll('*')) {
    const r = e.getBoundingClientRect();
    if (r.right > W + 1 && r.width > 0 && getComputedStyle(e).position !== 'fixed') wide.push({selector: sel(e), width: Math.round(r.width), right: Math.round(r.right)});
    if (wide.length >= 5) break;
  }
  const imgs = [...document.images].filter(i => {
    if (!i.hasAttribute('alt')) return true;
    if (i.getAttribute('alt') === '') return !(i.closest('[aria-hidden="true"]') || ['presentation','none'].includes(i.getAttribute('role')));
    return false;
  }).map(i => ({selector: sel(i), src: (i.currentSrc || i.src || '').slice(0, 160)}));
  const name = e => (e.getAttribute('aria-label') || '').trim() ||
    (e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim() ||
    (e.innerText || '').trim() || (e.getAttribute('title') || '').trim() ||
    [...e.querySelectorAll('img[alt]')].map(i => i.alt).join(' ').trim() ||
    [...e.querySelectorAll('svg title')].map(t => t.textContent).join(' ').trim() ||
    (e.tagName === 'INPUT' ? (e.value || '').trim() : '');
  const unnamed = [...document.querySelectorAll('button, a[href], [role="button"], input[type="submit"], input[type="button"]')]
    .filter(e => vis(e) && !e.closest('[aria-hidden="true"]') && !name(e))
    .map(e => ({selector: sel(e), href: e.getAttribute('href')}));
  const small = []; let smallCount = 0;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walker.nextNode());) {
    const t = n.textContent.trim(); const p = n.parentElement;
    if (!t || !p || ['SCRIPT','STYLE','NOSCRIPT'].includes(p.tagName) || !vis(p)) continue;
    const fs = parseFloat(getComputedStyle(p).fontSize);
    if (fs < 11) { smallCount++; if (small.length < 5) small.push({selector: sel(p), px: fs, text: t.slice(0, 60)}); }
  }
  return {url: location.href, text: document.body.innerText, overflow, scrollWidth: document.documentElement.scrollWidth,
    innerWidth: W, wide, imgs: imgs.slice(0, 20), imgCount: imgs.length, unnamed: unnamed.slice(0, 20),
    unnamedCount: unnamed.length, small, smallCount, qaErrors: window.__qa_errors || []};
})()"""

ERROR_COLLECTOR = """window.__qa_errors = window.__qa_errors || [];
addEventListener('error', e => window.__qa_errors.push({type: 'error', text: String(e.message || e)}));
addEventListener('unhandledrejection', e => window.__qa_errors.push({type: 'unhandledrejection', text: String(e.reason)}));"""


SELECTS = r"""(() => [...document.querySelectorAll('select')].map(e => {
  const lab = (e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || e.name || e.id || '';
  return {label: lab.trim().slice(0, 80), value: e.value, first: e.options.length ? e.options[0].value : null};
}))()"""


def visible_js(selector):
    return ("(() => { const e = document.querySelector(" + json.dumps(selector) + "); if (!e) return false;"
            " const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 &&"
            " e.checkVisibility({checkOpacity:true, checkVisibilityCSS:true}); })()")


def wait_visible(evaluate, selector, timeout=WAIT_FOR_TIMEOUT):
    """Poll until selector is visible. Returns {"pass", "selector", "waited_ms"}."""
    started = time.monotonic()
    ok = False
    while True:
        try:
            ok = bool(evaluate(visible_js(selector)))
        except Exception:
            ok = False
        if ok or time.monotonic() - started >= timeout:
            break
        time.sleep(0.1)
    return {"pass": ok, "selector": selector, "waited_ms": round((time.monotonic() - started) * 1000),
            **({} if ok else {"timeout_s": timeout})}


LOADER_PROBE = r"""(() => {
  if (document.readyState !== 'complete') return {busy: true, reason: 'loading'};
  const roots = [document.documentElement, document.body, document.querySelector('main')];
  if (roots.some(e => e && e.getAttribute('aria-busy') === 'true')) return {busy: true, reason: 'aria-busy'};
  const SEL = 'a[href],button,input,select,textarea,summary,[role=button],[role=link]';
  const all = [...document.querySelectorAll(SEL)];
  const vis = all.filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 &&
    e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); });
  if (all.length && vis.every(e => e.closest('[inert],[aria-hidden="true"]'))) return {busy: true, reason: 'inert'};
  const W = innerWidth, H = innerHeight, area = W * H;
  const pts = [[.5, .5], [.25, .25], [.75, .25], [.25, .75], [.75, .75]];
  let cover = null;
  for (const [fx, fy] of pts) {
    let e = document.elementFromPoint(W * fx, H * fy), found = null;
    for (; e && e.nodeType === 1; e = e.parentElement) {
      if (getComputedStyle(e).position !== 'fixed') continue;
      const r = e.getBoundingClientRect();
      const w = Math.max(0, Math.min(r.right, W) - Math.max(r.left, 0));
      const h = Math.max(0, Math.min(r.bottom, H) - Math.max(r.top, 0));
      if (w * h >= 0.9 * area) { found = e; break; }
    }
    if (!found || (cover && found !== cover)) { cover = null; break; }
    cover = found;
  }
  if (cover && !cover.querySelector(SEL) && !cover.matches(SEL)) return {busy: true, reason: 'overlay'};
  return {busy: false, reason: null};
})()"""
LOADER_POLL_S, LOADER_FADE_S = 0.15, 0.3


def wait_for_loader(evaluate, timeout_ms=10000, sleep=time.sleep, clock=time.monotonic):
    """Poll LOADER_PROBE until not busy or timeout. Returns {detected, reason, waited_ms, cleared}."""
    started = clock()
    detected, reason, cleared = False, None, False
    while True:
        try:
            r = evaluate(LOADER_PROBE) or {}
        except Exception:
            r = {"busy": True, "reason": "loading"}
        if not r.get("busy"):
            cleared = True
            break
        if not detected:
            detected, reason = True, r.get("reason")
        if (clock() - started) * 1000 >= timeout_ms:
            break
        sleep(LOADER_POLL_S)
    if detected and cleared:
        sleep(LOADER_FADE_S)
    return {"detected": detected, "reason": reason, "waited_ms": round((clock() - started) * 1000),
            "cleared": cleared}


def prepare_page(evaluate, flow):
    """After load/settle: loader wait (unless disabled), wait_for (if any), then sleep wait_ms.
    Returns {"wait_for": check or None, "loader": record or None}."""
    loader = None
    if flow.get("wait_for_loader", True):
        loader = wait_for_loader(evaluate, flow.get("loader_timeout_ms", 10000))
    check = wait_visible(evaluate, flow["wait_for"]) if flow.get("wait_for") else None
    if flow.get("wait_ms"):
        time.sleep(flow["wait_ms"] / 1000)
    return {"wait_for": check, "loader": loader}


def agent_viewport(flow, viewports):
    """The flow's first listed viewport, else the scenario's first."""
    want = flow.get("viewports")
    if want:
        for v in viewports:
            if v["name"] == want[0]:
                return v
    return viewports[0]


def action_labels(actions, cap=60):
    """Compact 'id kind role: label' lines for diagnosis. Never includes values."""
    out = []
    for a in (actions or [])[:cap]:
        role = f" {a['role']}" if a.get("role") else ""
        out.append(f"{a.get('id')} {a.get('kind')}{role}: {(a.get('label') or '')[:80]}")
    return out


DISCLOSURE_JS = """(() => { const e = window.__jevFast?.nodes.get(%d); if (!e) return null;
  const s = e.tagName === 'SUMMARY', d = s ? e.parentElement : null;
  return {disclosure: s || e.hasAttribute('aria-expanded'),
          open: s && d && d.tagName === 'DETAILS' ? d.open : null,
          expanded: e.getAttribute('aria-expanded')}; })()"""
FALLBACK_CLICK_JS = """(() => { const e = window.__jevFast?.nodes.get(%d);
  if (!e || !e.isConnected) return false; e.click(); return true; })()"""


def make_browser_class(base, vp, script=None, stale=None, sleep=time.sleep):
    """Browser subclass: forces the flow's viewport, screen-sized scrolls at the viewport centre, a JS click
    fallback for disclosures whose state did not change, and (optionally) an init_script before first navigate."""
    if stale is None:
        from jev_ultrafast.browser import StalePage as stale
    width, height, mobile = vp["width"], vp["height"], bool(vp.get("mobile"))

    class QABrowser(base):
        def call(self, method, **params):
            if method == "Emulation.setDeviceMetricsOverride":
                params = {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": mobile}
            if script and method == "Page.navigate" and not getattr(self, "_jevqa_init_done", False):
                self._jevqa_init_done = True
                super().call("Page.enable")
                super().call("Page.addScriptToEvaluateOnNewDocument", source=script)
            return super().call(method, **params)

        def act(self, action, page, text=None):
            if action["kind"] == "scroll":
                if not self.fresh(page, action):
                    raise stale("Page changed since this decision. Observe again.")
                sign = 1 if action.get("delta", 0) >= 0 else -1
                self.call("Input.dispatchMouseEvent", type="mouseWheel", x=width / 2, y=height / 2,
                          deltaX=0, deltaY=sign * round(0.8 * height))
                self.after_input = action
                return {"executed": action["id"]}
            if action["kind"] != "click" or type(action.get("node")) is not int:
                return super().act(action, page, text=text)
            node = action["node"]
            try:
                before = self.evaluate(DISCLOSURE_JS % node)
            except Exception:
                before = None
            result = super().act(action, page, text=text)
            if before and before.get("disclosure"):
                changed = False
                for _ in range(6):
                    sleep(0.05)
                    try:
                        now = self.evaluate(DISCLOSURE_JS % node)
                    except Exception:
                        now = None
                    if now is None or (now.get("open"), now.get("expanded")) != (before.get("open"), before.get("expanded")):
                        changed = True
                        break
                if not changed:
                    try:
                        clicked = self.evaluate(FALLBACK_CLICK_JS % node)
                    except Exception:
                        clicked = False
                    if clicked:
                        if not hasattr(self, "jevqa_fallbacks"):
                            self.jevqa_fallbacks = []
                        self.jevqa_fallbacks.append({"label": action.get("label"), "node": node})
            return result

    return QABrowser


def build_agent(flow, Agent, vp):
    """Construct the Agent with its Browser swapped (in this process only, for the constructor) for QABrowser."""
    import jev_ultrafast.agent as agent_mod
    base = agent_mod.Browser
    agent_mod.Browser = make_browser_class(base, vp, flow.get("init_script"))
    try:
        return Agent(flow["url"], flow["goal"])
    finally:
        agent_mod.Browser = base


class Tab:
    def __init__(self, cdp, session):
        self.cdp, self.session = cdp, session

    def call(self, method, **params):
        return self.cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        r = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def settle(self, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.05)
        time.sleep(0.8)


def check_viewport(tab, flow, vp, out_dir, reload, prepare=False):
    tab.call("Emulation.setDeviceMetricsOverride", width=vp["width"], height=vp["height"],
             deviceScaleFactor=1, mobile=bool(vp.get("mobile")))
    if reload:
        tab.call("Page.reload")
        time.sleep(0.2)
    tab.settle()
    prep = prepare_page(tab.evaluate, flow) if prepare else {}
    shots = []
    height = tab.evaluate("document.documentElement.scrollHeight") or vp["height"]
    for i in range(flow["max_slices"]):
        y = i * vp["height"]
        if i and y >= height:
            break
        tab.evaluate(f"window.scrollTo(0, {y})")
        time.sleep(0.4)
        data = tab.call("Page.captureScreenshot", format="png")["data"]
        path = os.path.join(out_dir, f"{flow['name']}-{vp['name']}-{i + 1:02d}.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        shots.append(path)
        height = tab.evaluate("document.documentElement.scrollHeight") or height
    tab.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.2)
    d = tab.evaluate(DOM_CHECKS) or {}
    checks = {
        "horizontal_overflow": {"pass": not d.get("overflow"), "scrollWidth": d.get("scrollWidth"),
                                "innerWidth": d.get("innerWidth"), "elements": d.get("wide", [])},
        "img_alt": {"pass": not d.get("imgCount"), "count": d.get("imgCount", 0), "items": d.get("imgs", [])},
        "accessible_names": {"pass": not d.get("unnamedCount"), "count": d.get("unnamedCount", 0),
                             "items": d.get("unnamed", [])},
        "min_font_11px": {"pass": not d.get("smallCount"), "count": d.get("smallCount", 0), "items": d.get("small", [])},
    }
    if prep.get("wait_for"):
        checks["wait_for"] = prep["wait_for"]
    if prep.get("loader"):
        checks["loader"] = {"pass": prep["loader"]["cleared"], **prep["loader"]}
    sheet = make_sheet(shots, os.path.join(out_dir, f"{flow['name']}-{vp['name']}-sheet.jpg"))
    if flow.get("expect_text"):
        text = d.get("text") or ""
        missing = [t for t in flow["expect_text"] if t not in text]
        checks["expect_text"] = {"pass": not missing, "missing": missing}
    if flow.get("expect_url"):
        checks["expect_url"] = {"pass": flow["expect_url"] in (d.get("url") or ""), "url": d.get("url"),
                                "want": flow["expect_url"]}
    return {"sheet": sheet, "screenshots": shots, "checks": checks, "url": d.get("url"), "page_errors": d.get("qaErrors", [])}


def console_errors(events, session):
    out = []
    for e in events:
        if e.get("session_id") != session:
            continue
        m, p = e.get("method"), e.get("params") or {}
        if m == "Runtime.exceptionThrown":
            det = p.get("exceptionDetails") or {}
            out.append({"type": "exception", "text": (det.get("exception") or {}).get("description") or det.get("text")})
        elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
            out.append({"type": "console." + p["type"],
                        "text": " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))})
        elif m == "Log.entryAdded" and (p.get("entry") or {}).get("level") == "error":
            out.append({"type": "log", "text": p["entry"].get("text"), "url": p["entry"].get("url")})
    return out


def run_goal(flow, allow_hosts, Agent, viewports):
    rec = {"status": None, "steps": 0, "elapsed_ms": 0}
    started = time.perf_counter()
    vp = agent_viewport(flow, viewports)
    rec["agent_viewport"] = vp["name"]
    agent = build_agent(flow, Agent, vp)
    try:
        tab = Tab(None, agent.browser.session)
        prep = prepare_page(agent.browser.evaluate, flow)
        if prep["wait_for"]:
            rec["wait_for"] = prep["wait_for"]
        rec["loader"] = prep["loader"]
        if flow.get("scroll_to"):
            sel = flow["scroll_to"]
            try:
                found = bool(agent.browser.evaluate(
                    "(() => { const e = document.querySelector(" + json.dumps(sel) + "); if (!e) return false;"
                    " e.scrollIntoView({block: 'start'}); return true; })()"))
            except Exception:
                found = False
            time.sleep(0.4)
            rec["scroll_to"] = {"selector": sel, "found": found}
        # Same refresh agent.py does after an action: the new observation carries its own fingerprint.
        agent.state["page"] = agent.browser.observe(screenshot=agent.screenshots)
        agent.state["decision"] = None
        rec["initial_actions"] = len(agent.state["page"].get("actions") or [])
        rec["initial_text_chars"] = len(agent.state["page"].get("text") or "")
        rec["initial_labels"] = action_labels(agent.state["page"].get("actions"))
        try:
            for state in agent.run():
                rec["steps"] = len(state["history"])
                url = state["page"]["url"]
                if not host_allowed(url, allow_hosts):
                    rec["status"] = "left_allowed_hosts"
                    rec["left_to"] = url
                    break
                if rec["steps"] >= flow["max_steps"] and state["status"] not in ("done", "blocked"):
                    rec["status"] = "step_cap"
                    break
            else:
                rec["status"] = agent.state["status"]
        except Exception as exc:
            rec["status"] = "error"
            rec["error"] = f"{type(exc).__name__}: {exc}"
        s = agent.state
        rec["final_labels"] = action_labels((s.get("page") or {}).get("actions"))
        rec["fallback_clicks"] = list(getattr(agent.browser, "jevqa_fallbacks", []))
        rec["steps"] = len(s["history"])
        rec["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        rec["agent_elapsed_ms"] = s.get("elapsed_ms")
        rec["actions"] = [{"step": h["step"], "kind": h["kind"], "label": h["action"],
                           "text": mask(h["action"], h.get("text")), "url": h.get("url")} for h in s["history"]]
        rec["decisions"] = [{"choice": d.get("choice"), "operation": d.get("operation"), "target": d.get("target"),
                             "confidence": d.get("confidence"), "latency_ms": d.get("latency_ms")}
                            for d in s["decisions"]]
        rec["text_calls"] = [{"field": t.get("field"), "value": mask(t.get("field"), t.get("value")),
                              "latency_ms": t.get("latency_ms")} for t in s["text_calls"]]
        return agent, tab, rec
    except BaseException:
        agent.close()
        raise


def run(args):
    with open(args.scenario) as f:
        scenario, errors = validate_scenario(json.load(f))
    if errors:
        raise QAError("invalid scenario:\n  " + "\n  ".join(errors))
    out_dir = os.path.abspath(args.out or os.path.join(".jev", "qa", time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(out_dir, exist_ok=True)

    os.environ["TYPESAFE_API_KEY"] = load_key()
    chrome = Chrome(headful=args.headful)
    shim = Shim().start()
    name = f"jevqa-{os.getpid()}-{chrome.port}"
    os.environ.update({
        "BU_CDP_URL": f"http://127.0.0.1:{chrome.port}", "BU_NAME": name,
        "BH_TELEMETRY": "0", "BROWSER_HARNESS_TELEMETRY": "0", "ANONYMIZED_TELEMETRY": "false",
        "TEXT_MODEL_BASE_URL": f"http://127.0.0.1:{shim.port}/v1", "TEXT_MODEL_API_KEY": "local-shim",
        "TEXT_MODEL": "opus-values", "TEXT_MODEL_REASONING": "none",
    })
    report = {"scenario": os.path.abspath(args.scenario), "out_dir": out_dir, "flows": []}
    admin = None
    try:
        # Imported only after BU_* is set: browser_harness reads BU_NAME at import time.
        from browser_harness import admin
        from browser_harness.helpers import cdp, drain_events
        from jev_ultrafast import Agent
        admin.ensure_daemon()
        vp_by_name = {v["name"]: v for v in scenario["viewports"]}
        for flow in scenario["flows"]:
            rec = {"name": flow["name"], "url": flow["url"], "goal": flow.get("goal")}
            report["flows"].append(rec)
            if flow["refused"]:
                rec.update(status="refused", reason=flow["refused"])
                continue
            shim.values, shim.served = flow["values"], []
            agent = target = None
            drain_events()
            try:
                if flow.get("goal"):
                    agent, tab, run_rec = run_goal(flow, scenario["allow_hosts"], Agent, scenario["viewports"])
                    tab.cdp = cdp
                    rec.update(run_rec)
                    try:
                        rec["untouched_selects"] = untouched_selects(tab.evaluate(SELECTS))
                    except Exception:
                        rec["untouched_selects"] = []
                    reload = False
                else:
                    started = time.perf_counter()
                    target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
                    session = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
                    tab = Tab(cdp, session)
                    # A background target is throttled (timers, rAF, frames) until its evaluates time out;
                    # focus emulation keeps it rendering like a visible tab, as jev-ultrafast's own Browser does.
                    tab.call("Emulation.setFocusEmulationEnabled", enabled=True)
                    tab.call("Page.enable")
                    tab.call("Page.addScriptToEvaluateOnNewDocument", source=ERROR_COLLECTOR)
                    if flow.get("init_script"):
                        tab.call("Page.addScriptToEvaluateOnNewDocument", source=flow["init_script"])
                    tab.call("Runtime.enable")
                    tab.call("Log.enable")
                    tab.call("Page.navigate", url=flow["url"])
                    tab.settle()
                    rec.update(status="captured", steps=0, elapsed_ms=round((time.perf_counter() - started) * 1000))
                    reload = True
                if flow.get("goal"):
                    tab.call("Runtime.enable")
                    tab.call("Log.enable")
                rec["text_values_served"] = list(shim.served)
                rec["viewports"] = {}
                if rec["status"] != "left_allowed_hosts":
                    for vname in flow.get("viewports") or list(vp_by_name):
                        rec["viewports"][vname] = check_viewport(tab, flow, vp_by_name[vname], out_dir, reload,
                                                                  prepare=not flow.get("goal"))
                        cur = rec["viewports"][vname]["url"] or ""
                        if not host_allowed(cur, scenario["allow_hosts"]):
                            rec["status"] = "left_allowed_hosts"
                            break
                events = drain_events()
                errs = console_errors(events, tab.session)
                for v in rec["viewports"].values():
                    errs += [e for e in v.pop("page_errors", []) if e not in errs]
                rec["console"] = {"pass": not errs, "errors": errs[:50]}
            except Exception as exc:
                if rec.get("status") in (None, "captured"):
                    rec["status"] = "error"
                rec["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                if agent:
                    try:
                        agent.close()
                    except Exception:
                        pass
                if target:
                    try:
                        cdp("Target.closeTarget", targetId=target)
                    except Exception:
                        pass
    finally:
        if admin is not None:
            try:
                admin.restart_daemon(name)
            except Exception:
                pass
        shim.stop()
        chrome.stop()
        write_report(report, out_dir)
    return report


def failed_checks(rec):
    fails = [f"{vn}:{cn}" for vn, v in (rec.get("viewports") or {}).items()
             for cn, c in v["checks"].items() if not c["pass"]]
    if rec.get("console") and not rec["console"]["pass"]:
        fails.append("console")
    if rec.get("wait_for") and not rec["wait_for"]["pass"]:
        fails.append("wait_for")
    return fails


def write_report(report, out_dir):
    with open(os.path.join(out_dir, "report.json"), "w") as f:
        json.dump(report, f, indent=2)
    lines = [f"# jevqa report", "", f"Scenario: `{report['scenario']}`", ""]
    sheets = [v["sheet"] for rec in report["flows"] for v in (rec.get("viewports") or {}).values() if v.get("sheet")]
    if sheets:
        lines += ["## Contact sheets", "", *[f"- `{os.path.basename(p)}`" for p in sheets], ""]
    for rec in report["flows"]:
        lines.append(f"## {rec['name']}: {rec.get('status')}")
        lines.append(f"- url: {rec['url']}; steps: {rec.get('steps', 0)}; elapsed: {rec.get('elapsed_ms', 0)} ms")
        if rec.get("error") or rec.get("reason"):
            lines.append(f"- note: {rec.get('error') or rec.get('reason')}")
        fails = failed_checks(rec)
        lines.append(f"- failed checks: {', '.join(fails) if fails else 'none'}")
        for vn, v in (rec.get("viewports") or {}).items():
            lines.append(f"- {vn}: {len(v['screenshots'])} screenshots")
        if rec.get("untouched_selects"):
            lines.append("- untouched_selects (still on first option; a hint, not a failure): " +
                         "; ".join(f"{s['label'] or '(unlabelled)'}: {s['value']}" for s in rec["untouched_selects"]))
        lines.append("")
    with open(os.path.join(out_dir, "report.md"), "w") as f:
        f.write("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jevqa")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("scenario")
    r.add_argument("--out")
    r.add_argument("--headful", action="store_true")
    args = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        report = run(args)
    except QAError as exc:
        print(f"jevqa: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("jevqa: interrupted", file=sys.stderr)
        return 130
    for rec in report["flows"]:
        fails = failed_checks(rec)
        print(f"{rec['name']}: {rec.get('status')} steps={rec.get('steps', 0)} elapsed_ms={rec.get('elapsed_ms', 0)} "
              f"failed={','.join(fails) or 'none'}")
    print(f"report: {os.path.join(report['out_dir'], 'report.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
