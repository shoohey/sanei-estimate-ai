"""ローカル製品レジストリ（JSONベース）

カタログから抽出した製品情報を JSON ファイル
（knowledge/products_registry.json）に保存・検索するモジュール。

主な公開関数:
    - load_registry()
    - save_registry(products)
    - add_product(product)
    - delete_product(product_id)
    - find_by_model(model, fuzzy=True)
    - find_by_maker_and_model(maker, model)
    - get_active_module_for_estimate(estimate_or_survey)
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# レジストリJSONの保存先
REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent / "knowledge" / "products_registry.json"
)

# スキーマバージョン（破壊的変更時にインクリメント）
SCHEMA_VERSION = 1


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------
def load_registry() -> list[dict]:
    """JSONファイルから全製品を読み込む。

    ファイルが存在しない、または読み込めない場合は空リストを返す。
    """
    if not REGISTRY_PATH.exists():
        return []
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"products_registry.json 読み込み失敗: {e}")
        return []

    if isinstance(data, list):
        # 旧形式（リストのみ）
        return [p for p in data if isinstance(p, dict)]
    if isinstance(data, dict):
        products = data.get("products", [])
        if isinstance(products, list):
            return [p for p in products if isinstance(p, dict)]
    return []


def save_registry(products: list[dict]) -> None:
    """全製品リストをJSONファイルに保存する。

    親ディレクトリが無ければ作成。UTF-8、indent=2、ensure_ascii=False で出力。
    """
    if not isinstance(products, list):
        raise TypeError("products は list である必要があります")

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "products": [_jsonable(p) for p in products],
    }
    tmp_path = REGISTRY_PATH.with_suffix(REGISTRY_PATH.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(REGISTRY_PATH)


def add_product(product: dict) -> dict:
    """製品を登録する。同じ maker + model が既にあれば更新。

    自動で id (uuid)、registered_at、updated_at を付与する。

    Returns:
        登録/更新後の製品dict（idが付与済み）
    """
    if not isinstance(product, dict):
        raise TypeError("product は dict である必要があります")

    products = load_registry()

    new_product = dict(product)
    maker = _safe_str(new_product.get("maker"))
    model = _safe_str(new_product.get("model"))

    now_iso = _now_iso()
    existing_index = -1
    if maker and model:
        for i, p in enumerate(products):
            if _safe_str(p.get("maker")).lower() == maker.lower() and \
               _safe_str(p.get("model")).lower() == model.lower():
                existing_index = i
                break

    if existing_index >= 0:
        existing = products[existing_index]
        # 既存のid・registered_at は維持
        new_product["id"] = existing.get("id") or _new_id()
        new_product["registered_at"] = existing.get("registered_at") or now_iso
        new_product["updated_at"] = now_iso
        products[existing_index] = new_product
    else:
        new_product["id"] = new_product.get("id") or _new_id()
        new_product["registered_at"] = new_product.get("registered_at") or now_iso
        new_product["updated_at"] = now_iso
        products.append(new_product)

    save_registry(products)
    return new_product


def delete_product(product_id: str) -> bool:
    """指定IDの製品を削除する。成功なら True、見つからなければ False。"""
    if not product_id:
        return False
    products = load_registry()
    new_products = [p for p in products if p.get("id") != product_id]
    if len(new_products) == len(products):
        return False
    save_registry(new_products)
    return True


def find_by_model(model: str, fuzzy: bool = True) -> list[dict]:
    """型式で製品を検索する。

    Args:
        model: 検索対象の型式
        fuzzy: True なら部分一致 + model_aliases も対象、False なら完全一致のみ

    Returns:
        スコア順（降順）に並べた製品リスト
    """
    if not model:
        return []
    products = load_registry()
    query = _normalize_model(model)
    if not query:
        return []

    scored: list[tuple[float, dict]] = []
    for p in products:
        score = _score_model_match(p, query, fuzzy=fuzzy)
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored]


def find_by_maker_and_model(maker: str, model: str) -> Optional[dict]:
    """メーカー + 型式の組合せで最も一致度の高い1件を返す。"""
    if not maker and not model:
        return None
    products = load_registry()
    if not products:
        return None

    maker_norm = _safe_str(maker).lower()
    model_query = _normalize_model(model)

    best: Optional[tuple[float, dict]] = None
    for p in products:
        score = 0.0
        p_maker = _safe_str(p.get("maker")).lower()
        if maker_norm and p_maker:
            if maker_norm == p_maker:
                score += 5.0
            elif maker_norm in p_maker or p_maker in maker_norm:
                score += 2.5
        if model_query:
            score += _score_model_match(p, model_query, fuzzy=True)
        if score > 0 and (best is None or score > best[0]):
            best = (score, p)

    return best[1] if best else None


def get_active_module_for_estimate(estimate_or_survey: Any) -> Optional[dict]:
    """SurveyData / EstimateData から「現在使用中のモジュール」情報を取得する。

    SurveyData:
        equipment.module_maker / equipment.module_model から検索

    EstimateData:
        cover.notes など自由テキストフィールドや、reasoning_list / line items 内の
        メーカー名・型式表記から探す（ベストエフォート）

    Returns:
        登録済み製品dict / 未登録または該当なしならNone
    """
    if estimate_or_survey is None:
        return None

    maker, model = _resolve_module_identity(estimate_or_survey)
    if not maker and not model:
        return None

    # 1. メーカー + 型式で完全/部分一致を狙う
    hit = find_by_maker_and_model(maker, model)
    if hit:
        return hit

    # 2. 型式のみでファジー検索
    if model:
        candidates = find_by_model(model, fuzzy=True)
        # 製品タイプが module のものを優先
        modules = [c for c in candidates if c.get("product_type") == "module"]
        if modules:
            return modules[0]
        if candidates:
            return candidates[0]

    return None


# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------
def _now_iso() -> str:
    """UTC現在時刻をISO8601文字列で返す。"""
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _safe_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    return str(val).strip()


def _jsonable(value: Any) -> Any:
    """datetime 等を JSON シリアライズ可能な形に変換する。"""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Pydantic model 等
    try:
        if hasattr(value, "model_dump"):
            return _jsonable(value.model_dump())
        if hasattr(value, "dict"):
            return _jsonable(value.dict())
    except Exception:
        pass
    return str(value)


def _normalize_model(model: str) -> str:
    """型式の比較用に正規化する（小文字化・記号統一）。"""
    if not model:
        return ""
    s = str(model).strip().lower()
    # 全角→半角の代表的な変換
    s = s.translate(str.maketrans({
        "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
        "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
        "ー": "-", "－": "-", "−": "-", "　": " ",
    }))
    # 連続空白を1つに
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _score_model_match(product: dict, query: str, fuzzy: bool) -> float:
    """1製品とクエリ型式のマッチスコアを返す。0なら不一致。"""
    if not query:
        return 0.0
    candidates: list[str] = []
    model = _normalize_model(product.get("model", ""))
    if model:
        candidates.append(model)
    if fuzzy:
        for alias in product.get("model_aliases") or []:
            n = _normalize_model(alias)
            if n:
                candidates.append(n)
    if not candidates:
        return 0.0

    best = 0.0
    for cand in candidates:
        if cand == query:
            best = max(best, 10.0)
        elif fuzzy:
            if query in cand:
                # クエリが候補に含まれる（候補の方が長い）
                best = max(best, 6.0 + len(query) / max(len(cand), 1))
            elif cand in query:
                # 候補がクエリに含まれる（クエリの方が長い）
                best = max(best, 4.0 + len(cand) / max(len(query), 1))
            else:
                # トークンレベルで部分一致を見る（ハイフン・空白で分解）
                q_tokens = set(t for t in re.split(r"[\s\-_/]+", query) if t)
                c_tokens = set(t for t in re.split(r"[\s\-_/]+", cand) if t)
                if q_tokens and c_tokens:
                    overlap = q_tokens & c_tokens
                    if overlap:
                        ratio = len(overlap) / max(len(q_tokens | c_tokens), 1)
                        best = max(best, 1.0 + 3.0 * ratio)
    return best


def _resolve_module_identity(obj: Any) -> tuple[str, str]:
    """SurveyData / EstimateData / dict / 文字列 から (maker, model) を抽出する。"""
    # 文字列なら model のみと解釈
    if isinstance(obj, str):
        return "", _safe_str(obj)

    # dict 直接
    if isinstance(obj, dict):
        # SurveyData 風 dict
        eq = obj.get("equipment") if isinstance(obj.get("equipment"), dict) else None
        if eq:
            return _safe_str(eq.get("module_maker")), _safe_str(eq.get("module_model"))
        # 平坦な dict
        return (
            _safe_str(obj.get("module_maker") or obj.get("maker")),
            _safe_str(obj.get("module_model") or obj.get("model")),
        )

    # SurveyData インスタンス（pydantic）
    equipment = getattr(obj, "equipment", None)
    if equipment is not None:
        maker = _safe_str(getattr(equipment, "module_maker", "") or "")
        model = _safe_str(getattr(equipment, "module_model", "") or "")
        if maker or model:
            return maker, model

    # EstimateData っぽいオブジェクトのフォールバック
    # cover.notes / reasoning_list / summary.sections 内のテキストを連結して
    # メーカー名/型式らしき文字列を探す（ベストエフォート）
    text_chunks: list[str] = []
    cover = getattr(obj, "cover", None)
    if cover is not None:
        for k in ("project_name", "notes"):
            v = getattr(cover, k, "")
            if v:
                text_chunks.append(str(v))
    reasoning = getattr(obj, "reasoning_list", None)
    if isinstance(reasoning, list):
        text_chunks.extend(str(r) for r in reasoning if r)
    summary = getattr(obj, "summary", None)
    if summary is not None:
        sections = getattr(summary, "sections", None)
        if isinstance(sections, list):
            for sec in sections:
                items = getattr(sec, "items", None)
                if isinstance(items, list):
                    for it in items:
                        name = getattr(it, "name", "") or (it.get("name") if isinstance(it, dict) else "")
                        spec = getattr(it, "spec", "") or (it.get("spec") if isinstance(it, dict) else "")
                        if name:
                            text_chunks.append(str(name))
                        if spec:
                            text_chunks.append(str(spec))

    blob = " ".join(text_chunks)
    if not blob:
        return "", ""

    # 既知メーカーの軽量マッチ
    known_makers = [
        "Canadian Solar", "Longi", "JA Solar", "Jinko", "Trina",
        "Q CELLS", "Q.CELLS", "Sharp", "Panasonic", "京セラ", "三菱",
    ]
    found_maker = ""
    for m in known_makers:
        if m.lower() in blob.lower():
            found_maker = m
            break

    # 型式パターン（英大文字+数字+ハイフン などの一般的な形）
    found_model = ""
    m = re.search(r"\b([A-Z]{1,4}[A-Z0-9]*-?[A-Z0-9]+(?:-[A-Z0-9]+)*)\b", blob)
    if m:
        found_model = m.group(1)
    return found_maker, found_model


# ----------------------------------------------------------------------------
# Self-test
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import tempfile

    # テスト用に REGISTRY_PATH を一時ファイルへ差し替え
    _orig_path = REGISTRY_PATH
    tmp_dir = tempfile.mkdtemp(prefix="product_registry_test_")
    test_path = Path(tmp_dir) / "products_registry.json"
    globals()["REGISTRY_PATH"] = test_path
    try:
        # 1. 空状態の確認
        assert load_registry() == [], "初期状態は空のはず"
        print("[OK] load_registry (empty)")

        # 2. add_product
        p1 = add_product({
            "product_type": "module",
            "maker": "Canadian Solar",
            "model": "CS7L-MS",
            "model_aliases": ["CS7L-MS-660", "CS7L-MS-665"],
            "output_w": 660,
            "physical": {"length_mm": 2384, "width_mm": 1303, "thickness_mm": 35, "weight_kg": 33.5},
            "electrical": {"vmp": 38.5, "imp": 17.15, "voc": 46.0, "isc": 18.31, "efficiency_pct": 21.3},
            "warranty": {"product_years": 12, "output_years": 25},
        })
        assert "id" in p1 and "registered_at" in p1 and "updated_at" in p1
        print(f"[OK] add_product (id={p1['id']})")

        p2 = add_product({
            "product_type": "pcs",
            "maker": "オムロン",
            "model": "KP-MU-PV",
            "output_w": 9.9,
        })
        assert p2["id"] != p1["id"]
        print(f"[OK] add_product 2nd (id={p2['id']})")

        # 3. 同maker+modelで再登録 → 更新
        p1_updated = add_product({
            "product_type": "module",
            "maker": "Canadian Solar",
            "model": "CS7L-MS",
            "output_w": 670,  # 値だけ変更
        })
        assert p1_updated["id"] == p1["id"], "同maker+modelは同じidで上書きされるべき"
        print(f"[OK] add_product update (id={p1_updated['id']}, new output={p1_updated['output_w']})")

        # 4. load_registry
        loaded = load_registry()
        assert len(loaded) == 2, f"製品数は2のはず: {len(loaded)}"
        print(f"[OK] load_registry (count={len(loaded)})")

        # 5. find_by_model 完全一致
        hits_exact = find_by_model("CS7L-MS", fuzzy=False)
        assert len(hits_exact) == 1
        print("[OK] find_by_model exact")

        # 6. find_by_model fuzzy（aliasでヒット）
        hits_fuzzy = find_by_model("CS7L-MS-665", fuzzy=True)
        assert len(hits_fuzzy) >= 1
        print("[OK] find_by_model fuzzy via alias")

        # 7. find_by_maker_and_model
        hit = find_by_maker_and_model("Canadian Solar", "CS7L-MS")
        assert hit is not None and hit["model"] == "CS7L-MS"
        print("[OK] find_by_maker_and_model")

        # 8. get_active_module_for_estimate（dict形式のSurveyData風）
        survey_like = {
            "equipment": {
                "module_maker": "Canadian Solar",
                "module_model": "CS7L-MS",
            }
        }
        active = get_active_module_for_estimate(survey_like)
        assert active is not None and active["model"] == "CS7L-MS"
        print("[OK] get_active_module_for_estimate (dict)")

        # 9. delete_product
        ok = delete_product(p2["id"])
        assert ok is True
        assert len(load_registry()) == 1
        print("[OK] delete_product")

        # 10. JSONファイル中身の確認
        with open(test_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        assert payload.get("schema_version") == SCHEMA_VERSION
        assert isinstance(payload.get("products"), list)
        print(f"[OK] schema_version={payload['schema_version']}, products={len(payload['products'])}")

        print("\nAll self-tests passed.")
    finally:
        # 後始末
        try:
            if test_path.exists():
                os.remove(test_path)
            os.rmdir(tmp_dir)
        except Exception:
            pass
        globals()["REGISTRY_PATH"] = _orig_path
