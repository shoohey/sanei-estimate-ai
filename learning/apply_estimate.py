"""学習済み見積ルールの pricing rules への適用

store（knowledge/learned_estimate_rules.json）の有効ルールを
pricing_rules.yaml のロード結果に反映する（docs/LEARNING_LOOP_DESIGN.md §11）。
pricing/knowledge_base.load_pricing_rules() の末尾から呼ばれるため、
単価上書き / 項目追加 / 項目抑止 がエンジン無改修で全カテゴリに効く。

- unit_price_override: 複合照合（摘要 + match_remarks / old_unit_price）で特定した
  1項目の unit_price を差替え、note に「（学習補正: ¥X→¥Y）」を追記。
  pricing_method が lump_formula の項目は unit_price 不使用
  （計算式の結果が金額になる。pricing_engine 参照）のため対象外。
  pricing_method が panel_rate の項目（修正③ 2026-07-23: パネル取付工事・
  架台取付工事）は unit_price が「1枚あたり単価」なので、学習は常に単価への
  学習として働き、上書き後も 金額 = 設置枚数 × 学習単価 で枚数連動が保たれる
  （金額の固定化はしない）。
- item_suppress: 複合照合で特定した1項目をリストから除去。
- 備考(remarks)違いの同名項目（例: pricing_rules.yaml 材料費「PVケーブル間」×5）が
  あるため、摘要のみの照合は禁止。ルールの match_remarks（正規化備考）、無ければ
  payload.old_unit_price（旧形式ルール互換）で対象を絞り、それでも2件以上一致する
  場合は適用せず警告ログのみ（誤爆で同名全項目を書き換え/削除する事故防止）。
- item_add: 対象カテゴリのリスト末尾に fixed 項目として追加。
- 入力 rules は deepcopy してから変更（YAMLロード結果の破壊防止）。

呼び出し側（pricing/knowledge_base.load_pricing_rules）は try/except で保護されるが、
本モジュール内でもストア読込失敗・個別ルールの不備を握り、見積生成を止めない。
"""
import copy
import logging

from learning import store
from learning.estimate_diff import normalize_desc

logger = logging.getLogger(__name__)

# 見積カテゴリ名 → pricing_rules.yaml のリスト名
# （特記事項は対応リストが無いため学習反映の対象外）
CATEGORY_TO_LIST = {
    "支給品": "supplied_items",
    "材料費": "material_items",
    "施工費": "construction_items",
    "その他・諸経費等": "overhead_items",
    "付帯工事": "additional_items",
}

# 項目構成（どの項目が載るか）を学習で変更しないカテゴリ（2026-08-13 顧客要望）。
# 例外案件で学習された item_add / item_suppress がデフォルト化し、
# 「材料が異常に多い」「支給品が全く無い」構成が全案件に波及した実害への対処。
# 過去に保存済みのルールもここで適用スキップされる（削除は学習センターUIから）。
FIXED_STRUCTURE_CATEGORIES = ("支給品", "材料費")
_FIXED_STRUCTURE_LISTS = ("supplied_items", "material_items")

# panel_rate（枚数連動）項目に適用できる学習単価の上限。
# 正規見積の「一式 ¥1,112,400」を単価として学習した不正ルールが枚数連動項目に
# 入ると 266枚×¥1,112,400=¥2.96億 の事故になる（2026-08-10 実障害）。
# 1枚単価は数千円のオーダーのため、これを超える値は単位取り違えとみなして
# 適用しない（basis_quantity_unit を持たない旧形式ルールへの防御）。
PANEL_RATE_MAX_UNIT_PRICE = 100_000


# =============================================================
# 内部ユーティリティ
# =============================================================

def _target_list_names(category: str) -> list:
    """ルールの category から反映先リスト名を返す。

    category 空（不明）のルールは全リスト対象。特記事項など
    対応リストが無いカテゴリは []（適用なし）。
    """
    if not category:
        return list(CATEGORY_TO_LIST.values())
    name = CATEGORY_TO_LIST.get(category)
    return [name] if name else []


def _narrow_candidates(candidates: list, rule: dict) -> list:
    """摘要一致候補を match_remarks / payload.old_unit_price で絞り込む。

    備考違いの同名項目（材料費「PVケーブル間」×5等）を1項目に特定するための
    複合照合。match_remarks（正規化備考）があれば備考一致で絞り、無ければ
    （旧形式ルール互換）現単価 == payload.old_unit_price の項目に絞る。
    どちらも無いルールは絞り込みなし（呼び出し側の件数ガードのみ）。
    """
    match_remarks = rule.get("match_remarks") or ""
    payload = rule.get("payload", {}) or {}
    old_price = payload.get("old_unit_price")
    if match_remarks:
        return [it for it in candidates
                if normalize_desc(str(it.get("remarks", ""))) == match_remarks]
    if old_price is not None:
        return [it for it in candidates if it.get("unit_price") == old_price]
    return candidates


def _apply_price_override(rules: dict, rule: dict) -> None:
    """unit_price_override: 複合照合で特定した1項目の単価を学習値に差し替える。

    摘要のみの照合では備考違いの同名項目を全て上書きしてしまうため、
    _narrow_candidates で絞った上、なお2件以上一致する場合は適用しない（安全側）。
    """
    payload = rule.get("payload", {}) or {}
    new_price = payload.get("unit_price")
    match_desc = rule.get("match_description", "")
    if new_price is None or not match_desc:
        return
    new_price = int(new_price)

    for list_name in _target_list_names(rule.get("category", "")):
        candidates = [
            it for it in (rules.get(list_name) or [])
            if it.get("pricing_method") != "lump_formula"  # unit_price 不使用
            and normalize_desc(str(it.get("description", ""))) == match_desc
        ]
        candidates = _narrow_candidates(candidates, rule)
        if len(candidates) > 1:
            logger.warning(
                "unit_price_override の一致項目が%d件あり対象を特定できないため"
                "適用しません（id=%s, list=%s, 摘要=%r）",
                len(candidates), rule.get("id", "?"), list_name,
                rule.get("display_description") or match_desc)
            continue
        for item in candidates:  # 0件 or 1件
            # panel_rate（枚数連動）項目は unit_price が「1枚あたり単価」。
            # 別単位（式等）で学習された単価や桁違いの値を掛けると金額が
            # 枚数倍に暴発するため、単位不一致・上限超過のルールは適用しない。
            if item.get("pricing_method") == "panel_rate":
                # 空文字は「単位不明」として扱い、単位照合はスキップして
                # 下の単価上限ガードのみに委ねる（正当な単価の誤ブロック防止）
                basis_unit = str((rule.get("payload", {}) or {}).get(
                    "basis_quantity_unit") or "").strip() or None
                item_unit = str(item.get("quantity_unit", "")).strip()
                if basis_unit is not None and basis_unit != item_unit:
                    logger.warning(
                        "unit_price_override の学習元単位(%r)が枚数連動項目の"
                        "単位(%r)と異なるため適用しません（id=%s, 摘要=%r）",
                        basis_unit, item_unit, rule.get("id", "?"),
                        rule.get("display_description") or match_desc)
                    continue
                if new_price > PANEL_RATE_MAX_UNIT_PRICE:
                    logger.warning(
                        "unit_price_override の学習単価 ¥%s が1枚単価の上限"
                        "（¥%s）を超えており単位取り違えの疑いがあるため"
                        "適用しません（id=%s, 摘要=%r）",
                        f"{new_price:,}", f"{PANEL_RATE_MAX_UNIT_PRICE:,}",
                        rule.get("id", "?"),
                        rule.get("display_description") or match_desc)
                    continue
            old_price = item.get("unit_price")
            if old_price == new_price:
                continue  # 既に学習値（note の重複追記を防ぐ）
            item["unit_price"] = new_price
            old_disp = f"¥{int(old_price):,}" if old_price is not None else "¥?"
            item["note"] = (f"{item.get('note', '')}"
                            f"（学習補正: {old_disp}→¥{new_price:,}）")


def _apply_suppress(rules: dict, rule: dict) -> None:
    """item_suppress: 複合照合で特定した1項目をリストから除去する。

    摘要のみの照合では備考違いの同名項目を全て削除してしまうため、
    _narrow_candidates で絞った上、なお2件以上一致する場合は適用しない（安全側）。
    """
    match_desc = rule.get("match_description", "")
    if not match_desc:
        return
    if rule.get("category", "") in FIXED_STRUCTURE_CATEGORIES:
        logger.info(
            "item_suppress は支給品・材料費には適用しません（項目構成は学習で"
            "変更しない方針。id=%s, 摘要=%r）",
            rule.get("id", "?"), rule.get("display_description") or match_desc)
        return
    for list_name in _target_list_names(rule.get("category", "")):
        if list_name in _FIXED_STRUCTURE_LISTS:
            continue  # カテゴリ不明ルールの全リスト適用からも支給品・材料費は除外
        lst = rules.get(list_name)
        if not lst:
            continue
        candidates = [
            it for it in lst
            if normalize_desc(str(it.get("description", ""))) == match_desc
        ]
        candidates = _narrow_candidates(candidates, rule)
        if not candidates:
            continue
        if len(candidates) > 1:
            logger.warning(
                "item_suppress の一致項目が%d件あり対象を特定できないため"
                "適用しません（id=%s, list=%s, 摘要=%r）",
                len(candidates), rule.get("id", "?"), list_name,
                rule.get("display_description") or match_desc)
            continue
        target = candidates[0]
        rules[list_name] = [it for it in lst if it is not target]


def _apply_add(rules: dict, rule: dict) -> None:
    """item_add: 対象カテゴリのリスト末尾に fixed 項目を追加する。"""
    payload = rule.get("payload", {}) or {}
    category = payload.get("category") or rule.get("category", "")
    if category in FIXED_STRUCTURE_CATEGORIES:
        logger.info(
            "item_add は支給品・材料費には適用しません（項目構成は学習で"
            "変更しない方針。id=%s, 摘要=%r）",
            rule.get("id", "?"),
            payload.get("description") or rule.get("display_description", ""))
        return
    list_name = CATEGORY_TO_LIST.get(category)
    if not list_name:
        # 追加先を特定できない item_add は適用しない（安全側）
        logger.warning("item_add のカテゴリが不明のため適用しません（id=%s, category=%r）",
                       rule.get("id", "?"), category)
        return

    description = payload.get("description") or rule.get("display_description", "")
    if not description:
        return

    lst = rules.get(list_name)
    if not isinstance(lst, list):
        lst = rules[list_name] = []

    # 既に同名項目がある場合は二重追加しない（防御）
    match_desc = normalize_desc(description)
    if any(normalize_desc(str(it.get("description", ""))) == match_desc for it in lst):
        return

    # 数量は数値で持つ（pricing_engine._resolve_quantity は数値も受け付ける）
    quantity = payload.get("quantity_value")
    quantity = 1.0 if quantity is None else float(quantity)
    if quantity == int(quantity):
        quantity = int(quantity)

    max_no = max((int(it.get("no", 0)) for it in lst), default=0)
    evidence = rule.get("evidence") or {}
    project = evidence.get("project_name") or evidence.get("file_name") or "学習データ"

    lst.append({
        "no": max_no + 1,
        "description": description,
        "remarks": payload.get("remarks", "") or "",
        "quantity": quantity,
        "quantity_unit": payload.get("quantity_unit", "") or "式",
        "unit_price": int(payload.get("unit_price") or 0),
        "pricing_method": "fixed",
        "note": f"学習により追加（{project}の正規見積より）",
    })


# =============================================================
# 公開関数
# =============================================================

def apply_learned_rules(rules: dict) -> dict:
    """有効な学習済み見積ルールを pricing rules に適用して返す。

    Args:
        rules: pricing_rules.yaml のロード結果 dict。

    Returns:
        学習ルール適用済みの dict（deepcopy。入力は変更しない）。
        学習ルールが無い/読込失敗の場合は入力をそのまま返す。
    """
    try:
        learned = store.enabled_rules("estimate")
    except Exception as e:
        logger.warning("学習済み見積ルールの読込に失敗（適用をスキップ）: %s", e)
        return rules
    if not learned or not isinstance(rules, dict):
        return rules

    rules = copy.deepcopy(rules)
    for rule in learned:
        try:
            kind = rule.get("kind", "")
            if kind == "unit_price_override":
                _apply_price_override(rules, rule)
            elif kind == "item_suppress":
                _apply_suppress(rules, rule)
            elif kind == "item_add":
                _apply_add(rules, rule)
            # 未知の kind は無視（前方互換）
        except Exception as e:
            # 1ルールの不備で他ルールの適用・見積生成を止めない
            logger.warning("学習ルールの適用に失敗（id=%s）: %s", rule.get("id", "?"), e)
            continue
    return rules


def learned_rules_summary() -> dict:
    """UI表示用の学習済みルール件数サマリ（有効ルールのみ）。

    Returns:
        {"total": 有効件数, "price": 単価上書き, "add": 項目追加, "suppress": 項目抑止}
    """
    summary = {"total": 0, "price": 0, "add": 0, "suppress": 0}
    try:
        rules = store.enabled_rules("estimate")
    except Exception as e:
        logger.warning("学習済み見積ルールの読込に失敗: %s", e)
        return summary

    kind_to_key = {
        "unit_price_override": "price",
        "item_add": "add",
        "item_suppress": "suppress",
    }
    for r in rules:
        summary["total"] += 1
        key = kind_to_key.get(r.get("kind", ""))
        if key:
            summary[key] += 1
    return summary
