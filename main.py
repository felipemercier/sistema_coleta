# main.py
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime, timedelta
import os, requests, re

# ==============================
# Config
# ==============================
API_URL    = os.getenv("WBUY_API_URL", "https://sistema.sistemawbuy.com.br/api/v1").rstrip("/")
WBUY_TOKEN = (os.getenv("WBUY_TOKEN") or "").strip()
PORT       = int(os.getenv("PORT", "5000"))

HEADERS = {
    "Authorization": f"Bearer {WBUY_TOKEN}" if WBUY_TOKEN else "",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "MartierCorreiosAPI/1.0",
}

# ==============================
# App
# ==============================
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ---------------- Helpers ----------------
def _ok(r): return r.status_code in (200, 201, 202)

def _dt(date_str):
    if not date_str: return None
    try: return datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception: return None

def _unwrap_list(obj):
    if obj is None: return []
    if isinstance(obj, list): return obj
    if isinstance(obj, dict):
        for k in ("data", "results", "items", "orders", "pedidos"):
            v = obj.get(k)
            if isinstance(v, list):
                return v
        return [obj]
    return []

def _extract_tracking(o):
    f = o.get("frete") or {}
    ship = o.get("shipping") or {}
    cands = [
        f.get("rastreio"),
        f.get("codigo_rastreamento"),
        f.get("tracking_code"),
        ship.get("tracking"),
        ship.get("tracking_code"),
        o.get("rastreamento"),
        o.get("tracking"),
        o.get("tracking_code"),
    ]
    for c in cands:
        if c: return str(c).strip().upper()
    return ""

def _extract_service(o):
    f = o.get("frete") or {}
    s = f.get("servico") or (o.get("shipping") or {}).get("service")
    return (s or "").strip()

def _created_any(o):
    raw = (o.get("data") or o.get("created_at") or o.get("criado_em") or o.get("date") or "")
    s = str(raw)[:10]
    return raw, _dt(s)

def _fetch_list(params=None):
    """Chama /order com params e devolve (items, json bruto, status)."""
    url = f"{API_URL}/order"
    r = requests.get(url, headers=HEADERS, params=(params or {}), timeout=45)
    try:
        js = r.json() if r.text else {}
    except Exception:
        js = {}
    items = js.get("data") if isinstance(js, dict) else None
    if not isinstance(items, list):
        items = _unwrap_list(js)
    return items or [], js, r.status_code

def _discover_pagination():
    """
    Descobre como paginar na sua instância.
    Retorna um dict:
      - mode: 'page' | 'offset' | 'limit_only' | 'none'
      - keys: {'page': '...', 'size': '...'} ou {'offset': '...', 'limit': '...'} ou {'limit': '...'}
      - size: int sugerido
    """
    candidates = [
        {"mode": "page",   "keys": {"page":"page",   "size":"page_size"},        "size": 200},
        {"mode": "page",   "keys": {"page":"page",   "size":"per_page"},         "size": 200},
        {"mode": "page",   "keys": {"page":"pagina", "size":"itens"},            "size": 200},
        {"mode": "page",   "keys": {"page":"pagina", "size":"itens_por_pagina"}, "size": 200},
        {"mode": "offset", "keys": {"offset":"offset","limit":"limit"},          "size": 200},
        {"mode": "offset", "keys": {"offset":"start", "limit":"limit"},          "size": 200},
        {"mode": "offset", "keys": {"offset":"skip",  "limit":"take"},           "size": 200},
        {"mode": "limit_only", "keys": {"limit":"limit"},                         "size": 1000},
    ]

    # 1) Sem paginação
    items0, js0, st0 = _fetch_list({})
    if st0 == 200 and items0:
        # tenta ampliar com 'limit'
        itemsL, _, stL = _fetch_list({"limit": 1000})
        if stL == 200 and len(itemsL) > len(items0):
            return {"mode":"limit_only", "keys":{"limit":"limit"}, "size":1000}
        return {"mode":"none", "keys":{}, "size":len(items0)}

    # 2) Testa candidatos
    for c in candidates:
        mode, keys, size = c["mode"], c["keys"], c["size"]
        if mode == "page":
            p1 = {keys["page"]: 1, keys["size"]: size}
            p2 = {keys["page"]: 2, keys["size"]: size}
            it1, _, s1 = _fetch_list(p1)
            it2, _, s2 = _fetch_list(p2)
            if s1 == 200 and it1 and s2 == 200 and it2 and (it1[0] != it2[0]):
                return {"mode": mode, "keys": keys, "size": size}
        elif mode == "offset":
            p1 = {keys["offset"]: 0,   keys["limit"]: size}
            p2 = {keys["offset"]: size, keys["limit"]: size}
            it1, _, s1 = _fetch_list(p1)
            it2, _, s2 = _fetch_list(p2)
            if s1 == 200 and it1 and s2 == 200 and it2 and (it1[0] != it2[0]):
                return {"mode": mode, "keys": keys, "size": size}
        elif mode == "limit_only":
            p = {keys["limit"]: size}
            it, _, s = _fetch_list(p)
            if s == 200 and it:
                return {"mode": mode, "keys": keys, "size": size}

    return {"mode":"none", "keys":{}, "size":100}

def _normalize_tracking(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())

def _find_order_by_tracking(tracking: str, max_pages: int = 80):
    """
    Procura um pedido cujo frete.rastreio == tracking,
    varrendo as páginas conforme a paginação descoberta.
    Retorna o objeto do pedido (dict) ou None.
    """
    tnorm = _normalize_tracking(tracking)
    if not tnorm:
        return None

    pg = _discover_pagination()
    mode, keys, size = pg["mode"], pg["keys"], pg["size"]

    def hit(params=None):
        items, _, st = _fetch_list(params or {})
        return items if st == 200 else []

    if mode in ("none", "limit_only"):
        params = {} if mode == "none" else {keys["limit"]: size}
        for o in hit(params):
            if _normalize_tracking(_extract_tracking(o)) == tnorm:
                return o
        return None

    if mode == "page":
        page = 1
        while page <= max_pages:
            params = {keys["page"]: page, keys["size"]: size}
            items = hit(params)
            if not items:
                break
            for o in items:
                if _normalize_tracking(_extract_tracking(o)) == tnorm:
                    return o
            page += 1
        return None

    if mode == "offset":
        offset = 0
        for _ in range(max_pages):
            params = {keys["offset"]: offset, keys["limit"]: size}
            items = hit(params)
            if not items:
                break
            for o in items:
                if _normalize_tracking(_extract_tracking(o)) == tnorm:
                    return o
            offset += size
        return None

    return None

# --------------- Health ---------------
@app.get("/")
def root():
    return "WBuy Orders API – OK"

@app.get("/health")
def health():
    return jsonify({"ok": True, "has_token": bool(WBUY_TOKEN), "api_url": API_URL})

# --------------- Listagem por período ---------------
@app.get("/api/wbuy/orders")
def list_orders():
    """
    Lista pedidos com ID, rastreio e serviço, varrendo páginas automaticamente.
    Params:
      - from, to: YYYY-MM-DD (padrão: últimos 30 dias)
      - q: orderId numérico (atalho)
      - max_pages: segurança (default 50)
    """
    if not WBUY_TOKEN:
        return jsonify({"ok": False, "error": "WBUY_TOKEN ausente"}), 500

    today = datetime.utcnow().date()
    dfrom = _dt(request.args.get("from")) or (today - timedelta(days=30))
    dto   = _dt(request.args.get("to")) or today
    q     = (request.args.get("q") or "").strip()
    max_pages = int(request.args.get("max_pages") or 50)

    # Atalho: detalhe por ID
    if q.isdigit():
        r = requests.get(f"{API_URL}/order/{q}", headers=HEADERS, timeout=45)
        if r.status_code == 200:
            o = r.json() if r.text else {}
            if isinstance(o, dict) and "data" in o and isinstance(o["data"], list) and o["data"]:
                o = o["data"][0]
            raw, _ = _created_any(o)
            frete = o.get("frete") or {}
            return jsonify({"ok": True, "from": str(dfrom), "to": str(dto), "count": 1, "rows": [{
                "orderId": o.get("id"),
                "numero": o.get("numero") or o.get("order_number") or o.get("identificacao"),
                "tracking": _extract_tracking(o),
                "service": _extract_service(o),
                "createdAt": raw,
                "updatedAt": o.get("updated_at") or o.get("atualizado_em") or "",
            }]})
        return jsonify({"ok": True, "from": str(dfrom), "to": str(dto), "count": 0, "rows": []})

    # Descobre paginação e varre
    pg = _discover_pagination()
    rows, seen = [], set()

    def add_rows(items):
        added = 0
        for o in items:
            raw, d = _created_any(o)
            if d:
                if d < dfrom:
                    continue
                if d > dto:
                    continue
            oid = o.get("id")
            if not oid or oid in seen:
                continue
            seen.add(oid)
            rows.append({
                "orderId": oid,
                "numero": o.get("numero") or o.get("order_number") or o.get("identificacao"),
                "tracking": _extract_tracking(o),
                "service": _extract_service(o),
                "createdAt": raw,
                "updatedAt": o.get("updated_at") or o.get("atualizado_em") or "",
            })
            added += 1
        return added

    mode = pg["mode"]; keys = pg["keys"]; size = pg["size"]

    if mode == "none":
        items, _, st = _fetch_list({})
        if st != 200:
            return jsonify({"ok": False, "error": f"HTTP {st} em /order"}), 502
        add_rows(items)

    elif mode == "limit_only":
        items, _, st = _fetch_list({keys["limit"]: size})
        if st != 200:
            return jsonify({"ok": False, "error": f"HTTP {st} em /order"}), 502
        add_rows(items)

    elif mode == "page":
        page = 1
        while page <= max_pages:
            params = {keys["page"]: page, keys["size"]: size}
            items, _, st = _fetch_list(params)
            if st != 200 or not items:
                break
            add_rows(items)
            dates = [_created_any(o)[1] for o in items if _created_any(o)[1] is not None]
            if dates and min(dates) and min(dates) < dfrom:
                # mais uma página para capturar fronteira e sai
                page += 1
                items2, _, st2 = _fetch_list({keys["page"]: page, keys["size"]: size})
                if st2 == 200 and items2:
                    add_rows(items2)
                break
            page += 1

    elif mode == "offset":
        offset = 0
        for _ in range(max_pages):
            params = {keys["offset"]: offset, keys["limit"]: size}
            items, _, st = _fetch_list(params)
            if st != 200 or not items:
                break
            add_rows(items)
            dates = [_created_any(o)[1] for o in items if _created_any(o)[1] is not None]
            if dates and min(dates) and min(dates) < dfrom:
                offset += size
                items2, _, st2 = _fetch_list({keys["offset"]: offset, keys["limit"]: size})
                if st2 == 200 and items2:
                    add_rows(items2)
                break
            offset += size

    return jsonify({"ok": True, "from": str(dfrom), "to": str(dto), "count": len(rows), "rows": rows})

# --------------- Lookup por rastreio ---------------
@app.get("/api/wbuy/lookup")
def lookup_by_tracking():
    """
    GET /api/wbuy/lookup?tracking=AA123456789BR
    Retorna: { ok, found, row }
    row => { orderId, numero, tracking, valorFrete, service, createdAt, updatedAt }
    """
    tracking = (request.args.get("tracking") or "").strip()
    if not tracking:
        return jsonify({"ok": False, "error": "Parâmetro tracking é obrigatório."}), 400

    o = _find_order_by_tracking(tracking, max_pages=int(request.args.get("max_pages") or 80))
    if not o:
        return jsonify({"ok": True, "found": False, "row": None})

    frete = o.get("frete") or {}
    row = {
        "orderId": o.get("id"),
        "numero": o.get("numero") or o.get("order_number") or o.get("identificacao"),
        "tracking": _extract_tracking(o),
        "valorFrete": str(frete.get("valor") or ""),
        "service": _extract_service(o),
        "createdAt": o.get("data") or o.get("created_at") or o.get("criado_em") or "",
        "updatedAt": o.get("updated_at") or o.get("atualizado_em") or "",
    }
    return jsonify({"ok": True, "found": True, "row": row})

# --------------- Run ---------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
