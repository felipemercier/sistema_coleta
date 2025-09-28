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
    # tenta os dois formatos ao mesmo tempo
    "Authorization": f"Bearer {WBUY_TOKEN}" if WBUY_TOKEN else "",
    "token": WBUY_TOKEN or "",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


# candidatos para auto-detecção
LIST_ENDPOINTS     = ["order", "orders", "pedido", "pedidos"]
STATUS_PARAM_KEYS  = ["status", "situacao"]

# ==============================
# App
# ==============================
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ---------------- Helpers ----------------
def _ok(r): return r.status_code in (200, 201, 202)

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

def _is_tracking(s): return bool(re.match(r"^[A-Z]{2}\d{9}BR$", (s or "").strip().upper()))
def _is_order_id(s): return (s or "").strip().isdigit()

def _created_any(o):
    """Tenta várias chaves de data; retorna (raw, date|None)."""
    raw = (o.get("data") or o.get("created_at") or o.get("criado_em") or o.get("date") or "")
    s = str(raw)[:10]
    return raw, _dt(s)

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
    Faz tentativas com endpoints/chaves de status diferentes e mostra um sample.
    """
    page = int(request.args.get("page") or 1)
    page_size = int(request.args.get("page_size") or 50)
    passed_status = request.args.get("status")

    tried = []
    for ep in LIST_ENDPOINTS:
        for key in STATUS_PARAM_KEYS + [None]:
            params = {"page": page, "page_size": page_size}
            if passed_status and key:
                params[key] = passed_status
            url = f"{API_URL}/{ep}"
            r = requests.get(url, headers=HEADERS, params=params, timeout=40)
            item = {"endpoint": ep, "status_key": key, "http": r.status_code}
            try:
                js = r.json() if r.text else {}
            except Exception:
                js = {}
            items = _unwrap_list(js)
            item["count"] = len(items)
            item["keys"] = list(js.keys()) if isinstance(js, dict) else type(js).__name__
            item["params"] = params
            item["sample"] = (items[0] if items else None)
            tried.append(item)
            if _ok(r) and items:
                return jsonify({"ok": True, "best": item, "tried": tried})
    return jsonify({"ok": False, "message": "Nenhum item retornou.", "tried": tried})

# --------------- Listagem por período ---------------
@app.get("/api/wbuy/orders")
def list_orders():
    """
    Lista pedidos com ID e código de rastreio.
    Params:
      - from: YYYY-MM-DD (opcional; default = hoje-30d)
      - to:   YYYY-MM-DD (opcional; default = hoje)
      - q:    orderId numérico OU código de rastreio (opcional)
      - status: pode repetir ?status=5&status=7 (opcional)
      - page_size: int (default 100)
      - max_pages: int (default 8)
    Retorno:
      { ok, from, to, count, rows: [{orderId, numero, tracking, service, createdAt, updatedAt}] }
    """
    if not WBUY_TOKEN:
        return jsonify({"ok": False, "error": "WBUY_TOKEN ausente"}), 500

    today = datetime.utcnow().date()
    dfrom = _dt(request.args.get("from")) or (today - timedelta(days=30))
    dto   = _dt(request.args.get("to")) or today
    q      = (request.args.get("q") or "").strip()
    page_size = int(request.args.get("page_size") or 100)
    max_pages = int(request.args.get("max_pages") or 8)

    # status desejados (opcional)
    statuses = [s for s in request.args.getlist("status") if s.isdigit()]
    default_statuses = [str(i) for i in range(1, 19)]  # fallback comum

    # Atalho: detalhe por orderId
    if _is_order_id(q):
        for ep in LIST_ENDPOINTS:
            u = f"{API_URL}/{ep}/{q}"
            r = requests.get(u, headers=HEADERS, timeout=30)
            if _ok(r):
                o = r.json() if r.text else {}
                if isinstance(o, dict) and "data" in o and isinstance(o["data"], list) and o["data"]:
                    o = o["data"][0]
                raw, _ = _created_any(o)
                row = {
                    "orderId": o.get("id"),
                    "numero": o.get("numero") or o.get("order_number") or o.get("identificacao"),
                    "tracking": _extract_tracking(o),
                    "service": _extract_service(o),
                    "createdAt": raw,
                    "updatedAt": o.get("updated_at") or o.get("atualizado_em") or "",
                }
                return jsonify({"ok": True, "from": str(dfrom), "to": str(dto), "count": 1, "rows": [row]})
        return jsonify({"ok": True, "from": str(dfrom), "to": str(dto), "count": 0, "rows": []})

    rows, seen = [], set()

    def fetch(with_status=True):
        acc, added = [], 0
        for ep in LIST_ENDPOINTS:
            keys = STATUS_PARAM_KEYS if with_status else [None]
            sts  = (statuses or default_statuses) if with_status else [None]
            for key in keys:
                for st in sts:
                    for page in range(1, max_pages + 1):
                        params = {"page": page, "page_size": page_size}
                        if key and st:
                            params[key] = st
                        url = f"{API_URL}/{ep}"
                        r = requests.get(url, headers=HEADERS, params=params, timeout=40)
                        if not _ok(r):
                            if r.status_code in (401, 403):
                                return {"error": f"HTTP {r.status_code} em {url}", "params": params}
                            break
                        try:
                            js = r.json() if r.text else {}
                        except Exception:
                            js = {}
                        items = _unwrap_list(js)
                        if not items:
                            break
                        for o in items:
                            raw, created_date = _created_any(o)
                            if created_date:
                                if created_date < dfrom or created_date > dto:
                                    continue
                            oid = o.get("id")
                            if not oid or oid in seen:
                                continue
                            seen.add(oid)
                            acc.append({
                                "orderId": oid,
                                "numero": o.get("numero") or o.get("order_number") or o.get("identificacao"),
                                "tracking": _extract_tracking(o),
                                "service": _extract_service(o),
                                "createdAt": raw,
                                "updatedAt": o.get("updated_at") or o.get("atualizado_em") or "",
                            })
                            added += 1
                        if len(items) < page_size:
                            break
        return acc if added or not isinstance(acc, dict) else acc

    # 1ª passada: com status
    out = fetch(with_status=True)
    if isinstance(out, dict) and out.get("error"):
        return jsonify({"ok": False, **out}), 502
    rows.extend(out)

    # fallback: sem status (caso a API ignore/filtre demais)
    if not rows:
        out = fetch(with_status=False)
        if isinstance(out, dict) and out.get("error"):
            return jsonify({"ok": False, **out}), 502
        rows.extend(out)

    return jsonify({"ok": True, "from": str(dfrom), "to": str(dto), "count": len(rows), "rows": rows})

# --------------- Run ---------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
