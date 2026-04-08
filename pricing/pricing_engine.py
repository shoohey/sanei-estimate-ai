"""現調データ→見積項目変換（コアビジネスロジック）"""
import ast
import math
import operator
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
    from datetime import timedelta
    validity_date = date.today() + timedelta(days=30)
    estimate.cover.validity_period = validity_date.strftime("%Y/%m/%d")
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

    # 値引き: 指定の切り捨て方式で税抜合計を丸める
    # discount_method:
    #   - round_down_10000: 万円単位切り捨て（デフォルト、サンプル準拠）
    #   - round_down_100000: 10万円単位切り捨て
    #   - none / その他: 値引きなし
    discount_method = rules.get("discount_method", "round_down_10000")
    if discount_method == "round_down_10000":
        rounded = math.floor(subtotal / 10000) * 10000
        estimate.summary.discount = rounded - subtotal  # 負の値
        estimate.summary.total_before_tax = rounded
    elif discount_method == "round_down_100000":
        rounded = math.floor(subtotal / 100000) * 100000
        estimate.summary.discount = rounded - subtotal  # 負の値
        estimate.summary.total_before_tax = rounded
    else:
        estimate.summary.discount = 0
        estimate.summary.total_before_tax = subtotal

    # 税額計算
    # tax_rounding_method:
    #   - floor: 切り捨て（デフォルト）
    #   - round: 四捨五入
    #   - ceil: 切り上げ
    tax_rate = rules.get("tax_rate", TAX_RATE)
    tax_raw = estimate.summary.total_before_tax * tax_rate
    tax_rounding_method = rules.get("tax_rounding_method", "floor")
    if tax_rounding_method == "round":
        estimate.summary.tax = int(round(tax_raw))
    elif tax_rounding_method == "ceil":
        estimate.summary.tax = int(math.ceil(tax_raw))
    else:
        estimate.summary.tax = int(math.floor(tax_raw))
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
        # 条件チェック（材料費でも条件付き計上ができるように）
        condition = item_def.get("condition", "")
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
        unit_price = item_def.get("unit_price", 0)

        if pricing_method == "kw_rate":
            # 材料費でも kW_rate をサポート（PV容量連動の雑材など）
            kw = survey.equipment.pv_capacity_kw
            amount = int(kw * unit_price)
            quantity_str = str(kw)
            quantity_val = kw
        elif pricing_method == "lump_formula":
            # lump_formula: 計算式の結果を金額そのものとして扱う（unit_price不使用）
            quantity_str, quantity_val = _resolve_quantity(item_def, survey)
            amount = int(quantity_val)
            # 表示用の数量は1式に統一
            quantity_str = "1"
            unit_price = amount
        else:
            quantity_str, quantity_val = _resolve_quantity(item_def, survey)
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
        unit_price = item_def.get("unit_price", 0)

        if pricing_method == "kw_rate":
            # kW単価: PV容量 × 単価
            kw = survey.equipment.pv_capacity_kw
            amount = int(kw * unit_price)
            quantity_str = str(kw)
            quantity_val = kw
        elif pricing_method == "lump_formula":
            quantity_str, quantity_val = _resolve_quantity(item_def, survey)
            amount = int(quantity_val)
            quantity_str = "1"
            unit_price = amount
        else:
            quantity_str, quantity_val = _resolve_quantity(item_def, survey)
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

        pricing_method = item_def.get("pricing_method", "fixed")
        unit_price = item_def.get("unit_price", 0)

        if pricing_method == "kw_rate" and not is_manual:
            kw = survey.equipment.pv_capacity_kw
            amount = int(kw * unit_price)
            quantity_str = str(kw)
            quantity_val = kw
        elif pricing_method == "lump_formula" and not is_manual:
            # lump_formula: 計算式の結果を金額そのものとして扱う
            quantity_str, quantity_val = _resolve_quantity(item_def, survey)
            amount = int(quantity_val)
            quantity_str = "1"
            unit_price = amount
        else:
            quantity_str, quantity_val = _resolve_quantity(item_def, survey)
            amount = int(quantity_val * unit_price) if not is_manual else 0

        # 自動計算値を持つ手動項目の場合、参考値として見せる（is_manual=false に設定されている場合は
        # 自動値のまま。is_manual=true の場合は金額0のまま）
        reasoning = generate_reasoning(item_def, quantity_val, unit_price, amount, survey)
        if is_manual:
            reasoning = LineItemReasoning(
                method=PricingMethod.MANUAL,
                formula="手動入力が必要です",
                source=item_def.get("note", ""),
                note="金額を手動で入力してください",
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

        pricing_method = item_def.get("pricing_method", "fixed")
        unit_price = item_def.get("unit_price", 0)

        # 手動入力項目は金額0で表示
        if is_manual:
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
            continue

        # 自動計算項目（kw_rate / fixed / distance / lump_formula）
        if pricing_method == "kw_rate":
            kw = survey.equipment.pv_capacity_kw
            amount = int(kw * unit_price)
            quantity_str = str(kw)
            quantity_val = kw
        elif pricing_method == "lump_formula":
            quantity_str, quantity_val = _resolve_quantity(item_def, survey)
            amount = int(quantity_val)
            quantity_str = "1"
            unit_price = amount
        else:
            quantity_str, quantity_val = _resolve_quantity(item_def, survey)
            amount = int(quantity_val * unit_price)

        reasoning = generate_reasoning(item_def, quantity_val, unit_price, amount, survey)

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


def _resolve_quantity(item_def: dict, survey: SurveyData) -> tuple[str, float]:
    """数量を解決

    優先度:
        1. quantity_formula（計算式） ← 新規追加
        2. quantity_source（フィールド参照）
        3. quantity（固定値、後方互換）
    """
    # 1. 計算式を最優先で評価
    quantity_formula = item_def.get("quantity_formula", "")
    if quantity_formula:
        try:
            val = _evaluate_formula(quantity_formula, survey)
            if val is not None:
                # 整数に近ければ整数、そうでなければ小数
                if abs(val - round(val)) < 1e-9:
                    return str(int(round(val))), float(val)
                return f"{val:.2f}", float(val)
        except Exception:
            # 失敗時は quantity（固定値）にフォールバック
            pass

    # 2. フィールド参照
    quantity_source = item_def.get("quantity_source", "")
    if quantity_source:
        val = _get_nested_value(survey, quantity_source)
        if val is not None:
            return str(val), float(val)

    # 3. 固定値
    quantity = item_def.get("quantity", "")
    if quantity:
        try:
            return quantity, float(quantity)
        except (ValueError, TypeError):
            return str(quantity), 0

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


# =====================================================
# 条件式エバリュエータ（拡張版）
# =====================================================
_COMPARE_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _evaluate_condition(condition: str, survey: SurveyData) -> bool:
    """条件式を評価

    サポートする構文:
      - equipment.pv_capacity_kw > 100
      - high_voltage.pre_use_self_check == true
      - a == true AND b == true
      - a > 10 OR b == false
      - NOT supplementary.crane_available

    AND/OR/NOT は大文字小文字を問わず受け付ける。
    Pythonの and/or/not もそのまま使える。
    """
    if not condition or not condition.strip():
        return True

    # 全体を評価する再帰的な実装
    # まず AND/OR を上から分解（簡易実装：左から右に評価）
    try:
        return _eval_condition_expr(condition, survey)
    except Exception:
        # 未対応の構文は安全側（True）に倒して見積に計上する
        return True


def _eval_condition_expr(expr: str, survey: SurveyData) -> bool:
    """条件式を再帰的に評価"""
    expr = expr.strip()
    if not expr:
        return True

    # NOT を先頭で処理
    upper = expr.upper()
    if upper.startswith("NOT "):
        return not _eval_condition_expr(expr[4:], survey)

    # OR を最優先で分解（ANDよりも優先度が低い）
    or_parts = _split_top_level(expr, [" OR ", " or "])
    if len(or_parts) > 1:
        return any(_eval_condition_expr(p, survey) for p in or_parts)

    # AND で分解
    and_parts = _split_top_level(expr, [" AND ", " and "])
    if len(and_parts) > 1:
        return all(_eval_condition_expr(p, survey) for p in and_parts)

    # 括弧で囲まれている場合は中身を評価
    if expr.startswith("(") and expr.endswith(")"):
        return _eval_condition_expr(expr[1:-1], survey)

    # 単一の比較式
    return _eval_single_comparison(expr, survey)


def _split_top_level(expr: str, separators: list[str]) -> list[str]:
    """括弧のネストを考慮して expr を separators で分割（トップレベルのみ）"""
    parts = []
    depth = 0
    i = 0
    last = 0
    while i < len(expr):
        c = expr[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0:
            matched = None
            for sep in separators:
                if expr[i:i + len(sep)] == sep:
                    matched = sep
                    break
            if matched:
                parts.append(expr[last:i])
                i += len(matched)
                last = i
                continue
        i += 1
    parts.append(expr[last:])
    if len(parts) == 1:
        return parts
    return [p for p in parts if p.strip()]


def _eval_single_comparison(expr: str, survey: SurveyData) -> bool:
    """単一の比較式を評価（例: equipment.pv_capacity_kw > 100）"""
    expr = expr.strip()

    # 比較演算子を優先順位の長い順に検索（>=, <=, != を == や > より先に）
    for op_str, op_func in [
        (">=", operator.ge),
        ("<=", operator.le),
        ("!=", operator.ne),
        ("==", operator.eq),
        (">", operator.gt),
        ("<", operator.lt),
    ]:
        idx = expr.find(op_str)
        if idx >= 0:
            left = expr[:idx].strip()
            right = expr[idx + len(op_str):].strip()
            left_val = _resolve_condition_operand(left, survey)
            right_val = _resolve_condition_operand(right, survey)
            # boolean と数値を混在できるように型を揃える
            left_val, right_val = _normalize_compare_values(left_val, right_val)
            try:
                return bool(op_func(left_val, right_val))
            except TypeError:
                return False

    # 比較演算子がない場合はbooleanとして評価（例: "supplementary.crane_available"）
    val = _resolve_condition_operand(expr, survey)
    return bool(val)


def _resolve_condition_operand(token: str, survey: SurveyData):
    """条件式の被演算子を解決（フィールド参照 or リテラル）"""
    token = token.strip()
    if not token:
        return None

    # boolean リテラル
    lower = token.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("none", "null"):
        return None

    # 数値リテラル
    try:
        if "." in token:
            return float(token)
        return int(token)
    except ValueError:
        pass

    # 文字列リテラル（クォート付き）
    if (token.startswith("'") and token.endswith("'")) or \
       (token.startswith('"') and token.endswith('"')):
        return token[1:-1]

    # フィールド参照
    val = _get_nested_value(survey, token)
    # Enum の場合は value を返す
    if hasattr(val, "value"):
        return val.value
    return val


def _normalize_compare_values(left, right):
    """比較時の型揃え（booleanと数値が混在した場合などに対応）"""
    # 片方が None の場合はそのまま（False と比較される）
    if left is None or right is None:
        return left, right
    # 両方数値なら float にそろえる
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left), float(right)
    # 片方が数値で片方が bool → bool を 0/1 に
    if isinstance(left, bool) and isinstance(right, (int, float)):
        return float(left), float(right)
    if isinstance(right, bool) and isinstance(left, (int, float)):
        return float(left), float(right)
    return left, right


# =====================================================
# 計算式エバリュエータ（数量計算用）
# =====================================================
# 許可する二項演算子
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

# 許可する単項演算子
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# 許可する関数
_ALLOWED_FUNCS = {
    "max": max,
    "min": min,
    "abs": abs,
    "round": round,
    "int": int,
    "float": float,
    "ceil": math.ceil,
    "floor": math.floor,
}


def _evaluate_formula(formula: str, survey: SurveyData) -> float:
    """数量計算式を安全に評価する（eval不使用）

    ast モジュールでパースしホワイトリスト方式で実行する。
    使える変数:
        - pv_capacity_kw: PV容量(kW)
        - planned_panels: 設置予定枚数
        - module_output_w: モジュール定格出力(W)
        - separation_ns_mm: 離隔 縦(南北) mm
        - separation_ew_mm: 離隔 横(東西) mm
        - separation_ns_m: 離隔 縦 m（mm/1000の便利変数）
        - separation_ew_m: 離隔 横 m
        - separation_total_m: (ns + ew) / 1000
    使える関数: max, min, abs, round, int, float, ceil, floor
    使える演算子: + - * / // % **

    Args:
        formula: 計算式の文字列
        survey: 現調データ

    Returns:
        float: 計算結果

    Raises:
        ValueError: 式が不正な場合
    """
    if not formula or not isinstance(formula, str):
        raise ValueError("formula is empty")

    variables = _build_formula_variables(survey)

    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"formula syntax error: {e}") from e

    return _eval_ast_node(tree.body, variables)


def _build_formula_variables(survey: SurveyData) -> dict:
    """計算式で使える変数のマッピングを構築"""
    eq = survey.equipment
    hv = survey.high_voltage
    sup = survey.supplementary

    sep_ns_mm = float(hv.separation_ns_mm or 0)
    sep_ew_mm = float(hv.separation_ew_mm or 0)

    return {
        "pv_capacity_kw": float(eq.pv_capacity_kw or 0),
        "planned_panels": float(eq.planned_panels or 0),
        "module_output_w": float(eq.module_output_w or 0),
        "separation_ns_mm": sep_ns_mm,
        "separation_ew_mm": sep_ew_mm,
        "separation_ns_m": sep_ns_mm / 1000.0,
        "separation_ew_m": sep_ew_mm / 1000.0,
        "separation_total_m": (sep_ns_mm + sep_ew_mm) / 1000.0,
        # 追加の便利変数
        "crane_available": bool(sup.crane_available),
        "scaffold_needed": bool(sup.scaffold_needed),
        "cubicle_location": bool(sup.cubicle_location),
        "pre_use_self_check": bool(hv.pre_use_self_check),
    }


def _eval_ast_node(node, variables: dict):
    """AST ノードを再帰的に評価（ホワイトリスト方式）"""
    # 数値リテラル（Py3.8+ はConstant）
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError(f"unsupported constant: {node.value!r}")

    # 変数参照
    if isinstance(node, ast.Name):
        if node.id in variables:
            return float(variables[node.id])
        if node.id in _ALLOWED_FUNCS:
            return _ALLOWED_FUNCS[node.id]
        raise ValueError(f"unknown variable: {node.id}")

    # 二項演算子
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BIN_OPS:
            raise ValueError(f"unsupported binary operator: {op_type.__name__}")
        left = _eval_ast_node(node.left, variables)
        right = _eval_ast_node(node.right, variables)
        return _BIN_OPS[op_type](left, right)

    # 単項演算子
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARY_OPS:
            raise ValueError(f"unsupported unary operator: {op_type.__name__}")
        return _UNARY_OPS[op_type](_eval_ast_node(node.operand, variables))

    # 関数呼び出し（max, min, ...）
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("only simple function calls are allowed")
        fname = node.func.id
        if fname not in _ALLOWED_FUNCS:
            raise ValueError(f"function not allowed: {fname}")
        args = [_eval_ast_node(a, variables) for a in node.args]
        return _ALLOWED_FUNCS[fname](*args)

    raise ValueError(f"unsupported expression node: {type(node).__name__}")


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
