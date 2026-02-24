"""各項目の根拠テキスト生成"""
from models.survey_data import SurveyData
from models.estimate_data import LineItemReasoning, PricingMethod


def generate_reasoning(
    item_def: dict,
    quantity_val: float,
    unit_price: int,
    amount: int,
    survey: SurveyData,
) -> LineItemReasoning:
    """見積項目の根拠を生成

    Args:
        item_def: YAML定義の項目情報
        quantity_val: 数量の数値
        unit_price: 単価
        amount: 金額
        survey: 現調データ

    Returns:
        LineItemReasoning: 根拠情報
    """
    pricing_method = item_def.get("pricing_method", "fixed")
    description = item_def.get("description", "")
    note = item_def.get("note", "")

    if pricing_method == "kw_rate":
        kw = survey.equipment.pv_capacity_kw
        return LineItemReasoning(
            method=PricingMethod.KW_RATE,
            formula=f"{kw}kW × ¥{unit_price:,}/kW = ¥{amount:,}",
            source="現調シート PV容量より",
            note=note,
        )

    elif pricing_method == "distance":
        unit = item_def.get("quantity_unit", "m")
        return LineItemReasoning(
            method=PricingMethod.DISTANCE,
            formula=f"{quantity_val}{unit} × ¥{unit_price:,}/{unit} = ¥{amount:,}",
            source="配線距離より算出",
            note=note,
        )

    elif pricing_method == "fixed":
        if quantity_val > 1:
            return LineItemReasoning(
                method=PricingMethod.FIXED,
                formula=f"¥{unit_price:,} × {int(quantity_val)} = ¥{amount:,}",
                source="定額単価",
                note=note,
            )
        return LineItemReasoning(
            method=PricingMethod.FIXED,
            formula=f"¥{unit_price:,}（固定額）",
            source="定額単価",
            note=note,
        )

    elif pricing_method == "conditional":
        condition = item_def.get("condition", "")
        return LineItemReasoning(
            method=PricingMethod.CONDITIONAL,
            formula=f"¥{unit_price:,}（条件: {_condition_to_japanese(condition)}）",
            source="条件付き計上",
            note=note,
        )

    elif pricing_method == "manual":
        return LineItemReasoning(
            method=PricingMethod.MANUAL,
            formula="手動入力が必要です",
            source="",
            note=note,
        )

    return LineItemReasoning(
        method=PricingMethod.FIXED,
        formula=f"¥{amount:,}",
        source="",
        note=note,
    )


def _condition_to_japanese(condition: str) -> str:
    """条件式を日本語に変換"""
    translations = {
        "supplementary.crane_available == true": "クレーンあり",
        "supplementary.cubicle_location == true": "キュービクルあり",
        "supplementary.scaffold_needed == true": "外部足場必要",
        "high_voltage.pre_use_self_check == true": "使用前自己確認あり",
    }
    return translations.get(condition, condition)
