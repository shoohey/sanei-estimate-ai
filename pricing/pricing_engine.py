"""現調データ→見積項目変換（コアビジネスロジック）"""
import math
from models.survey_data import SurveyData
from models.estimate_data import (
    EstimateData, EstimateCover, EstimateSummary, CategorySection,
    LineItem, LineItemReasoning, CategoryType, PricingMethod,
)
from pricing.knowledge_base import load_pricing_rules
from pricing.reasoning import generate_reasoning
from config import generate_estimate_id, TAX_RATE, COMPANY_INFO
from datetime import date


def generate_estimate(survey: SurveyData, client_name: str = "") -> EstimateData:
    """現調データから見積データを生成

    Args:
        survey: 現調シートデータ
        client_name: 宛先会社名

    Returns:
        EstimateData: 見積書データ
    """
    rules = load_pricing_rules()
    estimate = EstimateData()

    # カバーページ情報
    estimate.cover.estimate_id = generate_estimate_id()
    estimate.cover.issue_date = date.today().strftime("%Y/%m/%d")
    estimate.cover.client_name = client_name
    estimate.cover.project_name = (
        f"{survey.project.project_name}　太陽光設置工事 見積"
        f"（{survey.equipment.pv_capacity_kw}kW）"
    )
    estimate.cover.project_location = survey.project.address
    estimate.cover.representative = COMPANY_INFO["default_representative"]

    # 各カテゴリの見積項目を生成
    categories = []

    # 1. 支給品
    supplied = _build_supplied_section(rules, survey)
    categories.append(supplied)

    # 2. 材料費
    material = _build_material_section(rules, survey)
    categories.append(material)

    # 3. 施工費
    construction = _build_construction_section(rules, survey)
    categories.append(construction)

    # 4. その他・諸経費等
    overhead = _build_overhead_section(rules, survey)
    categories.append(overhead)

    # 5. 付帯工事
    additional = _build_additional_section(rules, survey)
    categories.append(additional)

    # 6. 特記事項（空）
    special = CategorySection(
        category=CategoryType.SPECIAL_NOTES,
        category_number=6,
        items=[],
        subtotal=0,
        total=0,
    )
    categories.append(special)

    # サマリー計算
    estimate.summary.categories = categories
    subtotal = sum(c.total for c in categories)
    estimate.summary.subtotal = subtotal

    # 値引き: 万円単位に切り捨て（デフォルト）
    # ※サンプルでは手動で -112,388 → 税抜9,130,000に調整されている
    # UIで手動調整可能
    discount_method = rules.get("discount_method", "round_down_10000")
    if discount_method == "round_down_10000":
        rounded = math.floor(subtotal / 10000) * 10000
        estimate.summary.discount = rounded - subtotal  # 負の値
        estimate.summary.total_before_tax = rounded
    else:
        estimate.summary.discount = 0
        estimate.summary.total_before_tax = subtotal

    # 税額計算
    tax_rate = rules.get("tax_rate", TAX_RATE)
    estimate.summary.tax = int(estimate.summary.total_before_tax * tax_rate)
    estimate.summary.total_with_tax = estimate.summary.total_before_tax + estimate.summary.tax

    # カバーページに合計転記
    estimate.cover.total_with_tax = estimate.summary.total_with_tax
    estimate.cover.total_before_tax = estimate.summary.total_before_tax
    estimate.cover.tax = estimate.summary.tax

    # 根拠一覧を収集
    estimate.reasoning_list = _collect_reasoning(categories)

    return estimate


def _build_supplied_section(rules: dict, survey: SurveyData) -> CategorySection:
    """支給品セクションを構築"""
    section = CategorySection(
        category=CategoryType.SUPPLIED,
        category_number=1,
    )
    items_def = rules.get("supplied_items", [])

    for item_def in items_def:
        # 数量の解決
        quantity_str, quantity_val = _resolve_quantity(item_def, survey)

        # 備考のテンプレート解決
        remarks = item_def.get("remarks", "")
        remarks_template = item_def.get("remarks_template", "")
        if remarks_template:
            remarks = _resolve_template(remarks_template, survey)

        item = LineItem(
            no=item_def["no"],
            description=item_def["description"],
            remarks=f"{remarks}\n御支給品" if remarks else "御支給品",
            quantity=f"{quantity_str}{item_def.get('quantity_unit', '')}",
            quantity_value=quantity_val,
            quantity_unit=item_def.get("quantity_unit", ""),
            unit_price=0,
            amount=0,
            reasoning=LineItemReasoning(
                method=PricingMethod.SUPPLIED,
                formula="御支給品のため ¥0",
                source="支給品リスト",
                note=item_def.get("note", ""),
            ),
        )
        section.items.append(item)

    section.calculate_totals()
    return section


def _build_material_section(rules: dict, survey: SurveyData) -> CategorySection:
    """材料費セクションを構築"""
    section = CategorySection(
        category=CategoryType.MATERIAL,
        category_number=2,
    )
    items_def = rules.get("material_items", [])

    for item_def in items_def:
        quantity_str, quantity_val = _resolve_quantity(item_def, survey)
        unit_price = item_def.get("unit_price", 0)
        amount = int(quantity_val * unit_price)

        reasoning = generate_reasoning(item_def, quantity_val, unit_price, amount, survey)

        item = LineItem(
            no=item_def["no"],
            description=item_def["description"],
            remarks=item_def.get("remarks", ""),
            quantity=f"{quantity_str}{item_def.get('quantity_unit', '')}",
            quantity_value=quantity_val,
            quantity_unit=item_def.get("quantity_unit", ""),
            unit_price=unit_price,
            amount=amount,
            reasoning=reasoning,
        )
        section.items.append(item)

    section.calculate_totals()
    return section


def _build_construction_section(rules: dict, survey: SurveyData) -> CategorySection:
    """施工費セクションを構築"""
    section = CategorySection(
        category=CategoryType.CONSTRUCTION,
        category_number=3,
    )
    items_def = rules.get("construction_items", [])

    for item_def in items_def:
        # 条件チェック
        condition = item_def.get("condition", "")
        if condition and not _evaluate_condition(condition, survey):
            # 条件不成立→項目は表示するが金額0
            item = LineItem(
                no=item_def["no"],
                description=item_def["description"],
                remarks=item_def.get("remarks", ""),
                quantity="",
                quantity_value=0,
                quantity_unit="",
                unit_price=0,
                amount=0,
                reasoning=LineItemReasoning(
                    method=PricingMethod.CONDITIONAL,
                    formula="条件不成立のため ¥0",
                    source=f"条件: {condition}",
                    note=item_def.get("note", ""),
                ),
            )
            section.items.append(item)
            continue

        pricing_method = item_def.get("pricing_method", "fixed")
        quantity_str, quantity_val = _resolve_quantity(item_def, survey)
        unit_price = item_def.get("unit_price", 0)

        if pricing_method == "kw_rate":
            # kW単価: PV容量 × 単価
            kw = survey.equipment.pv_capacity_kw
            amount = int(kw * unit_price)
            quantity_str = str(kw)
            quantity_val = kw
        else:
            amount = int(quantity_val * unit_price)

        reasoning = generate_reasoning(item_def, quantity_val, unit_price, amount, survey)

        item = LineItem(
            no=item_def["no"],
            description=item_def["description"],
            remarks=item_def.get("remarks", ""),
            quantity=f"{quantity_str}{item_def.get('quantity_unit', '')}",
            quantity_value=quantity_val,
            quantity_unit=item_def.get("quantity_unit", ""),
            unit_price=unit_price,
            amount=amount,
            reasoning=reasoning,
        )
        section.items.append(item)

    section.calculate_totals()
    return section


def _build_overhead_section(rules: dict, survey: SurveyData) -> CategorySection:
    """その他・諸経費等セクションを構築"""
    section = CategorySection(
        category=CategoryType.OVERHEAD,
        category_number=4,
    )
    items_def = rules.get("overhead_items", [])

    for item_def in items_def:
        is_manual = item_def.get("is_manual", False)
        condition = item_def.get("condition", "")

        # 条件付きかつ条件不成立→空欄表示
        if condition and not _evaluate_condition(condition, survey):
            item = LineItem(
                no=item_def["no"],
                description=item_def["description"],
                remarks=item_def.get("remarks", ""),
                quantity="",
                quantity_value=0,
                quantity_unit="",
                unit_price=0,
                amount=0,
                is_manual_input=is_manual,
                reasoning=LineItemReasoning(
                    method=PricingMethod.CONDITIONAL,
                    formula="条件不成立のため計上なし",
                    source=f"条件: {condition}",
                    note=item_def.get("note", ""),
                ),
            )
            section.items.append(item)
            continue

        quantity_str, quantity_val = _resolve_quantity(item_def, survey)
        unit_price = item_def.get("unit_price", 0)
        amount = int(quantity_val * unit_price) if not is_manual else 0

        method = PricingMethod.MANUAL if is_manual else PricingMethod.FIXED
        reasoning = LineItemReasoning(
            method=method,
            formula="手動入力が必要です" if is_manual else f"¥{unit_price:,} × {quantity_str}",
            source=item_def.get("note", ""),
            note="金額を手動で入力してください" if is_manual else "",
        )

        item = LineItem(
            no=item_def["no"],
            description=item_def["description"],
            remarks=item_def.get("remarks", ""),
            quantity=f"{quantity_str}{item_def.get('quantity_unit', '')}" if quantity_str else "",
            quantity_value=quantity_val,
            quantity_unit=item_def.get("quantity_unit", ""),
            unit_price=unit_price,
            amount=amount,
            is_manual_input=is_manual,
            reasoning=reasoning,
        )
        section.items.append(item)

    section.calculate_totals()
    return section


def _build_additional_section(rules: dict, survey: SurveyData) -> CategorySection:
    """付帯工事セクションを構築"""
    section = CategorySection(
        category=CategoryType.ADDITIONAL,
        category_number=5,
    )
    items_def = rules.get("additional_items", [])

    for item_def in items_def:
        is_manual = item_def.get("is_manual", False)

        item = LineItem(
            no=item_def["no"],
            description=item_def["description"],
            remarks=item_def.get("remarks", ""),
            quantity="",
            quantity_value=0,
            quantity_unit="",
            unit_price=0,
            amount=0,
            is_manual_input=is_manual,
            reasoning=LineItemReasoning(
                method=PricingMethod.MANUAL,
                formula="必要に応じて手動入力",
                source=item_def.get("note", ""),
                note="",
            ),
        )
        section.items.append(item)

    section.calculate_totals()
    return section


def _resolve_quantity(item_def: dict, survey: SurveyData) -> tuple[str, float]:
    """数量を解決"""
    quantity_source = item_def.get("quantity_source", "")

    if quantity_source:
        # survey dataからフィールド参照
        val = _get_nested_value(survey, quantity_source)
        if val is not None:
            return str(val), float(val)

    quantity = item_def.get("quantity", "")
    if quantity:
        try:
            return quantity, float(quantity)
        except ValueError:
            return quantity, 0

    return "", 0


def _resolve_template(template: str, survey: SurveyData) -> str:
    """テンプレート文字列を解決"""
    result = template
    if "{module_model}" in result:
        result = result.replace("{module_model}", survey.equipment.module_model)
    if "{module_maker}" in result:
        result = result.replace("{module_maker}", survey.equipment.module_maker)
    if "{pv_capacity_kw}" in result:
        result = result.replace("{pv_capacity_kw}", str(survey.equipment.pv_capacity_kw))
    return result


def _evaluate_condition(condition: str, survey: SurveyData) -> bool:
    """条件式を評価"""
    # "supplementary.crane_available == true" のような形式
    parts = condition.split("==")
    if len(parts) != 2:
        return True

    field_path = parts[0].strip()
    expected = parts[1].strip().lower()

    actual = _get_nested_value(survey, field_path)

    if expected in ("true", "false"):
        return bool(actual) == (expected == "true")

    return str(actual).lower() == expected


def _get_nested_value(obj, path: str):
    """ネストされたオブジェクトから値を取得"""
    parts = path.split(".")
    current = obj
    for part in parts:
        if hasattr(current, part):
            current = getattr(current, part)
        else:
            return None
    return current


def _collect_reasoning(categories: list[CategorySection]) -> list[str]:
    """全カテゴリから根拠テキストを収集"""
    reasoning_list = []
    for cat in categories:
        for item in cat.items:
            if item.reasoning and item.amount > 0:
                text = (
                    f"[{cat.category.value}] {item.description}: "
                    f"{item.reasoning.formula}"
                )
                if item.reasoning.source:
                    text += f"（{item.reasoning.source}）"
                reasoning_list.append(text)
    return reasoning_list
