"""Claude Vision APIで現調シートの手書きOCR"""
import json
import anthropic
from models.survey_data import (
    SurveyData, ProjectInfo, PlannedEquipment, HighVoltageChecklist,
    SupplementarySheet, FinalConfirmation, DesignStatus, GroundType,
    LocationType, BTPlacement, CInstallation, ConfidenceLevel,
)
from extraction.pdf_reader import pdf_to_images
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

SURVEY_EXTRACTION_PROMPT = """あなたは太陽光発電設備の現地調査シート（現調シート）を読み取る専門家です。
手書きの日本語を高精度で読み取ってください。

以下のJSON形式で、現調シートから読み取った全情報を返してください。
丸で囲まれた選択肢は、囲まれている方の値を選択してください。
チェックマーク（✓）が付いている項目は確認済みとしてください。
読み取れない文字や不確実な箇所には confidence を "low" にしてください。

必ず以下のJSON形式のみを返してください。説明文は不要です。

```json
{
  "project": {
    "project_name": "案件名（手書き）",
    "address": "所在地（郵便番号含む）",
    "postal_code": "郵便番号",
    "survey_date": "調査日（YYYY/MM/DD）",
    "weather": "天気",
    "surveyor": "調査者名"
  },
  "equipment": {
    "module_maker": "モジュールメーカー",
    "module_model": "モジュール型式",
    "module_output_w": 660,
    "planned_panels": 288,
    "pv_capacity_kw": 190.08,
    "design_status": "確定 / 仮 / 未定"
  },
  "high_voltage": {
    "building_drawing": true,
    "single_line_diagram": true,
    "single_line_diagram_note": "備考があれば",
    "ground_type": "A / C / D",
    "c_installation": "可 / 不可",
    "c_installation_note": "備考",
    "vt_available": true,
    "ct_available": true,
    "relay_space": true,
    "pcs_space": true,
    "pcs_location": "屋内 / 屋外 / null",
    "bt_space": "屋内 / 屋外 / 設置なし / null",
    "bt_backup_capacity": "",
    "tr_capacity": "十分 / 不足",
    "pre_use_self_check": true,
    "separation_ns_mm": 3000,
    "separation_ew_mm": 3000
  },
  "supplementary": {
    "crane_available": true,
    "scaffold_location": "足場設置予定位置の記載",
    "scaffold_needed": false,
    "pole_number": "電柱番号",
    "pole_type": "A / C / D",
    "wiring_route": "確定 / 未確定",
    "cubicle_location": true,
    "bt_location": "BT設置位置",
    "meter_photo": "",
    "handwritten_notes": "手書き欄の内容"
  },
  "confirmation": {
    "surveyor_name": "調査者名",
    "surveyor_date": "日付",
    "design_reviewer": "設計確認者名",
    "design_review_date": "日付",
    "works_reviewer": "ワークス部確認者名",
    "works_review_date": "日付",
    "notes": "備考"
  },
  "field_confidences": {
    "フィールドパス": "high / medium / low"
  },
  "extraction_warnings": [
    "読み取り不確実な箇所の警告メッセージ"
  ]
}
```

重要な注意事項：
- 手書き文字は文脈から推測して正確に読み取ってください
- 丸で囲まれた選択肢（あり/なし、屋内/屋外等）を正確に識別してください
- 数値は正確に読み取ってください（660W、288枚、190.08kW等）
- 不明確な文字には confidence: "low" を付与してください
- 未記入の項目は空文字列またはnullにしてください
"""


def extract_survey_data(pdf_path: str) -> SurveyData:
    """現調シートPDFからデータを抽出

    Args:
        pdf_path: 現調シートPDFのパス

    Returns:
        SurveyData: 抽出された現調データ
    """
    # PDF→画像変換
    pages = pdf_to_images(pdf_path, dpi=200)

    # Claude Vision APIで読み取り
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 全ページを一度に送信
    content = []
    for page in pages:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": page.get("media_type", "image/png"),
                "data": page["image_base64"],
            }
        })

    content.append({
        "type": "text",
        "text": SURVEY_EXTRACTION_PROMPT,
    })

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": content,
        }],
    )

    # レスポンスからJSONを抽出
    response_text = response.content[0].text
    json_str = _extract_json(response_text)
    raw_data = json.loads(json_str)

    # Pydanticモデルに変換
    survey = _parse_raw_data(raw_data)
    return survey


def _extract_json(text: str) -> str:
    """レスポンステキストからJSONを抽出"""
    # ```json ... ``` ブロックを探す
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        return text[start:end].strip()
    elif "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        return text[start:end].strip()
    # JSONブロックがない場合、テキスト全体をJSONとして扱う
    text = text.strip()
    if text.startswith("{"):
        return text
    raise ValueError(f"JSONが見つかりません: {text[:200]}")


def _parse_raw_data(raw: dict) -> SurveyData:
    """生データをSurveyDataモデルに変換"""
    project = ProjectInfo(**raw.get("project", {}))

    # equipment
    eq_raw = raw.get("equipment", {})
    design_status_map = {"確定": DesignStatus.CONFIRMED, "仮": DesignStatus.TENTATIVE, "未定": DesignStatus.UNDECIDED}
    if "design_status" in eq_raw:
        eq_raw["design_status"] = design_status_map.get(eq_raw["design_status"], DesignStatus.UNDECIDED)
    equipment = PlannedEquipment(**eq_raw)

    # high_voltage
    hv_raw = raw.get("high_voltage", {})
    if "ground_type" in hv_raw:
        gt_map = {"A": GroundType.A, "C": GroundType.C, "D": GroundType.D}
        hv_raw["ground_type"] = gt_map.get(hv_raw["ground_type"], GroundType.A)
    if "c_installation" in hv_raw:
        ci_map = {"可": CInstallation.POSSIBLE, "不可": CInstallation.IMPOSSIBLE}
        hv_raw["c_installation"] = ci_map.get(hv_raw["c_installation"], CInstallation.POSSIBLE)
    if "pcs_location" in hv_raw and hv_raw["pcs_location"]:
        loc_map = {"屋内": LocationType.INDOOR, "屋外": LocationType.OUTDOOR}
        hv_raw["pcs_location"] = loc_map.get(hv_raw["pcs_location"])
    if "bt_space" in hv_raw and hv_raw["bt_space"]:
        bt_map = {"屋内": BTPlacement.INDOOR, "屋外": BTPlacement.OUTDOOR, "設置なし": BTPlacement.NONE}
        hv_raw["bt_space"] = bt_map.get(hv_raw["bt_space"])
    if "tr_capacity" in hv_raw:
        hv_raw["tr_capacity"] = hv_raw["tr_capacity"]  # そのまま文字列
    high_voltage = HighVoltageChecklist(**hv_raw)

    # supplementary
    supplementary = SupplementarySheet(**raw.get("supplementary", {}))

    # confirmation
    confirmation = FinalConfirmation(**raw.get("confirmation", {}))

    # confidences
    confidences_raw = raw.get("field_confidences", {})
    confidences = {}
    for k, v in confidences_raw.items():
        conf_map = {"high": ConfidenceLevel.HIGH, "medium": ConfidenceLevel.MEDIUM, "low": ConfidenceLevel.LOW}
        confidences[k] = conf_map.get(v, ConfidenceLevel.HIGH)

    warnings = raw.get("extraction_warnings", [])

    return SurveyData(
        project=project,
        equipment=equipment,
        high_voltage=high_voltage,
        supplementary=supplementary,
        confirmation=confirmation,
        extraction_warnings=warnings,
        field_confidences=confidences,
    )
