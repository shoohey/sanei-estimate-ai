"""見積全体の組み立て"""
from models.survey_data import SurveyData
from models.estimate_data import EstimateData
from pricing.pricing_engine import generate_estimate


def build_estimate(survey: SurveyData, client_name: str = "") -> EstimateData:
    """現調データから見積データを組み立て

    Args:
        survey: 現調シートデータ
        client_name: 宛先会社名

    Returns:
        EstimateData: 完成した見積データ
    """
    estimate = generate_estimate(survey, client_name)
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

    # 全体合計を再計算
    estimate.summary.calculate_totals()

    # カバーページに反映
    estimate.cover.total_with_tax = estimate.summary.total_with_tax
    estimate.cover.total_before_tax = estimate.summary.total_before_tax
    estimate.cover.tax = estimate.summary.tax

    return estimate
