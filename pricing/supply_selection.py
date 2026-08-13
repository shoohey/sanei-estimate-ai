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
