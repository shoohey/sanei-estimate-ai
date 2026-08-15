"""見積全体の組み立て"""
from models.survey_data import SurveyData
from models.estimate_data import EstimateData
from pricing.pricing_engine import generate_estimate


def build_estimate(survey: SurveyData, client_name: str = "",
                   handoff: dict = None) -> EstimateData:
    """現調データから見積データを組み立て

    Args:
        survey: 現調シートデータ
        client_name: 宛先会社名
        handoff: 図面側の設計確定情報（2026-08-15 ルールブック。任意）

    Returns:
        EstimateData: 完成した見積データ
    """
    estimate = generate_estimate(survey, client_name, handoff)
    return estimate


def update_line_item(estimate: EstimateData, category_idx: int, item_idx: int,
                     quantity_value: float = None, unit_price: int = None,
                     amount: int = None) -> EstimateData:
    """明細行を手動更新して合計を再計算

    Args:
        estimate: 見積データ
        category_idx: カテゴリインデックス (0-5)
        item_idx: 項目インデックス
        quantity_value: 新しい数量
        unit_price: 新しい単価
        amount: 新しい金額（直接指定）

    Returns:
        EstimateData: 更新された見積データ
    """
    cat = estimate.summary.categories[category_idx]
    item = cat.items[item_idx]

    if quantity_value is not None:
        item.quantity_value = quantity_value
        item.quantity = f"{quantity_value}{item.quantity_unit}"

    if unit_price is not None:
        item.unit_price = unit_price

    if amount is not None:
        item.amount = amount
    elif quantity_value is not None or unit_price is not None:
        item.amount = int(item.quantity_value * item.unit_price)

    # カテゴリ合計を再計算
    cat.calculate_totals()

    # 全体合計を再計算。値引きが自動（端数切捨て）由来なら新しい小計で
    # 再計算する（手動編集で小計が変わった後も古い値引きが残ると税抜合計が
    # ずれる — Codexレビュー指摘）。手動で設定した値引きはそのまま維持する
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

    was_auto = summary.discount == _auto_total(summary.subtotal) - summary.subtotal
    summary.subtotal = sum(c.total for c in summary.categories)
    if was_auto:
        summary.discount = _auto_total(summary.subtotal) - summary.subtotal
    summary.total_before_tax = summary.subtotal + summary.discount
    from pricing.estimate_v2 import tax_amount
    summary.tax = tax_amount(summary.total_before_tax, rules)
    summary.total_with_tax = summary.total_before_tax + summary.tax

    # カバーページに反映
    estimate.cover.total_with_tax = summary.total_with_tax
    estimate.cover.total_before_tax = summary.total_before_tax
    estimate.cover.tax = summary.tax

    return estimate
