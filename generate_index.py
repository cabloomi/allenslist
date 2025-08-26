#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Offline index generator (NO AI). Rebuilds index.html from scratch each run.

Features:
- Theme picker = dropdown only
- Editable price buckets (label + low/high) saved in localStorage
- Discounts support BOTH % and $ (defaults & per-sheet)
- Rounding:
    * Always round to nearest $10
    * If result > $340 and ends with **10**, round down to **00**
    * If result > $340 and ends with **90**, round up to **00**
    * Otherwise leave as-is (so 670 stays 670)
- Fuzzy search with closest-match ranking (numeric proximity, partial tokens)

Usage:
  python3 generate_index.py    # looks for source.xlsx (or source.csv/.xls/...)
Env:
  ENGINE_PIN (default "1337")  # PIN for opening Engine Room (hashed client-side)
"""
import os, sys, re, json, math, hashlib
from pathlib import Path
import json
import urllib.parse
import urllib.request


# === injected loader & boot constants ===
CONFIG_LOADER_SNIPPET = r"""<script>
  (function(){
    const PRIMARY_URL = '/config.json';
    const FALLBACK_URL = '/engine/config.json';
    const BUILD_ID = (window.__BUILD_ID__ || Date.now());

    function sanitizeBuckets(buckets){
      if (!Array.isArray(buckets)) return buckets;
      try {
        return buckets.map((arr) => {
          if (!Array.isArray(arr)) return arr;
          const label = String(arr[0]);
          const lo = Number(arr[1]);
          let hi = arr.length > 2 ? arr[2] : undefined;
          if (hi === null || hi === "" || typeof hi === "undefined") hi = "Infinity";
          return [label, lo, hi];
        });
      } catch(e) { console.warn('[config] bucket sanitize failed', e); return buckets; }
    }

    function mergeConfig(current, remote) {
      const out = {
        defaults: (current && current.defaults) || {},
        perSheet: (current && current.perSheet) || {},
        buckets: (current && current.buckets) || []
      };
      if (remote && typeof remote === 'object') {
        if (remote.defaults && typeof remote.defaults === 'object') out.defaults = remote.defaults;
        if (remote.perSheet && typeof remote.perSheet === 'object') out.perSheet = remote.perSheet;
        if (Array.isArray(remote.buckets)) out.buckets = sanitizeBuckets(remote.buckets);
        if (remote.uiTheme) { try { applyTheme(String(remote.uiTheme)); } catch(e){} }
      }
      return out;
    }

    async function fetchJSON(url){
      const u = url + (url.includes('?') ? '&' : '?') + 'v=' + encodeURIComponent(BUILD_ID);
      const res = await fetch(u, { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP '+res.status);
      return await res.json();
    }

    async function initEngineConfigFromServer(){
      let remote = null;
      try { remote = await fetchJSON(PRIMARY_URL); }
      catch { try { remote = await fetchJSON(FALLBACK_URL); } catch(e){} }
      if (!remote) { console.warn('[config] no remote config'); return; }
      if (typeof getEngineConfig !== 'function' || typeof setEngineConfig !== 'function') return;
      const current = getEngineConfig();
      const merged = mergeConfig(current, remote);
      setEngineConfig(merged);
      window.__CONFIG_APPLIED__ = true;
      window.__CONFIG_SOURCE__ = remote;
      console.info('[config] applied');
    }

    window.initEngineConfigFromServer = initEngineConfigFromServer;

    window.dumpEngine = function(){
      try {
        const cur = (typeof getEngineConfig === 'function') ? getEngineConfig() : null;
        console.log('applied:', !!window.__CONFIG_APPLIED__);
        console.log('source:', window.__CONFIG_SOURCE__ || null);
        console.dir(cur);
        return cur;
      } catch(e) { console.error('dumpEngine error', e); return null; }
    };
  })();
</script>"""
BOOT_ASYNC = """// Boot (patched to wait for config)
(async () => {
  try { loadTheme && loadTheme(); } catch(e){}
  async function waitFor(fn, timeoutMs=3000){
    const start = Date.now();
    while (!window[fn]) {
      if (Date.now() - start > timeoutMs) break;
      await new Promise(r => setTimeout(r, 50));
    }
    if (window[fn]) { return await window[fn](); }
  }
  try { await waitFor('initEngineConfigFromServer', 3000); } catch(e){}
  try { render && render(); } catch(e){}
})();
"""

def inject_config_loader(html: str) -> str:
    """Injects the config loader snippet and replaces sync boot with async boot."""
    import re as _re
    if "initEngineConfigFromServer" not in html:
        i = html.lower().rfind("</body>")
        if i != -1:
            html = html[:i] + "\n\n" + CONFIG_LOADER_SNIPPET + "\n\n" + html[i:]
        else:
            html = html + "\n\n" + CONFIG_LOADER_SNIPPET + "\n"
    # Replace first occurrence of 'loadTheme(); render();' with async boot
    html = _re.sub(r"loadTheme\(\);\s*render\(\);\s*", BOOT_ASYNC, html, count=1, flags=_re.IGNORECASE)
    return html

try:
    import pandas as pd
except ImportError:
    print("This script requires pandas. Install with: pip install pandas openpyxl", file=sys.stderr)
    sys.exit(1)



# === Google Sheets (required) ==================================================
def _env(key, default=None): 
    return os.getenv(key, default)

def _get_gsheet_id_from_env_or_default():
    url = _env("GSHEET_URL") or _env("GOOGLE_SHEET_URL")
    if url and "/spreadsheets/d/" in url:
        try:
            return url.split("/spreadsheets/d/")[1].split("/")[0]
        except Exception:
            pass
    return _env("GSHEET_ID") or _env("GOOGLE_SHEET_ID") or "1_Zt2pj5KMEvHdfmxLmLUpmZR1crKQNFKpIfuuqhvbbU"

def _fetch_visible_sheets_meta(spreadsheet_id, api_key):
    meta_url = "https://sheets.googleapis.com/v4/spreadsheets/" + spreadsheet_id + "?fields=sheets(properties(sheetId,title,hidden))&key=" + api_key
    try:
        with urllib.request.urlopen(meta_url) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print("[FATAL] Failed to fetch sheet metadata:", e)
        return None
    out = []
    for sh in (meta.get("sheets") or []):
        props = sh.get("properties") or {}
        if not props.get("hidden", False):
            gid = props.get("sheetId")
            title = props.get("title") or ("Sheet_%s" % gid if gid is not None else "Sheet")
            out.append((title, gid))
    return out

def load_gsheet_tables():
    sid = _get_gsheet_id_from_env_or_default()
    api_key = _env("GOOGLE_API_KEY") or _env("GCP_API_KEY") or "AIzaSyAbPCaQjUAWwtL7WR9srX2cFpIEdEuJCIw"
    meta = _fetch_visible_sheets_meta(sid, api_key)
    if not meta:
        print("[FATAL] Could not list tabs (ensure sheet is link-viewable and API key is valid).")
        return None
    print(f"[GS] Visible tabs detected: {len(meta)}")
    data = {}
    for title, gid in meta:
        if gid is None:
            continue
        csv_url = "https://docs.google.com/spreadsheets/d/" + sid + "/export?format=csv&gid=" + str(gid)
        try:
            df = pd.read_csv(csv_url, header=None, dtype=object, na_filter=False)
        except Exception as e:
            print(f"[WARN] Failed CSV read for '{title}' (gid={gid}): {e}")
            continue
        rows = []
        for _, row in df.iterrows():
            d, p = row_to_device_and_price(row.tolist())
            if d is not None:
                rows.append({"device": d, "display": clean_name(d), "price": float(p)})
        if rows:
            data[title] = rows
        print(f"[GS] {title}: total={len(df)} kept={len(rows)} skipped={len(df)-len(rows)}")
    if not data:
        print("[FATAL] No rows found in any visible tabs.")
        return None
    return data
# =============================================================================
ENGINE_PIN = os.getenv("ENGINE_PIN", "1337")
PIN_SHA256 = hashlib.sha256(ENGINE_PIN.encode("utf-8")).hexdigest()

# Back-compat defaults (percent only). JS accepts both % and $ rules.
DEFAULT_RULES = {
    "1-100": 30,
    "101-220": 20,
    "221-300": 15,
    "301-469": 13,
    "470+": 10
}

# Default buckets (editable in Engine Room; saved as cfg.buckets).
PRICE_BUCKETS = [
    ("1-100",       1,   100),
    ("101-220",     101, 220),
    ("221-300",     221, 300),
    ("301-469",     301, 469),
    ("470+",        470, float("inf")),
]

SOURCE_NAME_BASE = "source"
SUP_EXTS = [".xlsx", ".xls", ".xlsm", ".xlsb", ".ods", ".csv"]

def find_source_file(cwd: Path) -> Path:
    # prefers files literally named "source.*" in current directory
    for p in cwd.iterdir():
        if p.is_file() and p.stem.lower() == SOURCE_NAME_BASE and p.suffix.lower() in SUP_EXTS:
            return p
    fallback = cwd / "source.xlsx"
    if fallback.exists():
        return fallback
    raise FileNotFoundError(
        "Couldn't find a file named 'source' with a supported extension in the current directory.\n"
        "Supported: .xlsx .xls .xlsm .xlsb .ods .csv"
    )

def first_text(cells):
    for v in cells:
        if isinstance(v, str):
            s = v.strip()
            if s:
                return s
    return None

def first_number(cells):
    for v in cells:
        if isinstance(v, (int, float)) and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            return float(v)
        if isinstance(v, str):
            sv = v.strip().replace(",", "")
            # exact number
            if re.fullmatch(r"-?\d+(?:\.\d+)?", sv):
                try:
                    return float(sv)
                except Exception:
                    pass
            # tolerant: mixed text like "$410", "410-"
            m = re.search(r"-?\d+(?:\.\d+)?", sv)
            if m:
                try:
                    return float(m.group(0))
                except Exception:
                    pass
    return None


HEADER_WORDS = {
    "device","price","prices","model","storage","color",
    "note","notes","sealed","open","active","natural",
    "locked carrier","activation status"
}

def looks_like_header_or_junk(text: str) -> bool:
    t = text.strip().lower()
    if not t or len(t) <= 1:
        return True
    if t in HEADER_WORDS:
        return True
    if any(x in t for x in ["header","total","sum of","subtotal","grand total","file","page","tab","sheet"]):
        return True
    # too many non-alphanumerics
    if len(re.sub(r"[A-Za-z0-9]", "", t)) > len(t) * 0.6:
        return True
    return False

def clean_name(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    s = text.strip()
    s = re.sub(r'(\d+)\s*\\"', r'\1-inch', s)
    s = re.sub(r'\b(\d+)\s*-\s*inch\b', r'\1-inch', s, flags=re.I)
    s = re.sub(r'\b(\d+)\s*inch(es)?\b', r'\1-inch', s, flags=re.I)
    s = re.sub(r'\b([2-6])\s*g\b', lambda m: m.group(1) + 'G', s, flags=re.I)
    s = re.sub(r'\b([2-6])g\b', lambda m: m.group(1) + 'G', s, flags=re.I)
    s = re.sub(r'\b(\d+)\s*(gb|g|gig|gigs)\b', r'\1GB', s, flags=re.I)
    s = re.sub(r'\b(\d+)\s*(tb|t|terabyte|terabytes)\b', r'\1TB', s, flags=re.I)
    s = re.sub(r'[–—]+', '-', s)
    s = re.sub(r'\s*-\s*', '-', s)
    s = re.sub(r'[^A-Za-z0-9\-+/# ]+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    def smart_cap(word: str) -> str:
        wl = word.lower()
        keep_upper = {'GB','TB','5G','4G','3G','LTE','SE','XR','XS','S','FE','Z','UHD'}
        exact = {'iPhone':'iPhone','iPad':'iPad','iMac':'iMac','iPod':'iPod','MacBook':'MacBook','AirPods':'AirPods',
                 'Galaxy':'Galaxy','Note':'Note','Watch':'Watch','Ultra':'Ultra','Pro':'Pro','Max':'Max','Plus':'Plus','Mini':'Mini'}
        if wl.upper() in keep_upper: return wl.upper()
        if re.match(r'^\d', word): return word
        for k,v in exact.items():
            if wl == k.lower(): return v
        return word.capitalize()
    parts = s.split(' ')
    s = ' '.join(smart_cap(w) for w in parts)
    s = re.sub(r'(\d+)-Inch', r'\1-inch', s)
    return s

def row_to_device_and_price(row):
    cells = list(row)
    dev = first_text(cells)
    price = first_number(cells[1:]) if dev is not None else None
    if dev is None or price is None:
        return (None, None)
    if looks_like_header_or_junk(dev):
        return (None, None)
    if price <= 0:
        return (None, None)
    return (dev.strip(), float(price))

def load_workbook_tables(src: Path):
    data = {}
    if src.suffix.lower() == ".csv":
        df = pd.read_csv(src, header=None, dtype=object, na_filter=False)
        rows = []
        for _, row in df.iterrows():
            d, p = row_to_device_and_price(row.tolist())
            if d is not None:
                rows.append({"device": d, "display": clean_name(d), "price": float(p)})
        if rows:
            data[src.stem] = rows
    else:
        xls = pd.ExcelFile(src)
        for sheet_name in xls.sheet_names:
            df = pd.read_excel(src, sheet_name=sheet_name, header=None, dtype=object, na_filter=False)
            rows = []
            for _, row in df.iterrows():
                d, p = row_to_device_and_price(row.tolist())
                if d is not None:
                    rows.append({"device": d, "display": clean_name(d), "price": float(p)})
            if rows:
                data[sheet_name] = rows
    if not data:
        raise ValueError("No valid device/price rows found. Check the 'source' file format.")
    return data

def build_html(data: dict, pin_sha256: str, defaults: dict, out_path: Path):
    dataset_js = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    defaults_js = json.dumps(defaults, separators=(",", ":"), ensure_ascii=False)
    price_buckets_js = json.dumps(PRICE_BUCKETS)

    html = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Allens List</title>
<style>
:root{
  --bg:#050708; --panel:#0a0f0b; --panel-2:#0f1611; --accent:#00ff9c; --accent-2:#33ffb7;
  --text:#d7ffe7; --muted:#72f7c6; --line:#10331f; --danger:#ff3b3b;
  --font-body: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
}
/* 20 themes */
body[data-theme="midnight-glass"]{ --bg:#0b1220; --panel:rgba(255,255,255,0.06); --panel-2:rgba(255,255,255,0.08); --accent:#60a5fa; --accent-2:#93c5fd; --text:#e7efff; --muted:#9db3d9; --line:rgba(148,163,184,0.25); --danger:#f87171; --font-body: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, "SF Pro Text", Arial; }
body[data-theme="clean-pro"]{ --bg:#0b0e11; --panel:#0f1419; --panel-2:#121922; --accent:#22d3ee; --accent-2:#67e8f9; --text:#e6f7ff; --muted:#9ad1de; --line:#1b2632; --danger:#f97373; --font-body: Inter, ui-sans-serif, system-ui; }
body[data-theme="ivory-gold-luxe"]{ --bg:#0e0e0c; --panel:rgba(255,255,240,0.06); --panel-2:rgba(255,255,240,0.1); --accent:#f5d483; --accent-2:#ffe49b; --text:#fff8e7; --muted:#e6d6b3; --line:rgba(245,212,131,0.25); --danger:#ff8b73; --font-body: "SF Pro Text", ui-sans-serif; }
body[data-theme="carbon-neon"]{ --bg:#0a0a0a; --panel:#121212; --panel-2:#171717; --accent:#22ff88; --accent-2:#55ff9f; --text:#d9fbe7; --muted:#9bf2c3; --line:#1e3626; --danger:#ff5a5a; --font-body:"JetBrains Mono",ui-monospace; }
body[data-theme="deep-purple"]{ --bg:#0d0716; --panel:#150d22; --panel-2:#1b1230; --accent:#a78bfa; --accent-2:#c4b5fd; --text:#efe9ff; --muted:#b9a9f3; --line:#2a1c46; --danger:#ff6b6b; --font-body:"Inter",ui-sans-serif; }
body[data-theme="forest-emerald"]{ --bg:#071410; --panel:#0b1a15; --panel-2:#0f241d; --accent:#10b981; --accent-2:#34d399; --text:#dbfff0; --muted:#94dbc0; --line:#173a2e; --danger:#f87171; --font-body:"Inter",ui-sans-serif; }
body[data-theme="rose-quartz"]{ --bg:#140d11; --panel:#1b1318; --panel-2:#251820; --accent:#fb7185; --accent-2:#fda4af; --text:#ffe6eb; --muted:#f2b9c1; --line:#3a1f2a; --danger:#ff9393; --font-body:"Inter",ui-sans-serif; }
body[data-theme="sunset-gold"]{ --bg:#120e09; --panel:#1a140d; --panel-2:#21190f; --accent:#f59e0b; --accent-2:#fbbf24; --text:#fff2d9; --muted:#e9c89a; --line:#3a2a11; --danger:#ff7a7a; --font-body:"Inter",ui-sans-serif; }
body[data-theme="ocean-breeze"]{ --bg:#07131a; --panel:#0a1a24; --panel-2:#0e2430; --accent:#38bdf8; --accent-2:#7dd3fc; --text:#e6f7ff; --muted:#a7d7f3; --line:#103245; --danger:#ff6b6b; --font-body:"Inter",ui-sans-serif; }
body[data-theme="matte-black"]{ --bg:#0a0b0c; --panel:#0f1012; --panel-2:#131417; --accent:#9ca3af; --accent-2:#c7cbd1; --text:#e5e7eb; --muted:#aeb3bb; --line:#1c1e22; --danger:#ef4444; --font-body:"SF Pro Text",ui-sans-serif; }
body[data-theme="slate-gray"]{ --bg:#0c0f12; --panel:#12161a; --panel-2:#171c21; --accent:#94a3b8; --accent-2:#cbd5e1; --text:#e2e8f0; --muted:#a8b0bd; --line:#1f2831; --danger:#f87171; --font-body:"Inter",ui-sans-serif; }
body[data-theme="mocha"]{ --bg:#110c09; --panel:#1a130f; --panel-2:#221912; --accent:#d6a676; --accent-2:#edc9a3; --text:#fff1e2; --muted:#cfbaaa; --line:#3a2a1d; --danger:#ff7b6e; --font-body:"Inter",ui-sans-serif; }
body[data-theme="arctic-ice"]{ --bg:#0b1014; --panel:rgba(255,255,255,0.05); --panel-2:rgba(255,255,255,0.08); --accent:#67e8f9; --accent-2:#a5f3fc; --text:#e6fbff; --muted:#b7e8f2; --line:rgba(135,206,235,0.28); --danger:#fb7185; --font-body:system-ui,-apple-system,Segoe UI,Roboto; }
body[data-theme="neon-pop"]{ --bg:#070b0f; --panel:#0c1218; --panel-2:#111a22; --accent:#22d3ee; --accent-2:#a78bfa; --text:#e5faff; --muted:#b8d4ff; --line:#162230; --danger:#ff5a89; --font-body:"JetBrains Mono",ui-monospace; }
body[data-theme="royal-indigo"]{ --bg:#0a0a1a; --panel:#100f26; --panel-2:#151232; --accent:#7c3aed; --accent-2:#a78bfa; --text:#ecebff; --muted:#b9b4f2; --line:#251d46; --danger:#ff6b7a; --font-body:Inter,ui-sans-serif; }
body[data-theme="copper-sand"]{ --bg:#0f0d0b; --panel:#17130f; --panel-2:#1f1a13; --accent:#e07a5f; --accent-2:#f2cc8f; --text:#fff3e6; --muted:#ddc3a6; --line:#2a2219; --danger:#ff7a7a; --font-body:Inter,ui-sans-serif; }
body[data-theme="aurora"]{ --bg:#0b0e1a; --panel:#11162a; --panel-2:#151c36; --accent:#34d399; --accent-2:#60a5fa; --text:#e9faff; --muted:#b0d4ff; --line:#1a2a4d; --danger:#fb7185; --font-body:Inter,ui-sans-serif; }
body[data-theme="jade-mist"]{ --bg:#081311; --panel:#0d1b18; --panel-2:#10241f; --accent:#2dd4bf; --accent-2:#99f6e4; --text:#dcfffb; --muted:#a6e9df; --line:#133a34; --danger:#f87171; --font-body:Inter,ui-sans-serif; }
body[data-theme="obsidian"]{ --bg:#070707; --panel:#0c0c0c; --panel-2:#121212; --accent:#22c55e; --accent-2:#86efac; --text:#f1f5f9; --muted:#9aa6b2; --line:#1c1c1c; --danger:#ef4444; --font-body:"JetBrains Mono",ui-monospace; }
body[data-theme="paper-white"]{ --bg:#0c0d0f; --panel:rgba(255,255,255,0.06); --panel-2:rgba(255,255,255,0.1); --accent:#ffffff; --accent-2:#e5e7eb; --text:#f8fafc; --muted:#cbd5e1; --line:rgba(255,255,255,0.18); --danger:#f87171; --font-body:"SF Pro Text",ui-sans-serif; }

body[data-theme] header, body[data-theme] .item, body[data-theme] .engine { backdrop-filter: blur(10px) saturate(140%); -webkit-backdrop-filter: blur(10px) saturate(140%); border:1px solid var(--line); }
body[data-theme] h1{ text-shadow:0 0 18px color-mix(in oklab, var(--accent) 35%, transparent); }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family: var(--font-body, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace); }
header { position:sticky; top:0; z-index:20; background:linear-gradient(180deg,rgba(0,0,0,.24),rgba(0,0,0,.15)); border-bottom:1px solid var(--line); padding:10px 14px; display:flex; align-items:center; gap:10px; }
h1 { font-size:18px; margin:0; letter-spacing:1px; color:var(--accent); cursor:pointer; }
h1:focus-visible{ outline:2px solid var(--accent); border-radius:6px; }
header .btn { display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:10px; border:1px solid var(--line); background:var(--panel-2); color:var(--muted); text-decoration:none; font-size:14px; height:36px; }
header .btn:hover { color:var(--text); border-color:var(--accent); }
.phone { margin-left:auto; } .tg { margin-left:8px; } .tg-icon { display:block; }
.pages { position:sticky; top:50px; z-index:15; padding:8px 10px; background:var(--panel); border-bottom:1px solid var(--line); }
.pages select { width:100%; padding:10px 12px; border-radius:10px; border:1px solid var(--line); background:var(--panel-2); color:var(--text); font-size:14px; }
.search { display:flex; gap:8px; padding:10px; background:var(--panel); border-bottom:1px solid var(--line); position:sticky; top:100px; z-index:14; }
.search input { flex:1; padding:12px 14px; border-radius:10px; border:1px solid var(--line); background:var(--panel-2); color:var(--text); font-size:16px; outline:none; }
.container { padding: 10px; } .section { display:none; } .section.active { display:block; }
.list { display:flex; flex-direction:column; gap:8px; }
.item { background:var(--panel-2); border:1px solid var(--line); border-radius:12px; padding:10px 12px; display:grid; grid-template-columns:1fr auto; gap:10px; align-items:center; }
.item .name { font-size:15px; letter-spacing:.2px;} .item .price { font-weight:700; font-size:15px; color:var(--accent-2); text-shadow:0 0 6px rgba(0,0,0,.25);}
.small { font-size:12px; opacity:.7; }
.engine-overlay { position:fixed; inset:0; background:rgba(0,0,0,.6); display:none; align-items:flex-start; justify-content:center; z-index:100; overflow:auto; padding:20px 8px; }
.engine { width:min(900px,95vw); background:var(--panel); border:1px solid var(--accent); border-radius:14px; padding:14px; box-shadow:0 0 24px rgba(0,0,0,.25); max-height:90vh; overflow:auto; }
.engine h2 { margin:0 0 6px; color:var(--accent); font-size:18px; }
.engine .row { display:grid; grid-template-columns: 1fr 1fr 1fr 1fr 1fr; gap:8px; margin:8px 0; }
.engine label { font-size:12px; color:var(--muted); }
.engine input[type="number"], .engine input[type="text"]{ width:100%; padding:8px; border-radius:8px; border:1px solid var(--line); background:var(--panel-2); color:var(--text); }
.engine .sheet-block { border:1px dashed var(--line); border-radius:12px; padding:12px; margin:10px 0; }
.engine .actions { display:flex; gap:8px; justify-content:flex-end; position:sticky; bottom:0; background:var(--panel); padding-top:8px; margin-top:8px; }
.engine button { padding:8px 12px; border-radius:10px; border:1px solid var(--line); background:var(--panel-2); color:var(--text); cursor:pointer; }
.engine .danger { border-color:var(--danger); color:#fff; }
.engine .pin { display:flex; gap:8px; align-items:center; margin:8px 0; } .engine .pin input { flex:1; } .engine .pin .ok { border-color:var(--accent); }
footer { padding:28px 14px; color:var(--muted); opacity:.75; }
@media (max-width:600px) { .engine .row { grid-template-columns:1fr 1fr; } }

/* === Mobile opacity fix for sticky bars === */
.pages { background: var(--bg) !important; z-index: 1001 !important; box-shadow: 0 6px 12px rgba(0,0,0,.15) !important; }
.search { background: var(--bg) !important; z-index: 1000 !important; box-shadow: 0 6px 12px rgba(0,0,0,.12) !important; }
.pages select, .search input { background-color: var(--panel-2) !important; -webkit-backdrop-filter: none !important; backdrop-filter: none !important; }
.pages, .search { position: sticky; transform: translateZ(0); }

</style>

    <style>
      .search-wrapper,
      .search-bar,
      .filters,
      .dropdown,
      .select,
      .combo {
        position: relative;
        z-index: 100;
      }
      .engine-overlay {
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.7);
        backdrop-filter: blur(2px);
        z-index: 10000;
        display: none;
      }
      .engine-modal {
        position: fixed;
        top: 50%;
        left: 50%;
        transform: translate(-50%, -50%);
        max-width: 520px;
        width: calc(100% - 32px);
        background: #0b0f0b;
        border: 1px solid #1aff6c55;
        border-radius: 16px;
        padding: 20px 18px;
        z-index: 10001;
        box-shadow: 0 10px 40px rgba(0,0,0,0.6);
      }
      .engine-modal h3 {
        color: #1aff6c;
        margin: 0 0 8px;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      }
      .engine-modal input[type="password"] {
        width: 100%;
        background: #0c120c;
        color: #e6ffe6;
        border: 1px solid #1aff6c55;
        border-radius: 10px;
        padding: 10px 12px;
        outline: none;
      }
      .engine-modal .row {
        display: flex;
        gap: 8px;
        margin-top: 12px;
        justify-content: flex-end;
      }
      .btn {
        border: 1px solid #1aff6c88;
        background: #0b140b;
        color: #caffca;
        border-radius: 10px;
        padding: 8px 12px;
        cursor: pointer;
      }
      .btn:hover { border-color: #1aff6c; color: #e9ffe9; }
    </style>
    
</head>
<body data-theme="midnight-glass">
<header>
  <h1 id="titleBtn" role="button" tabindex="0" aria-label="Open Engine Room">Allens List</h1>
  <a class="btn phone" href="tel:13135392838">(313) 539-2838</a>
  <a class="btn tg" href="https://t.me/apple_pays" target="_blank" rel="noopener" aria-label="Telegram channel">
    <svg class="tg-icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <path d="M21.5 3.5L2.7 10.8c-.8.3-.8 1.4 0 1.6l4.8 1.5 1.8 5.6c.2.8 1.2.9 1.7.3l2.6-2.7 4.8 3.6c.6.4 1.5.1 1.7-.6l3.2-15.2c.2-.9-.6-1.6-1.4-1.4zM8.4 12.9l9.5-6.1c.2-.1.4.2.2.3l-7.7 7.2c-.2.2-.3.4-.3.6l-.3 2.1c0 .2-.4.2-.4 0L8.4 12.9z" fill="currentColor"/>
    </svg>
  </a>
</header>
<div class="pages"><select id="sheetSelect"></select></div>
<div class="search">
  <input id="search" placeholder="Search devices (e.g. '15 Pro 256')">
</div>
<div class="container" id="sections"></div>

<div class="engine-overlay" id="engineOverlay" aria-hidden="true">
  <div class="engine">
    <h2>Engine Room</h2>
    <div class="pin">
      <label for="pinInput">Enter PIN to unlock settings</label>
    </div>
    <div class="pin">
      <input id="pinInput" type="password" placeholder="PIN">
      <button id="pinCheck" class="ok">Unlock</button>
      <button id="pinClose">Close</button>
    </div>
    <div id="engineBody" style="display:none;">
      <!-- THEME: dropdown only -->
      <div class="sheet-block">
        <label for="themeSelect"><b>Theme</b></label>
        <select id="themeSelect" style="margin-left:8px; padding:6px; border-radius:8px; background:var(--panel-2); color:var(--text); border:1px solid var(--line);"></select>
      </div>

      <!-- BUCKETS: editable ranges -->
      <div id="bucketsBlock" class="sheet-block">
        <label><b>Price Buckets</b> (label + range). These control which %/$ rules apply.</label>
        <div class="row" id="bucketsRow"></div>
        <p class="small">Tip: Ensure ranges don't overlap and cover your typical prices.</p>
      </div>

      <p class="small">Set discounts per bucket. Leave blank to fallback to defaults.</p>
      <div id="defaultsBlock" class="sheet-block">
        <label><b>Default Rules</b> (% off and/or $ off)</label>
        <div class="row" id="defaultsRow"></div>
      </div>

      <div id="sheetsBlock"></div>

      <div class="actions">
        <button id="saveEngine">💾 Save</button>
        <button id="exportConfig">⬇️ Export Config</button>
        <button id="resetEngine" class="danger">Reset to Defaults</button>
        <button id="closeEngine">Done</button>
      </div>
    </div>
  </div>
</div>

<footer class="small">
  We do not buy stolen or unlawfully obtained devices.
</footer>

<script>
// ==== DATA ====
const DATASETS = __DATASETS__;
const DEFAULT_RULES = __DEFAULTS__; // numeric % (back-compat)
const DEFAULT_BUCKETS = __PRICE_BUCKETS__; // [[label, lo, hi], ...]
const PIN_SHA256 = "__PIN_SHA256__";

// ==== THEMES ====
const THEMES = [
  "midnight-glass","clean-pro","ivory-gold-luxe","carbon-neon","deep-purple",
  "forest-emerald","rose-quartz","sunset-gold","ocean-breeze","matte-black",
  "slate-gray","mocha","arctic-ice","neon-pop","royal-indigo",
  "copper-sand","aurora","jade-mist","obsidian","paper-white"
];
function applyTheme(name){ if(!THEMES.includes(name)) name="midnight-glass"; document.body.setAttribute("data-theme", name); localStorage.setItem("uiThemeV1", name); }
function loadTheme(){ const t=localStorage.getItem("uiThemeV1")||"midnight-glass"; applyTheme(t); }

// ==== CONFIG ====
function getEngineConfig(){
  const raw = localStorage.getItem('engineConfigV1');
  if(!raw) return {defaults: DEFAULT_RULES, perSheet: {}, buckets: DEFAULT_BUCKETS};
  try{
    const cfg = JSON.parse(raw);
    cfg.defaults = cfg.defaults ?? DEFAULT_RULES;
    cfg.perSheet = cfg.perSheet ?? {};
    cfg.buckets = cfg.buckets ?? DEFAULT_BUCKETS;
    return cfg;
  }catch(e){
    return {defaults: DEFAULT_RULES, perSheet: {}, buckets: DEFAULT_BUCKETS};
  }
}
function setEngineConfig(cfg){ localStorage.setItem('engineConfigV1', JSON.stringify(cfg)); }
function downloadConfigFile(cfg){ const blob=new Blob([JSON.stringify(cfg,null,2)],{type:'application/json'}); const url=URL.createObjectURL(blob); const a=document.createElement('a'); a.href=url; a.download='config.json'; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url); }

// Normalize rules: number => {pct:number, flat:0}; object => {pct,flat}
function asRuleObj(v){
  if(v==null) return {pct:null, flat:null};
  if(typeof v === 'number') return {pct:v, flat:null};
  if(typeof v === 'object'){
    const pct = (v.pct===''||v.pct==null) ? null : Number(v.pct);
    const flat = (v.flat===''||v.flat==null) ? null : Number(v.flat);
    return {pct, flat};
  }
  return {pct:null, flat:null};
}

// ==== SEARCH / FUZZY ====
function normText(s){ if(!s) return ''; s=String(s).toLowerCase(); s=s.replace(/["”]/g,' inch '); s=s.replace(/\b(\d+)\s*-\s*inch\b/g,'$1 inch'); s=s.replace(/\b(\d+)\s*inch(es)?\b/g,'$1 inch'); s=s.replace(/-/g,' '); s=s.replace(/\b([2-6])\s*g\b/g,'$1g'); s=s.replace(/[^a-z0-9\. ]+/g,' '); s=s.replace(/\s+/g,' ').trim(); return s; }
function buildSearchBlob(name, sheetName){ const base=normText(name); const stripped=base.replace(/\b(ipad|iphone|apple|samsung)\b/g,'').replace(/\s+/g,' ').trim(); let sheetPart=''; if(sheetName){ sheetPart=' '+normText(String(sheetName)); } return (base+' '+stripped+sheetPart).trim(); }
function levenshtein(a,b){ a=a||''; b=b||''; const m=a.length,n=b.length; if(a===b)return 0; if(!m)return n; if(!n)return m; const dp=new Array(n+1); for(let j=0;j<=n;j++) dp[j]=j; for(let i=1;i<=m;i++){ let prev=dp[0]; dp[0]=i; for(let j=1;j<=n;j++){ const tmp=dp[j]; const cost=a[i-1]===b[j-1]?0:1; dp[j]=Math.min(dp[j]+1,dp[j-1]+1,prev+cost); prev=tmp; } } return dp[n]; }
function fuzzyTokenMatch(token, hayTokens){ if(!token) return true; const t=token; for(const h of hayTokens){ if(h.includes(t)) return true; } const maxEd=t.length>=6?2:1; for(const h of hayTokens){ if(Math.abs(h.length-t.length)>3) continue; if(levenshtein(t,h)<=maxEd) return true; } return false; }
function matchesQuery(row, q){ if(!q) return true; const hay=row.search; const nq=normText(q); const tokens=nq.split(' ').filter(Boolean); if(!tokens.length)return true; const hayTokens=hay.split(' ').filter(Boolean); for(const tok of tokens){ if(!fuzzyTokenMatch(tok, hayTokens)) return false; } return true; }
function extractNums(tokens){ const out=[]; for(const t of tokens){ const m=t.match(/^\d+(?:\.\d+)?$/); if(m){ out.push(parseFloat(m[0])); } } return out; }
function rankScore(row, q){
  const hay=row.search||''; const nq=normText(q||''); if(!nq) return 0;
  const tokens=nq.split(' ').filter(Boolean); const hayTokens=hay.split(' ').filter(Boolean);
  let score=0;
  if(hay.includes(nq)) score+=40;
  for(let i=0;i<tokens.length-1;i++){ const phrase=tokens[i]+' '+tokens[i+1]; if(hay.includes(phrase)) score+=12; }
  for(const tok of tokens){
    let best=0;
    for(const h of hayTokens){
      if(h===tok){ best=Math.max(best,10); continue; }
      if(h.startsWith(tok)||h.includes(tok)){ best=Math.max(best,6); continue; }
      const maxEd=tok.length>=6?2:1;
      if(Math.abs(h.length-tok.length)<=3&&levenshtein(tok,h)<=maxEd){ best=Math.max(best,3); }
    }
    score+=best;
  }
  const qNums=extractNums(tokens); const hNums=extractNums(hayTokens);
  if(qNums.length&&hNums.length){
    let bestDelta=Infinity;
    for(const qn of qNums){ for(const hn of hNums){ const d=Math.abs(hn-qn); if(d<bestDelta) bestDelta=d; } }
    if(bestDelta===0) score+=25; else if(bestDelta<=1) score+=10; else if(bestDelta<=2) score+=5; else score-=bestDelta;
  }
  score-=Math.max(0,hayTokens.length-tokens.length)*0.25;
  return score;
}

// ==== Rounding rule ====
function roundPrice(n){
  const x = Number(n);
  if (!isFinite(x)) return '';
  // Always round to nearest $10 first
  let p = Math.round(x/10)*10;
  // If above $340, avoid 10/90 endings
  if (p > 340) {
    const last = ((p % 100) + 100) % 100; // positive modulo
    if (last === 10) p -= 10;
    else if (last === 90) p += 10;
  }
  return p;
}

// ==== Buckets + rules ====
function activeBuckets(){
  const cfg=getEngineConfig();
  const b = Array.isArray(cfg.buckets) ? cfg.buckets : DEFAULT_BUCKETS;
  return b.map(arr => ({ label: String(arr[0]), lo: Number(arr[1]), hi: (arr[2]===Infinity ? Infinity : Number(arr[2])) }));
}

function resolveRule(label, sheetName){
  const cfg=getEngineConfig();
  const page = (cfg.perSheet && cfg.perSheet[sheetName]) ? cfg.perSheet[sheetName] : {};
  const vPage = page ? page[label] : null;
  const vDef = cfg.defaults ? cfg.defaults[label] : null;
  const rulePage = asRuleObj(vPage);
  const ruleDef  = asRuleObj(vDef);
  // fallback to DEFAULT_RULES numeric if both empty
  if(rulePage.pct==null && rulePage.flat==null && ruleDef.pct==null && ruleDef.flat==null){
    const num = (DEFAULT_RULES[label]!==undefined) ? Number(DEFAULT_RULES[label]) : 0;
    return {pct:num, flat:null};
  }
  // prefer page overrides if provided, else defaults
  return {
    pct: (rulePage.pct!=null ? rulePage.pct : ruleDef.pct),
    flat: (rulePage.flat!=null ? rulePage.flat : ruleDef.flat)
  };
}

function finalPriceFor(sheetName, base){
  const bucks = activeBuckets();
  for(const b of bucks){
    if(base>=b.lo && base<=b.hi){
      const rule = resolveRule(b.label, sheetName);
      const pct = Number(rule.pct||0);
      const flat = Number(rule.flat||0);
      let p = base * (1 - pct/100);
      p = p - flat;
      if(p < 0) p = 0;
      return roundPrice(p);
    }
  }
  return roundPrice(base);
}

// ==== PREP ====
let PREPARED=null; let activeSheet=Object.keys(DATASETS)[0]||null;
const sheetSelect=document.getElementById('sheetSelect'); const sectionsEl=document.getElementById('sections'); const searchEl=document.getElementById('search');
function prepareIndex(){ if(PREPARED) return; PREPARED={}; Object.entries(DATASETS).forEach(([sheet, rows])=>{ PREPARED[sheet]=rows.map(r=>({ device:r.device, price:r.price, search:buildSearchBlob((r.display||r.device), sheet) })); }); }

function render(){
  prepareIndex();
  // pages dropdown
  sheetSelect.innerHTML='';
  Object.keys(DATASETS).forEach(name=>{ const opt=document.createElement('option'); opt.value=name; opt.textContent=name; if(!activeSheet) activeSheet=name; if(name===activeSheet) opt.selected=true; sheetSelect.appendChild(opt); });
  // sections
  sectionsEl.innerHTML='';
  Object.entries(PREPARED).forEach(([name,rows])=>{
    const sec=document.createElement('section'); sec.className='section'+(name===activeSheet?' active':'');
    const list=document.createElement('div'); list.className='list';
    const q=searchEl.value||'';
    const matches=[];
    rows.forEach((r, idx)=>{
      const dev=String(r.device||r.display);
      const base=Number(r.price);
      if(!dev||isNaN(base)) return;
      if(!matchesQuery(r,q)) return;
      const sc=q?rankScore(r,q):0;
      const fp=finalPriceFor(name, base);
      matches.push({idx, dev, price:fp, sc});
    });
    matches.sort((a,b)=> (b.sc - a.sc) || (a.idx - b.idx));
    matches.forEach(m=>{
      const item=document.createElement('div'); item.className='item';
      const nameEl=document.createElement('div'); nameEl.className='name'; nameEl.textContent=m.dev;
      const priceEl=document.createElement('div'); priceEl.className='price'; priceEl.textContent=String(m.price);
      const right=document.createElement('div'); right.style.textAlign='right'; right.appendChild(priceEl);
      item.appendChild(nameEl); item.appendChild(right); list.appendChild(item);
    });
    sec.appendChild(list); sectionsEl.appendChild(sec);
  });
}

searchEl.addEventListener('input', render);
sheetSelect.addEventListener('change', (e)=>{ activeSheet=e.target.value; render(); });

// ==== Engine Room ====
const overlay=document.getElementById('engineOverlay'); const closeBtn=document.getElementById('closeEngine'); const pinClose=document.getElementById('pinClose'); const pinInput=document.getElementById('pinInput'); const pinCheck=document.getElementById('pinCheck'); const engineBody=document.getElementById('engineBody');
const defaultsRow=document.getElementById('defaultsRow'); const sheetsBlock=document.getElementById('sheetsBlock'); const saveBtn=document.getElementById('saveEngine'); const resetBtn=document.getElementById('resetEngine'); const exportBtn=document.getElementById('exportConfig'); const titleBtn=document.getElementById('titleBtn'); const themeSelect=document.getElementById('themeSelect'); const bucketsRow=document.getElementById('bucketsRow');

function openEngine(){ overlay.style.display='flex'; pinInput.value=''; engineBody.style.display='none'; }
function closeEngine(){ overlay.style.display='none'; }
document.addEventListener('keydown', (e)=>{ if(e.ctrlKey && (e.key==='e'||e.key==='E')){ e.preventDefault(); openEngine(); }});
titleBtn.addEventListener('click', openEngine);
titleBtn.addEventListener('keydown', (e)=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); openEngine(); }});
closeBtn.addEventListener('click', closeEngine); pinClose.addEventListener('click', closeEngine);

async function hashSHA256(str){ const enc=new TextEncoder().encode(str); const buf=await crypto.subtle.digest('SHA-256',enc); return [...new Uint8Array(buf)].map(b=>b.toString(16).padStart(2,'0')).join(''); }
pinCheck.addEventListener('click', async ()=>{ const h=await hashSHA256(pinInput.value||''); if(h===PIN_SHA256){ buildEngineUI(); engineBody.style.display='block'; } else { alert('Wrong PIN'); }});

// === Engine UI builders ===
const THEMES_LIST = THEMES;
function buildThemeUI(){
  themeSelect.innerHTML='';
  THEMES_LIST.forEach(t=>{ const opt=document.createElement('option'); opt.value=t; opt.textContent=t.replace(/-/g,' '); themeSelect.appendChild(opt); });
  const cur=document.body.getAttribute('data-theme')||'midnight-glass'; themeSelect.value=cur;
  themeSelect.addEventListener('change', (e)=>{ applyTheme(e.target.value); });
}

function buildBucketsUI(){
  const bucks = activeBuckets();
  bucketsRow.innerHTML='';
  bucks.forEach((b,i)=>{
    const wrap=document.createElement('div'); wrap.style.display='grid'; wrap.style.gridTemplateColumns='1fr 1fr 1fr'; wrap.style.gap='6px';
    const lab=document.createElement('input'); lab.type='text'; lab.value=b.label; lab.placeholder='Label'; lab.dataset.idx=i; lab.dataset.field='label';
    const lo=document.createElement('input'); lo.type='number'; lo.value=b.lo; lo.placeholder='Low'; lo.dataset.idx=i; lo.dataset.field='lo';
    const hi=document.createElement('input'); hi.type='number'; hi.value=(b.hi===Infinity? 999999 : b.hi); hi.placeholder='High'; hi.dataset.idx=i; hi.dataset.field='hi';
    wrap.appendChild(lab); wrap.appendChild(lo); wrap.appendChild(hi);
    bucketsRow.appendChild(wrap);
  });
  // add row button
  const add = document.createElement('button'); add.textContent = 'Add Bucket'; add.style.marginTop='8px';
  add.addEventListener('click', ()=>{
    const wrap=document.createElement('div'); wrap.style.display='grid'; wrap.style.gridTemplateColumns='1fr 1fr 1fr'; wrap.style.gap='6px';
    const lab=document.createElement('input'); lab.type='text'; lab.placeholder='Label';
    const lo=document.createElement('input'); lo.type='number'; lo.placeholder='Low';
    const hi=document.createElement('input'); hi.type='number'; hi.placeholder='High';
    wrap.appendChild(lab); wrap.appendChild(lo); wrap.appendChild(hi);
    bucketsRow.appendChild(wrap);
  });
  bucketsRow.parentElement.appendChild(add);
}

function makeRuleRow(label, scope, sheetName){
  const wrap=document.createElement('div'); wrap.className='row';
  const lab=document.createElement('label'); lab.textContent=label+' (% / $)';
  const pct=document.createElement('input'); pct.type='number'; pct.min='0'; pct.step='1'; pct.placeholder='%'; pct.dataset.type='pct';
  const flat=document.createElement('input'); flat.type='number'; flat.min='0'; flat.step='1'; flat.placeholder='$'; flat.dataset.type='flat';
  pct.dataset.key=label; flat.dataset.key=label; pct.dataset.scope=scope; flat.dataset.scope=scope;
  if(sheetName){ pct.dataset.sheet=sheetName; flat.dataset.sheet=sheetName; }
  const cfg=getEngineConfig(); const src = (scope==='defaults' ? cfg.defaults?.[label] : (cfg.perSheet?.[sheetName]?.[label]));
  const rule=asRuleObj(src);
  pct.value = (rule.pct==null || rule.pct===0) ? '' : rule.pct;
  flat.value = (rule.flat==null || rule.flat===0) ? '' : rule.flat;
  wrap.appendChild(lab); wrap.appendChild(pct); wrap.appendChild(flat);
  return wrap;
}

function buildRulesUI(){
  // defaults
  const bucks = activeBuckets();
  defaultsRow.innerHTML='';
  bucks.forEach(b => { defaultsRow.appendChild(makeRuleRow(b.label, 'defaults', null)); });

  // per-sheet
  sheetsBlock.innerHTML='';
  Object.keys(DATASETS).forEach(sheetName=>{
    const block=document.createElement('div'); block.className='sheet-block';
    const title=document.createElement('div'); title.style.marginBottom='6px'; title.innerHTML='<b>'+sheetName+'</b>';
    block.appendChild(title);
    const holder=document.createElement('div'); holder.id='rules-'+sheetName;
    bucks.forEach(b => { holder.appendChild(makeRuleRow(b.label, 'sheet', sheetName)); });
    block.appendChild(holder);
    sheetsBlock.appendChild(block);
  });
}

function buildEngineUI(){
  buildThemeUI();
  buildBucketsUI();
  buildRulesUI();
}

// Save
saveBtn.addEventListener('click', ()=>{
  const cfg=getEngineConfig();

  // buckets
  const allRows = bucketsRow.querySelectorAll('div');
  const rows=[];
  allRows.forEach(r=>{
    const inputs=r.querySelectorAll('input');
    if(inputs.length!==3) return;
    const label=(inputs[0].value||'').trim() || 'Bucket';
    const loVal=inputs[1].value; const hiVal=inputs[2].value;
    const low=Number(loVal||0);
    let high = (hiVal === '' ? Infinity : Number(hiVal));
    if(!isFinite(high)) high = Infinity;
    rows.push([label, low, high]);
  });
  if(rows.length){ cfg.buckets = rows; }

  // defaults rules
  const defRules = {};
  defaultsRow.querySelectorAll('.row').forEach(r=>{
    const lab=r.querySelector('label'); if(!lab) return;
    const label=lab.textContent.replace(' (% / $)','');
    const pct=r.querySelector('input[data-type="pct"]');
    const flat=r.querySelector('input[data-type="flat"]');
    const rule={ pct: (pct.value===''?null:Number(pct.value)), flat: (flat.value===''?null:Number(flat.value)) };
    defRules[label]=rule;
  });
  cfg.defaults = defRules;

  // per-sheet rules
  cfg.perSheet = {};
  Object.keys(DATASETS).forEach(sheetName=>{
    const holder=document.getElementById('rules-'+sheetName);
    const o={};
    holder.querySelectorAll('.row').forEach(r=>{
      const lab=r.querySelector('label'); if(!lab) return;
      const label=lab.textContent.replace(' (% / $)','');
      const pct=r.querySelector('input[data-type="pct"]');
      const flat=r.querySelector('input[data-type="flat"]');
      const rule={ pct: (pct.value===''?null:Number(pct.value)), flat: (flat.value===''?null:Number(flat.value)) };
      o[label]=rule;
    });
    cfg.perSheet[sheetName]=o;
  });

  setEngineConfig(cfg);
  render();
  alert('Saved. Buckets and rules updated.');
});

resetBtn.addEventListener('click', ()=>{
  if(!confirm('Reset ALL overrides and defaults to initial values?')) return;
  setEngineConfig({ defaults: DEFAULT_RULES, perSheet: {}, buckets: DEFAULT_BUCKETS });
  buildEngineUI(); render();
});
exportBtn.addEventListener('click', ()=>{ const cfg=getEngineConfig(); downloadConfigFile(cfg); });

// Boot
loadTheme();
render();
</script>

    <div class="engine-overlay" id="engineOverlay" aria-hidden="true">
      <div class="engine-modal" role="dialog" aria-modal="true" aria-labelledby="engineTitle">
        <h3 id="engineTitle">Engine Room</h3>
        <p style="color:#98ffa9;margin:0 0 10px;">Enter password to edit rules.</p>
        <input id="enginePwd" type="password" placeholder="Password" autocomplete="current-password" />
        <div class="row">
          <button class="btn" id="engineCancel">Cancel</button>
          <button class="btn" id="engineEnter">Enter</button>
        </div>
      </div>
    </div>
    

    <script>
      // ----- SEARCH NORMALIZATION -----
      const HYPHENS = /[\u2010\u2011\u2012\u2013\u2014\u2212-]/g;
      const QUOTES  = /["“”″]/g;
      const SPACES  = /\s+/g;

      function expandQueryVariants(q) {
        q = (q || '').toLowerCase().trim();
        const variants = new Set([q]);

        if (q.includes('inch')) {
          variants.add(q.replace(/inch/g, '"'));
          variants.add(q.replace(/inch/g, '″'));
          variants.add(q.replace(/inch/g, 'in'));
        }
        if (/[“”"″]/.test(q)) {
          variants.add(q.replace(QUOTES, 'inch'));
          variants.add(q.replace(QUOTES, 'in'));
        }

        const hyphenNormalized = q.replace(HYPHENS, '-');
        if (hyphenNormalized !== q) variants.add(hyphenNormalized);
        variants.add(hyphenNormalized.replace(/-/g, ' '));

        if (/\bwi\s*fi\b/.test(q) || /\bwifi\b/.test(q) || /\bwi-?fi\b/.test(q)) {
          ['wifi','wi-fi','wi fi','wi-fi/vzw','wi-fi / vzw','wi-fi vzw','wi-fi/vzw','wi‑fi','wi‑fi/vzw','wi‑fi vzw'].forEach(v => variants.add(v));
        }

        const common = [
          ['att', 'at&t', 'atnt'],
          ['tmobile','t-mobile','t mobile'],
          ['verizon','vz','vzw']
        ];
        common.forEach(group => {
          if (group.some(g => q.includes(g))) group.forEach(g => variants.add(g));
        });

        variants.add(q.replace(/[^\w\s]/g, ' '));
        [...variants].forEach(v => variants.add(v.replace(SPACES, ' ').trim()));

        return [...variants];
      }

      function normalizeTextForIndex(s) {
        if (!s) return '';
        let t = String(s).toLowerCase();
        t = t.replace(HYPHENS, '-').replace(QUOTES, '"');
        const withInch = t.replace(/"/g, ' inch ');
        const withSpaces = withInch.replace(/-/g, ' ');
        const enriched = [
          t,
          withInch,
          withSpaces,
          withSpaces.replace(/\bvzw\b/g, 'verizon'),
          withSpaces.replace(/\bvz\b/g, 'verizon')
        ].join('  ');
        return enriched.replace(SPACES, ' ').trim();
      }

      let __SEARCH_INDEX__ = [];
      function buildSearchIndexFromDOM() {
        __SEARCH_INDEX__ = [];
        document.querySelectorAll('.device-row').forEach(row => {
          const text = row.innerText || row.textContent || '';
          __SEARCH_INDEX__.push({
            textNorm: normalizeTextForIndex(text),
            el: row
          });
        });
      }
      document.addEventListener('DOMContentLoaded', buildSearchIndexFromDOM);

      function performSearch(rawQuery) {
        const queries = expandQueryVariants(rawQuery).map(q => normalizeTextForIndex(q));
        __SEARCH_INDEX__.forEach(({textNorm, el}) => {
          const hit = queries.some(q => q && textNorm.includes(q));
          el.style.display = hit ? '' : 'none';
        });
      }

      // Hook: if a #searchInput exists, wire it
      const __si = document.getElementById('searchInput');
      if (__si) {
        __si.addEventListener('input', (e) => performSearch(e.target.value));
      }

      // ----- ENGINE MODAL CONTROL -----
      function openEngineModal() {
        const ov = document.getElementById('engineOverlay');
        if (!ov) return;
        ov.style.display = 'block';
        requestAnimationFrame(() => {
          const input = document.getElementById('enginePwd');
          if (input) { input.value = ''; input.focus({ preventScroll: false }); }
        });
      }
      function closeEngineModal() {
        const ov = document.getElementById('engineOverlay');
        if (ov) ov.style.display = 'none';
      }
      const __cancel = document.getElementById('engineCancel');
      if (__cancel) __cancel.addEventListener('click', closeEngineModal);
      const __ov = document.getElementById('engineOverlay');
      if (__ov) __ov.addEventListener('click', (e) => { if (e.target.id === 'engineOverlay') closeEngineModal(); });
      const __enter = document.getElementById('engineEnter');
      if (__enter) __enter.addEventListener('click', () => {
        const pwd = (document.getElementById('enginePwd') || {}).value || '';
        if (typeof window.handleEnginePassword === 'function') {
          window.handleEnginePassword(pwd);
        } else {
          // Fallback: just close if no handler is wired
          closeEngineModal();
        }
      });
      document.addEventListener('keydown', (e) => {
        const ov = document.getElementById('engineOverlay');
        if (e.key === 'Escape' && ov && ov.style.display === 'block') closeEngineModal();
      });

      // Expose opener for existing "Engine Room" button to call
      window.openEngineModal = openEngineModal;
    </script>
    
</body>
</html>"""

    html = html.replace("__DATASETS__", dataset_js)
    html = html.replace("__DEFAULTS__", defaults_js)
    html = html.replace("__PRICE_BUCKETS__", price_buckets_js)
    html = html.replace("__PIN_SHA256__", pin_sha256)

    out_path.write_text(inject_config_loader(html), encoding="utf-8")

def main():
    cwd = Path('.').resolve()
    print('[GSHEETS] mode active')
    data = load_gsheet_tables()
    if not data:
        print('[FATAL] Google Sheets returned no data; check sharing/API key')
        sys.exit(2)

    out = cwd / "index.html"
    build_html(data, PIN_SHA256, DEFAULT_RULES, out)
    print(f"Wrote: {out}")

if __name__ == "__main__":
    main()
