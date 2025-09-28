# main.py
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime
import os, requests

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

# --------------- Helpers ---------------
def _ok(r): return r.status_code in (200, 201, 202)

def _dt(date_str):
    """YYYY-MM-DD -> date | None (para filtro)."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return None

def _order_date(o):
    """
    Retorna (raw, date) considerando os nomes que a WBuy usa.
    Ex.: "data": "2025-09-28 19:28:18"
    """
    raw = (o.get("data") or o.get("created_at") or o.get("criado_em") or "")
    s10 = str(raw)[:10]  # "YYYY-MM-DD"
    try:
        return raw, datetime.strptime(s10, "%Y-%m-%d").date()
    except Exception:
        return raw, None

def _unwrap_list(js):
    if js is None: return []
    if isinstance(js, list): return js
    if isinstance(js, dict):
        for k in ("data", "orders", "items", "results", "pedidos"):
            v = js.get(k)
            if isinstance(v, list):
                return v
        # alguns retornam um único objeto
        return [js]
    return []

# --------------- Health ---------------
@app.get("/")
def root():
    return "WBuy Orders API – OK"

@app.get("/health")
def health():
    return jsonify({"ok": True, "has_token": bool(WBUY_TOKEN), "api_url": API_URL})

# --------------- Core ---------------
@app.get("/api/wbuy/orders")
def list_orders():
    """
    Lista pedidos no período e devolve:
      { ok, rows: [{orderId, numero, tracking, trackingCode, createdAt}] }
    Params:
      - from: YYYY-MM-DD (opcional; default = hoje-30d)
      - to:   YYYY-MM-DD (opcional; default = hoje)
      - page_size: int (default 100)
      - max_pages: int (default 8)
    """
    if not WBUY_TOKEN:
        return jsonify({"ok": False, "error": "WBUY_TOKEN ausente"}), 500

    today = datetime.utcnow().date()
    f_from = _dt(request.args.get("from")) or (today.replace(day=today.day) if False else today)  # dummy, será ajustado já abaixo
    f_to   = _dt(request.args.get("to"))   or today

    # default últimos 30 dias se nada vier
    if request.args.get("from") is None and request.args.get("to") is None:
        f_from = today - timedelta(days=30)

    page_size = int(request.args.get("page_size") or 100)
    max_pages = int(request.args.get("max_pages") or 8)

    rows = []
    seen = set()

    # usamos SEM parâmetro de status (algumas contas ignoram/zeram)
    for page in range(1, max_pages + 1):
        params = {"page": page, "page_size": page_size}
        url = f"{API_URL}/order"
        r = requests.get(url, headers=HEADERS, params=params, timeout=40)
        if not _ok(r):
            return jsonify({"ok": False, "error": f"HTTP {r.status_code} em {url}", "params": params}), 502

        try:
            js = r.json() if r.text else {}
        except Exception:
            js = {}

        items = _unwrap_list(js)
        if not items:
            break

        added_this_page = 0
        for o in items:
            raw_date, d = _order_date(o)
            # filtra por período somente se a data for parseável
            if d:
                if request.args.get("from") and d < _dt(request.args.get("from")): 
                    continue
                if request.args.get("to") and d > _dt(request.args.get("to")): 
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
                "trackingCode": rastreio,  # compatibilidade com seu front
                "createdAt": raw_date,
            })
            added_this_page += 1

        # se a API ignorar paginação e repetir a 1ª página, paramos quando não vier nenhum novo id
        if added_this_page == 0:
            break

        # heurística comum: se vier menos que page_size, acabou
        if len(items) < page_size:
            break

    return jsonify({"ok": True, "rows": rows})

# --------------- Run ---------------
if __name__ == "__main__":
    from datetime import timedelta  # import local para evitar poluir topo
    app.run(host="0.0.0.0", port=PORT)
