"""産業用部材 単価マスター（㈱サンエー様 価格表）

`knowledge/price_master.json`（build_price_master.py が生成）を読み込み、
カテゴリ/メーカー/型番/品名/商品コードでの検索・単価参照を提供する。

製品カタログ（product_registry.py: モジュール/PCS等の詳細スペック）とは別系統で、
こちらは「部材ごとの単価表」を扱う。見積明細への単価自動引き当てに使う。

主な公開関数:
    - load_price_master()           マスター全体(dict)を読み込み（キャッシュ）
    - get_products()                製品リスト
    - get_categories() / get_makers()
    - get_conditions() / get_meta()
    - search(query, ...)            横断検索
    - find_by_code(code)            商品コード完全一致
    - find_by_model(model, fuzzy)   型番一致（スコア順）
    - find_price(maker, model, ...) 単価参照（canonical優先の1件）
    - format_price(value)           表示用フォーマット
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MASTER_PATH = (
    Path(__file__).resolve().parent.parent / "knowledge" / "price_master.json"
)

# 読み込みキャッシュ（mtime+sizeのシグネチャで自動失効）
_CACHE: dict[str, Any] = {"sig": None, "data": None}


def _file_signature() -> Optional[tuple]:
    """ファイル変更検知用シグネチャ (mtime, size)。無ければ None。"""
    try:
        st = MASTER_PATH.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# ロード
# ---------------------------------------------------------------------------
def load_price_master(force: bool = False) -> dict:
    """単価マスターJSONを読み込む（mtime+sizeベースのキャッシュ付き）。

    ファイルが無い/壊れている場合は空のマスターを返す。
    """
    sig = _file_signature()

    if not force and _CACHE["data"] is not None and _CACHE["sig"] == sig:
        return _CACHE["data"]

    if not MASTER_PATH.exists():
        logger.warning(f"price_master.json が見つかりません: {MASTER_PATH}")
        data = _empty_master()
    else:
        try:
            with open(MASTER_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            data = _normalize_master(raw)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"price_master.json 読み込み失敗: {e}")
            data = _empty_master()

    _CACHE["data"] = data
    _CACHE["sig"] = sig
    return data


def _empty_master() -> dict:
    return {
        "schema_version": 0,
        "source": "",
        "source_date": "",
        "currency": "JPY",
        "tax_included": False,
        "updated_at": "",
        "categories": [],
        "makers": [],
        "conditions": [],
        "product_count": 0,
        "products": [],
    }


def _normalize_master(raw: dict) -> dict:
    """読み込んだJSONを安全な形に整える（欠損キーを補完）。"""
    if not isinstance(raw, dict):
        return _empty_master()
    products = raw.get("products")
    if not isinstance(products, list):
        products = []
    products = [p for p in products if isinstance(p, dict)]
    # 重複判定キーの欠損補完（検索の重複排除を確実にする）
    for p in products:
        p.setdefault("is_duplicate", False)
        p.setdefault("is_canonical", True)
        p.setdefault("source_page", 0)

    base = _empty_master()
    base.update({
        "schema_version": raw.get("schema_version", 1),
        "source": raw.get("source", ""),
        "source_date": raw.get("source_date", ""),
        "currency": raw.get("currency", "JPY"),
        "tax_included": bool(raw.get("tax_included", False)),
        "updated_at": raw.get("updated_at", ""),
        "categories": raw.get("categories") or _derive(products, "category"),
        "makers": raw.get("makers") or _derive(products, "maker"),
        "conditions": raw.get("conditions") or [],
        "product_count": len(products),
        "products": products,
    })
    return base


def _derive(products: list[dict], key: str) -> list[str]:
    seen: list[str] = []
    for p in products:
        v = (p.get(key) or "").strip()
        if v and v not in seen:
            seen.append(v)
    return seen


# ---------------------------------------------------------------------------
# 取得系
# ---------------------------------------------------------------------------
def _products() -> list[dict]:
    """内部用: キャッシュ内の製品リスト（読み取り専用・コピーしない）。"""
    return load_price_master().get("products", [])


def get_products() -> list[dict]:
    """製品リスト（呼び出し側の破壊的変更がキャッシュを汚さないようコピーを返す）。"""
    return list(_products())


def get_categories() -> list[str]:
    return load_price_master().get("categories", [])


def get_makers() -> list[str]:
    return load_price_master().get("makers", [])


def get_conditions() -> list[str]:
    return load_price_master().get("conditions", [])


def get_meta() -> dict:
    m = load_price_master()
    return {
        "source": m.get("source", ""),
        "source_date": m.get("source_date", ""),
        "currency": m.get("currency", "JPY"),
        "tax_included": m.get("tax_included", False),
        "updated_at": m.get("updated_at", ""),
        "product_count": m.get("product_count", len(m.get("products", []))),
        "category_count": len(m.get("categories", [])),
        "maker_count": len(m.get("makers", [])),
    }


# ---------------------------------------------------------------------------
# 検索系
# ---------------------------------------------------------------------------
def find_by_code(code: str) -> Optional[dict]:
    """商品コード完全一致（大文字小文字無視）。重複時は canonical を優先。"""
    if not code:
        return None
    target = code.strip().lower()
    hits = [
        p for p in _products()
        if (p.get("product_code") or "").strip().lower() == target
    ]
    if not hits:
        return None
    hits.sort(key=lambda p: (not p.get("is_canonical", True), p.get("source_page", 99)))
    return hits[0]


def find_by_model(model: str, fuzzy: bool = True) -> list[dict]:
    """型番で検索する（スコア順降順）。fuzzy=Falseは完全一致のみ。"""
    if not model:
        return []
    query = _normalize_model(model)
    if not query:
        return []
    scored: list[tuple[float, dict]] = []
    for p in _products():
        score = _score_model(p.get("model", ""), query, fuzzy)
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: (-x[0], not x[1].get("is_canonical", True)))
    return [p for _, p in scored]


def search(
    query: str = "",
    category: str = "",
    maker: str = "",
    kind: str = "",
    canonical_only: bool = False,
    limit: Optional[int] = None,
) -> list[dict]:
    """横断検索。

    Args:
        query: 商品コード/型番/品名/備考/メーカー名に対する部分一致キーワード（空白区切りAND）
        category: カテゴリ完全一致での絞り込み
        maker: メーカー部分一致での絞り込み
        kind: item_kind 完全一致（product/warranty_extension/service 等・大文字小文字を区別）
        canonical_only: 重複行のうち canonical のみに絞る
        limit: 返却件数上限（0以下なら空リスト）
    """
    if limit is not None and limit <= 0:
        return []
    products = _products()
    tokens = [t for t in re.split(r"\s+", (query or "").strip().lower()) if t]
    cat = (category or "").strip()
    mk = (maker or "").strip().lower()
    kd = (kind or "").strip()

    results: list[dict] = []
    for p in products:
        if cat and (p.get("category") or "") != cat:
            continue
        if mk and mk not in (p.get("maker") or "").lower():
            continue
        if kd and (p.get("item_kind") or "") != kd:
            continue
        if canonical_only and not p.get("is_canonical", True):
            continue
        if tokens:
            haystack = " ".join([
                str(p.get("product_code") or ""),
                str(p.get("model") or ""),
                str(p.get("name") or ""),
                str(p.get("remarks") or ""),
                str(p.get("maker") or ""),
            ]).lower()
            if not all(tok in haystack for tok in tokens):
                continue
        results.append(p)
        if limit is not None and len(results) >= limit:
            break
    return results


def find_price(
    maker: str = "",
    model: str = "",
    code: str = "",
    prefer_canonical: bool = True,
    allow_fuzzy: bool = False,
) -> Optional[dict]:
    """単価参照: メーカー/型番/コードから最も確からしい1件を返す。

    優先順:
        1. 商品コード完全一致
        2. 型番**完全一致**（指定があればメーカー一致も必須）
        3. allow_fuzzy=True のときのみ 型番ファジー一致（canonical優先）

    安全側設計:
        - メーカーを指定したのに一致が無い場合は **None**（別メーカーへフォールバックしない）
        - allow_fuzzy=False（既定）では曖昧な部分一致を確定単価として返さない

    返り値の "unit_price" が None の場合は「別途問合せ」等で価格未定。
    """
    if code:
        hit = find_by_code(code)
        if hit:
            return hit

    model_q = _normalize_model(model)
    if not model_q:
        return None
    maker_q = (maker or "").strip().lower()

    candidates = find_by_model(model, fuzzy=True)

    # 1) 型番完全一致を最優先。無ければ allow_fuzzy 時のみ部分一致を許容。
    exact = [c for c in candidates if _normalize_model(c.get("model", "")) == model_q]
    pool = exact if exact else (candidates if allow_fuzzy else [])
    if not pool:
        return None

    # 2) メーカー指定時は厳格に絞る（一致無しならフォールバックせず None）
    if maker_q:
        filtered = [
            c for c in pool
            if maker_q in (c.get("maker") or "").lower()
            or (c.get("maker") or "").lower() in maker_q
        ]
        if not filtered:
            return None
        pool = filtered

    # 3) 重複は canonical を優先
    if prefer_canonical:
        canon = [c for c in pool if c.get("is_canonical", True)]
        if canon:
            pool = canon

    return pool[0]


# ---------------------------------------------------------------------------
# 表示ヘルパ
# ---------------------------------------------------------------------------
def format_price(value: Any, note: str = "") -> str:
    """単価を表示用文字列に整形する。"""
    # bool は int サブクラスなので先に弾く
    if value is None or value == "" or isinstance(value, bool):
        return note or "別途"
    if isinstance(value, (int, float)):
        return f"¥{int(round(value)):,}"
    return str(value)


def product_label(p: dict) -> str:
    """検索結果表示用の1行ラベル。"""
    code = p.get("product_code") or "—"
    model = p.get("model") or ""
    name = p.get("name") or ""
    price = format_price(p.get("unit_price"), p.get("unit_price_note", ""))
    parts = [code]
    if model:
        parts.append(model)
    parts.append(name)
    parts.append(price)
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# 内部: 型番正規化・スコアリング（product_registry と整合）
# ---------------------------------------------------------------------------
def _normalize_model(model: str) -> str:
    if not model:
        return ""
    # NFKC で全角英数字・全角ピリオド・全角ハイフン等を半角化してから小文字化
    s = unicodedata.normalize("NFKC", str(model)).strip().lower()
    # NFKC で吸収されない長音・各種ダッシュ・全角空白を統一
    s = s.translate(str.maketrans({
        "ー": "-", "−": "-", "–": "-", "—": "-", "‐": "-", "　": " ",
    }))
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _score_model(model: str, query: str, fuzzy: bool) -> float:
    cand = _normalize_model(model)
    if not cand or not query:
        return 0.0
    if cand == query:
        return 10.0
    if not fuzzy:
        return 0.0
    if query in cand:
        return 6.0 + len(query) / max(len(cand), 1)
    if cand in query:
        return 4.0 + len(cand) / max(len(query), 1)
    q_tokens = set(t for t in re.split(r"[\s\-_/]+", query) if t)
    c_tokens = set(t for t in re.split(r"[\s\-_/]+", cand) if t)
    if q_tokens and c_tokens:
        overlap = q_tokens & c_tokens
        if overlap:
            ratio = len(overlap) / max(len(q_tokens | c_tokens), 1)
            return 1.0 + 3.0 * ratio
    return 0.0


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    m = load_price_master()
    print(f"source: {m['source']} ({m['source_date']})")
    print(f"products: {m['product_count']}")
    print(f"categories: {m['categories']}")
    print(f"makers: {len(m['makers'])}件")

    assert m["product_count"] > 0, "製品が読み込めていません"

    # コード検索
    hit = find_by_code("B0S0001500")
    assert hit and hit["maker"] == "ネクストエナジー", hit
    print(f"[OK] find_by_code: {product_label(hit)}")

    # 重複コードは canonical を返す
    dup = find_by_code("B0S0011300")
    assert dup and dup.get("is_canonical") is True, dup
    print(f"[OK] find_by_code(dup→canonical): page{dup['source_page']} {format_price(dup['unit_price'])}")

    # 型番ファジー
    hits = find_by_model("SUN2000-4.95KTL-JPL1", fuzzy=True)
    assert hits, "型番検索で見つからず"
    print(f"[OK] find_by_model: {len(hits)}件, top={product_label(hits[0])}")

    # 横断検索
    cables = search(category="延長ケーブル", query="100m")
    assert cables, "ケーブル検索失敗"
    print(f"[OK] search(延長ケーブル,100m): {len(cables)}件")

    carport = search(category="カーポート", maker="ネクスト", limit=3)
    assert carport, "カーポート検索失敗"
    print(f"[OK] search(カーポート): {len(carport)}件 (limit3)")

    # 単価参照（型番完全一致）
    price = find_price(maker="HUAWEI", model="SUN2000-4.95KTL-JPL1")
    assert price and price["unit_price"] == 116000, price
    print(f"[OK] find_price(HUAWEI完全一致): {format_price(price['unit_price'])}")

    # 全角型番でも完全一致する（NFKC正規化）
    price_fw = find_price(model="ＳＵＮ２０００-４.９５ＫＴＬ-ＪＰＬ１")
    assert price_fw and price_fw["unit_price"] == 116000, price_fw
    print("[OK] find_price(全角型番)→正規化ヒット")

    # メーカー不一致はフォールバックせず None（別メーカー価格の誤返却を防ぐ）
    wrong = find_price(maker="存在しないメーカー", model="SUN2000-4.95KTL-JPL1")
    assert wrong is None, wrong
    print("[OK] find_price(メーカー不一致)→None")

    # 部分入力は既定(allow_fuzzy=False)では確定単価を返さない
    fuzzy_off = find_price(model="SUN2000")
    fuzzy_on = find_price(model="SUN2000", allow_fuzzy=True)
    assert fuzzy_off is None and fuzzy_on is not None, (fuzzy_off, fuzzy_on)
    print("[OK] find_price(曖昧入力): fuzzy_off=None / fuzzy_on=ヒット")

    # limit=0 は空（off-by-one回帰防止）
    assert search(category="カーポート", limit=0) == []
    assert len(search(category="カーポート", limit=3)) == 3
    print("[OK] search(limit境界): 0→空 / 3→3件")

    # format_price の異常値
    assert format_price(True) == "別途" and format_price(116000.7) == "¥116,001"
    print("[OK] format_price(bool/float)")

    # canonical_only
    all_dup = search(query="KPW-A55-2PJ4", canonical_only=False)
    canon_dup = search(query="KPW-A55-2PJ4", canonical_only=True)
    assert len(all_dup) > len(canon_dup), (len(all_dup), len(canon_dup))
    print(f"[OK] canonical_only: 全{len(all_dup)} → canonical{len(canon_dup)}")

    print("\nAll self-tests passed.")
