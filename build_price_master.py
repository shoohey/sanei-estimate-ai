"""産業用部材 価格表PDF → 単価マスターJSON ビルドスクリプト

㈱サンエー様向け「産業用部材価格表」PDF（3ページ）を pdfplumber で
テーブル抽出し、文字正規化・カテゴリ/メーカー補完・単価パース・品種分類を行い、
`knowledge/price_master.json` を生成する。

再実行で常に同じ結果（idは商品コード基準で安定）になるよう設計している。

使い方:
    python build_price_master.py [PDFパス]

PDFパスを省略した場合は既定の Downloads 配下のファイルを探す。
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "knowledge" / "price_master.json"
DEFAULT_PDF = Path(
    "/Users/takaishouhei/Downloads/㈱サンエー様 産業用部材価格表 資料.pdf"
)

SOURCE_NAME = "㈱サンエー様 産業用部材価格表 資料.pdf"
SOURCE_DATE = "2025-05-30"  # PDF右上の日付
SCHEMA_VERSION = 1

# NFKC では正規化されない「CJK部首補助(U+2E80-2EFF)」の補正マップ
RADICAL_FIX = {
    "⻑": "長",  # U+2ED1 CJK RADICAL LONG ONE
    "⻄": "西",  # U+2EC4 CJK RADICAL WEST TWO
}

# カテゴリ表示順（PDFの並び順）
CATEGORY_ORDER = [
    "パワーコンディショナ",
    "延長ケーブル",
    "架台",
    "遠隔監視装置",
    "カーポート",
]


def normalize_text(s: str) -> str:
    """全角互換文字・部首文字を通常の漢字へ正規化する。"""
    if not s:
        return ""
    s = s.replace("\n", " ")
    s = unicodedata.normalize("NFKC", s)
    for bad, good in RADICAL_FIX.items():
        s = s.replace(bad, good)
    # 連続空白を1つに、前後の空白除去
    s = re.sub(r"[ \t　]+", " ", s).strip()
    return s


def parse_price(raw: str) -> tuple[int | None, str]:
    """単価セルを (整数価格, 注記) に分解する。

    "90,000" -> (90000, "")
    "別途問合せ" -> (None, "別途問合せ")
    "" -> (None, "")
    """
    raw = normalize_text(raw)
    if not raw:
        return None, ""
    # 数値（カンマ区切り）として解釈できるか
    digits = raw.replace(",", "")
    if re.fullmatch(r"-?\d+", digits):
        return int(digits), ""
    # 数値でない（"別途問合せ" 等）
    return None, raw


def classify_item(name: str, model: str, unit_price: int | None, note: str) -> str:
    """品名等から品種(item_kind)を分類する。"""
    n = name
    if "延長保証" in n:
        return "warranty_extension"
    if any(k in n for k in ("現地調整費", "施工SV", "施工指導", "強度計算費", "運賃")):
        return "service"
    if note and unit_price is None:
        # "別途問合せ" 等の都度見積項目
        return "service"
    return "product"


def extract_warranty(remarks: str) -> str:
    """備考から保証文言を抽出（"保証"を含む場合のみ）。"""
    if remarks and "保証" in remarks:
        return remarks
    return ""


def build_rows(pdf_path: Path) -> tuple[list[tuple[int, list[str]]], list[str]]:
    """PDFから (データ行[(ページ番号, セル)], 条件フッター行) を抽出する。"""
    rows: list[tuple[int, list[str]]] = []
    conditions: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for pidx, page in enumerate(pdf.pages, start=1):
            for table in page.extract_tables():
                for r in table:
                    cells = [normalize_text(c or "") for c in r]
                    # ヘッダ行（部材/メーカー...）を除外
                    if cells[0].replace(" ", "") == "部材" and cells[1] == "メーカー":
                        continue
                    rows.append((pidx, cells))
            # 条件フッター（最終ページの【条件】以降）
            text = normalize_text(page.extract_text() or "")
            idx = text.find("【条件】")
            if idx >= 0:
                tail = text[idx:]
                # ページ番号表記を除去
                tail = re.sub(r"\s*\d\s*/\s*\d\s*ページ\s*$", "", tail).strip()
                # 文単位でざっくり分割（番号付き条文＋注意書き）
                for part in re.split(r"(?<=。)\s+|(?<=\))\s+(?=\d)", tail):
                    part = part.strip()
                    if part and part not in conditions:
                        conditions.append(part)
    return rows, conditions


def assign_categories(rows: list[list[str]]) -> list[str]:
    """各行のカテゴリを決定する（マージ欠落を補完）。

    - col0(部材) が非空の行を境界とする
    - 先頭カテゴリは「パワーコンディショナ」(col0が欠落しているため固定)
    - 架台 は延長ケーブル区間内の商品コードが JAM で始まる最初の行から開始
      （マージセル欠落の補正）
    """
    n = len(rows)
    categories = [""] * n

    # 1. 明示境界（col0非空）
    boundaries: list[tuple[int, str]] = []
    for i, r in enumerate(rows):
        if r[0]:
            boundaries.append((i, r[0]))
    # 先頭をパワコンに
    if not boundaries or boundaries[0][0] != 0:
        boundaries.insert(0, (0, "パワーコンディショナ"))

    # 2. 架台境界の補完: 「延長ケーブル」区間内で商品コードが JAM で始まる最初の行
    cable_start = None
    cable_end = n
    for k, (idx, name) in enumerate(boundaries):
        if name == "延長ケーブル":
            cable_start = idx
            cable_end = boundaries[k + 1][0] if k + 1 < len(boundaries) else n
            break
    if cable_start is not None:
        for i in range(cable_start, cable_end):
            if rows[i][2].startswith("JAM"):
                boundaries.append((i, "架台"))
                break

    boundaries.sort(key=lambda x: x[0])

    # 3. 前方補完
    cur = boundaries[0][1]
    bptr = 0
    for i in range(n):
        while bptr < len(boundaries) and boundaries[bptr][0] == i:
            cur = boundaries[bptr][1]
            bptr += 1
        categories[i] = cur
    return categories


def main() -> int:
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    if not pdf_path.exists():
        print(f"[ERROR] PDFが見つかりません: {pdf_path}")
        return 1

    page_rows, conditions = build_rows(pdf_path)
    pages = [p for p, _ in page_rows]
    rows = [c for _, c in page_rows]
    categories = assign_categories(rows)

    products: list[dict] = []
    last_parent_code = ""      # 直近の親製品コード（保証/オプションの紐付け用）
    last_parent_maker = ""
    cur_maker = ""
    makers_seen: list[str] = []

    for i, r in enumerate(rows):
        part, maker, code, model, name, price_raw, remarks = (
            r[0], r[1], r[2], r[3], r[4], r[5], r[6]
        )
        source_page = pages[i]
        # メーカー前方補完
        if maker:
            cur_maker = maker
        # --- メーカー補正: page1末尾のオムロン自家消費ブロックがマージ欠落で
        #     SUNGROW のまま引き継がれる問題を補正。KPW/KP- 型番はオムロン製。 ---
        if cur_maker == "SUNGROW" and (
            model.startswith("KPW") or model.startswith("KP-")
        ):
            cur_maker = "オムロン (自家消費用途)"
        maker = cur_maker
        if maker and maker not in makers_seen:
            makers_seen.append(maker)

        unit_price, price_note = parse_price(price_raw)
        item_kind = classify_item(name, model, unit_price, price_note)
        warranty = extract_warranty(remarks)

        # ¥0 かつ「都度見積/別途見積」系は実質「別途見積」。X0MED05100(別途問合せ)と
        # 同様に unit_price=None + note 扱いに正規化し、見積側で誤って¥0計上されるのを防ぐ。
        if unit_price == 0 and any(k in (remarks + name) for k in ("都度見積", "別途見積", "都度")):
            price_note = price_note or "別途見積"
            unit_price = None
            item_kind = "service"

        # 親コードの追跡（model付きのproduct行を親とみなす）
        if item_kind == "product" and model:
            last_parent_code = code
            last_parent_maker = maker
        parent_code = ""
        if item_kind in ("warranty_extension", "service") and not model:
            if last_parent_maker == maker:
                parent_code = last_parent_code

        products.append({
            "id": "",  # 後段でユニーク付与
            "category": categories[i],
            "maker": maker,
            "product_code": code,
            "model": model,
            "name": name,
            "unit_price": unit_price,
            "unit_price_note": price_note,
            "remarks": remarks,
            "warranty_text": warranty,
            "item_kind": item_kind,
            "parent_code": parent_code,
            "source_page": source_page,
            "row_index": i,          # 抽出順（canonical決定の安定化に使用）
            "is_duplicate": False,   # 同一コードが複数掲載されている場合 True
            "is_canonical": True,    # 重複時に優先採用する1件（後段で判定）
            "is_intra_page_duplicate": False,  # 同一ページ内で重複しているか
        })

    # ------------------------------------------------------------------
    # 重複コードの処理 + ユニークID付与
    #   PDFには「オムロン(自家消費用途)」のPCS群が page1/page2 で価格違いで
    #   重複掲載されている。破棄せず両方保持し、より網羅的な後ページ(page2)を
    #   canonical(価格参照の優先)とする。
    # ------------------------------------------------------------------
    code_indices: dict[str, list[int]] = {}
    for i, p in enumerate(products):
        if p["product_code"]:
            code_indices.setdefault(p["product_code"], []).append(i)

    for code, idxs in code_indices.items():
        if len(idxs) > 1:
            pages = [products[i]["source_page"] for i in idxs]
            intra = len(set(pages)) < len(pages)  # 同一ページ内重複あり
            # (ページ, 抽出順) で決定的にソート → 最後尾(後段/後ページ)を canonical 採用。
            # page1/page2クロス重複は後ページ(網羅版)、同一ページ内重複は下段ブロックを採用。
            idxs_sorted = sorted(
                idxs, key=lambda i: (products[i]["source_page"], products[i]["row_index"])
            )
            canonical_idx = idxs_sorted[-1]
            for i in idxs_sorted:
                products[i]["is_duplicate"] = True
                products[i]["is_canonical"] = (i == canonical_idx)
                products[i]["is_intra_page_duplicate"] = intra

    # ユニークID付与: 同一baseが複数あれば末尾に連番サフィックス(#1,#2)
    def _base_key(p: dict) -> str:
        return p["product_code"] or f"{p['category']}:{p['name']}"[:80]

    base_counts: dict[str, int] = {}
    for p in products:
        base_counts[_base_key(p)] = base_counts.get(_base_key(p), 0) + 1
    assign: dict[str, int] = {}
    for p in products:
        base = _base_key(p)
        if base_counts[base] > 1:
            assign[base] = assign.get(base, 0) + 1
            p["id"] = f"{base}#{assign[base]}"
        else:
            p["id"] = base

    # カテゴリ順を実データに合わせる
    present = [c for c in CATEGORY_ORDER if any(p["category"] == c for p in products)]
    # 想定外カテゴリがあれば末尾に追加
    for p in products:
        if p["category"] not in present:
            present.append(p["category"])

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE_NAME,
        "source_date": SOURCE_DATE,
        "currency": "JPY",
        "tax_included": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "categories": present,
        "makers": makers_seen,
        "conditions": conditions,
        "product_count": len(products),
        "products": products,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # 検証サマリ
    # ------------------------------------------------------------------
    print(f"[OK] 出力: {OUTPUT_PATH}")
    print(f"  製品数: {len(products)}")
    print(f"  メーカー数: {len(makers_seen)} -> {makers_seen}")
    print("  カテゴリ別件数:")
    for c in present:
        sub = [p for p in products if p["category"] == c]
        priced = [p for p in sub if p["unit_price"] is not None]
        print(f"    {c:14}: {len(sub):3}件 (価格あり {len(priced)})")
    kinds = {}
    for p in products:
        kinds[p["item_kind"]] = kinds.get(p["item_kind"], 0) + 1
    print(f"  品種別: {kinds}")
    # コード重複チェック
    codes = [p["product_code"] for p in products if p["product_code"]]
    dups = {c for c in codes if codes.count(c) > 1}
    dup_products = [p for p in products if p["is_duplicate"]]
    canonical_dups = [p for p in dup_products if p["is_canonical"]]
    intra = [p for p in dup_products if p.get("is_intra_page_duplicate")]
    cross = [p for p in dup_products if not p.get("is_intra_page_duplicate")]
    print(f"  商品コード: {len(codes)}件, 重複コード {len(dups)}種 / 重複行 {len(dup_products)}件")
    print(f"    └ 重複は全てメーカー: {sorted(set(p['maker'] for p in dup_products))}")
    print(f"    └ canonical(優先採用) {len(canonical_dups)}件 / superseded {len(dup_products)-len(canonical_dups)}件")
    print(f"    └ クロスページ重複 {len(cross)}件 / 同一ページ内重複 {len(intra)}件（要・正価格確認）")
    # ページ別件数
    from collections import Counter as _C
    pg = _C(p["source_page"] for p in products)
    print(f"  ページ別: {dict(sorted(pg.items()))}")
    # 価格未設定（注記なし）の行を警告
    missing = [p for p in products if p["unit_price"] is None and not p["unit_price_note"]]
    print(f"  価格null(注記なし): {len(missing)}件")
    for p in missing:
        print(f"    - [{p['category']}] {p['product_code']} {p['name'][:30]}")
    print(f"  条件フッター: {len(conditions)}項目")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
