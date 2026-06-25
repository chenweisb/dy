import json
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
_last_product_id = ""

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
    global _last_product_id
    for source in (summary, post, query):
        for key in ("product_id", "promotion_id", "item_id"):
            value = source.get(key)
            if value:
                return str(value)
    found = _find_product_id_deep(post)
    if found:
        return found
    return _last_product_id


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
            _find_product_id_deep(post)
            or query.get("product_id")
            or query.get("promotion_id")
            or _last_product_id
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

    exported = export_all_baojs()
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


def _save_product(product_id: str, patch: dict) -> None:
    global _last_product_id
    ensure_baojs_ready()
    raw_file = BAOJS_DIR / f"{product_id}.json"

    existing = _load_product_file(product_id)
    merged = {**existing, **patch}
    for key in ("detail", "recommend", "skus"):
        if key in existing and key in patch and isinstance(existing[key], dict) and isinstance(patch[key], dict):
            merged[key] = {**existing[key], **patch[key]}

    with raw_file.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    _last_product_id = product_id

    baotxt_path = export_baotxt(product_id, merged)
    if baotxt_path:
        print(f"[baotxt] 已生成 baotxt/{product_id}.txt")
        if not merged.get("product_skus"):
            print("[baotxt] 提示: 未抓到 SKU 面板，各规格价格可能相同，请在详情页点击「选规格」")
    elif not (merged.get("product_pack") or {}).get("promotion_v3"):
        print(f"[baotxt] 跳过 baotxt/{product_id}.txt（缺少 product_pack，需进入商品详情页）")


def response(flow: http.HTTPFlow) -> None:
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

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if api_type == "product_detail":
        if not product_id:
            product_id = _resolve_ids({}, post, {})
        if product_id:
            _save_product(
                product_id,
                {
                    "product_id": product_id,
                    "updated_at": timestamp,
                    "detail": {"summary": summary, "data": data.get("detail_info")},
                },
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

    if product_id:
        patch = {
            "product_id": product_id,
            "updated_at": timestamp,
            "summary": summary,
            api_type: data,
        }
        _save_product(product_id, patch)

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
        print(f"  已写入 output/summary.jsonl + baojs/{product_id}.json")
    else:
        print("  已写入 output/summary.jsonl")
