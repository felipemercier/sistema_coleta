# main.py
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime
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
}

# ==============================
# App
# ==============================
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# ---------------- Helpers ----------------
def _ok(r): return r.status_code in (200, 201, 202)

def _dt(date_str):
    """YYYY-MM-DD -> date | None (para os filtros da query)."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return None

def _unwrap_list(obj):
    """Normaliza diferentes formatos de listagem da WBuy para lista."""
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("data", "results", "items", "orders"):
            v = obj.get(k)
            if isinstance(v, list):
                return v
        return [obj]
    return []

# -------- rastreio mais abrangente --------
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
        if c:
            return str(c).strip().upper()
    return ""

def _extract_service(o):
    f = o.get("frete") or {}
    s = f.get("servico") or (o.get("shipping") or {}).get("service")
    return (s or "").strip()

def _is_tracking(s): return bool(re.match(r"^[A-Z]{2}\d{9}BR$", (s or "").strip().upper()))
def _is_order_id(s): return (s or "").strip().isdigit()

# --------------- Health ---------------
@app.get("/")
def root():
    return "WBuy Orders API – OK"

@app.get("/health")
def health():
    return jsonify({"ok": True, "has_token": bool(WBUY_TOKEN)})

# --------------- Core: listar pedidos + rastreio ---------------
@app.get("/api/wbuy/orders")
def list_orders():
    """
    Lista pedidos com seus códigos de rastreio.
    Params:
      - from: YYYY-MM-DD (opcional)
      - to:   YYYY-MM-DD (opcional)
      - q:    orderId numérico OU código de rastreio (AA123456789BR) (opcional)
      - status: inteiro da WBuy (opcional, repetível ?status=1&status=2)
      - max_pages: int (default 8)
      - page_size: int (default 100)
    Retorno:
      { ok, rows: [{orderId, numero, createdAt, updatedAt, tracking, service}] }
    """
    if not WBUY_TOKEN:
        return jsonify({"ok": False, "error": "WBUY_TOKEN ausente em variáveis de ambiente."}), 500

    f_from = _dt(request.args.get("from"))
    f_to   = _dt(request.args.get("to"))
    q      = (request.args.get("q") or "").strip()
    max_pages = int(request.args.get("max_pages") or 8)
    page_size = int(request.args.get("page_size") or 100)

    # status (opcional). Se não vier, tentaremos também SEM status (fallback).
    statuses = [s for s in request.args.getlist("status") if s.isdigit()]
    try_without_status = not statuses
    if not statuses:
        statuses = [str(i) for i in range(1, 19)]

    # Atalho: detalhe por orderId exato
    if _is_order_id(q):
        u = f"{API_URL}/order/{q}"
        r = requests.get(u, headers=HEADERS, timeout=30)
        if not _ok(r):
            return jsonify({"ok": False, "error": f"HTTP {r.status_code}"}), r.status_code
        o = r.json() if r.text else {}
        if isinstance(o, dict) and "data" in o and isinstance(o["data"], list) and o["data"]:
            o = o["data"][0]
        row = {
            "orderId": o.get("id"),
            "numero": o.get("numero") or o.get("order_number"),
            "createdAt": (o.get("created_at") or o.get("criado_em") or o.get("date") or o.get("data") or o.get("emissao") or ""),
            "updatedAt": (o.get("updated_at") or o.get("atualizado_em") or ""),
            "tracking": _extract_tracking(o),
            "service": _extract_service(o),
        }
        return jsonify({"ok": True, "rows": [row]})

    def fetch_loop(with_status=True):
        acc = []
        status_list = statuses if with_status else [None]
        for status in status_list:
            for page in range(1, max_pages + 1):
                params = {"page": page, "page_size": page_size}
                if status:
                    params["status"] = status
                r = requests.get(f"{API_URL}/order", headers=HEADERS, params=params, timeout=40)
                if not _ok(r):
                    break
                items = _unwrap_list(r.json() if r.text else {})
                if not items:
                    break

                for o in items:
                    # aceita vários nomes de data; se não parsear, NÃO filtra por data
                    created_raw = (o.get("created_at") or o.get("criado_em") or
                                   o.get("date") or o.get("data") or
                                   o.get("emissao") or o.get("emitted_at") or "")
                    created = str(created_raw)[:10]
                    created_date = _dt(created)

                    if f_from and created_date and created_date < f_from:
                        continue
                    if f_to and created_date and created_date > f_to:
                        continue

                    tracking = _extract_tracking(o)
                    if q and _is_tracking(q) and (tracking or "").upper() != q.upper():
                        continue

                    acc.append({
                        "orderId": o.get("id"),
                        "numero": o.get("numero") or o.get("order_number"),
                        "createdAt": created_raw,
                        "updatedAt": o.get("updated_at") or o.get("atualizado_em") or "",
                        "tracking": tracking,
                        "service": _extract_service(o),
                    })

                if len(items) < page_size:
                    break
        return acc

    # 1ª passada: com status 1..18
    rows = fetch_loop(with_status=True)

    # fallback: se vazio e você não passou status manualmente, tenta sem status
    if not rows and try_without_status:
        rows = fetch_loop(with_status=False)

    return jsonify({"ok": True, "rows": rows})

# --------------- Run ---------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
