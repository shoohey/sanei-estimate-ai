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
    note = item_def.get("note", "")
    unit = item_def.get("quantity_unit", "")
    quantity_formula = item_def.get("quantity_formula", "")

    # ========================================
    # lump_formula: 計算式の結果を金額そのものとして表示
    # ========================================
    if pricing_method == "lump_formula":
        return LineItemReasoning(
            method=PricingMethod.FIXED,
            formula=f"¥{amount:,}（式: {quantity_formula}）",
            source="PV容量等から自動算出（参考値）",
            note=note or "現場により調整要",
        )

    # ========================================
    # kW_rate: PV容量連動
    # ========================================
    if pricing_method == "kw_rate":
        kw = survey.equipment.pv_capacity_kw
        # 詳細な計算式表示
        formula = f"{kw}kW × ¥{unit_price:,}/kW = ¥{amount:,}"
        return LineItemReasoning(
            method=PricingMethod.KW_RATE,
            formula=formula,
            source="現調シート PV容量より",
            note=note,
        )

    # ========================================
    # distance: 配線距離・離隔連動
    # ========================================
    if pricing_method == "distance":
        # 計算式あり → 式の根拠と計算結果を表示
        if quantity_formula:
            sep_ns = survey.high_voltage.separation_ns_mm
            sep_ew = survey.high_voltage.separation_ew_mm
            source_parts = []
            if sep_ns or sep_ew:
                source_parts.append(
                    f"離隔 南北 {int(sep_ns)}mm / 東西 {int(sep_ew)}mm"
                )
            source_parts.append(f"式: {quantity_formula}")
            source = " / ".join(source_parts)
            formula = (
                f"{_format_qty(quantity_val)}{unit} × ¥{unit_price:,}/{unit} = ¥{amount:,}"
            )
            return LineItemReasoning(
                method=PricingMethod.DISTANCE,
                formula=formula,
                source=source,
                note=note or "配線距離より自動算出",
            )
        # 固定距離（旧方式）
        return LineItemReasoning(
            method=PricingMethod.DISTANCE,
            formula=(
                f"{_format_qty(quantity_val)}{unit} × ¥{unit_price:,}/{unit} = ¥{amount:,}"
            ),
            source="配線距離（標準値）",
            note=note,
        )

    # ========================================
    # fixed: 固定数量 or 計算式
    # ========================================
    if pricing_method == "fixed":
        # 計算式あり
        if quantity_formula:
            return LineItemReasoning(
                method=PricingMethod.FIXED,
                formula=(
                    f"{_format_qty(quantity_val)}{unit} × ¥{unit_price:,} = ¥{amount:,}"
                ),
                source=f"式: {quantity_formula}",
                note=note or "PV容量等から自動算出",
            )
        # 数量>1 → 単価×数量
        if quantity_val > 1:
            qty_display = _format_qty(quantity_val)
            return LineItemReasoning(
                method=PricingMethod.FIXED,
                formula=f"¥{unit_price:,} × {qty_display}{unit} = ¥{amount:,}",
                source="定額単価",
                note=note,
            )
        # 数量=1 → 固定額
        return LineItemReasoning(
            method=PricingMethod.FIXED,
            formula=f"¥{unit_price:,}（固定額）",
            source="定額単価",
            note=note,
        )

    # ========================================
    # conditional: 条件付き（条件成立時に計上）
    # ========================================
    if pricing_method == "conditional":
        condition = item_def.get("condition", "")
        if quantity_formula:
            formula = (
                f"{_format_qty(quantity_val)}{unit} × ¥{unit_price:,} = ¥{amount:,}"
            )
        elif quantity_val > 1:
            formula = (
                f"¥{unit_price:,} × {_format_qty(quantity_val)}{unit} = ¥{amount:,}"
            )
        else:
            formula = f"¥{amount:,}"
        return LineItemReasoning(
            method=PricingMethod.CONDITIONAL,
            formula=f"{formula}（条件: {_condition_to_japanese(condition)}）",
            source="条件付き計上",
            note=note,
        )

    # ========================================
    # manual: 手動入力
    # ========================================
    if pricing_method == "manual":
        return LineItemReasoning(
            method=PricingMethod.MANUAL,
            formula="手動入力が必要です",
            source="",
            note=note,
        )

    # フォールバック
    return LineItemReasoning(
        method=PricingMethod.FIXED,
        formula=f"¥{amount:,}",
        source="",
        note=note,
    )


def _format_qty(quantity_val: float) -> str:
    """数量を表示用にフォーマット"""
    if quantity_val is None:
        return "0"
    # 整数に近ければ整数表示
    if abs(quantity_val - round(quantity_val)) < 1e-9:
        return str(int(round(quantity_val)))
    return f"{quantity_val:.2f}"


def _condition_to_japanese(condition: str) -> str:
    """条件式を日本語に変換"""
    # 既知のパターン
    translations = {
        "supplementary.crane_available == true": "クレーンあり",
        "supplementary.cubicle_location == true": "キュービクルあり",
        "supplementary.scaffold_needed == true": "外部足場必要",
        "high_voltage.pre_use_self_check == true": "使用前自己確認あり",
        "high_voltage.vt_available == true": "VTあり",
        "high_voltage.ct_available == true": "CTあり",
        "high_voltage.pcs_space == true": "PCSスペースあり",
        "high_voltage.relay_space == true": "継電器スペースあり",
    }
    if condition in translations:
        return translations[condition]

    # AND/OR 式の場合は各要素を翻訳してつなぐ
    cond_lower = condition.lower()
    if " and " in cond_lower:
        parts = [p.strip() for p in condition.replace(" AND ", " and ").split(" and ")]
        return " かつ ".join(_condition_to_japanese(p) for p in parts)
    if " or " in cond_lower:
        parts = [p.strip() for p in condition.replace(" OR ", " or ").split(" or ")]
        return " または ".join(_condition_to_japanese(p) for p in parts)

    # 比較演算子を含む場合は簡易変換
    if ">=" in condition or "<=" in condition or ">" in condition or "<" in condition:
        return condition.replace("equipment.pv_capacity_kw", "PV容量")

    return condition
