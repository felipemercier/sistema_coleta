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
    # sua instância aceita Bearer; outros headers causam 403
    "Authorization": f"Bearer {WBUY_TOKEN}" if WBUY_TOKEN else "",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "MartierCorreiosAPI/1.0",
}

# candidatos (só para probe; na listagem usamos /order)
LIST_ENDPOINTS = ["order", "orders", "pedido", "pedidos"]

# ==============================
# App
# ==============================
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ---------------- Helpers ----------------
def _dt(date_str):
    """YYYY-MM-DD -> date | None"""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return None

def _unwrap_list(obj):
    """Normaliza diferentes formatos para lista."""
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("data", "results", "items", "orders", "pedidos"):
            v = obj.get(k)
            if isinstance(v, list):
                return v
        return [obj]
    return []

def _extract_tracking(o):
    """Extrai código de rastreio (prioriza frete.rastreio)."""
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
        if c:
            return str(c).strip().upper()
    return ""

def _extract_service(o):
    f = o.get("frete") or {}
    s = f.get("servico") or (o.get("shipping") or {}).get("service")
    return (s or "").strip()

# --------------- Health ---------------
@app.get("/")
def root():
    return "WBuy Orders API – OK"

@app.get("/health")
def health():
    return jsonify({"ok": True, "has_token": bool(WBUY_TOKEN), "api_url": API_URL})

# --------------- Diagnóstico ---------------
@app.get("/api/wbuy/probe")
def probe():
    """
    Testa Authorization: Bearer <token> com e sem paginação.
    Sua instância aceita sem paginação.
    """
    page = int(request.args.get("page") or 1)
    page_size = int(request.args.get("page_size") or 25)

    header_variants = [
        {"Authorization": f"Bearer {WBUY_TOKEN}"},
    ]
    param_bases = [
        {},  # sem paginação (o que deve funcionar)
        {"page": page, "page_size": page_size},  # só para mostrar rejeição
    ]

    tried = []
    for ep in LIST_ENDPOINTS:
        for h in header_variants:
            hdr = {k: v for k, v in HEADERS.items() if k != "Authorization"}
            hdr.update(h)
            for base in param_bases:
                url = f"{API_URL}/{ep}"
                try:
                    r = requests.get(url, headers=hdr, params=base, timeout=45)
                    try:
                        js = r.json() if r.text else {}
                    except Exception:
                        js = {}
                    items = js.get("data") if isinstance(js, dict) else None
                    if not isinstance(items, list):
                        items = _unwrap_list(js)

                    item = {
                        "endpoint": ep,
                        "http": r.status_code,
                        "params": base,
                        "headers_used": list(h.keys()),
                        "message": (js.get("message") if isinstance(js, dict) else None),
                        "keys": (list(js.keys()) if isinstance(js, dict) else type(js).__name__),
                        "count": len(items or []),
                        "sample": (items[0] if items else None),
                    }
                    tried.append(item)
                    if r.status_code == 200 and items:
                        return jsonify({"ok": True, "best": item, "tried": tried})
                except Exception as e:
                    tried.append({"endpoint": ep, "error": str(e), "headers_used": list(h.keys()), "params": base})

    return jsonify({"ok": False, "message": "Nenhuma combinação retornou itens.", "tried": tried})

# --------------- Listagem por período ---------------
@app.get("/api/wbuy/orders")
def list_orders():
    """
    Lista pedidos com ID e código de rastreio.
    - NÃO usa paginação nem status (sua API rejeita).
    Params:
      - from: YYYY-MM-DD (opcional; default = hoje-30d)
      - to:   YYYY-MM-DD (opcional; default = hoje)
      - q:    orderId numérico (opcional)
    """
    if not WBUY_TOKEN:
        return jsonify({"ok": False, "error": "WBUY_TOKEN ausente"}), 500

    today = datetime.utcnow().date()
    dfrom = _dt(request.args.get("from")) or (today - timedelta(days=30))
    dto   = _dt(request.args.get("to")) or today
    q     = (request.args.get("q") or "").strip()

    # detalhe por ID
    if q.isdigit():
        r = requests.get(f"{API_URL}/order/{q}", headers=HEADERS, timeout=45)
        if r.status_code == 200:
            o = r.json() if r.text else {}
            if isinstance(o, dict) and "data" in o and isinstance(o["data"], list) and o["data"]:
                o = o["data"][0]
            raw = (o.get("data") or o.get("created_at") or o.get("criado_em") or "")
            frete = o.get("frete") or {}
            rastreio = (frete.get("rastreio") or "").strip().upper()
            return jsonify({"ok": True, "from": str(dfrom), "to": str(dto), "count": 1, "rows": [{
                "orderId": o.get("id"),
                "numero": o.get("numero") or o.get("order_number") or o.get("identificacao"),
                "tracking": rastreio,
                "service": (frete.get("servico") or ""),
                "createdAt": raw,
                "updatedAt": o.get("updated_at") or o.get("atualizado_em") or "",
            }]})
        return jsonify({"ok": True, "from": str(dfrom), "to": str(dto), "count": 0, "rows": []})

    # listagem SEM paginação/status
    r = requests.get(f"{API_URL}/order", headers=HEADERS, timeout=60)
    if r.status_code != 200:
        return jsonify({"ok": False, "error": f"HTTP {r.status_code} em /order"}), 502

    try:
        js = r.json() if r.text else {}
    except Exception:
        js = {}

    items = js.get("data") if isinstance(js, dict) else None
    if not isinstance(items, list):
        items = _unwrap_list(js)

    rows, seen = [], set()
    for o in items or []:
        raw = (o.get("data") or o.get("created_at") or o.get("criado_em") or "")
        d = _dt(str(raw)[:10])
        if d and (d < dfrom or d > dto):
            continue
        oid = o.get("id")
        if not oid or oid in seen:
            continue
        seen.add(oid)
        frete = o.get("frete") or {}
        rastreio = (frete.get("rastreio") or "").strip().upper()
        rows.append({
            "orderId": oid,
            "numero": o.get("numero") or o.get("order_number") or o.get("identificacao"),
            "tracking": rastreio,
            "service": (frete.get("servico") or ""),
            "createdAt": raw,
            "updatedAt": o.get("updated_at") or o.get("atualizado_em") or "",
        })

    return jsonify({"ok": True, "from": str(dfrom), "to": str(dto), "count": len(rows), "rows": rows})

# --------------- Run ---------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
