"""抽出データの整合性チェック"""
from models.survey_data import SurveyData


class ValidationResult:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.feedback: list[str] = []  # 現調シートへのフィードバック

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


def validate_survey_data(data: SurveyData) -> ValidationResult:
    """現調データの整合性をチェック

    Args:
        data: 抽出された現調データ

    Returns:
        ValidationResult: 検証結果
    """
    result = ValidationResult()

    # 1. 必須項目の欠損チェック
    _check_required_fields(data, result)

    # 2. PV容量計算の一致確認
    _check_pv_capacity(data, result)

    # 3. 条件分岐の論理チェック
    _check_conditional_logic(data, result)

    # 4. 数値の妥当性チェック
    _check_value_ranges(data, result)

    return result


def _check_required_fields(data: SurveyData, result: ValidationResult):
    """必須項目の欠損チェック"""
    if not data.project.project_name:
        result.errors.append("案件名が未記入です")
        result.feedback.append("現調シート: 案件名を記入してください")

    if not data.project.address:
        result.errors.append("所在地が未記入です")
        result.feedback.append("現調シート: 所在地を記入してください")

    if not data.equipment.module_maker:
        result.warnings.append("モジュールメーカーが未記入です")

    if not data.equipment.module_model:
        result.warnings.append("モジュール型式が未記入です")

    if data.equipment.module_output_w == 0:
        result.errors.append("モジュール定格出力が未記入です")
        result.feedback.append("現調シート: モジュール定格出力(W/枚)を記入してください")

    if data.equipment.planned_panels == 0:
        result.errors.append("設置予定枚数が未記入です")
        result.feedback.append("現調シート: 設置予定枚数を記入してください")

    if data.equipment.pv_capacity_kw == 0:
        result.errors.append("想定PV容量が未記入です")

    if not data.project.survey_date:
        result.warnings.append("調査日が未記入です")

    if not data.project.surveyor:
        result.warnings.append("調査者が未記入です")


def _check_pv_capacity(data: SurveyData, result: ValidationResult):
    """容量計算：枚数 × 定格出力 ÷ 1000 = PV容量(kW) の一致確認"""
    if data.equipment.module_output_w > 0 and data.equipment.planned_panels > 0:
        calculated = data.equipment.planned_panels * data.equipment.module_output_w / 1000
        actual = data.equipment.pv_capacity_kw

        if actual > 0 and abs(calculated - actual) > 0.1:
            result.warnings.append(
                f"PV容量の計算が不一致: {data.equipment.planned_panels}枚 × "
                f"{data.equipment.module_output_w}W ÷ 1000 = {calculated:.2f}kW "
                f"（記載値: {actual}kW）"
            )


def _check_conditional_logic(data: SurveyData, result: ValidationResult):
    """条件分岐の論理チェック"""
    # PCS設置スペースありの場合→場所指定必須
    if data.high_voltage.pcs_space and not data.high_voltage.pcs_location:
        result.warnings.append("PCS設置スペース「あり」ですが、設置場所（屋内/屋外）が未指定です")
        result.feedback.append("現調シート: PCS設置場所（屋内/屋外）を選択してください")

    # 使用前自己確認ありの場合の確認
    if data.high_voltage.pre_use_self_check:
        pass  # 特に追加チェックなし

    # クレーンありの場合
    if data.supplementary.crane_available:
        pass  # クレーン費が見積に計上される

    # キュービクルありの場合
    if data.supplementary.cubicle_location:
        pass  # キュービクル改造工事が計上される


def _check_value_ranges(data: SurveyData, result: ValidationResult):
    """数値の妥当性チェック"""
    # モジュール出力の妥当性（200W〜800W程度）
    if data.equipment.module_output_w > 0:
        if data.equipment.module_output_w < 200 or data.equipment.module_output_w > 800:
            result.warnings.append(
                f"モジュール定格出力 {data.equipment.module_output_w}W は通常の範囲外です"
            )

    # パネル枚数の妥当性
    if data.equipment.planned_panels > 0:
        if data.equipment.planned_panels > 2000:
            result.warnings.append(
                f"設置予定枚数 {data.equipment.planned_panels}枚 は非常に多いです。確認してください"
            )

    # 離隔距離の妥当性
    if data.high_voltage.separation_ns_mm > 0:
        if data.high_voltage.separation_ns_mm < 500:
            result.warnings.append(
                f"離隔距離（南北）{data.high_voltage.separation_ns_mm}mm は短すぎる可能性があります"
            )
