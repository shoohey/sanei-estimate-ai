"""見積書PDF/CSV → ParsedEstimate 変換パーサー（差分学習ループ v2.6 / Agent B）

正規見積PDF（人が修正した最終版）と、本ツールが出力したAI見積CSVを
共通の中間表現 ParsedEstimate に変換する。差分抽出（estimate_diff）の入力を作る役割。

公開関数:
    - parse_estimate_pdf(pdf_path, source): 見積書PDFをClaudeで構造化してパース
        1) PyMuPDF のテキスト層が十分（>300字）ならテキスト→Claude（安価・高精度）
        2) 乏しければ（スキャンPDF等）pdf_to_images + Vision にフォールバック
    - parse_estimate_csv(csv_bytes, source, file_name): 本ツール出力CSVをパース（API不要）
        generation/csv_exporter.py の detailed形式（"=== 見積明細 ===" セクション式）と
        simple形式（ヘッダー「カテゴリ,No,摘要,備考,数量,単価,金額」）を自動判別

JSON修復・数値パースは extraction/survey_extractor.py の実装を import して流用する。
APIキーが無い環境でも import 自体は成功する（API呼び出しは関数実行時のみ）。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional, Union

import anthropic
import fitz  # PyMuPDF

from config import CLAUDE_MODEL, get_api_key
from extraction.pdf_reader import pdf_to_images
from extraction.survey_extractor import (  # JSON修復・数値パースを流用（コピーしない）
    _extract_json,        # 応答テキスト→JSON文字列（内部で _sanitize_json_str を適用）
    _sanitize_json_str,   # JSONのよくある不正形式の自動修復
    _safe_float,
    _safe_int,
)
from learning.models import ESTIMATE_CATEGORIES, ParsedEstimate, ParsedLineItem

logger = logging.getLogger(__name__)

# APIリトライ設定（survey_extractor のパターン踏襲・線形バックオフ）
MAX_RETRIES = 3
RETRY_DELAY_SEC = 2  # 待機秒 = RETRY_DELAY_SEC × 試行回数

# 応答の最大トークン。明細行が多い帳票（原価表等）は応答が長くなり、
# 上限に達すると JSON が途中で切れて全リトライが失敗する（temperature=0 のため
# 同条件の再試行は同じ位置で切れる）。途中切れを検知したら CEILING まで引き上げて再試行する。
MAX_OUTPUT_TOKENS = 32_000
MAX_OUTPUT_TOKENS_CEILING = 64_000  # 安全側の実用上限（claude-sonnet-4-6 のモデル上限は128K）

# テキスト層がこの文字数を超えていれば「テキスト→Claude」で処理（未満はVisionフォールバック）
TEXT_LAYER_MIN_CHARS = 300

# プロンプトに載せる見積書テキストの上限（トークン暴発防止の防御値）
MAX_TEXT_CHARS = 100_000

# 明細に紛れ込んだ集計行を防御的に除外するためのパターン
_SUMMARY_ROW_RE = re.compile(r"(小計|合計|消費税|値引)")

# カテゴリ名の「1. 」「２．」のような番号プレフィクス
_CAT_PREFIX_RE = re.compile(r"^\s*[0-9０-９]+\s*[.．、]\s*")

# カテゴリ名の表記揺れ → 正規カテゴリ名
_CATEGORY_ALIASES = {
    "その他諸経費等": "その他・諸経費等",
    "その他・諸経費": "その他・諸経費等",
    "その他諸経費": "その他・諸経費等",
    "諸経費": "その他・諸経費等",
    "諸経費等": "その他・諸経費等",
}

# 数量文字列の先頭数値部分（"288枚" → "288" と "枚"。全角数字・カンマ・小数対応）
_QUANTITY_RE = re.compile(
    r"^\s*([-+]?[0-9０-９][0-9０-９,，]*(?:[.．][0-9０-９]+)?)\s*(.*)$"
)

ESTIMATE_PARSE_PROMPT = """あなたは株式会社サンエーの太陽光発電設備工事の見積書を読み取る専門家です。
与えられた見積書から、全明細行と集計金額を正確に抽出してください。

【サンエー見積書の6カテゴリ】
明細の category は必ず以下の6種のいずれかに正規化してください
（例:「1. 支給品」→「支給品」、「４．その他・諸経費等」→「その他・諸経費等」）。
どのカテゴリか判別できない場合のみ空文字列 "" のままにしてください。
1. 支給品 / 2. 材料費 / 3. 施工費 / 4. その他・諸経費等 / 5. 付帯工事 / 6. 特記事項

必ず以下のJSON形式のみを返してください。説明文は不要です。

```json
{
  "estimate_id": "見積番号・見積ID（例: 22730522-4367674）",
  "client_name": "宛先会社名（「御中」「様」等の敬称は除く）",
  "project_name": "工事名・件名",
  "issue_date": "発行日（YYYY/MM/DD）",
  "items": [
    {
      "category": "材料費",
      "no": 1,
      "description": "摘要（項目名）",
      "remarks": "備考",
      "quantity_value": 288,
      "quantity_unit": "枚",
      "unit_price": 3300,
      "amount": 950400
    }
  ],
  "subtotal": 8000000,
  "discount": -200000,
  "total_before_tax": 7800000,
  "tax": 780000,
  "total_with_tax": 8580000,
  "warnings": ["読み取り不確実な箇所の警告メッセージ"]
}
```

重要な注意事項:
- 小計・合計・税抜合計・消費税・税込合計・値引きの行は items（明細）に絶対に含めないでください
- 値引き行（「お値引き」「出精値引き」「特別値引き」等）は items に入れず、
  discount に負の値で入れてください（例: 200,000円の値引き → -200000）
- subtotal は税抜・値引前の小計、total_before_tax は値引後の税抜合計、
  tax は消費税、total_with_tax は税込合計です。記載が無い項目は null にしてください
- unit_price / amount は数値型（カンマ・円マークなし・円単位）。記載が無い場合は null
- quantity_value は数量の数値部分（例: "288枚" → 288）、quantity_unit は単位（枚・式・m・台 等）。
  数量の記載が無い場合は quantity_value を null にしてください
- 支給品カテゴリの明細は単価・金額が0円のことがあります（そのまま 0 で返す）
- 全角数字は半角に変換してください
- 読み取りが不確実な箇所は warnings に日本語で記載してください
- JSON以外のテキスト（説明文・前置き・コメント）は一切含めないでください
- 明細行が何行あっても省略せず、全行を items に含めてください
  （「以下省略」「// 残りは同様」のような省略表現は禁止です）
- 応答が長くなり途中で切れるのを防ぐため、JSONは改行・インデントなしの
  コンパクト形式（1行）で出力してください
"""


# =============================================================
# 共通ヘルパー
# =============================================================

def _opt_int(val) -> Optional[int]:
    """空欄/None は None、それ以外は _safe_int で防御的に整数化する。"""
    if val is None:
        return None
    if isinstance(val, str) and not val.strip():
        return None
    return _safe_int(val)


def _opt_float(val) -> Optional[float]:
    """空欄/None は None、それ以外は _safe_float で防御的に数値化する。"""
    if val is None:
        return None
    if isinstance(val, str) and not val.strip():
        return None
    return _safe_float(val)


def _split_quantity(raw) -> tuple[Optional[float], str]:
    """数量セルを (数値, 単位) に分離する。

    例: "288枚" → (288.0, "枚") / "1式" → (1.0, "式") / "30m" → (30.0, "m")
    数値が先頭に無い場合（例: "一式"）は (None, 元文字列) を返す。
    """
    if raw is None:
        return None, ""
    if isinstance(raw, (int, float)):
        return float(raw), ""
    s = str(raw).strip()
    if not s:
        return None, ""
    m = _QUANTITY_RE.match(s)
    if m:
        return _safe_float(m.group(1)), m.group(2).strip()
    return None, s


def _normalize_category(raw) -> str:
    """カテゴリ表記を6カテゴリ名に正規化する。判別不能は ""。

    "1. 支給品" のような番号プレフィクスを除去し、
    中黒・空白の表記揺れも吸収して ESTIMATE_CATEGORIES と照合する。
    """
    if not raw:
        return ""
    s = _CAT_PREFIX_RE.sub("", str(raw).strip()).strip()
    if s in ESTIMATE_CATEGORIES:
        return s
    if s in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[s]
    # 中黒・空白を除いた比較（例: "その他 諸経費等" → "その他・諸経費等"）
    compact = re.sub(r"[\s・]", "", s)
    for cat in ESTIMATE_CATEGORIES:
        if compact == re.sub(r"[\s・]", "", cat):
            return cat
    return ""


def _strip_honorific(name: str) -> str:
    """宛先名から末尾の敬称（御中・様）を除去する。"""
    return re.sub(r"\s*(御中|様)$", "", (name or "").strip()).strip()


def _normalize_discount(value: Optional[int]) -> Optional[int]:
    """値引きを負値に正規化する（見積書上は正の金額で書かれることがあるため）。"""
    if value is None:
        return None
    return value if value <= 0 else -value


def _check_totals_consistency(parsed: ParsedEstimate) -> None:
    """明細合計と抽出した小計の不整合（±1円超）を warnings に追記する。"""
    amounts = [item.amount for item in parsed.items if item.amount is not None]
    if not amounts or parsed.subtotal is None:
        return
    items_total = sum(amounts)
    if abs(items_total - parsed.subtotal) > 1:
        parsed.warnings.append(
            f"明細の合計 ¥{items_total:,} と小計 ¥{parsed.subtotal:,} が一致しません"
            f"（差 ¥{items_total - parsed.subtotal:,}）。読み落とし・誤読の可能性があります。"
        )


# =============================================================
# PDFパース（Claude 呼び出し）
# =============================================================

def parse_estimate_pdf(pdf_path: Union[str, Path], source: str) -> ParsedEstimate:
    """見積書PDFを Claude で構造化して ParsedEstimate に変換する。

    テキスト層が十分にあるPDF（本ツール生成PDFや電子見積）はテキストで、
    スキャンPDFは画像（Vision）でパースする。

    Args:
        pdf_path: 見積書PDFのパス
        source: "ai" | "official"（どちら側の見積か）

    Returns:
        ParsedEstimate（origin="pdf"）

    Raises:
        RuntimeError: PDFが読めない、または全リトライ失敗時
    """
    pdf_path = Path(pdf_path)
    file_name = pdf_path.name

    # --- ステップ1: テキスト層の抽出を試みる ---
    text = _extract_pdf_text(pdf_path)

    if len(text) > TEXT_LAYER_MIN_CHARS:
        # テキスト→Claude（安価・高精度）
        logger.info("見積PDFをテキストモードでパース（%d文字）: %s", len(text), file_name)
        content = [{
            "type": "text",
            "text": f"{ESTIMATE_PARSE_PROMPT}\n\n【見積書テキスト】\n{text[:MAX_TEXT_CHARS]}",
        }]
    else:
        # --- ステップ2: Visionフォールバック（スキャンPDF等） ---
        # 見積書は印字帳票のため画像強調なしの生画像で渡す
        logger.info("見積PDFをVisionモードでパース（テキスト%d文字）: %s", len(text), file_name)
        pages = pdf_to_images(str(pdf_path), dpi=200, enhance_contrast=False)
        if not pages:
            raise RuntimeError("PDFからページを読み取れませんでした。ファイルが空でないか確認してください。")
        content = []
        for page in pages:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": page.get("media_type", "image/png"),
                    "data": page["image_base64"],
                },
            })
        content.append({"type": "text", "text": ESTIMATE_PARSE_PROMPT})

    # --- ステップ3: Claude呼び出し（リトライ付き）→ ParsedEstimate 変換 ---
    raw = _call_claude_with_retry(content)
    parsed = _json_to_parsed(raw, source=source, origin="pdf", file_name=file_name)
    _check_totals_consistency(parsed)
    return parsed


def _extract_pdf_text(pdf_path: Path) -> str:
    """PyMuPDF で全ページのテキスト層を抽出する。失敗時は ""（Visionへフォールバック）。"""
    try:
        doc = fitz.open(str(pdf_path))
        try:
            texts = []
            for page in doc:
                try:
                    texts.append(page.get_text())
                except Exception as e:
                    logger.warning("見積PDFのテキスト抽出に失敗（ページスキップ）: %s", e)
            return "\n".join(texts).strip()
        finally:
            doc.close()
    except Exception as e:
        logger.warning("見積PDFを開けませんでした（Visionへフォールバック）: %s", e)
        return ""


def _call_claude_with_retry(content: list[dict]) -> dict:
    """Claude API を呼び出してJSON dictを返す（3回リトライ・線形バックオフ）。

    survey_extractor と同じ方針:
    - temperature=0 で決定論的な出力（結果の再現性を確保）
    - 前置きやコードフェンス混じりの応答は _extract_json で除去・修復
      （_sanitize_json_str は _extract_json 内部で適用される）

    明細行が多い帳票への防御:
    - max_tokens が大きいと非ストリーミングは SDK の10分制限で拒否されるため
      ストリーミングで受信する（Streamlit Cloud のHTTPタイムアウト回避も兼ねる）
    - stop_reason=max_tokens（応答の途中切れ）は上限を引き上げて再試行し、
      上限でも収まらない場合は原因が分かるメッセージで即時失敗する
    - 途中切れによる上限引き上げはリトライ予算（MAX_RETRIES）を消費しない
      （一時エラーの直後の最終試行で途中切れしても引き上げ再試行が実行されるように）
    """
    last_error = None
    max_tokens = MAX_OUTPUT_TOKENS
    attempt = 1
    while attempt <= MAX_RETRIES:
        try:
            client = anthropic.Anthropic(api_key=get_api_key())
            with client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=max_tokens,
                temperature=0,
                messages=[{"role": "user", "content": content}],
            ) as stream:
                response = stream.get_final_message()
            response_text = response.content[0].text
            logger.info(
                "見積パースAPI応答（試行%d・stop_reason=%s・出力%dトークン）: %s...",
                attempt, response.stop_reason, response.usage.output_tokens,
                response_text[:200],
            )
        except anthropic.APIError as e:
            last_error = e
            logger.warning(
                "見積パース試行 %d/%d - APIエラー: %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC * attempt)  # 線形バックオフ（2秒→4秒）
            attempt += 1
            continue
        except Exception as e:
            last_error = e
            logger.warning(
                "見積パース試行 %d/%d - 予期せぬエラー: %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC * attempt)
            attempt += 1
            continue

        if response.stop_reason == "max_tokens":
            # temperature=0 では同じ上限での再試行は同じ位置で切れるため、上限を上げる
            last_error = RuntimeError(
                f"応答が max_tokens={max_tokens} に達して途中で切れました（明細行が多い帳票）")
            if max_tokens < MAX_OUTPUT_TOKENS_CEILING:
                logger.warning(
                    "応答が max_tokens=%d で途中切れ。上限を %d に引き上げて再試行します",
                    max_tokens, MAX_OUTPUT_TOKENS_CEILING)
                max_tokens = MAX_OUTPUT_TOKENS_CEILING
                # 容量不足はAPI起因の一時エラーではないため、待機せず・attemptも消費せず再試行
                # （2回目の途中切れは上の raise に到達するため無限ループにはならない）
                continue
            raise RuntimeError(
                f"見積書の明細が多すぎるため、AI応答が上限（{MAX_OUTPUT_TOKENS_CEILING:,}トークン）に"
                "収まりませんでした。\nPDFをページ分割して明細を減らしてから再度お試しください。"
            )

        try:
            return json.loads(_extract_json(response_text))
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            logger.warning(
                "見積パース試行 %d/%d - JSON解析エラー: %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC * attempt)
            attempt += 1

    raise RuntimeError(
        f"見積書のAI読み取りに{MAX_RETRIES}回失敗しました。\n"
        f"PDFの画質・APIキー設定・API稼働状況を確認してください。\n"
        f"詳細エラー: {last_error}"
    )


def _json_to_parsed(raw: dict, source: str, origin: str, file_name: str) -> ParsedEstimate:
    """Claude応答のJSON dict を ParsedEstimate に防御的に変換する。"""
    parsed = ParsedEstimate(
        source=source,
        origin=origin,
        file_name=file_name,
        estimate_id=str(raw.get("estimate_id") or "").strip(),
        client_name=_strip_honorific(str(raw.get("client_name") or "")),
        project_name=str(raw.get("project_name") or "").strip(),
        issue_date=str(raw.get("issue_date") or "").strip(),
        subtotal=_opt_int(raw.get("subtotal")),
        discount=_normalize_discount(_opt_int(raw.get("discount"))),
        total_before_tax=_opt_int(raw.get("total_before_tax")),
        tax=_opt_int(raw.get("tax")),
        total_with_tax=_opt_int(raw.get("total_with_tax")),
    )

    for it in raw.get("items", []) or []:
        if not isinstance(it, dict):
            continue
        description = str(it.get("description") or "").strip()
        # 防御: プロンプトで禁止していても小計・合計・値引き行が紛れ込むことがある
        if description and _SUMMARY_ROW_RE.search(description) and it.get("unit_price") is None:
            parsed.warnings.append(f"集計らしき行を明細から除外しました: {description}")
            continue
        # 数量: quantity_value/quantity_unit を優先、無ければ "288枚" 形式の quantity を分離
        quantity_value = _opt_float(it.get("quantity_value"))
        quantity_unit = str(it.get("quantity_unit") or "").strip()
        if quantity_value is None and it.get("quantity"):
            quantity_value, split_unit = _split_quantity(it.get("quantity"))
            quantity_unit = quantity_unit or split_unit
        parsed.items.append(ParsedLineItem(
            category=_normalize_category(it.get("category")),
            no=_safe_int(it.get("no")),
            description=description,
            remarks=str(it.get("remarks") or "").strip(),
            quantity_value=quantity_value,
            quantity_unit=quantity_unit,
            unit_price=_opt_int(it.get("unit_price")),
            amount=_opt_int(it.get("amount")),
        ))

    # Claudeが返した警告も引き継ぐ
    for w in raw.get("warnings", []) or []:
        if w:
            parsed.warnings.append(str(w))

    return parsed


# =============================================================
# CSVパース（API不要）
# =============================================================

def parse_estimate_csv(csv_bytes: bytes, source: str, file_name: str = "") -> ParsedEstimate:
    """本ツール出力のCSV（detailed/simple）を ParsedEstimate に変換する。

    generation/csv_exporter.py の出力形式に対応:
    - detailed形式: "=== 見積書情報 ===" / "=== 見積明細 ===" / "=== 集計 ===" のセクション式
      （明細ヘッダーに計算方法・計算式・根拠元・補足の列が続く）
    - simple形式: ヘッダー「カテゴリ,No,摘要,備考,数量,単価,金額」+ 末尾に集計行

    BOM（utf-8-sig）とCRLF改行に対応。API不要で完結する。

    Args:
        csv_bytes: CSVファイルのバイト列
        source: "ai" | "official"
        file_name: 元ファイル名（表示用）

    Returns:
        ParsedEstimate（origin="csv"）
    """
    # BOM除去 + 改行コード統一（CRLF/CR → LF）
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    rows = list(csv.reader(io.StringIO(text)))

    parsed = ParsedEstimate(source=source, origin="csv", file_name=file_name)

    if _is_detailed_csv(rows):
        _parse_detailed_rows(rows, parsed)
    else:
        _parse_simple_rows(rows, parsed)

    if not parsed.items:
        parsed.warnings.append("CSVから明細行を1件も読み取れませんでした。形式を確認してください。")

    _check_totals_consistency(parsed)
    return parsed


def _is_detailed_csv(rows: list[list[str]]) -> bool:
    """detailed形式（"=== ... ===" セクションマーカー）かを判定する。"""
    for row in rows:
        if row and row[0].strip().startswith("==="):
            return True
    return False


def _is_blank_row(row: list[str]) -> bool:
    return not row or all(not c.strip() for c in row)


def _parse_detailed_rows(rows: list[list[str]], parsed: ParsedEstimate) -> None:
    """detailed形式CSVをセクションごとにパースして parsed に書き込む。"""
    section = ""
    for row in rows:
        if _is_blank_row(row):
            continue
        first = row[0].strip()

        # セクションマーカー（例: "=== 見積明細 ==="）
        m = re.match(r"^===\s*(.+?)\s*===$", first)
        if m:
            section = m.group(1)
            continue

        if section == "見積書情報":
            value = row[1].strip() if len(row) > 1 else ""
            if first == "見積ID":
                parsed.estimate_id = value
            elif first == "工事名":
                parsed.project_name = value
            elif first == "宛先":
                parsed.client_name = _strip_honorific(value)
            elif first == "発行日":
                parsed.issue_date = value
        elif section == "見積明細":
            if first == "カテゴリ":
                continue  # ヘッダー行
            item = _row_to_item(row)
            if item is not None:
                parsed.items.append(item)
        elif section == "集計":
            value = row[1] if len(row) > 1 else ""
            _apply_summary_row(parsed, first, value)
        # "見積根拠一覧" セクションは学習に不要のため読み飛ばす


def _parse_simple_rows(rows: list[list[str]], parsed: ParsedEstimate) -> None:
    """simple形式CSV（明細 + 末尾集計行）をパースして parsed に書き込む。"""
    for row in rows:
        if _is_blank_row(row):
            continue
        first = row[0].strip()
        if first == "カテゴリ":
            continue  # ヘッダー行
        # 集計行（"小計,,,,,,8000000" のように金額は末尾列に入る）
        if _SUMMARY_ROW_RE.search(first) and not _normalize_category(first):
            last_value = next((c for c in reversed(row) if c.strip()), "")
            _apply_summary_row(parsed, first, last_value)
            continue
        item = _row_to_item(row)
        if item is not None:
            parsed.items.append(item)


def _row_to_item(row: list[str]) -> Optional[ParsedLineItem]:
    """明細1行（カテゴリ,No,摘要,備考,数量,単価,金額,...）を ParsedLineItem に変換する。

    列不足は空文字で補い、集計らしき行や空行は None を返して除外する。
    """
    cells = [str(c) for c in row] + [""] * max(0, 7 - len(row))
    cat_raw = cells[0].strip()
    description = cells[2].strip()

    # 防御: 集計行が明細セクションに紛れた場合は除外（摘要が空で先頭が小計/合計等）
    if not description:
        return None
    if not _normalize_category(cat_raw) and _SUMMARY_ROW_RE.search(cat_raw):
        return None

    quantity_value, quantity_unit = _split_quantity(cells[4])
    return ParsedLineItem(
        category=_normalize_category(cat_raw),
        no=_safe_int(cells[1]),
        description=description,
        remarks=cells[3].strip(),
        quantity_value=quantity_value,
        quantity_unit=quantity_unit,
        unit_price=_opt_int(cells[5]),
        amount=_opt_int(cells[6]),
    )


def _apply_summary_row(parsed: ParsedEstimate, label: str, raw_value) -> None:
    """集計行（小計/お値引き/税抜合計/消費税/税込合計）を parsed に反映する。"""
    label = (label or "").strip()
    value = _opt_int(raw_value)
    if value is None:
        return
    if label == "小計":
        parsed.subtotal = value
    elif "値引" in label:
        parsed.discount = _normalize_discount(value)
    elif label == "税抜合計":
        parsed.total_before_tax = value
    elif label.startswith("消費税"):
        parsed.tax = value
    elif label == "税込合計":
        parsed.total_with_tax = value
