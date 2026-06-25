"""把旧的 dy_mall_data.json 整理成 output/summary.jsonl 和 baojs/{product_id}.json"""

import json
from pathlib import Path
from urllib.parse import urlparse

from dy_phone_spy import (
    BAOJS_DIR,
    OUTPUT_DIR,
    SUMMARY_FILE,
    ensure_baojs_ready,
    _extract_summary,
    _is_noise,
    _match_api,
    _parse_query,
    _resolve_ids,
    _save_product,
)
LEGACY_FILE = Path(__file__).with_name("dy_mall_data.json")


def clean_legacy():
    if not LEGACY_FILE.is_file():
        print(f"未找到 {LEGACY_FILE.name}")
        return

    if OUTPUT_DIR.exists():
        import shutil
        shutil.rmtree(OUTPUT_DIR)

    ensure_baojs_ready()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    kept = 0
    merged_detail = 0
    skipped = 0

    with LEGACY_FILE.open(encoding="utf-8") as src, SUMMARY_FILE.open(
        "w", encoding="utf-8"
    ) as out:
        for line in src:
            if not line.strip():
                continue

            row = json.loads(line)
            url = row["url"]
            if _is_noise(url):
                skipped += 1
                continue

            path = urlparse(url).path
            api_type = _match_api(path)
            if not api_type:
                skipped += 1
                continue

            data = row.get("data", {})
            if isinstance(data, dict) and data.get("status_code") not in (None, 0, 200):
                skipped += 1
                continue

            query = _parse_query(url)
            post = row.get("post") or {}
            summary = _extract_summary(api_type, data, {**query, **post})
            product_id = _resolve_ids(query, post, summary)

            if api_type == "product_detail":
                if product_id:
                    _save_product(
                        product_id,
                        {
                            "product_id": product_id,
                            "updated_at": "legacy",
                            "detail": {"summary": summary, "data": data.get("detail_info")},
                        },
                    )
                    merged_detail += 1
                else:
                    skipped += 1
                continue

            record = {
                "time": "legacy",
                "api": api_type,
                "method": row.get("method"),
                "path": path,
                "status": row.get("status"),
                "summary": summary,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")

            if product_id:
                _save_product(
                    product_id,
                    {
                        "product_id": product_id,
                        "updated_at": "legacy",
                        "summary": summary,
                        api_type: data,
                    },
                )

            kept += 1

    print(f"整理完成: 摘要 {kept} 条, 详情合并 {merged_detail} 条, 跳过 {skipped} 条")
    print(f"摘要 -> {SUMMARY_FILE}")
    print(f"商品 -> {BAOJS_DIR}")

if __name__ == "__main__":
    clean_legacy()
