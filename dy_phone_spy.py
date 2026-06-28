import json
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mitmproxy import http

from to_baotxt import BAOTXT_DIR, export_all_baojs, export_baotxt

OUTPUT_DIR = Path(__file__).with_name("output")
BAOJS_DIR = Path(__file__).with_name("baojs")
LEGACY_PRODUCTS_DIR = OUTPUT_DIR / "products"
SUMMARY_FILE = OUTPUT_DIR / "summary.jsonl"
_baojs_ready = False
_active_product_id = ""  # 当前详情页商品（仅 product_pack 更新）
_pack_visit_count: dict[str, int] = {}  # 本会话内各商品进入次数
_baojs_lock = threading.Lock()
_export_lock = threading.Lock()
_last_export_ts: dict[str, float] = {}
EXPORT_DEBOUNCE_SEC = 2.0
_PROTECTED_BAOJS_KEYS = ("product_pack", "product_skus", "product_recommend", "summary")

# 只保留有价值的商品接口（白名单）
API_RULES = {
    "/aweme/v2/shop/promotion/pack/detail/": "product_detail",
    "/aweme/v2/shop/promotion/pack/": "product_pack",
    "/aweme/v2/shop/promotion/recommend/": "product_recommend",
    "/live/promotion/common_skus/": "product_skus",
}

# 明确丢弃的噪音接口
NOISE_PATHS = (
    "/impression",
    "/negfeedback/",
    "/user/behavior/",
    "/homepage/",
    "/marketing_resource/",
    "/suggest_query/",
    "/resource/show_control",
    "/resource_config",
    "/abtest",
    "/skin",
    "/popup/",
    "/gecko/resource/",
    "/prefetch/ack/",
    "/order/getUserAuth/",
    "/order/saveUserAuth/",
)


def _match_api(path: str) -> str | None:
    for pattern, api_type in API_RULES.items():
        if path.startswith(pattern) or path == pattern.rstrip("/"):
            return api_type
    return None


def _is_noise(url: str) -> bool:
    path = urlparse(url).path
    return any(noise in path for noise in NOISE_PATHS)


def _safe_get(data, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _parse_query(url: str) -> dict:
    qs = parse_qs(urlparse(url).query, keep_blank_values=False)
    return {k: v[0] for k, v in qs.items() if v}


def _parse_post_json(flow: http.HTTPFlow) -> dict:
    if flow.request.method != "POST":
        return {}
    try:
        text = flow.request.get_text()
        return json.loads(text) if text else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _find_product_id_deep(obj, depth=0) -> str:
    if depth > 8:
        return ""
    if isinstance(obj, dict):
        for key in ("product_id", "promotion_id", "commodity_id", "item_id"):
            value = obj.get(key)
            if value:
                return str(value)
        for value in obj.values():
            found = _find_product_id_deep(value, depth + 1)
            if found:
                return found
    elif isinstance(obj, str):
        text = obj.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return _find_product_id_deep(json.loads(text), depth + 1)
            except (json.JSONDecodeError, TypeError):
                pass
    return ""


def _resolve_ids(query: dict, post: dict, summary: dict) -> str:
    """只从当前请求解析商品 ID，不用历史 fallback（避免切换商品写错文件）。"""
    for source in (summary, post, query):
        for key in ("product_id", "promotion_id", "item_id"):
            value = source.get(key)
            if value:
                return str(value)
    found = _find_product_id_deep(post)
    if found:
        return found
    return ""


def _fen_to_yuan(value) -> float | None:
    try:
        return round(int(value) / 100, 2)
    except (TypeError, ValueError):
        return None


def _extract_summary(api_type: str, data: dict, query: dict) -> dict:
    summary = {
        "product_id": query.get("product_id") or query.get("promotion_id"),
        "promotion_id": query.get("promotion_id"),
        "title": None,
        "price_yuan": None,
        "shop_name": None,
        "sku_count": None,
        "recommend_count": None,
    }

    if api_type in ("product_pack", "product_detail"):
        meta = _safe_get(data, "promotion_v3", "page_meta", default={})
        common = _safe_get(meta, "common_meta", default={})
        track = _safe_get(meta, "track_meta", default={})

        summary["product_id"] = (
            track.get("product_id")
            or common.get("promotion_id")
            or summary["product_id"]
        )
        summary["promotion_id"] = common.get("promotion_id") or summary["promotion_id"]
        summary["title"] = _safe_get(
            data, "promotion_v3", "top_right_controls", "vo", "meta", "share", "title"
        )
        summary["shop_name"] = _safe_get(
            data, "promotion_v3", "bottom_button", "vo", "meta", "buy", "shop_name"
        )
        min_price = _safe_get(
            common, "activity_info", "price", "price_info", "min_price"
        )
        summary["price_yuan"] = _fen_to_yuan(min_price)

    elif api_type == "product_recommend":
        products = data.get("products") or []
        summary["recommend_count"] = len(products)
        if products:
            first = products[0]
            base = first.get("base_info") or {}
            summary["product_id"] = base.get("product_id") or summary["product_id"]
            summary["title"] = base.get("title") or base.get("name")
            summary["price_yuan"] = _fen_to_yuan(first.get("price") or base.get("price"))

    elif api_type == "product_detail":
        detail = data.get("detail_info") or {}
        imgs = detail.get("detail_imgs") or detail.get("detail_imgs_new") or []
        summary["detail_image_count"] = len(imgs)
        configs = detail.get("detail_config") or []
        if configs and isinstance(configs[0], dict):
            summary["detail_section"] = configs[0].get("main_title")

    elif api_type == "product_skus":
        skus = data.get("skus") or {}
        summary["sku_count"] = len(skus) if isinstance(skus, dict) else len(skus or [])
        cover = data.get("cover")
        if isinstance(cover, dict):
            summary["title"] = cover.get("title")
        summary["price_yuan"] = _fen_to_yuan(data.get("min_price"))
        summary["product_id"] = (
            _find_product_id_deep(query)
            or query.get("product_id")
            or query.get("promotion_id")
        )

    return {k: v for k, v in summary.items() if v not in (None, "", [])}


def ensure_baojs_ready() -> None:
    """启动时自动创建 baojs 目录，并迁移 output/products 里的旧文件。"""
    global _baojs_ready
    if _baojs_ready:
        return

    BAOJS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _baojs_ready = True

    migrated = 0
    if LEGACY_PRODUCTS_DIR.is_dir():
        for legacy_file in LEGACY_PRODUCTS_DIR.glob("*.json"):
            product_id = legacy_file.stem
            target = BAOJS_DIR / f"{product_id}.json"
            if target.is_file():
                continue
            try:
                data = json.loads(legacy_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _save_product(product_id, data)
                    migrated += 1
            except (json.JSONDecodeError, OSError) as err:
                print(f"[baojs] 迁移跳过 {legacy_file.name}: {err}")

    if migrated:
        print(f"[baojs] 已自动迁移 {migrated} 个商品文件 -> {BAOJS_DIR.resolve()}")

    exported = export_all_baojs()[0]
    if exported:
        print(f"[baotxt] 已自动生成 {exported} 个王者上货数据包 -> {BAOTXT_DIR.resolve()}")


def load(loader):
    ensure_baojs_ready()
    print(f"[baojs] 商品 JSON: {BAOJS_DIR.resolve()}")
    print(f"[baotxt] 抖店数据包: {BAOTXT_DIR.resolve()}")


def _load_product_file(product_id: str) -> dict:
    raw_file = BAOJS_DIR / f"{product_id}.json"
    if raw_file.is_file():
        with raw_file.open(encoding="utf-8") as f:
            return json.load(f)
    return {}


def _has_product_pack(baojs: dict) -> bool:
    return bool((baojs.get("product_pack") or {}).get("promotion_v3"))


def _merge_baojs(existing: dict, patch: dict) -> dict:
    merged = {**existing, **patch}
    for key in ("detail", "recommend", "skus"):
        if key in existing and key in patch and isinstance(existing[key], dict) and isinstance(patch[key], dict):
            merged[key] = {**existing[key], **patch[key]}

    for key in _PROTECTED_BAOJS_KEYS:
        if key not in existing:
            continue
        if key not in patch:
            merged[key] = existing[key]
            continue

        old = existing[key]
        new = patch[key]
        if key == "product_pack":
            if _has_product_pack(existing) and not _has_product_pack({"product_pack": new}):
                merged[key] = old
        elif key == "product_skus" and old and not new:
            merged[key] = old
        elif key == "summary":
            if isinstance(old, dict) and isinstance(new, dict):
                merged[key] = {
                    **old,
                    **{k: v for k, v in new.items() if v not in (None, "", [], {})},
                }
            elif old and not new:
                merged[key] = old

    if _has_product_pack(existing) and not _has_product_pack(merged):
        merged["product_pack"] = existing["product_pack"]
        if existing.get("summary") and not merged.get("summary"):
            merged["summary"] = existing["summary"]
        print(f"[baojs] 警告: {merged.get('product_id')} 保留已有 product_pack，拒绝被不完整数据覆盖")

    return merged


def _write_baojs_atomic(raw_file: Path, data: dict) -> None:
    tmp = raw_file.with_suffix(".json.tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(raw_file)


def _baotxt_path(product_id: str) -> Path:
    return BAOTXT_DIR / f"{product_id}.txt"


def _run_export_baotxt(product_id: str, merged: dict, *, updating: bool) -> bool:
    """导出 baotxt；updating=True 时做 debounce，避免 pack/skus 连发重复写。"""
    txt_path = _baotxt_path(product_id)
    had_txt = txt_path.is_file()
    if updating and had_txt:
        now = time.time()
        with _export_lock:
            last = _last_export_ts.get(product_id, 0)
            if now - last < EXPORT_DEBOUNCE_SEC:
                print(f"[baotxt] 跳过重复导出 baotxt/{product_id}.txt（{EXPORT_DEBOUNCE_SEC}s 内）")
                return False
            _last_export_ts[product_id] = now

    baotxt_path = export_baotxt(product_id, merged)
    if baotxt_path:
        action = "已更新" if had_txt else "已生成"
        print(f"[baotxt] {action} baotxt/{product_id}.txt")
        if not merged.get("product_skus"):
            print("[baotxt] 提示: 未抓到 SKU 面板，各规格价格可能相同，请在详情页点击「选规格」")
        return True

    if not (merged.get("product_pack") or {}).get("promotion_v3"):
        print(f"[baotxt] 跳过 baotxt/{product_id}.txt（数据未齐，请点「选规格」或再次进入商品）")
    return False


def _save_product(product_id: str, patch: dict, *, export_txt: bool = False) -> bool:
    ensure_baojs_ready()
    raw_file = BAOJS_DIR / f"{product_id}.json"

    with _baojs_lock:
        existing = _load_product_file(product_id)
        merged = _merge_baojs(existing, patch)
        _write_baojs_atomic(raw_file, merged)

    if export_txt:
        return _run_export_baotxt(product_id, merged, updating=True)

    if not _baotxt_path(product_id).is_file():
        return _run_export_baotxt(product_id, merged, updating=False)
    return False


def _should_export_txt(product_id: str, api_type: str) -> bool:
    """切换商品不改其它 txt；当前商品：缺 txt 则生成，点选规格或再次进入则更新。"""
    global _pack_visit_count
    if api_type == "product_skus" and product_id == _active_product_id:
        return True
    if api_type == "product_pack":
        n = _pack_visit_count.get(product_id, 0) + 1
        _pack_visit_count[product_id] = n
        if not _baotxt_path(product_id).is_file():
            return True
        return n >= 2
    return False


def ingest_api_response(api_type: str, data: dict, product_id: str | None = None) -> bool:
    """Frida 内循环 / 其它来源写入 baojs，逻辑与 mitm response 一致。"""
    global _active_product_id

    if not isinstance(data, dict):
        return False
    if data.get("status_code") not in (None, 0, 200):
        return False

    ensure_baojs_ready()
    summary = _extract_summary(api_type, data, {})
    product_id = product_id or _resolve_ids({}, {}, summary) or _find_product_id_deep(data)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if api_type == "product_detail":
        product_id = product_id or _active_product_id
        if product_id and product_id == _active_product_id:
            _save_product(
                product_id,
                {
                    "product_id": product_id,
                    "updated_at": timestamp,
                    "detail": {"summary": summary, "data": data.get("detail_info")},
                },
                export_txt=False,
            )
            print(f"[frida] product_detail -> baojs/{product_id}.json")
            return True
        return False

    if not product_id:
        return False

    if api_type == "product_pack":
        _active_product_id = product_id
    elif product_id != _active_product_id:
        print(f"[frida] 跳过 {api_type}（当前 {_active_product_id or '无'}，非 {product_id}）")
        return False

    export_txt = _should_export_txt(product_id, api_type)
    patch = {
        "product_id": product_id,
        "updated_at": timestamp,
        "summary": summary,
        api_type: data,
    }
    txt_exported = _save_product(product_id, patch, export_txt=export_txt)
    title = summary.get("title", "")
    print(f"[frida] {api_type} -> baojs/{product_id}.json" + (" + baotxt" if txt_exported else ""))
    if title:
        print(f"  标题: {title[:60]}")
    return True


def response(flow: http.HTTPFlow) -> None:
    global _active_product_id
    url = flow.request.pretty_url
    if _is_noise(url) or not flow.response:
        return

    path = urlparse(url).path
    api_type = _match_api(path)
    if not api_type:
        return

    body = flow.response.get_text(strict=False)
    if not body:
        return

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return

    if isinstance(data, dict) and data.get("status_code") not in (None, 0, 200):
        return

    query = _parse_query(url)
    post = _parse_post_json(flow)
    summary = _extract_summary(api_type, data, {**query, **post})
    product_id = _resolve_ids(query, post, summary)
    if not product_id and api_type == "product_skus":
        product_id = _active_product_id

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if api_type == "product_detail":
        product_id = _resolve_ids(query, post, summary) or _active_product_id
        if product_id and product_id == _active_product_id:
            _save_product(
                product_id,
                {
                    "product_id": product_id,
                    "updated_at": timestamp,
                    "detail": {"summary": summary, "data": data.get("detail_info")},
                },
                export_txt=False,
            )
            print(f"\n[product_detail] 已合并到 baojs/{product_id}.json")
        return

    record = {
        "time": timestamp,
        "api": api_type,
        "method": flow.request.method,
        "path": path,
        "status": flow.response.status_code,
        "summary": summary,
    }

    with SUMMARY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    txt_exported = False
    if product_id:
        if api_type == "product_pack":
            _active_product_id = product_id
        elif product_id != _active_product_id:
            print(f"\n[{api_type}] 跳过（当前详情页 {_active_product_id or '无'}，非 {product_id}）")
            return

        should_export = _should_export_txt(product_id, api_type)
        patch = {
            "product_id": product_id,
            "updated_at": timestamp,
            "summary": summary,
            api_type: data,
        }
        txt_exported = _save_product(product_id, patch, export_txt=should_export)

    title = summary.get("title", "")
    price = summary.get("price_yuan", "")
    print(f"\n[{api_type}] {path}")
    if product_id:
        print(f"  商品ID: {product_id}")
    if title:
        print(f"  标题: {title[:60]}")
    if price:
        print(f"  价格: ¥{price}")
    if product_id:
        print(f"  已写入 baojs/{product_id}.json" + (" + baotxt" if txt_exported else "（txt 未改）"))
    else:
        print("  已写入 output/summary.jsonl")
