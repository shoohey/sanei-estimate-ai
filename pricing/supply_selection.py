"""支給品⇄材料費の商品ごと切替（2026-08-13 顧客要望 / 8-10会議アクション）

見積プレビューで商品（支給品カテゴリの品目）ごとに「支給品かどうか」を
チェックボックスで選択し、見積へ即反映する:

- 支給品（チェックON・既定）: 従来どおり支給品カテゴリに ¥0 で計上
- 非支給品（チェックOFF）: 材料費カテゴリへ移動し、単価マスター
  （product/price_master.py。顧客側で単価修正可能）の価格 × 数量で計上。
  マスターに価格が見つからない場合は手動入力（is_manual）行として追加

設計:
- 見積生成直後の「支給品リスト」をスナップショットし、チェック状態から
  支給品カテゴリと「切替由来の材料費行」を組み立て直す（冪等）
- 材料費側は「切替由来の行（reasoning.note の【支給品切替】マーカーで識別）」
  だけを差し替え、それ以外の行（基本5項目・手動編集・単価マスターからの
  明細追加）は現在の内容をそのまま維持する（Codexレビュー指摘: スナップ
  ショット再構築だと後から追加・編集した明細が消える）
- UI（app.py）からは snapshot_sections / apply_supply_selection / item_key を使う
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Optional

from models.estimate_data import (
    CategoryType,
    EstimateData,
    LineItem,
    LineItemReasoning,
    PricingMethod,
)

logger = logging.getLogger(__name__)


def item_key(item: LineItem) -> str:
    """支給品チェックの状態を保持するためのキー（摘要+備考）。"""
    return f"{item.description}|{item.remarks}"


def _find_section(estimate: EstimateData, category: CategoryType):
    for cat in estimate.summary.categories:
        if cat.category == category:
            return cat
    return None


# 切替由来の材料費行を識別するマーカー（reasoning.note の先頭に付与）。
# 材料費側の再構築でこの行だけを差し替え、手動編集・明細追加は温存する。
_MOVED_NOTE_PREFIX = "【支給品切替】"


def _is_moved_row(item: LineItem) -> bool:
    r = getattr(item, "reasoning", None)
    return bool(r and (r.note or "").startswith(_MOVED_NOTE_PREFIX))


def snapshot_sections(estimate: EstimateData) -> dict:
    """見積生成直後の支給品リストを deepcopy で控える。

    チェックボックスの一覧表示と、ONに戻した際の復元（¥0・御支給品表記）の
    原本として使う。見積オブジェクトごとに1回だけ作成して使い回すこと。
    """
    supplied = _find_section(estimate, CategoryType.SUPPLIED)
    return {
        "supplied": copy.deepcopy(supplied.items) if supplied else [],
    }


# ---------------------------------------------------------------------------
# 単価マスター照合
# ---------------------------------------------------------------------------

def master_price_for(item: LineItem) -> tuple[Optional[int], str]:
    """支給品項目に対応する単価マスターの価格を探す。

    備考の型式（例: SUN2000-50KTL-NHM3）→ 摘要・備考のキーワード検索の順で
    照合し、(単価, 出典ラベル) を返す。見つからなければ (None, "")。
    """
    try:
        from product import price_master as pm
    except Exception:
        return None, ""

    def _price_of(p: dict) -> Optional[int]:
        try:
            v = p.get("unit_price")
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # 1) 備考の型式で照合（最も確実）
    remarks = (item.remarks or "").strip()
    if remarks:
        # 「鶴田電機（50KVA）」のような括弧書きはキーワード扱いにする
        model_token = re.split(r"[\s／/（(]", remarks)[0]
        if model_token:
            try:
                hits = pm.find_by_model(model_token, fuzzy=True)
            except Exception:
                hits = []
            for p in hits:
                price = _price_of(p)
                if price:
                    return price, f"単価マスター: {p.get('model') or p.get('name', '')}"

    # 2) 摘要・備考のキーワード検索（canonical のみ）
    for query in (remarks, item.description):
        if not query:
            continue
        try:
            hits = pm.search(query=query, canonical_only=True)
        except Exception:
            hits = []
        for p in hits:
            price = _price_of(p)
            if price:
                return price, f"単価マスター: {p.get('model') or p.get('name', '')}"
    return None, ""


def _to_material_item(item: LineItem) -> LineItem:
    """支給品項目を材料費行に変換する（単価マスター照合つき）。

    備考の「御支給品」表記は購入品としては矛盾するため除去する
    （Codexレビュー指摘）。
    """
    it = copy.deepcopy(item)
    it.remarks = "\n".join(
        line for line in (it.remarks or "").splitlines()
        if line.strip() != "御支給品").strip()
    price, source = master_price_for(it)
    qty = it.quantity_value if (it.quantity_value or 0) > 0 else 1
    if price is not None:
        it.unit_price = int(price)
        it.amount = int(qty * price)
        it.is_manual_input = False
        it.reasoning = LineItemReasoning(
            method=PricingMethod.FIXED,
            formula=f"{_fmt_qty(qty)}{it.quantity_unit} × ¥{int(price):,} = ¥{it.amount:,}",
            source=source,
            note=f"{_MOVED_NOTE_PREFIX}非支給品（購入品）に変更。単価マスターの価格を適用",
        )
    else:
        it.unit_price = 0
        it.amount = 0
        it.is_manual_input = True
        it.reasoning = LineItemReasoning(
            method=PricingMethod.MANUAL,
            formula="手動入力が必要です",
            source="単価マスターに該当製品が見つかりません",
            note=f"{_MOVED_NOTE_PREFIX}非支給品に変更。単価を手動で入力してください",
        )
    return it


def _fmt_qty(v) -> str:
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------------------
# 反映
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# v2: 支給品の属性化（2026-08-15 顧客ルールブック【見積側】4条）
# 「支給品」は大分類ではなく明細の属性。支給品にしても明細は
# 太陽光発電システム機器カテゴリに残し、備考「御支給品」・金額0円とする。
# 工事費（設置工事カテゴリ）は0円にしない。
# ---------------------------------------------------------------------------

def snapshot_equipment(estimate: EstimateData) -> dict:
    """機器カテゴリの原本（購入価格つき）を控える（v2属性モード用）。

    設計確定情報「支給品」で生成時に¥0化された明細があるため、
    生成側が控えた購入価格つきスナップショット（equipment_purchase_snapshot）
    を優先する。無い場合（旧データ等）は現在の明細を原本とする。
    """
    purchase = getattr(estimate, "equipment_purchase_snapshot", None) or []
    if purchase:
        return {"equipment": copy.deepcopy(purchase)}
    eq = _find_section(estimate, CategoryType.EQUIPMENT)
    return {"equipment": copy.deepcopy(eq.items) if eq else []}


def initial_supply_flags(estimate: EstimateData) -> dict:
    """現在の機器明細から支給品チェックの初期状態を作る（v2属性モード用）。

    設計確定情報から自動で御支給品になった明細を初期チェックONにする。
    キーは原本（購入価格つき）側の item_key に合わせる。
    """
    eq = _find_section(estimate, CategoryType.EQUIPMENT)
    if eq is None:
        return {}
    snap = snapshot_equipment(estimate)
    base_keys = {item_key(it) for it in snap.get("equipment", [])}
    flags = {}
    for it in eq.items:
        k = _supply_base_key(it)
        if k in base_keys and "御支給品" in (it.remarks or ""):
            flags[k] = True
    return flags


def _supply_base_key(item: LineItem) -> str:
    """支給品属性の付け外しで変化しない照合キー。

    属性ONで備考に「御支給品」行が追記されるため、素の item_key では
    原本と照合できなくなる。御支給品行を除いた備考でキーを作る。
    """
    remarks = "\n".join(
        line for line in (item.remarks or "").splitlines()
        if line.strip() != "御支給品").strip()
    return f"{item.description}|{remarks}"


def apply_supply_attribute(estimate: EstimateData, snapshot: dict,
                           flags: dict) -> None:
    """機器明細の支給品属性を反映する（in-place・冪等）。

    flags[item_key(原本)] = True → 御支給品（金額0・備考に御支給品）
                            False（既定）→ 購入品（原本の単価マスター価格）
    """
    eq = _find_section(estimate, CategoryType.EQUIPMENT)
    if eq is None:
        return
    originals = {_supply_base_key(it): it
                 for it in snapshot.get("equipment", [])}
    new_items = []
    for it in eq.items:
        base = originals.get(_supply_base_key(it))
        if base is None:
            new_items.append(it)  # 後から追加された明細はそのまま
            continue
        want_supplied = flags.get(item_key(base), False)
        is_supplied = "御支給品" in (it.remarks or "")
        if want_supplied == is_supplied:
            # 状態変化なし: 現在の行を維持（架台・PCS等の手動編集を消さない。
            # Codexレビュー指摘: 全行を原本から再構築すると編集が失われる）
            new_items.append(it)
            continue
        it2 = copy.deepcopy(base)
        if want_supplied:
            it2.unit_price = 0
            it2.amount = 0
            it2.is_manual_input = False
            if "御支給品" not in (it2.remarks or ""):
                it2.remarks = (f"{it2.remarks}\n御支給品" if it2.remarks
                               else "御支給品")
            it2.reasoning = LineItemReasoning(
                method=PricingMethod.SUPPLIED,
                formula="御支給品のため ¥0",
                source="支給品の選択（属性）",
                note="機器は支給・設置工事は計上（工事費は0円にしない）",
            )
        new_items.append(it2)
    eq.items = new_items
    eq.calculate_totals()
    # 小計が大きく変わるため、値引き（端数切捨て）も新しい小計で再計算する。
    # 旧値引きのまま calculate_totals すると、支給品化で小計が減った際に
    # 税抜合計がマイナスになり得る（Codexレビュー指摘）
    _recompute_discount_and_totals(estimate)


def _recompute_discount_and_totals(estimate: EstimateData) -> None:
    """カテゴリ合計から値引き→税抜/税込を再計算する。

    値引きが自動（discount_method の端数切捨て）由来のときだけ新小計で
    再計算し、「値引き調整」で手動設定された値引きは維持する
    （Codexレビュー指摘: 支給品切替で手動値引きが消えていた）。
    """
    summary = estimate.summary
    try:
        from pricing.knowledge_base import load_pricing_rules
        rules = load_pricing_rules()
    except Exception:
        rules = {}
    method = rules.get("discount_method", "round_down_10000")

    def _auto_total(sub: int) -> int:
        if method == "round_down_10000":
            return (sub // 10000) * 10000
        if method == "round_down_100000":
            return (sub // 100000) * 100000
        return sub

    was_auto = summary.discount == \
        _auto_total(summary.subtotal) - summary.subtotal
    summary.subtotal = sum(c.total for c in summary.categories)
    if was_auto:
        summary.discount = _auto_total(summary.subtotal) - summary.subtotal
    summary.total_before_tax = summary.subtotal + summary.discount
    from pricing.estimate_v2 import tax_amount
    summary.tax = tax_amount(summary.total_before_tax, rules)
    summary.total_with_tax = summary.total_before_tax + summary.tax
    estimate.cover.total_before_tax = summary.total_before_tax
    estimate.cover.tax = summary.tax
    estimate.cover.total_with_tax = summary.total_with_tax


def apply_supply_selection(estimate: EstimateData, snapshot: dict,
                           flags: dict) -> None:
    """支給品チェック状態を見積に反映する（in-place・冪等）。

    支給品カテゴリと「切替由来の材料費行」だけを組み立て直す。
    材料費のそれ以外の行（基本項目・手動編集・単価マスターからの明細追加）と、
    ユーザーが後から支給品カテゴリに追加した行は現状のまま維持する。

    Args:
        estimate: 反映先の見積
        snapshot: snapshot_sections() の戻り値（生成直後の支給品リスト原本）
        flags: {item_key: bool}。True=支給品（既定）、False=材料費へ移動
    """
    supplied_sec = _find_section(estimate, CategoryType.SUPPLIED)
    material_sec = _find_section(estimate, CategoryType.MATERIAL)
    if supplied_sec is None or material_sec is None:
        return

    originals = snapshot.get("supplied", [])
    orig_keys = {item_key(it) for it in originals}

    # 支給品: 原本のうちONのもの ＋ ユーザーが後から追加した支給品行を維持
    supplied_items = [copy.deepcopy(it) for it in originals
                      if flags.get(item_key(it), True)]
    supplied_items.extend(
        it for it in supplied_sec.items if item_key(it) not in orig_keys)

    # 材料費: 切替由来の行だけ差し替え、それ以外は現在の内容を維持
    material_items = [it for it in material_sec.items if not _is_moved_row(it)]
    material_items.extend(
        _to_material_item(it) for it in originals
        if not flags.get(item_key(it), True))

    for i, it in enumerate(supplied_items, start=1):
        it.no = i
    for i, it in enumerate(material_items, start=1):
        it.no = i

    supplied_sec.items = supplied_items
    material_sec.items = material_items
    supplied_sec.calculate_totals()
    material_sec.calculate_totals()
    estimate.summary.calculate_totals()
    estimate.cover.total_before_tax = estimate.summary.total_before_tax
    estimate.cover.tax = estimate.summary.tax
    estimate.cover.total_with_tax = estimate.summary.total_with_tax
