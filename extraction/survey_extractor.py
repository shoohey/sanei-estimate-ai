"""Claude Vision APIで現調シートの手書きOCR（複数文書タイプ対応）"""
import json
import re
import time
import logging
import anthropic
from models.survey_data import (
    SurveyData, ProjectInfo, PlannedEquipment, HighVoltageChecklist,
    SupplementarySheet, FinalConfirmation, DesignStatus, GroundType,
    LocationType, BTPlacement, CInstallation, ConfidenceLevel,
)
from extraction.pdf_reader import pdf_to_images
from config import get_api_key, CLAUDE_MODEL

logger = logging.getLogger(__name__)

# APIリトライ設定
MAX_RETRIES = 3
RETRY_DELAY_SEC = 2  # 初回待機秒（指数バックオフ）

# ページ数上限（多すぎるとAPI制限に引っかかる）
MAX_TOTAL_PAGES = 20

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

MULTI_DOC_EXTRACTION_PROMPT = """あなたは太陽光発電設備の設計・施工に関する書類を読み取る専門家です。

以下の画像には、太陽光発電設備の設計・施工に関連する複数種類の書類が含まれている場合があります：
- 現調シート（現地調査シート）：手書きの調査記録
- 配管図：電気配管の経路・仕様を示す図面
- 単線結線図：電気設備の接続関係を示す図面
- その他の設計図面・資料

これらの書類すべてから読み取れる情報を統合して、以下のJSON形式で返してください。
現調シートがある場合はそこから主要な情報を読み取ってください。
配管図や単線結線図からは、案件名・所在地などの基本情報や、
電気設備の仕様（VT/CT有無、接地種類、PCS仕様等）を読み取ってください。

丸で囲まれた選択肢は、囲まれている方の値を選択してください。
チェックマーク（✓）が付いている項目は確認済みとしてください。
読み取れない文字や不確実な箇所には confidence を "low" にしてください。

必ず以下のJSON形式のみを返してください。説明文は不要です。
情報が読み取れない項目は空文字列・null・0・falseを入れてください。

```json
{
  "project": {
    "project_name": "案件名",
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
    "crane_available": false,
    "scaffold_location": "",
    "scaffold_needed": false,
    "pole_number": "電柱番号",
    "pole_type": "",
    "wiring_route": "確定 / 未確定",
    "cubicle_location": false,
    "bt_location": "",
    "meter_photo": "",
    "handwritten_notes": "図面から読み取った備考情報"
  },
  "confirmation": {
    "surveyor_name": "",
    "surveyor_date": "",
    "design_reviewer": "",
    "design_review_date": "",
    "works_reviewer": "",
    "works_review_date": "",
    "notes": ""
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
- 図面のタイトル欄から案件名・所在地・日付等を読み取ってください
- 単線結線図からはVT/CT有無、接地種類、変圧器容量等の電気設備情報を読み取ってください
- 配管図からは配管ルート・PCS設置場所等の施工情報を読み取ってください
- 数値は正確に読み取ってください
- 図面から読み取れない項目は空文字列・null・0・falseにしてください
- extraction_warningsに「配管図から読み取り」「単線結線図から読み取り」など、情報源を記載してください
"""


def extract_survey_data(pdf_path: str) -> SurveyData:
    """現調シートPDFからデータを抽出

    Args:
        pdf_path: 現調シートPDFのパス

    Returns:
        SurveyData: 抽出された現調データ
    """
    return extract_survey_data_multi([pdf_path])


def extract_survey_data_multi(pdf_paths: list[str]) -> SurveyData:
    """複数PDFからデータを統合抽出

    現調シート・配管図・単線結線図など複数種類の文書に対応。
    リトライ機構付き。

    Args:
        pdf_paths: PDFファイルパスのリスト

    Returns:
        SurveyData: 抽出された現調データ

    Raises:
        RuntimeError: 全リトライ失敗時
    """
    # 全PDFを画像に変換
    all_pages = []
    for pdf_path in pdf_paths:
        try:
            pages = pdf_to_images(pdf_path, dpi=200)
            all_pages.extend(pages)
        except Exception as e:
            logger.error(f"PDF画像変換エラー: {pdf_path}: {e}")
            raise RuntimeError(
                f"PDFファイルを画像に変換できませんでした。"
                f"ファイルが破損していないか確認してください: {e}"
            ) from e

    if not all_pages:
        raise RuntimeError("PDFからページを読み取れませんでした。ファイルが空でないか確認してください。")

    # ページ数上限チェック
    if len(all_pages) > MAX_TOTAL_PAGES:
        logger.warning(f"ページ数が上限を超えています（{len(all_pages)}ページ）。先頭{MAX_TOTAL_PAGES}ページのみ処理します。")
        all_pages = all_pages[:MAX_TOTAL_PAGES]

    # Claude Vision APIで読み取り（リトライ付き）
    content = []
    for page in all_pages:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": page.get("media_type", "image/png"),
                "data": page["image_base64"],
            }
        })

    # 常に複数文書対応プロンプトを使用（配管図・単線結線図にも対応するため）
    prompt = MULTI_DOC_EXTRACTION_PROMPT

    content.append({
        "type": "text",
        "text": prompt,
    })

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw_data = _call_claude_api(content, attempt)
            survey = _parse_raw_data(raw_data)
            return survey
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            logger.warning(f"読み取り試行 {attempt}/{MAX_RETRIES} - JSON解析エラー: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC * attempt)
        except anthropic.APIError as e:
            last_error = e
            logger.warning(f"読み取り試行 {attempt}/{MAX_RETRIES} - APIエラー: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC * attempt)
        except Exception as e:
            last_error = e
            logger.warning(f"読み取り試行 {attempt}/{MAX_RETRIES} - エラー: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC * attempt)

    raise RuntimeError(
        f"AI読み取りに{MAX_RETRIES}回失敗しました。"
        f"PDFの内容が複雑すぎるか、一時的なAPI障害の可能性があります。"
        f"しばらく待ってから再度お試しください。\n詳細: {last_error}"
    )


def _call_claude_api(content: list[dict], attempt: int) -> dict:
    """Claude Vision APIを呼び出してJSONレスポンスを返す"""
    client = anthropic.Anthropic(api_key=get_api_key())

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        messages=[{
            "role": "user",
            "content": content,
        }],
    )

    # レスポンスからJSONを抽出
    response_text = response.content[0].text
    logger.info(f"API応答（試行{attempt}）: {response_text[:200]}...")
    json_str = _extract_json(response_text)
    raw_data = json.loads(json_str)
    return raw_data


def _extract_json(text: str) -> str:
    """レスポンステキストからJSONを抽出（複数パターン対応）"""
    # 1. ```json ... ``` ブロックを探す
    json_block = re.search(r"```json\s*\n?(.*?)```", text, re.DOTALL)
    if json_block:
        return json_block.group(1).strip()

    # 2. ``` ... ``` ブロック（言語指定なし）
    code_block = re.search(r"```\s*\n?(.*?)```", text, re.DOTALL)
    if code_block:
        candidate = code_block.group(1).strip()
        if candidate.startswith("{"):
            return candidate

    # 3. テキスト中の最初の{...}ブロックを抽出
    text = text.strip()
    brace_start = text.find("{")
    if brace_start >= 0:
        # 対応する閉じ括弧を探す
        depth = 0
        for i in range(brace_start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[brace_start:i + 1]

    raise ValueError(f"JSONが見つかりません。APIの応答形式が不正です: {text[:300]}")


def _sanitize_dict(d: dict, float_keys: list[str] = None, int_keys: list[str] = None,
                   bool_keys: list[str] = None, str_keys: list[str] = None) -> dict:
    """Claude APIレスポンスのnull値をデフォルト値に変換"""
    for k in (float_keys or []):
        if k in d and d[k] is None:
            d[k] = 0.0
    for k in (int_keys or []):
        if k in d and d[k] is None:
            d[k] = 0
    for k in (bool_keys or []):
        if k in d and d[k] is None:
            d[k] = False
    for k in (str_keys or []):
        if k in d and d[k] is None:
            d[k] = ""
    return d


def _safe_float(val) -> float:
    """値をfloatに安全変換"""
    if val is None:
        return 0.0
    try:
        if isinstance(val, str):
            # "660W" -> 660, "190.08kW" -> 190.08 のような文字列を処理
            cleaned = re.sub(r"[^\d.\-]", "", val)
            return float(cleaned) if cleaned else 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val) -> int:
    """値をintに安全変換"""
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            cleaned = re.sub(r"[^\d\-]", "", val)
            return int(cleaned) if cleaned else 0
        return int(val)
    except (ValueError, TypeError):
        return 0


def _safe_bool(val) -> bool:
    """値をboolに安全変換"""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "あり", "有", "○")
    return bool(val)


def _parse_raw_data(raw: dict) -> SurveyData:
    """生データをSurveyDataモデルに変換（堅牢なエラーハンドリング付き）"""
    warnings = list(raw.get("extraction_warnings", []) or [])

    # --- project ---
    proj_raw = raw.get("project", {}) or {}
    _sanitize_dict(proj_raw, str_keys=[
        "project_name", "address", "postal_code", "survey_date", "weather", "surveyor"])
    try:
        project = ProjectInfo(**proj_raw)
    except Exception as e:
        logger.warning(f"ProjectInfo変換エラー: {e}")
        project = ProjectInfo()
        warnings.append(f"案件情報の一部を読み取れませんでした: {e}")

    # --- equipment ---
    eq_raw = raw.get("equipment", {}) or {}
    _sanitize_dict(eq_raw, str_keys=["module_maker", "module_model"])
    # 数値フィールドは安全変換
    eq_raw["module_output_w"] = _safe_float(eq_raw.get("module_output_w"))
    eq_raw["pv_capacity_kw"] = _safe_float(eq_raw.get("pv_capacity_kw"))
    eq_raw["planned_panels"] = _safe_int(eq_raw.get("planned_panels"))
    design_status_map = {"確定": DesignStatus.CONFIRMED, "仮": DesignStatus.TENTATIVE, "未定": DesignStatus.UNDECIDED}
    if "design_status" in eq_raw:
        eq_raw["design_status"] = design_status_map.get(eq_raw.get("design_status"), DesignStatus.UNDECIDED)
    try:
        equipment = PlannedEquipment(**eq_raw)
    except Exception as e:
        logger.warning(f"PlannedEquipment変換エラー: {e}")
        equipment = PlannedEquipment()
        warnings.append(f"設備情報の一部を読み取れませんでした: {e}")

    # --- high_voltage ---
    hv_raw = raw.get("high_voltage", {}) or {}
    # bool値を安全変換
    for bk in ["building_drawing", "single_line_diagram", "vt_available",
                "ct_available", "relay_space", "pcs_space", "pre_use_self_check"]:
        if bk in hv_raw:
            hv_raw[bk] = _safe_bool(hv_raw[bk])
    # float値を安全変換
    for fk in ["separation_ns_mm", "separation_ew_mm"]:
        if fk in hv_raw:
            hv_raw[fk] = _safe_float(hv_raw[fk])
    _sanitize_dict(hv_raw, str_keys=["single_line_diagram_note", "c_installation_note",
                                      "bt_backup_capacity", "tr_capacity"])
    if "ground_type" in hv_raw and hv_raw["ground_type"]:
        gt_map = {"A": GroundType.A, "C": GroundType.C, "D": GroundType.D}
        mapped = gt_map.get(str(hv_raw["ground_type"]).strip().upper())
        if mapped:
            hv_raw["ground_type"] = mapped
        else:
            del hv_raw["ground_type"]
    elif "ground_type" in hv_raw:
        del hv_raw["ground_type"]  # null/空の場合はデフォルトに任せる
    if "c_installation" in hv_raw and hv_raw["c_installation"]:
        ci_map = {"可": CInstallation.POSSIBLE, "不可": CInstallation.IMPOSSIBLE}
        mapped = ci_map.get(hv_raw["c_installation"])
        if mapped:
            hv_raw["c_installation"] = mapped
        else:
            del hv_raw["c_installation"]
    elif "c_installation" in hv_raw:
        del hv_raw["c_installation"]
    if "pcs_location" in hv_raw and hv_raw["pcs_location"]:
        loc_map = {"屋内": LocationType.INDOOR, "屋外": LocationType.OUTDOOR}
        hv_raw["pcs_location"] = loc_map.get(hv_raw["pcs_location"])
    elif "pcs_location" in hv_raw:
        hv_raw["pcs_location"] = None
    if "bt_space" in hv_raw and hv_raw["bt_space"]:
        bt_map = {"屋内": BTPlacement.INDOOR, "屋外": BTPlacement.OUTDOOR, "設置なし": BTPlacement.NONE}
        hv_raw["bt_space"] = bt_map.get(hv_raw["bt_space"])
    elif "bt_space" in hv_raw:
        hv_raw["bt_space"] = None
    try:
        high_voltage = HighVoltageChecklist(**hv_raw)
    except Exception as e:
        logger.warning(f"HighVoltageChecklist変換エラー: {e}")
        high_voltage = HighVoltageChecklist()
        warnings.append(f"高圧チェック項目の一部を読み取れませんでした: {e}")

    # --- supplementary ---
    sup_raw = raw.get("supplementary", {}) or {}
    _sanitize_dict(sup_raw,
                   str_keys=["scaffold_location", "pole_number", "pole_type",
                             "wiring_route", "bt_location", "meter_photo", "handwritten_notes"],
                   bool_keys=["crane_available", "scaffold_needed", "cubicle_location"])
    # bool値を安全変換
    for bk in ["crane_available", "scaffold_needed", "cubicle_location"]:
        if bk in sup_raw:
            sup_raw[bk] = _safe_bool(sup_raw[bk])
    try:
        supplementary = SupplementarySheet(**sup_raw)
    except Exception as e:
        logger.warning(f"SupplementarySheet変換エラー: {e}")
        supplementary = SupplementarySheet()
        warnings.append(f"補足情報の一部を読み取れませんでした: {e}")

    # --- confirmation ---
    conf_raw = raw.get("confirmation", {}) or {}
    _sanitize_dict(conf_raw,
                   str_keys=["surveyor_name", "surveyor_date", "design_reviewer",
                             "design_review_date", "works_reviewer", "works_review_date", "notes"])
    try:
        confirmation = FinalConfirmation(**conf_raw)
    except Exception as e:
        logger.warning(f"FinalConfirmation変換エラー: {e}")
        confirmation = FinalConfirmation()
        warnings.append(f"確認情報の一部を読み取れませんでした: {e}")

    # --- confidences ---
    confidences_raw = raw.get("field_confidences", {}) or {}
    confidences = {}
    conf_map = {"high": ConfidenceLevel.HIGH, "medium": ConfidenceLevel.MEDIUM, "low": ConfidenceLevel.LOW}
    for k, v in confidences_raw.items():
        confidences[k] = conf_map.get(v, ConfidenceLevel.HIGH)

    return SurveyData(
        project=project,
        equipment=equipment,
        high_voltage=high_voltage,
        supplementary=supplementary,
        confirmation=confirmation,
        extraction_warnings=warnings,
        field_confidences=confidences,
    )
