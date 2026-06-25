"""将 baojs 转为王者上货抖店数据包格式（与 mobna.txt 一致）。"""

import json
import re
from pathlib import Path
from urllib.parse import urlparse

BAOJS_DIR = Path(__file__).with_name("baojs")
BAOTXT_DIR = Path(__file__).with_name("baotxt")

SIZE_PATTERN = re.compile(r"^(XS|S|M|L|XL|2XL|3XL|4XL|5XL|\d+XL|\d+)$", re.I)
IMAGE_HOST_KEYWORDS = ("ecombdimg.com", "douyinpic.com", "byteimg.com")


def _safe_get(data, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _to_fen(value) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value < 1000:
            return int(round(value * 100))
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _yuan_label(fen: int) -> str:
    yuan = fen / 100
    text = f"{yuan:.2f}".rstrip("0").rstrip(".")
    return f"¥{text}"


def _is_product_image(url: str) -> bool:
    if not isinstance(url, str) or not url.startswith("http"):
        return False
    lower = url.lower()
    if not any(host in lower for host in IMAGE_HOST_KEYWORDS):
        return False
    blocked = (
        "/play/?video_id=",
        "sellpoint",
        "main_img_pop",
        "arrow-right.png",
        ".mp4",
        "douyinvod.com",
        "aweme-avatar",
        "amemv.com/aweme/v1/play",
        "byteimg.com/tos-cn-i-8j",
    )
    return not any(x in lower for x in blocked)


def _dedupe(items):
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _guess_dim_name(names, index):
    if not names:
        return f"规格{index + 1}"
    clean = [str(n).strip() for n in names if n]
    if clean and all(SIZE_PATTERN.match(n) for n in clean):
        return "尺码"
    if index == 0:
        return "颜色分类"
    if any(SIZE_PATTERN.match(n) for n in clean):
        return "尺码"
    return f"规格{index + 1}"


def _extract_spec_dims(pv3: dict) -> list[dict]:
    spec_info = _safe_get(pv3, "page_meta", "common_meta", "sku_spec_info", default=[])
    dims = []
    if isinstance(spec_info, list):
        for idx, group in enumerate(spec_info):
            if not isinstance(group, dict):
                continue
            ids = group.get("spec_ids") or []
            names = group.get("spec_names") or []
            items = []
            for spec_id, spec_name in zip(ids, names):
                items.append({"spec_id": str(spec_id), "spec_name": str(spec_name)})
            if items:
                dims.append(
                    {
                        "name": group.get("spec_default_name")
                        or _guess_dim_name(names, idx),
                        "items": items,
                    }
                )
    if dims:
        return dims

    for comp in pv3.get("components") or []:
        for sl in comp.get("slices") or []:
            data = sl.get("data")
            if not isinstance(data, dict):
                continue
            parsed = []
            for item in data.get("spec_items") or []:
                spec_id = item.get("spec_id")
                spec_name = item.get("spec_name")
                if spec_id and spec_name:
                    parsed.append({"spec_id": str(spec_id), "spec_name": str(spec_name)})
            if parsed:
                dims.append(
                    {
                        "name": _guess_dim_name([i["spec_name"] for i in parsed], len(dims)),
                        "items": parsed,
                    }
                )
                break
        if dims:
            break
    return dims


def _extract_small_pic(pv3: dict) -> dict:
    small_pic = {}
    for comp in pv3.get("components") or []:
        for sl in comp.get("slices") or []:
            if _safe_get(sl, "template", "name") != "std_sku_preview":
                continue
            for item in (sl.get("data") or {}).get("spec_items") or []:
                spec_id = item.get("spec_id")
                image = item.get("image") or {}
                url = image.get("url")
                if not url:
                    urls = image.get("url_list") or []
                    url = urls[0] if urls else None
                if spec_id and url:
                    small_pic[str(spec_id)] = url
    return small_pic


def _extract_main_imgs(pv3: dict) -> list:
    content_list = []
    seen_urls = set()

    for cell in _safe_get(pv3, "header_v2", "cells", default=[]) or []:
        media = cell.get("media") or {}
        url_model = _safe_get(media, "image", "url_model", default={})
        if not url_model:
            url_model = _safe_get(media, "video", "poster_image", "url_model", default={})

        url_list = url_model.get("url_list") or []
        url = url_list[0] if url_list else None
        width = url_model.get("width") or 1080
        height = url_model.get("height") or 1080

        if url and _is_product_image(url) and url not in seen_urls:
            seen_urls.add(url)
            content_list.append({"width": width, "url": url, "height": height})

    if not content_list:
        for url in _collect_image_urls(pv3.get("header_v2") or {}):
            if url not in seen_urls:
                seen_urls.add(url)
                content_list.append({"width": 1080, "url": url, "height": 1080})

    if not content_list:
        return []

    return [{"content_list": content_list, "name": "商品", "type": "image"}]


def _collect_image_urls(obj, urls=None, depth=0):
    if urls is None:
        urls = []
    if depth > 14:
        return urls
    if isinstance(obj, dict):
        if "url_list" in obj and isinstance(obj["url_list"], list):
            for url in obj["url_list"]:
                if _is_product_image(url):
                    urls.append(url)
        url = obj.get("url")
        if _is_product_image(url):
            urls.append(url)
        for value in obj.values():
            _collect_image_urls(value, urls, depth + 1)
    elif isinstance(obj, list):
        for value in obj[:30]:
            _collect_image_urls(value, urls, depth + 1)
    return urls


def _url_to_uri(url: str) -> str:
    path = urlparse(url).path
    if "/img/" in path:
        return path.split("/img/", 1)[1]
    return path.lstrip("/")


def _extract_detail_imgs(baojs: dict, fallback_urls: list[str] | None = None) -> list:
    detail = _safe_get(baojs, "detail", "data", default={})
    images = []

    for key in ("detail_imgs", "detail_imgs_new"):
        for item in detail.get(key) or []:
            if not isinstance(item, dict):
                continue
            url_list = [u for u in (item.get("url_list") or []) if _is_product_image(u)]
            if not url_list and item.get("url"):
                url_list = [item["url"]]
            if not url_list:
                continue
            images.append(
                {
                    "height": item.get("height") or 1080,
                    "data_size": item.get("data_size") or 0,
                    "uri": item.get("uri") or _url_to_uri(url_list[0]),
                    "url_list": url_list,
                    "width": item.get("width") or 1080,
                }
            )

    if images:
        return images

    for url in fallback_urls or []:
        images.append(
            {
                "height": 1080,
                "data_size": 0,
                "uri": _url_to_uri(url),
                "url_list": [url],
                "width": 1080,
            }
        )
    return images


def _extract_product_format(pv3: dict, baojs: dict) -> list:
    detail_format = _safe_get(baojs, "detail", "data", "product_format")
    if detail_format:
        if isinstance(detail_format, list):
            return detail_format
        return [detail_format]

    pack_format = _safe_get(baojs, "product_pack", "promotion_v3", "product_format")
    if pack_format:
        if isinstance(pack_format, list):
            return pack_format
        return [pack_format]

    props = []
    for comp in pv3.get("components") or []:
        for sl in comp.get("slices") or []:
            if _safe_get(sl, "template", "name") != "product_properties_v2":
                continue
            data = sl.get("data") or {}
            for item in data.get("property_list") or data.get("content_list") or []:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("text") or item.get("tag_code")
                desc = item.get("value") or item.get("desc")
                if isinstance(desc, dict):
                    desc = desc.get("desc")
                if name and desc:
                    props.append(
                        {
                            "name": name,
                            "message": [{"desc": str(desc)}],
                            "pic_urls": None,
                            "property_id": str(item.get("property_id") or item.get("id") or len(props)),
                        }
                    )
    if props:
        return [{"format": props, "media_elements": None}]
    return [{"format": [], "media_elements": None}]


def _extract_prices(pv3: dict, summary: dict) -> tuple[int, int, int]:
    """返回 (券后价分, 活动价分, 原价分)。"""
    common = _safe_get(pv3, "page_meta", "common_meta", default={}) or {}

    coupon_fen = _to_fen(
        _safe_get(common, "activity_info", "price", "discount_price", "price_info", "min_price")
    )
    effective_fen = _to_fen(
        _safe_get(common, "activity_info", "price", "price_info", "min_price")
    )
    original_fen = _to_fen(_safe_get(common, "activity_info", "price", "price_info", "original_price"))

    entrance = common.get("entrance_info")
    if isinstance(entrance, str) and "price_info" in entrance:
        try:
            info = json.loads(entrance).get("price_info") or {}
            effective_fen = effective_fen or _to_fen(info.get("min_price"))
            original_fen = original_fen or _to_fen(info.get("original_price"))
        except json.JSONDecodeError:
            pass

    marketing = _safe_get(common, "pdp_bcm_params", "standard_product_marketing_param")
    if not marketing:
        marketing = common.get("standard_product_marketing_param")
    if isinstance(marketing, str):
        try:
            info = json.loads(marketing)
            coupon_fen = coupon_fen or _to_fen(info.get("discount_price"))
            effective_fen = effective_fen or _to_fen(info.get("effective_price"))
            original_fen = original_fen or _to_fen(info.get("regular_price"))
        except json.JSONDecodeError:
            pass

    coupon_fen = coupon_fen or _to_fen(common.get("discount_min_price"))
    effective_fen = effective_fen or _to_fen(common.get("product_min_price"))
    original_fen = original_fen or _to_fen(common.get("regular_price"))

    std_coupon, std_effective = _parse_std_price_list(pv3)
    coupon_fen = coupon_fen or std_coupon
    effective_fen = effective_fen or std_effective

    if not effective_fen:
        effective_fen = _to_fen(summary.get("price_yuan"))
    if not original_fen:
        original_fen = effective_fen
    if not coupon_fen:
        coupon_fen = effective_fen
    return coupon_fen, effective_fen, original_fen


def _parse_std_price_list(pv3: dict) -> tuple[int, int]:
    coupon_fen = effective_fen = 0
    for comp in pv3.get("components") or []:
        for sl in comp.get("slices") or []:
            if _safe_get(sl, "template", "name") != "std_price":
                continue
            for block in (sl.get("data") or {}).get("price_list") or []:
                if not isinstance(block, dict):
                    continue
                left = block.get("left_price") or {}
                integer = str(left.get("integer") or "0")
                decimal = str(left.get("decimal") or "0").lstrip(".")
                try:
                    fen = _to_fen(float(f"{integer}.{decimal or '0'}"))
                except ValueError:
                    continue
                prefix = str(block.get("prefix") or "")
                price_type = block.get("PriceType")
                if "券后" in prefix or price_type == 1:
                    coupon_fen = coupon_fen or fen
                elif price_type == 5:
                    effective_fen = effective_fen or fen
    return coupon_fen, effective_fen


def _extract_sku_price_index(baojs: dict) -> tuple[dict, dict]:
    """从 product_skus 接口提取 sku_id / 规格组合 -> 价格信息。"""
    by_sku_id: dict[str, dict] = {}
    by_spec_key: dict[str, dict] = {}

    api = baojs.get("product_skus") or {}
    if not isinstance(api, dict):
        return by_sku_id, by_spec_key

    skus = api.get("skus") or {}
    if not isinstance(skus, dict):
        return by_sku_id, by_spec_key

    for combo_key, item in skus.items():
        if not isinstance(item, dict):
            continue
        sku_id = str(item.get("sku_id") or "")
        effective = _to_fen(item.get("price"))
        coupon = _to_fen(item.get("discount_price")) or effective
        regular = _to_fen(item.get("regular_price")) or _to_fen(item.get("origin_price"))
        stock = item.get("stock_num")
        if stock is None:
            stock = item.get("now_stock")
        if stock is None:
            stock = 999
        entry = {
            "price": regular or effective,
            "effective": effective,
            "discount_price": coupon,
            "stock": int(stock) if stock is not None else 999,
        }
        by_spec_key[str(combo_key)] = entry
        if sku_id:
            by_sku_id[sku_id] = entry
    return by_sku_id, by_spec_key


def _lookup_sku_price(
    by_sku_id: dict,
    by_spec_key: dict,
    *,
    spec_key: str | None = None,
    platform_sku_id: str | None = None,
    coupon_fen: int,
    effective_fen: int,
    original_fen: int,
) -> dict:
    entry = None
    if platform_sku_id:
        entry = by_sku_id.get(str(platform_sku_id))
    if not entry and spec_key:
        entry = by_spec_key.get(str(spec_key))
    if entry:
        coupon = entry["discount_price"] or coupon_fen
        effective = entry["effective"] or effective_fen
        original = entry["price"] or original_fen or effective
        return {
            "price": original,
            "effective": effective,
            "discount_price": coupon,
            "stock": entry["stock"],
        }
    return {
        "price": original_fen or effective_fen,
        "effective": effective_fen,
        "discount_price": coupon_fen or effective_fen,
        "stock": 999,
    }


def _spec_block(name: str, spec_items: list) -> dict:
    return {
        "name": name,
        "spec_items": spec_items,
        "hide_spec": False,
        "default_select": False,
        "spec_type": 0,
        "show_big_pic": True,
        "show_small_pic": True,
        "spec_mode": 2,
    }


def _build_specs_and_skus(
    pv3: dict,
    spec_dims: list[dict],
    coupon_fen: int,
    effective_fen: int,
    original_fen: int,
    preview_small_pic: dict,
    sku_price_index: tuple[dict, dict] | None = None,
) -> tuple[list, dict, dict]:
    """王者上货要求：specs 只有 1 组，且 skus 的 key 必须等于 spec_items[].id。"""
    mappings = _safe_get(pv3, "page_meta", "sku_meta", "sku_mappings", default={}) or {}
    by_sku_id, by_spec_key = sku_price_index or ({}, {})

    id_to_name = {}
    for dim in spec_dims:
        for item in dim["items"]:
            id_to_name[item["spec_id"]] = item["spec_name"]

    spec_items = []
    skus = {}
    small_pic = {}

    def add_item(
        item_id: str,
        name: str,
        pic_key: str | None = None,
        spec_key: str | None = None,
        platform_sku_id: str | None = None,
    ):
        item_id = str(item_id)
        if any(x["id"] == item_id for x in spec_items):
            return
        price_info = _lookup_sku_price(
            by_sku_id,
            by_spec_key,
            spec_key=spec_key or item_id,
            platform_sku_id=platform_sku_id,
            coupon_fen=coupon_fen,
            effective_fen=effective_fen,
            original_fen=original_fen,
        )
        coupon = price_info["discount_price"]
        spec_items.append(
            {
                "id": item_id,
                "name": name,
                "default_select": False,
                "icon": None,
                "price": _yuan_label(coupon),
            }
        )
        skus[item_id] = {
            "price": price_info["price"] or price_info["effective"],
            "discount_price": coupon,
            "stock": price_info["stock"],
        }
        if pic_key and pic_key in preview_small_pic:
            small_pic[item_id] = preview_small_pic[pic_key]

    if isinstance(mappings, dict) and mappings:
        if len(spec_dims) <= 1:
            for spec_id, sku_id in mappings.items():
                sid = str(spec_id).split("_")[0]
                name = id_to_name.get(sid, sid)
                add_item(sid, name, sid, spec_key=str(spec_id), platform_sku_id=str(sku_id))
        else:
            for combo_key, sku_id in mappings.items():
                sid = str(sku_id)
                parts = str(combo_key).split("_")
                names = [id_to_name.get(p, "") for p in parts if id_to_name.get(p)]
                combined = " ".join(names) if names else sid
                color_id = parts[0] if parts else None
                add_item(sid, combined, color_id, spec_key=str(combo_key), platform_sku_id=sid)
    elif len(spec_dims) == 1:
        for item in spec_dims[0]["items"]:
            add_item(item["spec_id"], item["spec_name"], item["spec_id"], spec_key=item["spec_id"])

    if not spec_items:
        return [], {}, {}

    spec_name = spec_dims[0]["name"] if len(spec_dims) == 1 else "规格"
    return [_spec_block(spec_name, spec_items)], skus, small_pic


def validate_package(package: dict) -> list[str]:
    errors = []
    if "item" in package:
        errors.append("包含旧版 item 包裹")
    if not package.get("main_imgs"):
        errors.append("缺少 main_imgs")
    if not package.get("goods_title"):
        errors.append("缺少 goods_title")
    if not package.get("goods_id"):
        errors.append("缺少 goods_id")
    specs = package.get("specs") or []
    skus = package.get("skus") or {}
    if not specs or not skus:
        errors.append("缺少 specs 或 skus")
    elif len(specs) != 1:
        errors.append(f"specs 维度应为 1，当前 {len(specs)}")
    else:
        spec_ids = {str(i["id"]) for i in specs[0].get("spec_items") or []}
        sku_keys = {str(k) for k in skus.keys()}
        if spec_ids != sku_keys:
            errors.append(f"spec id 与 sku key 不一致: {len(spec_ids)} vs {len(sku_keys)}")
    return errors


def convert_baojs_to_package(baojs: dict) -> dict | None:
    product_id = str(baojs.get("product_id") or (baojs.get("summary") or {}).get("product_id") or "")
    if not product_id:
        return None

    summary = baojs.get("summary") or {}
    pack = baojs.get("product_pack") or {}
    pv3 = pack.get("promotion_v3")

    if not pv3:
        title = summary.get("title") or ""
        price_fen = _to_fen(summary.get("price_yuan"))
        images = []
        recommend = baojs.get("product_recommend") or {}
        for product in recommend.get("products") or []:
            base = product.get("base_info") or {}
            if str(base.get("product_id")) == product_id or not title:
                title = title or base.get("title") or ""
                for img in base.get("images") or []:
                    images.extend(img.get("url_list") or [])
                if not price_fen:
                    price_fen = _to_fen((product.get("price") or {}).get("min_price"))
                break
        images = _dedupe([u for u in images if _is_product_image(u)])
        content_list = [{"width": 1080, "url": u, "height": 1080} for u in images]
        return {
            "main_imgs": [{"content_list": content_list, "name": "商品", "type": "image"}] if content_list else [],
            "goods_title": title,
            "price": price_fen,
            "goods_id": product_id,
            "product_format": [{"format": [], "media_elements": None}],
            "detail_imgs": _extract_detail_imgs(baojs, images),
            "skus": {},
            "specs": [],
            "small_pic": {},
        }

    title = summary.get("title") or _safe_get(
        pv3, "top_right_controls", "vo", "meta", "share", "title", default=""
    )
    coupon_fen, effective_fen, original_fen = _extract_prices(pv3, summary)
    spec_dims = _extract_spec_dims(pv3)
    preview_small_pic = _extract_small_pic(pv3)
    sku_price_index = _extract_sku_price_index(baojs)
    specs, skus, small_pic = _build_specs_and_skus(
        pv3,
        spec_dims,
        coupon_fen,
        effective_fen,
        original_fen,
        preview_small_pic,
        sku_price_index,
    )
    main_imgs = _extract_main_imgs(pv3)
    fallback_urls = [img["url"] for block in main_imgs for img in block.get("content_list", [])]
    detail_imgs = _extract_detail_imgs(baojs, fallback_urls)

    goods_price = coupon_fen or effective_fen
    if skus:
        goods_price = min(item.get("discount_price") or goods_price for item in skus.values())

    return {
        "main_imgs": main_imgs,
        "goods_title": title,
        "price": goods_price,
        "goods_id": product_id,
        "product_format": _extract_product_format(pv3, baojs),
        "detail_imgs": detail_imgs,
        "skus": skus,
        "specs": specs,
        "small_pic": small_pic,
    }


def export_baotxt(product_id: str, baojs: dict | None = None) -> Path | None:
    BAOTXT_DIR.mkdir(parents=True, exist_ok=True)
    if baojs is None:
        src = BAOJS_DIR / f"{product_id}.json"
        if not src.is_file():
            return None
        baojs = json.loads(src.read_text(encoding="utf-8"))

    package = convert_baojs_to_package(baojs)
    if not package:
        return None

    errors = validate_package(package)
    if errors:
        print(f"[baotxt] {product_id} 校验失败: {', '.join(errors)}")
        stale = BAOTXT_DIR / f"{product_id}.txt"
        if stale.is_file():
            stale.unlink()
        return None

    out = BAOTXT_DIR / f"{product_id}.txt"
    out.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
    return out


def cleanup_stale_baotxt() -> int:
    """删除旧版 item 格式或校验失败的 txt。"""
    removed = 0
    if not BAOTXT_DIR.is_dir():
        return removed
    for txt in BAOTXT_DIR.glob("*.txt"):
        try:
            package = json.loads(txt.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            txt.unlink(missing_ok=True)
            removed += 1
            continue
        if validate_package(package):
            txt.unlink()
            removed += 1
    return removed


def export_all_baojs() -> int:
    cleanup_stale_baotxt()
    count = 0
    if not BAOJS_DIR.is_dir():
        return count
    for src in BAOJS_DIR.glob("*.json"):
        if export_baotxt(src.stem):
            count += 1
    return count


if __name__ == "__main__":
    total = export_all_baojs()
    print(f"已导出 {total} 个王者上货数据包 -> {BAOTXT_DIR.resolve()}")

    ok = fail = 0
    for txt in BAOTXT_DIR.glob("*.txt"):
        pkg = json.loads(txt.read_text(encoding="utf-8"))
        errs = validate_package(pkg)
        if errs:
            fail += 1
            print(f"  FAIL {txt.name}: {errs}")
        else:
            ok += 1
    print(f"校验通过 {ok} 个, 失败 {fail} 个")
