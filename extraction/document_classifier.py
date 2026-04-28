"""PDF書類が「住宅(低圧)」か「法人(高圧)」かを判別する分類器

Claude Vision API を用いて、現調シートを含むPDF群の先頭ページを画像化し、
書類カテゴリを自動判別する。これにより抽出時のプロンプトを切り替え、
住宅/法人それぞれに最適化された読み取りで精度向上を図る。

使用例:
    >>> from extraction.document_classifier import classify_documents, classify_single_pdf
    >>>
    >>> # 単一PDFを判別
    >>> result = classify_single_pdf("/path/to/survey.pdf")
    >>> print(result["category"])  # "residential" / "commercial" / "unknown"
    >>>
    >>> # 複数PDFをまとめて判別
    >>> result = classify_documents([
    ...     "/path/to/survey.pdf",
    ...     "/path/to/wiring_diagram.pdf",
    ... ])
    >>> if result["category"] == "commercial":
    ...     # 法人(高圧)用の抽出プロンプトを使う
    ...     ...
    >>> elif result["category"] == "residential":
    ...     # 住宅(低圧)用の抽出プロンプトを使う
    ...     ...

返り値の構造:
    {
      "category": "residential" | "commercial" | "unknown",
      "confidence": "high" | "medium" | "low",
      "evidence": "判定根拠の短文",
      "page_assignments": [
        {"pdf": "path", "page": 1,
         "type": "現調シート|別紙|配管図|単線結線図|配置図|その他"},
        ...
      ]
    }
"""
import json
import logging
import re
import anthropic

from extraction.pdf_reader import pdf_to_images
from config import get_api_key, CLAUDE_MODEL

logger = logging.getLogger(__name__)

MAX_RETRIES = 1  # 分類は失敗しても unknown フォールバックでよい
CLASSIFY_DPI = 150  # 速度重視
PAGES_PER_PDF = 2  # 先頭2ページのみ画像化

CLASSIFICATION_PROMPT = """あなたは太陽光発電設備の設計・施工書類を分類する専門家です。

提供された画像群は、複数のPDF（現調シート・配管図・単線結線図・配置図など）の先頭ページです。
これらが「住宅(低圧、屋根上)」向けか「法人(高圧、産業向け)」向けかを判別してください。

【法人 (commercial) の特徴】
- タイトル・見出しに「高圧」「現地調査チェックシート(高圧)」「高圧受電」等の文言
- 項目に「VT」「CT」「接地種類A種・C種・D種」「Tr容量(変圧器容量)」「PCS設置スペース」「キュービクル」
- 案件名に「株式会社」「(株)」「工業」「工場」「事業所」「センター」「物流」「倉庫」等の法人名
- PV容量が 50kW 以上（高圧連系の目安）
- 単線結線図・配管図・キュービクル配置図などの設計図面が同梱されている

【住宅 (residential) の特徴】
- タイトル・見出しに「住宅用」「低圧」「住宅太陽光」「戸建て」等の文言
- 家屋見取図・屋根伏図・戸建て住宅の写真
- PV容量が 10kW 未満（低圧、住宅用の目安）
- 施主名が個人名（「株式会社」「(株)」等の法人接尾辞を含まない）
- パッケージ商品コード・住宅用パワコン型式
- 屋根材（スレート/瓦/金属屋根）や勾配の記載

【判別の優先順位】
1. タイトル・帳票名に「高圧」「住宅用」など明示的な文言があればそれを最優先
2. 案件名・施主名（法人名 vs 個人名）
3. PV容量（50kW以上=法人、10kW未満=住宅、間は他要素で判断）
4. 含まれる図面の種類（単線結線図/キュービクル=法人、屋根伏図=住宅）

【判別不能な場合】
- 画像が判読不能、白紙、上記特徴のいずれも当てはまらない場合は "unknown"
- 住宅と法人の特徴が混在している場合は、より強いシグナル側を採用し confidence を "low" にする

【ページ種別の判定】
各PDFの先頭ページが、以下のどれに該当するかも判別してください:
- "現調シート": 現地調査チェックシート(主表)
- "別紙": 現調シートの2ページ目(補足チェック項目)
- "配管図": 電気配管の経路図
- "単線結線図": 電気設備の接続図
- "配置図": 機器配置図・屋根伏図など
- "その他": 上記いずれにも当てはまらない

必ず以下のJSON形式のみを返してください。説明文・前置き・コードブロックは不要です。

{
  "category": "residential" | "commercial" | "unknown",
  "confidence": "high" | "medium" | "low",
  "evidence": "判定根拠を1〜2文で簡潔に記述（例: タイトルに『現地調査チェックシート(高圧)』、案件名に『株式会社○○工業』、PV容量190kW）",
  "page_assignments": [
    {"pdf_index": 0, "page": 1, "type": "現調シート"},
    {"pdf_index": 0, "page": 2, "type": "別紙"},
    {"pdf_index": 1, "page": 1, "type": "単線結線図"}
  ]
}
"""


def classify_single_pdf(pdf_path: str) -> dict:
    """単一PDFを住宅/法人で分類する簡易版

    Args:
        pdf_path: PDFファイルパス

    Returns:
        classify_documents と同じ構造の dict
    """
    return classify_documents([pdf_path])


def classify_documents(pdf_paths: list[str]) -> dict:
    """複数PDFをまとめて住宅/法人で分類する

    各PDFの先頭2ページのみを画像化して Claude Vision API に投げる。
    失敗時は unknown を返し、例外は握りつぶさず logger.warning で記録する。

    Args:
        pdf_paths: PDFファイルパスのリスト

    Returns:
        {
          "category": "residential" | "commercial" | "unknown",
          "confidence": "high" | "medium" | "low",
          "evidence": str,
          "page_assignments": [{"pdf": str, "page": int, "type": str}, ...]
        }
    """
    if not pdf_paths:
        return _unknown_result(pdf_paths, "PDFパスが指定されていません")

    # 各PDFの先頭2ページを画像化
    content_blocks: list[dict] = []
    pdf_page_index: list[tuple[int, int]] = []  # (pdf_index, page_number) の対応

    for pdf_index, pdf_path in enumerate(pdf_paths):
        try:
            pages = pdf_to_images(pdf_path, dpi=CLASSIFY_DPI)
        except Exception as e:
            logger.warning(f"分類用PDF画像化失敗: {pdf_path}: {e}")
            continue

        for page in pages[:PAGES_PER_PDF]:
            content_blocks.append({
                "type": "text",
                "text": f"[PDF #{pdf_index} ({pdf_path}) - Page {page['page']}]",
            })
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": page.get("media_type", "image/png"),
                    "data": page["image_base64"],
                },
            })
            pdf_page_index.append((pdf_index, page["page"]))

    if not content_blocks:
        return _unknown_result(pdf_paths, "すべてのPDFで画像化に失敗しました")

    content_blocks.append({"type": "text", "text": CLASSIFICATION_PROMPT})

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw = _call_classify_api(content_blocks, attempt)
            return _parse_classification(raw, pdf_paths)
        except Exception as e:
            last_error = e
            logger.warning(f"分類API試行 {attempt}/{MAX_RETRIES} 失敗: {e}")

    logger.warning(f"分類に失敗したため unknown を返します: {last_error}")
    return _unknown_result(pdf_paths, f"分類API呼び出し失敗: {last_error}")


def _call_classify_api(content: list[dict], attempt: int) -> dict:
    """Claude Vision APIで分類を実行（temperature=0、prefill="{"）"""
    client = anthropic.Anthropic(api_key=get_api_key())

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        temperature=0,
        messages=[
            {"role": "user", "content": content},
            {"role": "assistant", "content": "{"},
        ],
    )

    response_text = response.content[0].text
    if not response_text.lstrip().startswith("{"):
        response_text = "{" + response_text
    logger.info(f"分類API応答(試行{attempt}): {response_text[:200]}...")
    json_str = _extract_json_block(response_text)
    return json.loads(json_str)


def _extract_json_block(text: str) -> str:
    """応答テキストから最初のJSONオブジェクトを抽出する"""
    # ```json ... ``` ブロック
    block = re.search(r"```json\s*\n?(.*?)```", text, re.DOTALL)
    if block:
        return block.group(1).strip()

    # 最初の { から対応する } までを抽出
    text = text.strip()
    start = text.find("{")
    if start < 0:
        raise ValueError(f"JSONが見つかりません: {text[:200]}")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
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
                return text[start:i + 1]

    raise ValueError(f"JSONの閉じ括弧が見つかりません: {text[:200]}")


def _parse_classification(raw: dict, pdf_paths: list[str]) -> dict:
    """生データを正規化された分類結果に変換する"""
    category = raw.get("category", "unknown")
    if category not in ("residential", "commercial", "unknown"):
        category = "unknown"

    confidence = raw.get("confidence", "low")
    if confidence not in ("high", "medium", "low"):
        confidence = "low"

    evidence = str(raw.get("evidence", "")).strip() or "判定根拠なし"

    # page_assignments を pdf_index ベースから pdf パスベースに変換
    valid_types = {"現調シート", "別紙", "配管図", "単線結線図", "配置図", "その他"}
    page_assignments = []
    for pa in raw.get("page_assignments", []) or []:
        try:
            pdf_index = int(pa.get("pdf_index", -1))
            page_num = int(pa.get("page", 0))
            ptype = str(pa.get("type", "その他"))
            if ptype not in valid_types:
                ptype = "その他"
            if 0 <= pdf_index < len(pdf_paths) and page_num > 0:
                page_assignments.append({
                    "pdf": pdf_paths[pdf_index],
                    "page": page_num,
                    "type": ptype,
                })
        except (ValueError, TypeError) as e:
            logger.warning(f"page_assignment解析失敗: {pa}: {e}")
            continue

    return {
        "category": category,
        "confidence": confidence,
        "evidence": evidence,
        "page_assignments": page_assignments,
    }


def _unknown_result(pdf_paths: list[str], reason: str) -> dict:
    """unknown フォールバック結果を生成する"""
    return {
        "category": "unknown",
        "confidence": "low",
        "evidence": reason,
        "page_assignments": [
            {"pdf": p, "page": 1, "type": "その他"} for p in (pdf_paths or [])
        ],
    }
