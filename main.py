from flask import Flask, jsonify, request, make_response, Blueprint
from flask_cors import CORS
import os, requests

# ==============================
# Config
# ==============================
API_URL     = os.getenv("WBUY_API_URL", "https://sistema.sistemawbuy.com.br/api/v1").rstrip("/")
WBUY_TOKEN  = (os.getenv("WBUY_TOKEN") or "").strip()
PORT        = int(os.getenv("PORT", "5000"))

HEADERS = {
    "Authorization": f"Bearer {WBUY_TOKEN}" if WBUY_TOKEN else "",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# ==============================
# App
# ==============================
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)

# ------------------------------ Helpers ------------------------------
def _ok(r): 
    return r.status_code in (200, 201, 202)

def _json_safe(resp):
    try:
        return resp.json()
    except Exception:
        try:
            return resp.text
        except Exception:
            return {}

def _num_centavos(v):
    """Normaliza dinheiro (aceita int/float/str BR) -> centavos (int)."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v < 1000:
            return int(round(v * 100))
        return int(round(v))
    s = str(v).strip()
    if not s:
        return 0
    s = s.replace(".", "").replace(",", ".")
    try:
        return int(round(float(s) * 100))
    except Exception:
        return 0

def _unwrap_first(obj):
    """
    Retorna o primeiro objeto "pedido" de formatos típicos:
      {'data': {...}} | {'data': [{...}]} | [{...}] | {...}
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        if "data" in obj:
            d = obj.get("data")
            if isinstance(d, list):
                return d[0] if d else {}
            if isinstance(d, dict):
                return d
        return obj
    if isinstance(obj, list):
        return obj[0] if obj else {}
    return {}

def _extract_shipping_total(order_json):
    cands = [
        order_json.get("shipping_total"),
        order_json.get("valor_frete"),
        order_json.get("frete_total"),
        order_json.get("total_frete"),
        (order_json.get("frete") or {}).get("valor"),
        (order_json.get("totals") or {}).get("shipping"),
        (order_json.get("order") or {}).get("shipping_total"),
        # às vezes vem por item (fallback)
        (order_json.get("valor_total") or {}).get("frete"),
    ]
    for c in cands:
        n = _num_centavos(c)
        if n > 0:
            return n
    return 0

def _extract_tracking(order_json):
    cands = [
        (order_json.get("frete") or {}).get("rastreio"),
        order_json.get("rastreamento"),
        (order_json.get("shipping") or {}).get("tracking"),
        (order_json.get("order") or {}).get("tracking"),
    ]
    for c in cands:
        if c:
            return str(c).strip().upper()
    return ""

def _detail_by_id_any(order_id, tried):
    """
    Garante o detalhe do pedido como objeto:
      1) /order/{id}
      2) /order?id={id}&limit=1&complete=1
    """
    # 1)
    try:
        u = f"{API_URL}/order/{order_id}"
        tried.append(u)
        r = requests.get(u, headers=HEADERS, timeout=30)
        if _ok(r):
            raw = _json_safe(r)
            obj = _unwrap_first(raw)
            if obj:
                return obj, raw
    except Exception:
        pass
    # 2)
    try:
        u = f"{API_URL}/order?id={order_id}&limit=1&complete=1"
        tried.append(u)
        r = requests.get(u, headers=HEADERS, timeout=30)
        if _ok(r):
            raw = _json_safe(r)
            obj = _unwrap_first(raw)
            if obj:
                return obj, raw
    except Exception:
        pass
    return {}, {}

# ------------------------------ Raiz/health ------------------------------
@app.get("/")
def home():
    return "Correios API v2 OK"

@app.get("/health")
def health():
    return jsonify({"ok": True})

# ------------------------------ Blueprint v2 (Correios) ------------------------------
api_v2 = Blueprint("api_v2", __name__, url_prefix="/api/v2/wbuy")

@api_v2.get("/ping")
def ping():
    if not WBUY_TOKEN:
        return jsonify({"ok": False, "reason": "no_token"}), 200
    try:
        u = f"{API_URL}/order?limit=1"
        r = requests.get(u, headers=HEADERS, timeout=20)
        return jsonify({"ok": _ok(r)}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200

# -------- NOVA ROTA: procurar pelo RASTREIO de forma direta --------
@api_v2.get("/order/by-tracking/<tracking>")
def by_tracking(tracking):
    """
    Busca direta na WBuy usando ?search=<tracking>.
    Retorna: order_id, shipping_total (centavos), shipping_total_reais, tracking.
    """
    tried = []
    try:
        tracking = (tracking or "").strip().upper()
        if not tracking:
            return jsonify({"error": "tracking_required"}), 400

        # tentativa direta por search
        u = f"{API_URL}/order?search={tracking}&limit=1&complete=1"
        tried.append(u)
        r = requests.get(u, headers=HEADERS, timeout=35)
        if not _ok(r):
            return jsonify({"error": "not_found", "debug": {"tried": tried}}), 404

        raw = _json_safe(r)
        obj = _unwrap_first(raw)

        if not obj:
            return jsonify({"error": "not_found", "debug": {"tried": tried}}), 404

        oid = str(obj.get("id") or obj.get("order_id") or "").strip()
        ship = _extract_shipping_total(obj)
        trk  = _extract_tracking(obj)

        return jsonify({
            "order_id": oid or None,
            "shipping_total": ship,
            "shipping_total_reais": ship / 100.0,
            "tracking": trk or None,
            "debug": {"tried": tried}
        }), 200

    except Exception as e:
        return jsonify({"error": str(e), "debug": {"tried": tried}}), 500

# -------- rotas que você já tinha (por ID e por query) --------
@api_v2.get("/order/<order_id>")
def by_id(order_id):
    tried = []
    try:
        obj, raw = _detail_by_id_any(order_id, tried)
        if not obj:
            return jsonify({"error": "not_found", "debug": {"tried": tried}}), 404
        ship = _extract_shipping_total(obj)
        trk  = _extract_tracking(obj)
        return jsonify({
            "order_id": str(order_id),
            "shipping_total": ship,               # centavos
            "shipping_total_reais": ship / 100.0, # reais
            "tracking": trk,
            "debug": {"tried": tried}
        }), 200
    except Exception as e:
        return jsonify({"error": str(e), "debug": {"tried": tried}}), 500

@api_v2.get("/order")
def by_tracking_or_id():
    """
    /api/v2/wbuy/order?id=1234
    /api/v2/wbuy/order?tracking=AA123456789BR[&deep=1]   (deep=1 default)
    """
    order_id = (request.args.get("id") or "").strip()
    if order_id:
        return by_id(order_id)

    tracking = (request.args.get("tracking") or "").strip().upper()
    deep = (request.args.get("deep") or "1").strip().lower() in ("1","true","yes","y")

    if not tracking:
        return jsonify({"error": "informe tracking ou id"}), 400

    tried = []

    # 1) tentativa rápida via search
    try:
        u = f"{API_URL}/order?limit=1&complete=1&search={tracking}"
        tried.append(u)
        r = requests.get(u, headers=HEADERS, timeout=35)
        if _ok(r):
            raw = _json_safe(r)
            first = _unwrap_first(raw)
            oid = str(first.get("id") or first.get("order_id") or "").strip()
            if oid:
                obj, _ = _detail_by_id_any(oid, tried)
                ship = _extract_shipping_total(obj)
                trk  = _extract_tracking(obj)
                return jsonify({
                    "order_id": oid,
                    "shipping_total": ship,
                    "shipping_total_reais": ship/100.0,
                    "tracking": trk,
                    "debug": {"matches": ["search"], "tried": tried}
                }), 200
    except Exception:
        pass

    if not deep:
        return jsonify({"order_id": None, "shipping_total": 0, "tracking": None, "debug": {"matches": [], "tried": tried}}), 200

    # 2) varredura robusta (status 1..18, 5 páginas de 100) — mantém compat
    STATUSES = list(range(1, 19))
    MAX_PAGES_PER_STATUS = 5
    LIMIT = 100

    for status in STATUSES:
        for page in range(1, MAX_PAGES_PER_STATUS + 1):
            try:
                u = f"{API_URL}/order?limit={LIMIT}&complete=1&page={page}&status={status}"
                tried.append(u)
                r = requests.get(u, headers=HEADERS, timeout=40)
                if not _ok(r):
                    continue
                raw = _json_safe(r)

                arr = []
                if isinstance(raw, dict): 
                    arr = raw.get("data", [])
                elif isinstance(raw, list): 
                    arr = raw
                if isinstance(arr, dict): 
                    arr = arr.get("data", [])
                if not isinstance(arr, list) or not arr:
                    break

                for item in arr:
                    oid = item.get("id") or item.get("order_id")
                    if not oid:
                        continue
                    obj, _ = _detail_by_id_any(oid, tried)
                    if not obj:
                        continue
                    trk = _extract_tracking(obj)
                    if trk == tracking:
                        ship = _extract_shipping_total(obj)
                        return jsonify({
                            "order_id": str(oid),
                            "shipping_total": ship,
                            "shipping_total_reais": ship/100.0,
                            "tracking": trk,
                            "debug": {"matches": ["deep"], "tried": tried}
                        }), 200
            except Exception:
                continue

    return jsonify({"order_id": None, "shipping_total": 0, "tracking": None, "debug": {"matches": [], "tried": tried}}), 200

# registra blueprint
app.register_blueprint(api_v2)

# ------------------------------ Run ------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
