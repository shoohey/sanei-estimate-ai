"""抽出データの整合性チェック

拡張ポイント:
- 業務ルールベースの詳細バリデーション（接地/Tr/単線結線図/日付/郵便番号）
- 自動修正機能（auto_fixes）
- 読み取り信頼度（field_confidences）の活用
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Callable, Optional

from models.survey_data import (
    SurveyData,
    CInstallation,
    ConfidenceLevel,
    GroundType,
)


# =====================================================================
# AutoFix: 自動修正提案
# =====================================================================
class AutoFix:
    """自動修正の1項目

    description: 画面に表示する説明文
    apply_fn: クリック時に実行するコールバック（SurveyDataを受け取り、in-placeで更新）
    category: 軽い分類（info/safety/number）
    """

    def __init__(self, description: str, apply_fn: Callable[[SurveyData], None], category: str = "info"):
        self.description = description
        self.apply_fn = apply_fn
        self.category = category

    def apply(self, data: SurveyData) -> None:
        self.apply_fn(data)


class ValidationResult:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.feedback: list[str] = []  # 現調シートへのフィードバック（改善提案）
        # 新規: 自動修正提案
        self.auto_fixes: list[AutoFix] = []
        # 新規: 低信頼度フィールドのリスト（ドット区切り: "project.project_name" など）
        self.low_confidence_fields: list[str] = []

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    @property
    def has_auto_fixes(self) -> bool:
        return len(self.auto_fixes) > 0


# =====================================================================
# メインエントリ
# =====================================================================
def validate_survey_data(data: SurveyData) -> ValidationResult:
    """現調データの整合性をチェック

    Args:
        data: 抽出された現調データ

    Returns:
        ValidationResult: 検証結果（errors/warnings/feedback/auto_fixes/low_confidence_fields）
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

    # 5. 業務ルール（接地/Tr/単線結線図/日付）
    _check_business_rules(data, result)

    # 6. 郵便番号の形式チェック
    _check_postal_code(data, result)

    # 7. 信頼度（AI読み取り）を活用した警告
    _check_confidence_levels(data, result)

    # 8. 全角数字 → 半角数字など、一般的なクリーニング fix を追加
    _suggest_general_cleanups(data, result)

    return result


# =====================================================================
# 1. 必須項目
# =====================================================================
def _check_required_fields(data: SurveyData, result: ValidationResult):
    """必須項目の欠損チェック（親切なメッセージに改善）"""
    if not data.project.project_name:
        result.errors.append(
            "❌ 案件名を記入してください（例: テックランド掛川店）"
        )
        result.feedback.append("案件名は見積書の工事名として必須です。現調シート冒頭の「案件名」欄を確認してください。")

    if not data.project.address:
        result.errors.append(
            "❌ 所在地を記入してください（例: 静岡県掛川市細田231-1）"
        )
        result.feedback.append("所在地は見積書の工事場所として必須です。現調シート冒頭の「所在地」欄を確認してください。")

    if not data.equipment.module_maker:
        result.warnings.append(
            "⚠️ モジュールメーカーが未入力です（例: LONGI, シャープ, 京セラ など）"
        )

    if not data.equipment.module_model:
        result.warnings.append(
            "⚠️ モジュール型式が未入力です（例: LR7-72HVH-660M など）"
        )

    if data.equipment.module_output_w == 0:
        result.errors.append(
            "❌ モジュール定格出力 (W/枚) が未入力です。現調シートの「計画設備」欄を確認してください。典型値: 400〜700W"
        )
        result.feedback.append("モジュール定格出力はPV容量の計算に必須です。")

    if data.equipment.planned_panels == 0:
        result.errors.append(
            "❌ 設置予定枚数が未入力です。現調シートの「計画設備」欄を確認してください。"
        )
        result.feedback.append("設置予定枚数はPV容量の計算と見積の数量算出に必須です。")

    if data.equipment.pv_capacity_kw == 0 and data.equipment.module_output_w > 0 and data.equipment.planned_panels > 0:
        # 自動計算で補完できるケース → 自動修正提案
        calc_kw = data.equipment.planned_panels * data.equipment.module_output_w / 1000
        result.warnings.append(
            f"⚠️ 想定PV容量が未入力です。枚数×定格出力で自動計算できます（{calc_kw:.2f}kW）"
        )
        result.auto_fixes.append(AutoFix(
            description=f"想定PV容量を自動計算値 {calc_kw:.2f}kW に設定",
            apply_fn=lambda d, v=calc_kw: setattr(d.equipment, "pv_capacity_kw", round(v, 2)),
            category="number",
        ))
    elif data.equipment.pv_capacity_kw == 0:
        result.errors.append("❌ 想定PV容量が未入力です。モジュール定格出力と設置予定枚数を入力してください。")

    if not data.project.survey_date:
        result.warnings.append("⚠️ 調査日が未入力です（例: 2025/10/01）")

    if not data.project.surveyor:
        result.warnings.append("⚠️ 調査者が未入力です")


# =====================================================================
# 2. PV容量の整合性
# =====================================================================
def _check_pv_capacity(data: SurveyData, result: ValidationResult):
    """容量計算：枚数 × 定格出力 ÷ 1000 = PV容量(kW) の一致確認"""
    if data.equipment.module_output_w > 0 and data.equipment.planned_panels > 0:
        calculated = data.equipment.planned_panels * data.equipment.module_output_w / 1000
        actual = data.equipment.pv_capacity_kw

        if actual > 0 and abs(calculated - actual) > 0.1:
            # 既に _check_required_fields で 0 の場合の auto_fix は出しているので、
            # ここでは「記載値と計算値がズレている」ケースだけを扱う
            result.warnings.append(
                f"⚠️ PV容量の計算が不一致: {data.equipment.planned_panels}枚 × "
                f"{data.equipment.module_output_w:.0f}W ÷ 1000 = {calculated:.2f}kW "
                f"（入力値: {actual:.2f}kW）"
            )
            # 自動修正: 計算値に合わせる
            result.auto_fixes.append(AutoFix(
                description=f"PV容量を計算値 {calculated:.2f}kW に補正（現在: {actual:.2f}kW）",
                apply_fn=lambda d, v=calculated: setattr(d.equipment, "pv_capacity_kw", round(v, 2)),
                category="number",
            ))


# =====================================================================
# 3. 条件分岐の論理チェック
# =====================================================================
def _check_conditional_logic(data: SurveyData, result: ValidationResult):
    """条件分岐の論理チェック"""
    # PCS設置スペースありの場合→場所指定必須
    if data.high_voltage.pcs_space and not data.high_voltage.pcs_location:
        result.warnings.append(
            "⚠️ PCS設置スペース「あり」ですが、設置場所（屋内/屋外）が未指定です"
        )
        result.feedback.append("PCS設置場所は屋内/屋外を選択してください。屋外の場合は防水ボックス代が計上されます。")


# =====================================================================
# 4. 数値の妥当性チェック
# =====================================================================
def _check_value_ranges(data: SurveyData, result: ValidationResult):
    """数値の妥当性チェック"""
    # モジュール出力の妥当性（200W〜800W程度）
    if data.equipment.module_output_w > 0:
        if data.equipment.module_output_w < 200 or data.equipment.module_output_w > 800:
            result.warnings.append(
                f"⚠️ モジュール定格出力 {data.equipment.module_output_w:.0f}W は通常の範囲外です（200〜800W が一般的）"
            )

    # パネル枚数の妥当性
    if data.equipment.planned_panels > 0:
        if data.equipment.planned_panels > 2000:
            result.warnings.append(
                f"⚠️ 設置予定枚数 {data.equipment.planned_panels}枚 は非常に多いです。桁の入力ミスがないかご確認ください。"
            )

    # 離隔距離の妥当性
    if data.high_voltage.separation_ns_mm > 0:
        if data.high_voltage.separation_ns_mm < 500:
            result.warnings.append(
                f"⚠️ 離隔距離（南北）{data.high_voltage.separation_ns_mm:.0f}mm は短すぎる可能性があります（500mm以上推奨）"
            )
    if data.high_voltage.separation_ew_mm > 0:
        if data.high_voltage.separation_ew_mm < 500:
            result.warnings.append(
                f"⚠️ 離隔距離（東西）{data.high_voltage.separation_ew_mm:.0f}mm は短すぎる可能性があります（500mm以上推奨）"
            )


# =====================================================================
# 5. 業務ルール（新規追加）
# =====================================================================
def _check_business_rules(data: SurveyData, result: ValidationResult):
    """業務ルール由来の整合性チェック"""
    hv = data.high_voltage

    # 5-1. 接地種類未設定 + C種別設置可否=可 → 整合性警告
    # 接地種類はデフォルトで GroundType.A が入るが、念のため「値がない」状況も吸収
    ground_value = getattr(hv.ground_type, "value", None)
    if not ground_value and hv.c_installation == CInstallation.POSSIBLE:
        result.warnings.append(
            "⚠️ 接地種類が未設定ですが、C種別設置可否が「可」になっています。整合性を確認してください。"
        )
        result.feedback.append("C種別設置を「可」にする場合は、接地種類（A/C/D）を記入してください。")

    # 5-2. Tr容量「不足」の場合 → 追加工事の可能性
    if hv.tr_capacity and "不足" in hv.tr_capacity:
        result.warnings.append(
            "⚠️ Tr容量が「不足」と記録されています。容量増設などの追加工事が必要な可能性があります。"
        )
        result.feedback.append("Tr容量不足の場合、変圧器の容量増設または設備の見直しが必要です。見積に追加工事を含めるか確認してください。")

    # 5-3. 単線結線図なし → 設計確認が必要
    if not hv.single_line_diagram:
        result.warnings.append(
            "⚠️ 単線結線図がありません。既存設備の配線状況が不明なため、設計時の再確認が必要です。"
        )
        result.feedback.append("単線結線図は高圧設備の設計に必須です。次回現調時に取得してください。")

    # 5-4. 調査日の妥当性チェック（未来/古すぎ）
    parsed_survey_date = _parse_japanese_date(data.project.survey_date)
    if parsed_survey_date is not None:
        today = date.today()
        # 未来日付
        if parsed_survey_date > today:
            result.warnings.append(
                f"⚠️ 調査日 ({parsed_survey_date.isoformat()}) が未来の日付になっています。入力ミスの可能性があります。"
            )
        else:
            # 6ヶ月以上前
            delta_days = (today - parsed_survey_date).days
            if delta_days >= 180:
                months = delta_days // 30
                result.warnings.append(
                    f"⚠️ 現調から時間が経っています（約{months}ヶ月前）。現場状況に変化がないか再確認をおすすめします。"
                )


# =====================================================================
# 6. 郵便番号の形式チェック
# =====================================================================
_POSTAL_RE_STRICT = re.compile(r"^\d{3}-\d{4}$")
_POSTAL_RE_LOOSE = re.compile(r"^\d{7}$")  # ハイフン抜けの数字のみ


def _check_postal_code(data: SurveyData, result: ValidationResult):
    """郵便番号の形式（XXX-XXXX）チェック + 自動修正提案"""
    zip_code = (data.project.postal_code or "").strip()
    if not zip_code:
        # 郵便番号は任意項目扱いなので警告のみ
        return

    # 既に正しい場合は何もしない
    if _POSTAL_RE_STRICT.match(zip_code):
        return

    # 全角数字/記号を半角に変換したうえで評価
    normalized = _to_half_width(zip_code).replace("〒", "").replace(" ", "").strip()

    if _POSTAL_RE_STRICT.match(normalized):
        # ハイフンは正しいが全角だった場合など
        result.warnings.append(
            f"⚠️ 郵便番号 '{zip_code}' は全角文字を含んでいます。半角に自動修正できます。"
        )
        result.auto_fixes.append(AutoFix(
            description=f"郵便番号を半角形式 {normalized} に補正",
            apply_fn=lambda d, v=normalized: setattr(d.project, "postal_code", v),
            category="number",
        ))
        return

    if _POSTAL_RE_LOOSE.match(normalized):
        fixed = f"{normalized[:3]}-{normalized[3:]}"
        result.warnings.append(
            f"⚠️ 郵便番号 '{zip_code}' の形式が正しくありません。XXX-XXXX形式に自動修正できます。"
        )
        result.auto_fixes.append(AutoFix(
            description=f"郵便番号を {fixed} 形式に補正",
            apply_fn=lambda d, v=fixed: setattr(d.project, "postal_code", v),
            category="number",
        ))
        return

    # 上記いずれにも当てはまらない場合は単純な警告
    result.warnings.append(
        f"⚠️ 郵便番号 '{zip_code}' の形式が正しくありません。XXX-XXXX形式で記入してください（例: 436-0048）"
    )


# =====================================================================
# 7. 信頼度（AI読み取り）を活用した警告
# =====================================================================
# 重要度の高いフィールドを列挙（見積の金額計算に直結するもの）
_IMPORTANT_FIELDS = {
    "project.project_name",
    "project.address",
    "equipment.module_maker",
    "equipment.module_model",
    "equipment.module_output_w",
    "equipment.planned_panels",
    "equipment.pv_capacity_kw",
}


def _check_confidence_levels(data: SurveyData, result: ValidationResult):
    """field_confidences を参照して低信頼度フィールドを警告する"""
    if not data.field_confidences:
        return

    low_fields: list[str] = []
    medium_fields: list[str] = []
    for field_path, conf in data.field_confidences.items():
        # Pydanticモデルにセットされる際は Enum、手動セットは文字列の可能性がある
        conf_value = conf.value if hasattr(conf, "value") else conf
        if conf_value == ConfidenceLevel.LOW.value:
            low_fields.append(field_path)
        elif conf_value == ConfidenceLevel.MEDIUM.value:
            medium_fields.append(field_path)

    result.low_confidence_fields = low_fields

    if not low_fields:
        return

    # 重要なフィールドに低信頼度が含まれる場合は強めの警告
    important_low = [f for f in low_fields if f in _IMPORTANT_FIELDS]
    if important_low:
        result.warnings.append(
            f"⚠️ 重要項目（{len(important_low)}件）のAI読み取り信頼度が低いです。"
            "金額計算に影響するため、必ず目視で確認してください。"
        )
    elif len(low_fields) >= 3:
        result.warnings.append(
            f"⚠️ AI読み取り結果に不安定な箇所が{len(low_fields)}件あります。内容を確認してください。"
        )


# =====================================================================
# 8. 一般的なクリーニング（全角→半角など）
# =====================================================================
def _suggest_general_cleanups(data: SurveyData, result: ValidationResult):
    """全角数字混入などの一般的なクリーニング提案"""
    # プロジェクト名/所在地に全角英数が混入していても見積の金額には影響しないが、
    # 文字列フィールドで明らかに数値として意図されているものだけ対象にする。
    # 今回は電柱番号と調査日を対象にする。
    pole = data.supplementary.pole_number or ""
    if pole and _contains_full_width_digit(pole):
        normalized = _to_half_width(pole)
        if normalized != pole:
            result.auto_fixes.append(AutoFix(
                description=f"電柱番号の全角数字を半角に補正 ({pole} → {normalized})",
                apply_fn=lambda d, v=normalized: setattr(d.supplementary, "pole_number", v),
                category="number",
            ))

    survey_date = data.project.survey_date or ""
    if survey_date and _contains_full_width_digit(survey_date):
        normalized = _to_half_width(survey_date)
        if normalized != survey_date:
            result.auto_fixes.append(AutoFix(
                description=f"調査日の全角数字を半角に補正 ({survey_date} → {normalized})",
                apply_fn=lambda d, v=normalized: setattr(d.project, "survey_date", v),
                category="number",
            ))


# =====================================================================
# ヘルパー
# =====================================================================
_FULL_WIDTH_DIGITS = "0123456789"
_HALF_WIDTH_DIGITS = "0123456789"
_FULL_TO_HALF_TABLE = str.maketrans({
    **{f: h for f, h in zip(_FULL_WIDTH_DIGITS, _HALF_WIDTH_DIGITS)},
    "ー": "-",
    "−": "-",
    "ー": "-",
    "-": "-",  # 全角ハイフン
})


def _to_half_width(s: str) -> str:
    """全角数字/ハイフンを半角に変換"""
    return s.translate(_FULL_TO_HALF_TABLE)


def _contains_full_width_digit(s: str) -> bool:
    return any(ch in _FULL_WIDTH_DIGITS for ch in s)


def _parse_japanese_date(s: str) -> Optional[date]:
    """日本語の日付フォーマットを柔軟にパース

    対応: 2025/10/01, 2025-10-01, 2025年10月1日
    """
    if not s:
        return None
    s = _to_half_width(s).strip()
    # 「2025年10月1日」形式
    m = re.match(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None
