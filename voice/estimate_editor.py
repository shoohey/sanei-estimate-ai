"""見積データ編集モジュール

voice_command_parser.parse_voice_command() で得られた構造化コマンドを
EstimateData に適用する。

副作用なし: ディープコピーされた EstimateData を返す。
各コマンド適用後に calculate_totals() で合計を再計算する。

使い方:
    from voice.estimate_editor import apply_commands
    new_estimate, logs = apply_commands(estimate, commands)
    for log in logs:
        print(log)
"""
from __future__ import annotations

import copy
import logging
from typing import Optional

from models.estimate_data import (
    EstimateData, CategorySection, LineItem,
    CategoryType, PricingMethod, LineItemReasoning,
)

logger = logging.getLogger(__name__)


def apply_commands(
    estimate: EstimateData, commands: list[dict]
) -> tuple[EstimateData, list[str]]:
    """構造化コマンドを順次適用する

    Args:
        estimate: 元の EstimateData（副作用なし、ディープコピーされる）
        commands: 構造化コマンドのリスト

    Returns:
        (更新後の EstimateData, 適用ログメッセージのリスト)
    """
    new_estimate = copy.deepcopy(estimate)
    logs: list[str] = []

    handler_map = {
        "update_unit_price": _handle_update_unit_price,
        "update_quantity": _handle_update_quantity,
        "update_amount": _handle_update_amount,
        "update_description": _handle_update_description,
        "update_remarks": _handle_update_remarks,
        "delete_item": _handle_delete_item,
        "add_item": _handle_add_item,
        "set_discount": _handle_set_discount,
        "set_client_name": _handle_set_client_name,
        "set_project_name": _handle_set_project_name,
        "set_validity_period": _handle_set_validity_period,
        "unknown": _handle_unknown,
    }

    for cmd in commands:
        if not isinstance(cmd, dict):
            logs.append(f"❌ 不正なコマンド形式: {cmd}")
            continue
        action = cmd.get("action", "unknown")
        handler = handler_map.get(action, _handle_unknown)
        try:
            log = handler(new_estimate, cmd)
        except Exception as e:
            logger.exception(f"コマンド適用エラー: {cmd}")
            log = f"❌ {action} 適用中にエラー: {e}"
        logs.append(log)

        # 各コマンド適用後に合計を再計算
        try:
            _recalculate_all(new_estimate)
        except Exception as e:
            logger.warning(f"合計再計算エラー: {e}")

    return new_estimate, logs


def _recalculate_all(estimate: EstimateData) -> None:
    """全カテゴリ + 全体の合計を再計算"""
    for cat in estimate.summary.categories:
        cat.calculate_totals()
    estimate.summary.calculate_totals()
    # cover の金額も同期
    estimate.cover.total_before_tax = estimate.summary.total_before_tax
    estimate.cover.tax = estimate.summary.tax
    estimate.cover.total_with_tax = estimate.summary.total_with_tax


def _find_item(
    estimate: EstimateData, category: str, item_match: str
) -> tuple[Optional[CategorySection], Optional[LineItem]]:
    """カテゴリ名と部分一致文字列から明細を探す

    Args:
        estimate: 検索対象の EstimateData
        category: カテゴリ名（完全一致または前方一致）
        item_match: 明細の摘要に対する部分一致文字列

    Returns:
        (CategorySection, LineItem) のタプル。見つからなければ (None, None)
    """
    cat_section = _find_category(estimate, category)
    if cat_section is None:
        return None, None

    if not item_match:
        return cat_section, None

    item_match_norm = str(item_match).strip()
    # 完全一致を優先
    for item in cat_section.items:
        if item.description == item_match_norm:
            return cat_section, item
    # 部分一致（明細の摘要に含まれる）
    for item in cat_section.items:
        if item_match_norm in item.description:
            return cat_section, item
    # 部分一致（item_match の方に明細が含まれる: 逆方向）
    for item in cat_section.items:
        if item.description and item.description in item_match_norm:
            return cat_section, item
    # remarks にもマッチを試みる
    for item in cat_section.items:
        if item.remarks and item_match_norm in item.remarks:
            return cat_section, item

    return cat_section, None


def _find_category(estimate: EstimateData, category: str) -> Optional[CategorySection]:
    """カテゴリ名から CategorySection を探す（完全一致 → 前方一致）"""
    if not category:
        return None
    cat_str = str(category).strip()
    # 完全一致
    for cat in estimate.summary.categories:
        cat_value = cat.category.value if hasattr(cat.category, "value") else str(cat.category)
        if cat_value == cat_str:
            return cat
    # 前方一致
    for cat in estimate.summary.categories:
        cat_value = cat.category.value if hasattr(cat.category, "value") else str(cat.category)
        if cat_value.startswith(cat_str) or cat_str.startswith(cat_value):
            return cat
    return None


# ============================================================
# 各アクションのハンドラ
# ============================================================

def _handle_update_unit_price(estimate: EstimateData, cmd: dict) -> str:
    category = cmd.get("category", "")
    item_match = cmd.get("item_match", "")
    new_value = cmd.get("new_value")
    if new_value is None:
        return f"❌ 単価変更: new_value が指定されていません"
    try:
        new_price = int(new_value)
    except (ValueError, TypeError):
        return f"❌ 単価変更: new_value({new_value})を整数に変換できません"

    cat, item = _find_item(estimate, category, item_match)
    if cat is None:
        return f"❌ 単価変更: カテゴリ「{category}」が見つかりません"
    if item is None:
        return f"❌ 単価変更: {category}内に「{item_match}」を含む明細が見つかりません"

    old_price = item.unit_price
    item.unit_price = new_price
    # 数量から金額を再計算
    if item.quantity_value:
        item.amount = int(item.quantity_value * new_price)
    else:
        # quantity_value が無い場合は quantity 文字列から推定
        from re import search
        m = search(r"[\d.]+", item.quantity or "")
        if m:
            try:
                qv = float(m.group(0))
                item.amount = int(qv * new_price)
            except ValueError:
                pass

    return (f"✅ {cat.category.value if hasattr(cat.category, 'value') else cat.category}"
            f"「{item.description}」の単価を ¥{old_price:,} → ¥{new_price:,} に変更しました")


def _handle_update_quantity(estimate: EstimateData, cmd: dict) -> str:
    category = cmd.get("category", "")
    item_match = cmd.get("item_match", "")
    new_value = cmd.get("new_value")
    new_unit = cmd.get("new_unit", "")
    if new_value is None:
        return f"❌ 数量変更: new_value が指定されていません"
    try:
        new_qty = float(new_value)
    except (ValueError, TypeError):
        return f"❌ 数量変更: new_value({new_value})を数値に変換できません"

    cat, item = _find_item(estimate, category, item_match)
    if cat is None:
        return f"❌ 数量変更: カテゴリ「{category}」が見つかりません"
    if item is None:
        return f"❌ 数量変更: {category}内に「{item_match}」を含む明細が見つかりません"

    old_qty = item.quantity
    item.quantity_value = new_qty
    if new_unit:
        item.quantity_unit = str(new_unit)
    unit = item.quantity_unit or ""
    # 数値表記（整数なら整数、小数なら小数）
    if new_qty == int(new_qty):
        item.quantity = f"{int(new_qty)}{unit}"
    else:
        item.quantity = f"{new_qty}{unit}"
    # 金額を再計算
    if item.unit_price:
        item.amount = int(new_qty * item.unit_price)

    return (f"✅ {cat.category.value if hasattr(cat.category, 'value') else cat.category}"
            f"「{item.description}」の数量を {old_qty} → {item.quantity} に変更しました")


def _handle_update_amount(estimate: EstimateData, cmd: dict) -> str:
    category = cmd.get("category", "")
    item_match = cmd.get("item_match", "")
    new_value = cmd.get("new_value")
    if new_value is None:
        return f"❌ 金額変更: new_value が指定されていません"
    try:
        new_amount = int(new_value)
    except (ValueError, TypeError):
        return f"❌ 金額変更: new_value({new_value})を整数に変換できません"

    cat, item = _find_item(estimate, category, item_match)
    if cat is None:
        return f"❌ 金額変更: カテゴリ「{category}」が見つかりません"
    if item is None:
        return f"❌ 金額変更: {category}内に「{item_match}」を含む明細が見つかりません"

    old_amount = item.amount
    item.amount = new_amount

    return (f"✅ {cat.category.value if hasattr(cat.category, 'value') else cat.category}"
            f"「{item.description}」の金額を ¥{old_amount:,} → ¥{new_amount:,} に変更しました")


def _handle_update_description(estimate: EstimateData, cmd: dict) -> str:
    category = cmd.get("category", "")
    item_match = cmd.get("item_match", "")
    new_value = cmd.get("new_value", "")

    cat, item = _find_item(estimate, category, item_match)
    if cat is None:
        return f"❌ 摘要変更: カテゴリ「{category}」が見つかりません"
    if item is None:
        return f"❌ 摘要変更: {category}内に「{item_match}」を含む明細が見つかりません"

    old_desc = item.description
    item.description = str(new_value)
    return (f"✅ {cat.category.value if hasattr(cat.category, 'value') else cat.category}"
            f"の摘要を「{old_desc}」→「{item.description}」に変更しました")


def _handle_update_remarks(estimate: EstimateData, cmd: dict) -> str:
    category = cmd.get("category", "")
    item_match = cmd.get("item_match", "")
    new_value = cmd.get("new_value", "")

    cat, item = _find_item(estimate, category, item_match)
    if cat is None:
        return f"❌ 備考変更: カテゴリ「{category}」が見つかりません"
    if item is None:
        return f"❌ 備考変更: {category}内に「{item_match}」を含む明細が見つかりません"

    old_remarks = item.remarks
    item.remarks = str(new_value)
    return (f"✅ {cat.category.value if hasattr(cat.category, 'value') else cat.category}"
            f"「{item.description}」の備考を「{old_remarks}」→「{item.remarks}」に変更しました")


def _handle_delete_item(estimate: EstimateData, cmd: dict) -> str:
    category = cmd.get("category", "")
    item_match = cmd.get("item_match", "")

    cat, item = _find_item(estimate, category, item_match)
    if cat is None:
        return f"❌ 削除: カテゴリ「{category}」が見つかりません"
    if item is None:
        return f"❌ 削除: {category}内に「{item_match}」を含む明細が見つかりません"

    desc = item.description
    cat.items.remove(item)
    # 行番号を振り直し
    for i, it in enumerate(cat.items, start=1):
        it.no = i
    return (f"✅ {cat.category.value if hasattr(cat.category, 'value') else cat.category}"
            f"「{desc}」を削除しました")


def _handle_add_item(estimate: EstimateData, cmd: dict) -> str:
    category = cmd.get("category", "")
    description = cmd.get("description", "")
    quantity = cmd.get("quantity", "1式")
    unit_price = cmd.get("unit_price", 0)
    amount = cmd.get("amount", 0)

    if not description:
        return f"❌ 明細追加: description が指定されていません"

    cat = _find_category(estimate, category)
    if cat is None:
        return f"❌ 明細追加: カテゴリ「{category}」が見つかりません"

    try:
        unit_price_int = int(unit_price) if unit_price else 0
    except (ValueError, TypeError):
        unit_price_int = 0
    try:
        amount_int = int(amount) if amount else 0
    except (ValueError, TypeError):
        amount_int = 0

    # 数量から数値部分を抽出
    import re
    qty_str = str(quantity)
    qty_match = re.match(r"\s*([\d.]+)\s*(.*)", qty_str)
    if qty_match:
        try:
            qty_value = float(qty_match.group(1))
        except ValueError:
            qty_value = 1.0
        qty_unit = qty_match.group(2).strip() or "式"
    else:
        qty_value = 1.0
        qty_unit = qty_str.strip() or "式"

    # 金額が指定されていなければ単価×数量で計算
    if amount_int == 0 and unit_price_int > 0:
        amount_int = int(qty_value * unit_price_int)

    new_no = len(cat.items) + 1
    new_item = LineItem(
        no=new_no,
        description=str(description),
        quantity=qty_str,
        quantity_value=qty_value,
        quantity_unit=qty_unit,
        unit_price=unit_price_int,
        amount=amount_int,
        reasoning=LineItemReasoning(
            method=PricingMethod.MANUAL,
            note="音声指示で追加",
        ),
        is_manual_input=True,
    )
    cat.items.append(new_item)
    return (f"✅ {cat.category.value if hasattr(cat.category, 'value') else cat.category}"
            f"に明細「{description}」(数量:{quantity}, 単価:¥{unit_price_int:,}, 金額:¥{amount_int:,})を追加しました")


def _handle_set_discount(estimate: EstimateData, cmd: dict) -> str:
    new_value = cmd.get("new_value")
    if new_value is None:
        return f"❌ 値引き設定: new_value が指定されていません"
    try:
        new_discount = int(new_value)
    except (ValueError, TypeError):
        return f"❌ 値引き設定: new_value({new_value})を整数に変換できません"

    old_discount = estimate.summary.discount
    estimate.summary.discount = new_discount
    return f"✅ お値引きを ¥{old_discount:,} → ¥{new_discount:,} に変更しました"


def _handle_set_client_name(estimate: EstimateData, cmd: dict) -> str:
    new_value = cmd.get("new_value", "")
    if not new_value:
        return f"❌ 宛先変更: new_value が指定されていません"
    old = estimate.cover.client_name
    estimate.cover.client_name = str(new_value)
    return f"✅ 宛先会社名を「{old}」→「{estimate.cover.client_name}」に変更しました"


def _handle_set_project_name(estimate: EstimateData, cmd: dict) -> str:
    new_value = cmd.get("new_value", "")
    if not new_value:
        return f"❌ 工事名変更: new_value が指定されていません"
    old = estimate.cover.project_name
    estimate.cover.project_name = str(new_value)
    return f"✅ 工事名を「{old}」→「{estimate.cover.project_name}」に変更しました"


def _handle_set_validity_period(estimate: EstimateData, cmd: dict) -> str:
    new_value = cmd.get("new_value", "")
    if not new_value:
        return f"❌ 有効期限変更: new_value が指定されていません"
    old = estimate.cover.validity_period
    estimate.cover.validity_period = str(new_value)
    return f"✅ 有効期限を「{old}」→「{estimate.cover.validity_period}」に変更しました"


def _handle_unknown(estimate: EstimateData, cmd: dict) -> str:
    reason = cmd.get("reason", "解釈できませんでした")
    return f"⚠ 未対応の指示: {reason}"


if __name__ == "__main__":
    # 動作確認: モックコマンドを直書きして apply_commands をテスト
    from models.estimate_data import (
        EstimateData, EstimateCover, EstimateSummary,
        CategorySection, LineItem, CategoryType,
    )

    sample_estimate = EstimateData(
        cover=EstimateCover(
            client_name="株式会社サンプル",
            project_name="太陽光発電設備設置工事",
            validity_period="発行日より60日間",
        ),
        summary=EstimateSummary(
            categories=[
                CategorySection(
                    category=CategoryType.SUPPLIED,
                    category_number=1,
                    items=[
                        LineItem(
                            no=1, description="太陽光パネル 540W 単結晶",
                            quantity="288枚", quantity_value=288, quantity_unit="枚",
                            unit_price=0, amount=0,
                        ),
                    ],
                ),
                CategorySection(
                    category=CategoryType.MATERIAL,
                    category_number=2,
                    items=[
                        LineItem(
                            no=1, description="ケーブルラック",
                            quantity="30m", quantity_value=30, quantity_unit="m",
                            unit_price=5000, amount=150000,
                        ),
                        LineItem(
                            no=2, description="電線管 PF管",
                            quantity="50m", quantity_value=50, quantity_unit="m",
                            unit_price=800, amount=40000,
                        ),
                    ],
                ),
                CategorySection(
                    category=CategoryType.CONSTRUCTION,
                    category_number=3,
                    items=[
                        LineItem(
                            no=1, description="架台組立工事",
                            quantity="1式", quantity_value=1, quantity_unit="式",
                            unit_price=500000, amount=500000,
                        ),
                    ],
                ),
            ],
            discount=0,
        ),
    )

    # テストコマンド一覧
    mock_commands = [
        {
            "action": "update_unit_price",
            "category": "支給品",
            "item_match": "太陽光パネル",
            "new_value": 50000,
            "reason": "支給品の太陽光パネル単価を5万円に変更",
        },
        {
            "action": "delete_item",
            "category": "材料費",
            "item_match": "ケーブルラック",
            "reason": "ケーブルラックを削除",
        },
        {
            "action": "set_discount",
            "new_value": -50000,
            "reason": "お値引きを5万円に設定",
        },
        {
            "action": "set_client_name",
            "new_value": "株式会社テスト商事",
            "reason": "宛先を変更",
        },
        {
            "action": "update_quantity",
            "category": "施工費",
            "item_match": "架台組立",
            "new_value": 2,
            "new_unit": "式",
            "reason": "架台組立工事の数量を2式に変更",
        },
        {
            "action": "add_item",
            "category": "材料費",
            "description": "圧着端子",
            "quantity": "100個",
            "unit_price": 50,
            "amount": 5000,
            "reason": "圧着端子を追加",
        },
        {
            "action": "delete_item",
            "category": "支給品",
            "item_match": "存在しない項目",
            "reason": "テスト用の失敗ケース",
        },
        {
            "action": "unknown",
            "reason": "解釈できませんでした",
        },
    ]

    print("===== apply_commands 動作確認 =====\n")
    print(f"[適用前] 税込合計: ¥{sample_estimate.summary.total_with_tax:,}")
    print(f"[適用前] 値引き: ¥{sample_estimate.summary.discount:,}")
    print(f"[適用前] 宛先: {sample_estimate.cover.client_name}")
    print()

    new_estimate, logs = apply_commands(sample_estimate, mock_commands)
    for log in logs:
        print(log)

    print()
    print(f"[適用後] 税込合計: ¥{new_estimate.summary.total_with_tax:,}")
    print(f"[適用後] 税抜合計: ¥{new_estimate.summary.total_before_tax:,}")
    print(f"[適用後] 値引き: ¥{new_estimate.summary.discount:,}")
    print(f"[適用後] 宛先: {new_estimate.cover.client_name}")
    print(f"[適用後] 元データの宛先 (副作用なし確認): {sample_estimate.cover.client_name}")
    print()
    print("===== 適用後の明細 =====")
    for cat in new_estimate.summary.categories:
        cat_name = cat.category.value if hasattr(cat.category, "value") else cat.category
        print(f"\n■ {cat_name} (小計: ¥{cat.subtotal:,})")
        for item in cat.items:
            print(f"  {item.no}. {item.description} | {item.quantity} | "
                  f"単価¥{item.unit_price:,} | 金額¥{item.amount:,}")
