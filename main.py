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
    """YYYY-MM-DD -> date | None"""
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
        # às vezes já vem um único objeto
        return [obj]
    return []

def _extract_tracking(o):
    """Tenta achar o rastreio em campos comuns."""
    cands = [
        (o.get("frete") or {}).get("rastreio"),
        o.get("rastreamento"),
        (o.get("shipping") or {}).get("tracking"),
        (o.get("order") or {}).get("tracking"),
        o.get("tracking"),
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
    Parâmetros:
      - from: YYYY-MM-DD (opcional)
      - to:   YYYY-MM-DD (opcional)
      - q:    orderId numérico OU código de rastreio (AA123456789BR) (opcional)
      - status: inteiro da WBuy (opcional, pode repetir ?status=1&status=2...)
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

    # Filtros opcionais por status (pode vir múltiplo)
    statuses = request.args.getlist("status")
    statuses = [s for s in statuses if s.isdigit()]
    if not statuses:
        # se não passar status, buscamos todas as situações comuns (1..18)
        statuses = [str(i) for i in range(1, 19)]

    rows = []

    # Atalho: q = orderId -> busca direta do detalhe
    if _is_order_id(q):
        u = f"{API_URL}/order/{q}"
        r = requests.get(u, headers=HEADERS, timeout=30)
        if not _ok(r):
            return jsonify({"ok": False, "error": f"HTTP {r.status_code}"}), r.status_code
        o = r.json() if r.text else {}
        # Alguns formatos retornam objeto direto; normaliza:
        if isinstance(o, dict) and "data" in o and isinstance(o["data"], list) and o["data"]:
            o = o["data"][0]
        row = {
            "orderId": o.get("id"),
            "numero": o.get("numero") or o.get("order_number"),
            "createdAt": (o.get("created_at") or o.get("criado_em") or ""),
            "updatedAt": (o.get("updated_at") or o.get("atualizado_em") or ""),
            "tracking": _extract_tracking(o),
            "service": _extract_service(o),
        }
        return jsonify({"ok": True, "rows": [row]})

    # Varredura paginada por status
    for status in statuses:
        for page in range(1, max_pages + 1):
            url = f"{API_URL}/order?page={page}&page_size={page_size}&status={status}"
            r = requests.get(url, headers=HEADERS, timeout=40)
            if not _ok(r):
                continue

            items = _unwrap_list(r.json() if r.text else {})
            if not items:
                break

            for o in items:
                created = (o.get("created_at") or o.get("criado_em") or "")[:10]
                created_date = _dt(created)

                # filtro por período
                if f_from and (not created_date or created_date < f_from):
                    continue
                if f_to and (not created_date or created_date > f_to):
                    continue

                tracking = _extract_tracking(o)
                # filtro por q = tracking
                if q and _is_tracking(q) and tracking.upper() != q.upper():
                    continue

                rows.append({
                    "orderId": o.get("id"),
                    "numero": o.get("numero") or o.get("order_number"),
                    "createdAt": o.get("created_at") or o.get("criado_em") or "",
                    "updatedAt": o.get("updated_at") or o.get("atualizado_em") or "",
                    "tracking": tracking,
                    "service": _extract_service(o),
                })

            # heurística: fim da listagem se trouxe menos que page_size
            if len(items) < page_size:
                break

    return jsonify({"ok": True, "rows": rows})

# --------------- Run ---------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
