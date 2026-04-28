"""Claude Vision APIで現調シートの手書きOCR（複数文書タイプ対応・住宅/法人自動判別）"""
import base64
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
from extraction.prompts import (
    COMMERCIAL_EXTRACTION_PROMPT,
    RESIDENTIAL_EXTRACTION_PROMPT,
)
from extraction.image_preprocessor import auto_select_pipeline
from extraction.self_consistency import merge_extractions
from extraction.post_validators import validate_and_correct
from config import get_api_key, CLAUDE_MODEL

logger = logging.getLogger(__name__)

# APIリトライ設定
MAX_RETRIES = 3
RETRY_DELAY_SEC = 2  # 初回待機秒（指数バックオフ）

# ページ数上限（多すぎるとAPI制限に引っかかる）
MAX_TOTAL_PAGES = 20

# 自己一貫性パスのデフォルト設定（環境変数で切り替え可能）
import os
_SELF_CONSISTENCY_ENABLED = os.environ.get("SURVEY_SELF_CONSISTENCY", "0") == "1"
_SELF_CONSISTENCY_TEMPS = [0.0, 0.2, 0.3]

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
- 現調シート（現地調査シート）：手書きの調査記録（通常2ページ構成：1ページ目が主要チェック項目、2ページ目が別紙の補足チェック項目）
- 配管図：電気配管の経路・仕様を示す図面
- 単線結線図：電気設備の接続関係を示す図面
- その他の設計図面・資料

【現調シートの構造について】
- 1ページ目: 案件情報（案件名・所在地・調査日等）、計画設備（モジュール仕様・PV容量等）、高圧チェック項目（接地種類・VT/CT・離隔距離等）、最終確認欄
- 2ページ目（別紙）: クレーン有無、足場設置予定位置、電柱番号、1号柱位置、配管・配線ルート、キュービクル位置、BT設置位置、引込柱・メーター番号、手書きメモ欄

これらの書類すべてから読み取れる情報を統合して、以下のJSON形式で返してください。
現調シートがある場合はそこから主要な情報を読み取ってください。
配管図や単線結線図からは、案件名・所在地などの基本情報や、
電気設備の仕様（VT/CT有無、接地種類、PCS仕様等）を読み取ってください。

【手書き数字の読み取り注意事項】
- 「6」と「0」の区別: 6は上部が開いており丸みがある、0は完全に閉じた楕円形。迷ったら文脈（例: モジュール出力は600W台が一般的）を考慮すること。
- 「1」と「7」の区別: 7は上部に横線があり斜めに下がる、1は縦の直線。手書きの7は上に横棒がつく。
- 「3」と「8」の区別: 3は右側が開いている、8は上下とも閉じた形。3の上下の丸は右側が開放されている。
- 数値の桁数に注意: PV容量は通常50〜500kWの範囲、モジュール出力は300〜700Wの範囲、枚数は50〜1000枚の範囲が一般的。
- 全角数字（０〜９）が含まれる場合は半角数字に変換して読み取ってください。

【フィールドごとの典型値（読み取り時の参考）】
- module_output_w（モジュール出力W）: 400, 450, 500, 540, 550, 600, 660, 670, 700, 720 のいずれかが一般的。それ以外の値の場合は再度手書き文字を確認すること。
- planned_panels（設置予定枚数）: 通常50〜1000枚。100枚単位や288枚など特定の数値が多い。
- pv_capacity_kw（PV容量）: 通常 module_output_w × planned_panels / 1000 と一致する。例: 660W × 288枚 = 190.08kW
- module_maker（メーカー）: Canadian Solar, Longi, JA Solar, Jinko, Trina, Q CELLS, Sharp, Panasonic, 京セラ, 三菱 など
- postal_code: 必ず「XXX-XXXX」形式の7桁（例: 530-0001）。ハイフン無しの場合でも「XXX-XXXX」形式に整形して返してください。
- address: 都道府県から始まる完全な住所（例:「大阪府大阪市北区梅田1-2-3」）。可能な限り番地まで読み取ってください。
- separation_ns_mm / separation_ew_mm: 離隔距離。単位はmm。手書きで「3m」と書かれていたら3000を返してください。

【読み取り例 ビフォー/アフター】
例1 - 現調シート1ページ目（手書き）:
  生の手書き画像: 案件名欄「◯◯工業株式会社 太陽光発電設備」、所在地「〒530-0001 大阪府大阪市北区梅田1-2-3」、
  モジュール「Canadian Solar CS7L-MS 660W」、枚数「288枚」、PV容量「190.08kW」
  → 出力JSON:
    project.project_name: "◯◯工業株式会社 太陽光発電設備"
    project.address: "大阪府大阪市北区梅田1-2-3"
    project.postal_code: "530-0001"
    equipment.module_maker: "Canadian Solar"
    equipment.module_model: "CS7L-MS"
    equipment.module_output_w: 660
    equipment.planned_panels: 288
    equipment.pv_capacity_kw: 190.08

例2 - 接地種類が丸で囲まれている場合:
  「A種 C種 D種」と印字された選択肢のうち、「C種」が丸で囲まれている
  → high_voltage.ground_type: "C"

例3 - VT/CTが「あり/なし」のうち「あり」に丸が付いている:
  → high_voltage.vt_available: true
  → high_voltage.ct_available: true

【丸で囲まれた選択肢の認識】
- 完全に丸で囲まれていなくても、部分的に丸や弧が描かれている場合はその選択肢が選ばれていると判断してください。
- 半円、U字型の線、不完全な円でも、特定の選択肢を囲もうとした意図があればその選択肢を採用してください。
- チェックマーク（✓）が付いている項目は確認済みとしてください。

【接地種類（A/C/D）の丸識別 詳細ガイド】
現調シートの接地種類欄には通常「A種 C種 D種」と横並びで印字されています。手書きの丸で囲まれた文字を識別してください。
- 「A種」の丸: 最も左に位置する「A」の周囲にある丸。「A」の字形は上部が尖った三角形＋中央の横線。
- 「C種」の丸: 中央に位置する「C」の周囲にある丸。「C」の字形は右側が開いた半円。
- 「D種」の丸: 最も右に位置する「D」の周囲にある丸。「D」の字形は右側が閉じた形。
- 丸が複数ある（書き直しの跡がある）場合は、最も濃く・最後に描かれたと思われる丸を採用してください。
- 取り消し線が引かれている丸は無視してください。
- どれにも丸が無い、もしくは判別不可能な場合は空文字列を返して confidence を "low" にしてください。

【c_installation（C種別設置可否）について】
- 選択肢は「可」「不可」の2択。漢字の周囲に丸や丸印があるほうを選択してください。

【pcs_location / bt_space（場所の識別）について】
- 「屋内」「屋外」の2択。「屋」の字＋「内/外」の組み合わせを識別してください。
- bt_space は「屋内」「屋外」「設置なし」の3択。

【配管図・単線結線図からの情報抽出】
- 配管の延長距離（m）、ケーブルラックの長さ、接地電極の位置などが読み取れれば handwritten_notes に含めてください。
- 配管図に「配管延長 xx m」「ケーブルラック xx m」のような表記がある場合は数値を抽出して handwritten_notes に「配管延長: 45m」のような形で追加してください。
- 単線結線図の変圧器容量（例: 300kVA, 500kVA）は tr_capacity の備考として handwritten_notes に追加してください。

読み取れない文字や不確実な箇所には confidence を "low" にしてください。

必ず以下のJSON形式のみを返してください。説明文は不要です。
情報が読み取れない項目は空文字列・null・0・falseを入れてください。

```json
{
  "project": {
    "project_name": "案件名",
    "address": "都道府県から始まる完全な所在地",
    "postal_code": "XXX-XXXX形式の7桁郵便番号",
    "survey_date": "調査日（YYYY/MM/DD）",
    "weather": "天気",
    "surveyor": "調査者名（敬称なし）"
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
    "handwritten_notes": "図面から読み取った備考情報・配管延長・変圧器容量など"
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
- 配管図からは配管ルート・PCS設置場所・配管延長距離等の施工情報を読み取ってください
- 数値は正確に読み取ってください
- 図面から読み取れない項目は空文字列・null・0・falseにしてください
- extraction_warningsに「配管図から読み取り」「単線結線図から読み取り」など、情報源を記載してください
- JSON以外のテキスト（説明文・前置き・コメント）は一切含めないでください。純粋なJSONオブジェクトのみを返してください。
"""


def extract_survey_data(pdf_path: str) -> SurveyData:
    """現調シートPDFからデータを抽出

    Args:
        pdf_path: 現調シートPDFのパス

    Returns:
        SurveyData: 抽出された現調データ
    """
    return extract_survey_data_multi([pdf_path])


def extract_survey_data_multi(
    pdf_paths: list[str],
    category: str | None = None,
    use_image_enhancement: bool = True,
    use_self_consistency: bool | None = None,
) -> SurveyData:
    """複数PDFからデータを統合抽出（v2.2 高精度版）

    住宅/法人を自動判別し、それぞれ専用プロンプトで抽出。
    画像前処理（傾き補正・コントラスト強化）と後処理バリデーションで精度を最大化。

    Args:
        pdf_paths: PDFファイルパスのリスト
        category: 'commercial' / 'residential' / None（Noneなら自動判別）
        use_image_enhancement: 手書きOCR向け画像前処理を適用するか
        use_self_consistency: 自己一貫性パス（複数回サンプリング多数決）。
                              Noneの場合は環境変数 SURVEY_SELF_CONSISTENCY=1 で有効化

    Returns:
        SurveyData: 抽出された現調データ

    Raises:
        RuntimeError: 全リトライ失敗時
    """
    if use_self_consistency is None:
        use_self_consistency = _SELF_CONSISTENCY_ENABLED

    # --- ステップ1: PDF→画像変換 ---
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

    if len(all_pages) > MAX_TOTAL_PAGES:
        logger.warning(f"ページ数が上限を超えています（{len(all_pages)}ページ）。先頭{MAX_TOTAL_PAGES}ページのみ処理します。")
        all_pages = all_pages[:MAX_TOTAL_PAGES]

    # --- ステップ2: 手書きOCR向け画像前処理（オプション） ---
    if use_image_enhancement:
        for p in all_pages:
            try:
                enhanced_bytes, media_type = auto_select_pipeline(p["image_bytes"])
                p["image_bytes"] = enhanced_bytes
                p["media_type"] = media_type
                p["image_base64"] = base64.standard_b64encode(enhanced_bytes).decode("utf-8")
            except Exception as e:
                logger.warning(f"画像前処理失敗（元画像で続行）: {e}")

    # --- ステップ3: 文書カテゴリ判定（住宅/法人） ---
    if category is None:
        try:
            from extraction.document_classifier import classify_documents
            cls = classify_documents(pdf_paths)
            category = cls.get("category") or "commercial"
            logger.info(f"文書分類: {category} (confidence={cls.get('confidence')}, evidence={cls.get('evidence')})")
        except Exception as e:
            logger.warning(f"文書分類失敗: {e}。法人(commercial)として処理します。")
            category = "commercial"

    if category not in ("commercial", "residential"):
        category = "commercial"

    # --- ステップ4: プロンプト選択 ---
    prompt = COMMERCIAL_EXTRACTION_PROMPT if category == "commercial" else RESIDENTIAL_EXTRACTION_PROMPT

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
    content.append({
        "type": "text",
        "text": prompt,
    })

    # --- ステップ5: 抽出（自己一貫性パスの場合は複数回サンプリング） ---
    last_error = None
    last_error_kind = None  # JSONエラー / APIエラー / その他 を記録

    # 自己一貫性パス: 複数回サンプリングして多数決
    if use_self_consistency:
        try:
            results = []
            for temp in _SELF_CONSISTENCY_TEMPS:
                try:
                    r = _call_claude_api(content, attempt=1, temperature=temp)
                    results.append(r)
                except Exception as e:
                    logger.warning(f"self-consistency サンプル失敗 (temp={temp}): {e}")
            if results:
                merged, sc_confs = merge_extractions(results)
                logger.info(f"self-consistency: {len(results)}サンプルから多数決")
                # 後処理バリデーター適用
                merged, validator_warnings, validator_confs = validate_and_correct(merged)
                survey = _parse_raw_data(merged)
                if validator_warnings:
                    survey.extraction_warnings.extend(validator_warnings)
                # 信頼度を統合（self-consistency と validator の "low" を優先）
                for k, v in {**sc_confs, **validator_confs}.items():
                    cur = survey.field_confidences.get(k)
                    new_level = ConfidenceLevel(v) if isinstance(v, str) else v
                    if cur is None or new_level == ConfidenceLevel.LOW:
                        survey.field_confidences[k] = new_level
                return survey
        except Exception as e:
            logger.warning(f"self-consistency失敗、単一パスにフォールバック: {e}")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw_data = _call_claude_api(content, attempt)
            # 後処理バリデーター適用
            raw_data, validator_warnings, validator_confs = validate_and_correct(raw_data)
            survey = _parse_raw_data(raw_data)
            if validator_warnings:
                survey.extraction_warnings.extend(validator_warnings)
            # 信頼度マージ（"low" は上書き優先）
            for k, v in (validator_confs or {}).items():
                new_level = ConfidenceLevel(v) if isinstance(v, str) else v
                cur = survey.field_confidences.get(k)
                if cur is None or new_level == ConfidenceLevel.LOW:
                    survey.field_confidences[k] = new_level
            return survey
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            last_error_kind = "JSON"
            wait_sec = RETRY_DELAY_SEC * attempt
            logger.warning(
                f"読み取り試行 {attempt}/{MAX_RETRIES} - JSON解析エラー: {e}. "
                f"{wait_sec}秒待機してリトライします。"
            )
            if attempt < MAX_RETRIES:
                time.sleep(wait_sec)
        except anthropic.APIError as e:
            last_error = e
            last_error_kind = "API"
            wait_sec = RETRY_DELAY_SEC * attempt
            logger.warning(
                f"読み取り試行 {attempt}/{MAX_RETRIES} - APIエラー: {e}. "
                f"APIタイムアウトまたは一時障害の可能性があります。{wait_sec}秒待機してリトライします。"
            )
            if attempt < MAX_RETRIES:
                time.sleep(wait_sec)
        except Exception as e:
            last_error = e
            last_error_kind = "OTHER"
            wait_sec = RETRY_DELAY_SEC * attempt
            logger.warning(
                f"読み取り試行 {attempt}/{MAX_RETRIES} - 予期せぬエラー: {e}. "
                f"{wait_sec}秒待機してリトライします。"
            )
            if attempt < MAX_RETRIES:
                time.sleep(wait_sec)

    # 失敗原因別のヒントメッセージを生成
    hint = ""
    if last_error_kind == "JSON":
        hint = (
            "考えられる原因: AIが想定外のレスポンス形式を返しました。"
            "PDFの画質が低い、または手書き文字が極端に読み取りにくい可能性があります。"
            "より鮮明なPDFでお試しください。"
        )
    elif last_error_kind == "API":
        hint = (
            "考えられる原因: Claude APIの一時障害、レート制限、または認証エラーです。"
            "しばらく時間をおいてから再度お試しください。"
            "APIキーが正しく設定されているかも確認してください。"
        )
    else:
        hint = (
            "考えられる原因: PDFの構造が想定外、または画像変換に問題があります。"
            "PDFが破損していないか、ページ数が多すぎないかを確認してください。"
        )

    raise RuntimeError(
        f"AI読み取りに{MAX_RETRIES}回失敗しました。\n"
        f"{hint}\n"
        f"詳細エラー: {last_error}"
    )


def _call_claude_api(content: list[dict], attempt: int, temperature: float = 0.0) -> dict:
    """Claude Vision APIを呼び出してJSONレスポンスを返す

    改善点:
    - 既定 temperature=0 で決定論的な出力を得る（手書きOCRで結果の再現性を確保）
    - 自己一貫性パス時のみ temperature を上げて多様性を出す
    - assistant の最初のメッセージに "{" をプリフィルすることで、
      モデルが余計な前置き（「以下がJSONです:」など）を出さず、純粋なJSONのみを返すよう誘導する
    """
    client = anthropic.Anthropic(api_key=get_api_key())

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8192,
        temperature=temperature,
        messages=[
            {
                "role": "user",
                "content": content,
            },
            {
                # Prefill: assistant の応答を "{" から開始させることで
                # JSON以外の余計な文字列（前置き・マークダウン等）を排除する
                "role": "assistant",
                "content": "{",
            },
        ],
    )

    # レスポンスからJSONを抽出
    # Prefill の "{" はレスポンスには含まれないため、手動で先頭に付与する
    response_text = response.content[0].text
    if not response_text.lstrip().startswith("{"):
        response_text = "{" + response_text
    logger.info(f"API応答（試行{attempt}）: {response_text[:200]}...")
    json_str = _extract_json(response_text)
    raw_data = json.loads(json_str)
    return raw_data


def _extract_json(text: str) -> str:
    """レスポンステキストからJSONを抽出（複数パターン対応）+ よくある不正形式を自動修復

    修復する形式:
    - // コメント行 (単一行コメント)
    - /* ... */ コメントブロック
    - trailing comma（末尾カンマ）: {"a":1,} → {"a":1}
    - Python風のTrue/False/None → true/false/null
    - シングルクオート文字列 → ダブルクオート（他のクオートが混在していない場合のみ）
    """
    # 1. ```json ... ``` ブロックを探す
    json_block = re.search(r"```json\s*\n?(.*?)```", text, re.DOTALL)
    if json_block:
        return _sanitize_json_str(json_block.group(1).strip())

    # 2. ``` ... ``` ブロック（言語指定なし）
    code_block = re.search(r"```\s*\n?(.*?)```", text, re.DOTALL)
    if code_block:
        candidate = code_block.group(1).strip()
        if candidate.startswith("{"):
            return _sanitize_json_str(candidate)

    # 3. テキスト中の最初の{...}ブロックを抽出
    text = text.strip()
    brace_start = text.find("{")
    if brace_start >= 0:
        # 対応する閉じ括弧を探す（文字列リテラル内のブレースは無視）
        depth = 0
        in_string = False
        escape = False
        for i in range(brace_start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return _sanitize_json_str(text[brace_start:i + 1])

    raise ValueError(f"JSONが見つかりません。APIの応答形式が不正です: {text[:300]}")


def _sanitize_json_str(json_str: str) -> str:
    """JSON文字列のよくある不正形式を自動修復する。

    json.loads が受け入れない表現を、可能な範囲で標準JSONに変換する。
    文字列リテラル内の内容は変更しないよう注意している。
    """
    if not json_str:
        return json_str

    # 1. ブロックコメント /* ... */ を除去（文字列内は保護）
    json_str = _strip_outside_strings(json_str, pattern=r"/\*.*?\*/", flags=re.DOTALL)

    # 2. 行コメント // ... を除去（文字列内は保護）
    json_str = _strip_outside_strings(json_str, pattern=r"//[^\n]*", flags=0)

    # 3. 末尾カンマを除去: ,] や ,} のような形を ] や } に変換（文字列内は保護）
    json_str = _replace_outside_strings(json_str, r",(\s*[\]}])", r"\1")

    # 4. Python風リテラル(True/False/None)を標準JSONに変換（文字列内は保護）
    #    単語境界 (\b) を使って True/False を対象にする
    json_str = _replace_outside_strings(json_str, r"\bTrue\b", "true")
    json_str = _replace_outside_strings(json_str, r"\bFalse\b", "false")
    json_str = _replace_outside_strings(json_str, r"\bNone\b", "null")

    # 5. シングルクオートでキーや値が囲われている場合にダブルクオートに変換
    #    ただし、既にダブルクオートが含まれている文字列ではスキップ（複雑な混在を避ける）
    if "'" in json_str and _looks_like_single_quoted_json(json_str):
        json_str = _convert_single_to_double_quotes(json_str)

    return json_str


def _strip_outside_strings(text: str, pattern: str, flags: int = 0) -> str:
    """文字列リテラルの外側だけに対して正規表現マッチを除去する。"""
    return _replace_outside_strings(text, pattern, "", flags=flags)


def _replace_outside_strings(text: str, pattern: str, replacement, flags: int = 0) -> str:
    """文字列リテラルの外側だけに対して正規表現置換を行う。

    JSONの " で囲まれた文字列の内部は変更しないように保護する。
    """
    result = []
    i = 0
    n = len(text)
    regex = re.compile(pattern, flags)
    while i < n:
        ch = text[i]
        if ch == '"':
            # 文字列リテラルを探す
            start = i
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            result.append(text[start:i])
        else:
            # 文字列外の範囲を抽出し、置換を適用
            start = i
            while i < n and text[i] != '"':
                i += 1
            segment = text[start:i]
            if callable(replacement):
                segment = regex.sub(replacement, segment)
            else:
                segment = regex.sub(replacement, segment)
            result.append(segment)
    return "".join(result)


def _looks_like_single_quoted_json(text: str) -> bool:
    """シングルクオートでJSON風の構造が書かれているかを判定する。"""
    # {'key': のようなパターンが見つかればシングルクオートJSONと判断
    return bool(re.search(r"[{,]\s*'[^']+'\s*:", text))


def _convert_single_to_double_quotes(text: str) -> str:
    """シングルクオートで囲まれたJSONキー・値をダブルクオートに変換する。

    制限事項: 文字列内にアポストロフィ(')が含まれているケースは完全対応しない。
    ベストエフォート変換。
    """
    # キー: '...' を "..." に置換
    text = re.sub(r"([{,]\s*)'([^']*)'(\s*:)", r'\1"\2"\3', text)
    # 値: : '...' を : "..." に置換
    text = re.sub(r"(:\s*)'([^']*)'", r'\1"\2"', text)
    return text


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


# 全角数字→半角数字への変換テーブル（全角マイナス・全角ピリオドも対応）
_ZENKAKU_TO_HANKAKU = str.maketrans({
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "．": ".", "，": ",", "ー": "-", "−": "-", "－": "-",
})


def _normalize_zenkaku_digits(s: str) -> str:
    """全角数字・全角記号を半角に変換する。"""
    if not isinstance(s, str):
        return s
    return s.translate(_ZENKAKU_TO_HANKAKU)


def _safe_float(val) -> float:
    """値をfloatに安全変換（全角数字対応）"""
    if val is None:
        return 0.0
    try:
        if isinstance(val, str):
            # 全角数字を半角に変換
            s = _normalize_zenkaku_digits(val)
            # "660W" -> 660, "190.08kW" -> 190.08 のような文字列を処理
            cleaned = re.sub(r"[^\d.\-]", "", s)
            return float(cleaned) if cleaned else 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val) -> int:
    """値をintに安全変換（全角数字対応）"""
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            # 全角数字を半角に変換
            s = _normalize_zenkaku_digits(val)
            cleaned = re.sub(r"[^\d\-]", "", s)
            return int(cleaned) if cleaned else 0
        return int(val)
    except (ValueError, TypeError):
        return 0


def _sanitize_text(val, strip_honorifics: bool = False) -> str:
    """文字列の余計な空白・記号を除去する。

    Args:
        val: 入力値（str以外はstrに変換）
        strip_honorifics: True の場合、敬称（殿/様/さん/くん/君/氏）を除去する（人名向け）
    """
    if val is None:
        return ""
    if not isinstance(val, str):
        val = str(val)
    # 全角スペースを半角に統一し、前後の空白を除去
    s = val.replace("\u3000", " ").strip()
    # 連続する空白を1つに圧縮
    s = re.sub(r"\s+", " ", s)
    if strip_honorifics:
        # 末尾の敬称を除去（例: 「田中太郎 殿」「山田様」「鈴木さん」）
        s = re.sub(r"\s*(殿|様|さん|くん|君|氏)$", "", s)
    return s


def _sanitize_module_model(val) -> str:
    """モジュール型式の余計な空白・記号を除去する。

    型式は英数字・ハイフン・アンダースコア・スラッシュ・ドットで構成されることが多い。
    末尾の「W」や「ワット」は除去する（出力は別フィールドで管理）。
    """
    if val is None:
        return ""
    if not isinstance(val, str):
        val = str(val)
    s = val.replace("\u3000", " ").strip()
    # 連続空白を圧縮
    s = re.sub(r"\s+", " ", s)
    # 末尾が「...W」「...ワット」のように出力値で終わっている場合は除去
    s = re.sub(r"\s*\d+\s*(W|ワット|w)$", "", s).strip()
    return s


def _safe_bool(val) -> bool:
    """値をboolに安全変換"""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "あり", "有", "○")
    return bool(val)


def _normalize_date(date_str: str) -> str:
    """調査日のフォーマットを YYYY/MM/DD に正規化する。

    対応形式:
    - 2025/12/18, 2025.12.18, 2025-12-18 → 2025/12/18
    - 12/18, 12.18, 12-18 → (現在年)/12/18
    - R7.12.18, R07.12.18, 令和7年12月18日 → 2025/12/18
    """
    if not date_str or not date_str.strip():
        return date_str

    s = date_str.strip()

    # 令和表記: 令和7年12月18日 / R7.12.18 / R07/12/18
    reiwa_match = re.match(r"(?:令和|R)\s*(\d{1,2})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})\s*日?", s)
    if reiwa_match:
        year = 2018 + int(reiwa_match.group(1))
        month = int(reiwa_match.group(2))
        day = int(reiwa_match.group(3))
        return f"{year}/{month:02d}/{day:02d}"

    # YYYY/MM/DD, YYYY.MM.DD, YYYY-MM-DD
    full_match = re.match(r"(\d{4})\s*[./\-]\s*(\d{1,2})\s*[./\-]\s*(\d{1,2})", s)
    if full_match:
        return f"{full_match.group(1)}/{int(full_match.group(2)):02d}/{int(full_match.group(3)):02d}"

    # MM/DD, MM.DD, MM-DD（年なし → 現在年を補完）
    short_match = re.match(r"(\d{1,2})\s*[./\-]\s*(\d{1,2})$", s)
    if short_match:
        import datetime
        current_year = datetime.datetime.now().year
        month = int(short_match.group(1))
        day = int(short_match.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{current_year}/{month:02d}/{day:02d}"

    # 変換できない場合はそのまま返す
    return s


def _normalize_postal_code(val) -> str:
    """郵便番号を「XXX-XXXX」形式に正規化する。

    対応形式:
    - "5300001" → "530-0001"
    - "530-0001" → "530-0001"
    - "〒530-0001" → "530-0001"
    - "〒530 0001" → "530-0001"
    - 全角数字を含む → 半角変換後にフォーマット
    """
    if not val:
        return ""
    if not isinstance(val, str):
        val = str(val)
    # 全角→半角変換
    s = _normalize_zenkaku_digits(val)
    # 〒マーク・空白除去
    s = s.replace("〒", "").strip()
    # 数字のみを抽出
    digits = re.sub(r"\D", "", s)
    if len(digits) == 7:
        return f"{digits[:3]}-{digits[3:]}"
    # 7桁でない場合はそのまま返す（信頼度判定で low とマークされる）
    return s.strip()


def _is_valid_postal_code(postal: str) -> bool:
    """郵便番号が「XXX-XXXX」形式で7桁かを判定する。"""
    if not postal:
        return False
    return bool(re.match(r"^\d{3}-\d{4}$", postal))


def _normalize_separation_mm(val) -> float:
    """離隔距離をmm単位に正規化する。

    m, cm で返された場合に自動変換:
    - 3m, 3.0m → 3000.0
    - 300cm → 3000.0
    - 3000mm, 3000 → 3000.0
    """
    if val is None:
        return 0.0

    if isinstance(val, (int, float)):
        # 数値のみの場合: 10未満ならm単位と推定、10以上100未満ならcm単位と推定
        fval = float(val)
        if 0 < fval < 10:
            logger.info(f"離隔距離 {fval} をm単位と推定し、mm変換します")
            return fval * 1000
        if 10 <= fval < 100:
            logger.info(f"離隔距離 {fval} をcm単位と推定し、mm変換します")
            return fval * 10
        return fval

    if isinstance(val, str):
        s = val.strip().lower()
        # "3m", "3.0m" パターン
        m_match = re.match(r"([\d.]+)\s*m$", s)
        if m_match:
            return float(m_match.group(1)) * 1000
        # "300cm" パターン
        cm_match = re.match(r"([\d.]+)\s*cm$", s)
        if cm_match:
            return float(cm_match.group(1)) * 10
        # "3000mm" パターン
        mm_match = re.match(r"([\d.]+)\s*mm$", s)
        if mm_match:
            return float(mm_match.group(1))
        # 単位なし数値文字列
        return _safe_float(val)

    return _safe_float(val)


def _parse_raw_data(raw: dict) -> SurveyData:
    """生データをSurveyDataモデルに変換（堅牢なエラーハンドリング付き）"""
    warnings = list(raw.get("extraction_warnings", []) or [])

    # --- project ---
    proj_raw = raw.get("project", {}) or {}
    _sanitize_dict(proj_raw, str_keys=[
        "project_name", "address", "postal_code", "survey_date", "weather", "surveyor"])
    # 文字列フィールドをサニタイズ（全角空白・連続空白の正規化）
    for text_key in ["project_name", "address", "weather"]:
        if proj_raw.get(text_key):
            proj_raw[text_key] = _sanitize_text(proj_raw[text_key])
    # 調査者名は敬称を除去
    if proj_raw.get("surveyor"):
        proj_raw["surveyor"] = _sanitize_text(proj_raw["surveyor"], strip_honorifics=True)
    # 郵便番号を「XXX-XXXX」形式に正規化
    if proj_raw.get("postal_code"):
        proj_raw["postal_code"] = _normalize_postal_code(proj_raw["postal_code"])
    # 住所中に含まれる郵便番号を抽出して postal_code に補完（postal_code が空の場合）
    if not proj_raw.get("postal_code") and proj_raw.get("address"):
        zip_match = re.search(r"〒?\s*(\d{3})[\s\-]?(\d{4})", _normalize_zenkaku_digits(proj_raw["address"]))
        if zip_match:
            proj_raw["postal_code"] = f"{zip_match.group(1)}-{zip_match.group(2)}"
            # 住所から郵便番号部分を除去して純粋な住所だけ残す
            proj_raw["address"] = re.sub(
                r"〒?\s*\d{3}[\s\-]?\d{4}\s*", "", proj_raw["address"]
            ).strip()
    # 調査日のフォーマット正規化
    if proj_raw.get("survey_date"):
        proj_raw["survey_date"] = _normalize_date(proj_raw["survey_date"])
    try:
        project = ProjectInfo(**proj_raw)
    except Exception as e:
        logger.warning(f"ProjectInfo変換エラー: {e}")
        project = ProjectInfo()
        warnings.append(f"案件情報の一部を読み取れませんでした: {e}")

    # --- equipment ---
    eq_raw = raw.get("equipment", {}) or {}
    _sanitize_dict(eq_raw, str_keys=["module_maker", "module_model"])
    # モジュールメーカー・型式の文字列サニタイズ
    if eq_raw.get("module_maker"):
        eq_raw["module_maker"] = _sanitize_text(eq_raw["module_maker"])
    if eq_raw.get("module_model"):
        eq_raw["module_model"] = _sanitize_module_model(eq_raw["module_model"])
    # 数値フィールドは安全変換
    eq_raw["module_output_w"] = _safe_float(eq_raw.get("module_output_w"))
    eq_raw["pv_capacity_kw"] = _safe_float(eq_raw.get("pv_capacity_kw"))
    eq_raw["planned_panels"] = _safe_int(eq_raw.get("planned_panels"))
    # module_output_w の典型範囲チェック（200W未満 or 800W超は異常値の可能性）
    if 0 < eq_raw["module_output_w"] < 200 or eq_raw["module_output_w"] > 800:
        warnings.append(
            f"モジュール出力({eq_raw['module_output_w']}W)が典型範囲(200〜800W)から外れています。"
            f"手書き文字の誤読の可能性があります。"
        )
    # PV容量の自動計算補正: module_output_w × planned_panels / 1000 と比較
    module_w = eq_raw.get("module_output_w", 0) or 0
    panels = eq_raw.get("planned_panels", 0) or 0
    pv_kw = eq_raw.get("pv_capacity_kw", 0) or 0
    if module_w > 0 and panels > 0:
        calculated_kw = round(module_w * panels / 1000, 2)
        if pv_kw > 0:
            deviation = abs(calculated_kw - pv_kw) / calculated_kw
            if deviation > 0.10:
                warnings.append(
                    f"PV容量の読み取り値({pv_kw}kW)と計算値({calculated_kw}kW = {module_w}W × {panels}枚 / 1000)が"
                    f"{deviation:.0%}乖離しています。計算値で補正しました。"
                )
                eq_raw["pv_capacity_kw"] = calculated_kw
        elif pv_kw == 0:
            # PV容量が読み取れなかった場合は計算値で補完
            eq_raw["pv_capacity_kw"] = calculated_kw
            warnings.append(f"PV容量が読み取れなかったため、計算値({calculated_kw}kW)で補完しました。")

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
    # 離隔距離をmm単位に正規化（m/cm自動変換対応）
    for fk in ["separation_ns_mm", "separation_ew_mm"]:
        if fk in hv_raw:
            hv_raw[fk] = _normalize_separation_mm(hv_raw[fk])
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
    # 人名フィールドは敬称を除去してサニタイズ
    for name_key in ["surveyor_name", "design_reviewer", "works_reviewer"]:
        if conf_raw.get(name_key):
            conf_raw[name_key] = _sanitize_text(conf_raw[name_key], strip_honorifics=True)
    if conf_raw.get("notes"):
        conf_raw["notes"] = _sanitize_text(conf_raw["notes"])
    # 確認欄の日付も正規化
    for date_key in ["surveyor_date", "design_review_date", "works_review_date"]:
        if conf_raw.get(date_key):
            conf_raw[date_key] = _normalize_date(conf_raw[date_key])
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

    # --- 信頼度の自動判定（APIが field_confidences を返さなかった場合の補完） ---
    # 数値フィールドが0の場合 → low
    if "equipment.module_output_w" not in confidences and equipment.module_output_w == 0:
        confidences["equipment.module_output_w"] = ConfidenceLevel.LOW
    if "equipment.planned_panels" not in confidences and equipment.planned_panels == 0:
        confidences["equipment.planned_panels"] = ConfidenceLevel.LOW
    if "equipment.pv_capacity_kw" not in confidences and equipment.pv_capacity_kw == 0:
        confidences["equipment.pv_capacity_kw"] = ConfidenceLevel.LOW

    # module_output_w が典型範囲外（200未満 or 800超）→ low
    if 0 < equipment.module_output_w < 200 or equipment.module_output_w > 800:
        confidences["equipment.module_output_w"] = ConfidenceLevel.LOW

    # PV容量と計算値の一致判定
    if equipment.module_output_w > 0 and equipment.planned_panels > 0:
        calc_kw = round(equipment.module_output_w * equipment.planned_panels / 1000, 2)
        diff = abs(calc_kw - equipment.pv_capacity_kw)
        if diff < 0.01:
            # 完全一致 → high
            confidences["equipment.pv_capacity_kw"] = ConfidenceLevel.HIGH
        elif calc_kw > 0 and diff / calc_kw < 0.05:
            # 5%未満の誤差 → medium
            confidences["equipment.pv_capacity_kw"] = ConfidenceLevel.MEDIUM
        elif equipment.pv_capacity_kw > 0:
            # 5%以上ズレている → low
            confidences["equipment.pv_capacity_kw"] = ConfidenceLevel.LOW

    # 郵便番号が7桁でない → low
    if project.postal_code and not _is_valid_postal_code(project.postal_code):
        confidences["project.postal_code"] = ConfidenceLevel.LOW
        warnings.append(
            f"郵便番号「{project.postal_code}」が「XXX-XXXX」形式（7桁）ではありません。"
            f"手書き文字の誤読の可能性があります。"
        )
    elif "project.postal_code" not in confidences and not project.postal_code:
        confidences["project.postal_code"] = ConfidenceLevel.LOW

    # 重要な文字列フィールドが空の場合 → low
    if "project.project_name" not in confidences and not project.project_name:
        confidences["project.project_name"] = ConfidenceLevel.LOW
    if "project.address" not in confidences and not project.address:
        confidences["project.address"] = ConfidenceLevel.LOW
    if "project.survey_date" not in confidences and not project.survey_date:
        confidences["project.survey_date"] = ConfidenceLevel.LOW
    if "equipment.module_maker" not in confidences and not equipment.module_maker:
        confidences["equipment.module_maker"] = ConfidenceLevel.LOW
    if "equipment.module_model" not in confidences and not equipment.module_model:
        confidences["equipment.module_model"] = ConfidenceLevel.LOW

    return SurveyData(
        project=project,
        equipment=equipment,
        high_voltage=high_voltage,
        supplementary=supplementary,
        confirmation=confirmation,
        extraction_warnings=warnings,
        field_confidences=confidences,
    )
