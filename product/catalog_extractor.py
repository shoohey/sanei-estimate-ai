"""製品カタログ抽出モジュール

太陽光モジュール / PCS / 蓄電池などの製品カタログ（PDF・画像）を
Claude Vision API で読み取り、メーカー・型式・寸法・電気特性・保証情報などを
構造化JSONとして返す。

使い方:
    from product.catalog_extractor import extract_product_catalog
    info = extract_product_catalog("/path/to/module_catalog.pdf")
    print(info["maker"], info["model"], info["output_w"])
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import anthropic

from config import CLAUDE_MODEL, get_api_key
from extraction.pdf_reader import pdf_to_images

logger = logging.getLogger(__name__)

# APIリトライ設定
MAX_RETRIES = 2
RETRY_DELAY_SEC = 2

# 同時に送信する最大画像枚数（PDFのページ数が多すぎる場合の安全弁）
MAX_PAGES = 6

# サムネイル切り出し時のサイズ
THUMBNAIL_MAX_PX = 800


def _build_extraction_prompt() -> str:
    """製品カタログ抽出専用プロンプトを生成する。

    モジュール / PCS / 蓄電池の3カテゴリを判別し、
    それぞれに必要な仕様項目を網羅したJSONを返すよう指示する。
    """
    return """あなたは太陽光発電関連製品（モジュール / パワーコンディショナ / 蓄電池）のカタログを読み取る専門家です。
画像/PDFから製品情報を抽出し、構造化されたJSONで返してください。

【製品タイプの判定】
- "module": 太陽光モジュール（ソーラーパネル）。仕様欄に「Pmax」「Vmp」「Imp」「Voc」「Isc」「変換効率」などが含まれる
- "pcs": パワーコンディショナ（パワコン、インバータ、PCS、Power Conditioner）。仕様欄に「定格出力」「kVA」「kW」「入力電圧」などがあり、太陽光発電用の交流変換装置
- "battery": 蓄電池（バッテリー、storage battery）。「kWh」「サイクル数」「リチウムイオン」などの記載がある
- "other": 上記いずれにも該当しない製品

【単位の統一】
- 寸法はすべて mm 単位。cm/m が記載されていたら mm に換算する
- 重量は kg 単位
- 出力（output_w）の意味:
  - module の場合: 1枚あたりの定格出力（W）。例: 660
  - pcs の場合: 定格出力（kVA換算した数値）。例: 9.9
  - battery の場合: 定格容量（kWh）。例: 9.8
- 不明・読み取り不可な値はすべて null にする

【電気特性】
- module: vmp（最大動作電圧V）, imp（最大動作電流A）, voc（開放電圧V）, isc（短絡電流A）, efficiency_pct（モジュール変換効率%）
- pcs: rated_input_v（定格入力電圧V）, rated_output_kva（定格出力kVA）, efficiency_pct（変換効率%）
- battery: rated_capacity_kwh, usable_capacity_kwh, cycle_life（サイクル寿命）

【保証】
- product_years: 製品保証年数（一般的にモジュールは10〜25年）
- output_years: 出力保証年数（モジュール: 通常25年）

【返却JSON形式】
必ず以下のJSONのみを返してください。説明文・前置き・マークダウンは一切含めないでください。

{
  "product_type": "module",
  "maker": "メーカー名",
  "model": "型式（主たる型番）",
  "model_aliases": ["関連型式やシリーズ名（任意・配列）"],
  "output_w": 660,
  "physical": {
    "length_mm": 2384,
    "width_mm": 1303,
    "thickness_mm": 35,
    "weight_kg": 33.5
  },
  "electrical": {
    "vmp": 38.5,
    "imp": 17.15,
    "voc": 46.0,
    "isc": 18.31,
    "efficiency_pct": 21.3
  },
  "warranty": {
    "product_years": 12,
    "output_years": 25
  },
  "extracted_warnings": ["読み取り不確実な箇所のメモ"],
  "raw_text_excerpt": "カタログから読み取れたキーワードを500文字以内で記載"
}

【注意事項】
- 読み取れない値は必ず null にしてください（ダミーの数値を入れてはいけません）
- product_type が "pcs" の場合、electrical の vmp/imp/voc/isc は null とし、
  rated_input_v / rated_output_kva / efficiency_pct のみ埋めてください
- product_type が "battery" の場合、electrical は rated_capacity_kwh / usable_capacity_kwh / cycle_life を含めて構いません
- model_aliases は同シリーズの他型式（例: CS7L-MS-660 / CS7L-MS-665 / CS7L-MS-670）が記載されていたら配列で返してください
- raw_text_excerpt にはカタログから抽出した重要キーワード（メーカー名・型式・主要スペック）を500文字以内で要約してください
- extracted_warnings にはOCRで自信が無い項目を日本語で記載してください
"""


def extract_product_catalog(pdf_or_image_path: str) -> dict:
    """カタログPDFや画像から製品情報を構造化抽出する。

    Args:
        pdf_or_image_path: PDFまたは画像（PNG/JPEG）ファイルパス

    Returns:
        dict: 製品情報（仕様参照: モジュールdocstring）
    """
    src_path = Path(pdf_or_image_path)
    warnings: list[str] = []

    if not src_path.exists():
        return _empty_result(warnings=[f"ファイルが存在しません: {pdf_or_image_path}"])

    # --- 画像 or PDF を判定して画像化 ---
    suffix = src_path.suffix.lower()
    pages: list[dict] = []
    catalog_image_path: Optional[str] = None

    try:
        if suffix == ".pdf":
            pages = pdf_to_images(str(src_path), dpi=200)
            if not pages:
                return _empty_result(warnings=[f"PDFからページを取得できませんでした: {src_path}"])
            if len(pages) > MAX_PAGES:
                warnings.append(
                    f"カタログPDFが{len(pages)}ページあるため、先頭{MAX_PAGES}ページのみ解析します"
                )
                pages = pages[:MAX_PAGES]
            # サムネイル抽出（先頭ページから）
            try:
                catalog_image_path = extract_catalog_thumbnail(str(src_path))
            except Exception as e:
                logger.debug(f"サムネイル抽出失敗（無視）: {e}")
        elif suffix in (".png", ".jpg", ".jpeg", ".webp"):
            with open(src_path, "rb") as f:
                img_bytes = f.read()
            media_type = "image/jpeg" if suffix in (".jpg", ".jpeg") else (
                "image/webp" if suffix == ".webp" else "image/png"
            )
            pages = [{
                "page": 1,
                "image_bytes": img_bytes,
                "image_base64": base64.standard_b64encode(img_bytes).decode("utf-8"),
                "media_type": media_type,
            }]
            catalog_image_path = str(src_path)
        else:
            return _empty_result(
                warnings=[f"対応していないファイル形式です: {suffix}（.pdf / .png / .jpg / .jpeg / .webp）"]
            )
    except Exception as e:
        logger.error(f"カタログ画像化エラー: {e}")
        return _empty_result(warnings=[f"カタログを画像化できませんでした: {e}"])

    # --- Vision API 呼び出し ---
    content: list[dict] = []
    for page in pages:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": page.get("media_type", "image/png"),
                "data": page["image_base64"],
            },
        })
    content.append({"type": "text", "text": _build_extraction_prompt()})

    last_error: Optional[Exception] = None
    raw: Optional[dict] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw = _call_claude_api(content, attempt)
            break
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            logger.warning(f"カタログJSON解析失敗 試行{attempt}: {e}")
        except anthropic.APIError as e:
            last_error = e
            logger.warning(f"カタログAPIエラー 試行{attempt}: {e}")
        except Exception as e:
            last_error = e
            logger.warning(f"カタログ抽出予期せぬエラー 試行{attempt}: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SEC * attempt)

    if raw is None:
        warnings.append(f"AI抽出に失敗しました: {last_error}")
        result = _empty_result(warnings=warnings)
        if catalog_image_path:
            result["catalog_image_path"] = catalog_image_path
        return result

    # --- 正規化 ---
    result = _normalize_extracted(raw)
    # 既存のwarningsと統合
    if warnings:
        result.setdefault("extracted_warnings", []).extend(warnings)
    if catalog_image_path:
        result["catalog_image_path"] = catalog_image_path

    return result


def extract_catalog_thumbnail(
    pdf_path: str,
    page: int = 0,
    output_path: Optional[str] = None,
) -> Optional[str]:
    """PDFの指定ページから製品写真と思しきサムネイルを切り出して保存する。

    厳密な物体検出は行わず、ページ画像をリサイズして保存する簡易実装。
    失敗してもエラーを投げず None を返す。

    Args:
        pdf_path: 対象PDFパス
        page: サムネイルを取り出すページ番号（0-origin、デフォルト0）
        output_path: 出力先パス。Noneなら knowledge/product_thumbnails/ に保存

    Returns:
        保存先パス（成功時） / None（失敗時）
    """
    try:
        import fitz  # PyMuPDF
        from PIL import Image

        src_path = Path(pdf_path)
        if not src_path.exists():
            return None

        doc = fitz.open(str(src_path))
        try:
            if len(doc) == 0:
                return None
            page_index = max(0, min(page, len(doc) - 1))
            pix = doc[page_index].get_pixmap(dpi=150)
            img_bytes = pix.tobytes("png")
        finally:
            doc.close()

        img = Image.open(io.BytesIO(img_bytes))
        # アスペクト比を保ちつつ THUMBNAIL_MAX_PX に収める
        img.thumbnail((THUMBNAIL_MAX_PX, THUMBNAIL_MAX_PX))

        if output_path is None:
            base_dir = Path(__file__).resolve().parent.parent / "knowledge" / "product_thumbnails"
            base_dir.mkdir(parents=True, exist_ok=True)
            safe_stem = re.sub(r"[^A-Za-z0-9_\-]+", "_", src_path.stem)[:80] or "catalog"
            output_path = str(base_dir / f"{safe_stem}.png")
        else:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        img.save(output_path, format="PNG", optimize=True)
        return output_path
    except Exception as e:
        logger.debug(f"サムネイル生成失敗: {e}")
        return None


# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------
def _call_claude_api(content: list[dict], attempt: int) -> dict:
    """Claude Vision API を呼び出してJSONレスポンスを返す。

    survey_extractor と同様に prefill "{" を使い、純粋なJSONのみを得る。
    """
    client = anthropic.Anthropic(api_key=get_api_key())
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        temperature=0.0,
        messages=[
            {"role": "user", "content": content},
            {"role": "assistant", "content": "{"},
        ],
    )
    response_text = response.content[0].text
    if not response_text.lstrip().startswith("{"):
        response_text = "{" + response_text
    logger.info(f"カタログAPI応答（試行{attempt}）: {response_text[:200]}...")
    json_str = _extract_json(response_text)
    return json.loads(json_str)


def _extract_json(text: str) -> str:
    """レスポンスからJSON部分を切り出す（最低限のサニタイズ付き）。"""
    # ```json ... ``` ブロック
    block = re.search(r"```json\s*\n?(.*?)```", text, re.DOTALL)
    if block:
        return _sanitize_json_str(block.group(1).strip())
    block = re.search(r"```\s*\n?(.*?)```", text, re.DOTALL)
    if block and block.group(1).strip().startswith("{"):
        return _sanitize_json_str(block.group(1).strip())

    text = text.strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("JSONが見つかりません")
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
                return _sanitize_json_str(text[start:i + 1])
    raise ValueError("JSONの末尾が見つかりません")


def _sanitize_json_str(s: str) -> str:
    """末尾カンマ・Python風リテラルなど軽微な不正を補正する。"""
    if not s:
        return s
    # 末尾カンマ
    s = re.sub(r",(\s*[\]}])", r"\1", s)
    # Python風リテラル
    s = re.sub(r"\bTrue\b", "true", s)
    s = re.sub(r"\bFalse\b", "false", s)
    s = re.sub(r"\bNone\b", "null", s)
    return s


def _safe_float(val) -> Optional[float]:
    """float化（不可ならNone）。単位文字列を除去して数値部分のみ取り出す。"""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        try:
            f = float(val)
            return f
        except Exception:
            return None
    if isinstance(val, str):
        s = val.strip()
        if not s or s.lower() in ("null", "none", "nan", "-", "—"):
            return None
        cleaned = re.sub(r"[^\d.\-]", "", s)
        if not cleaned or cleaned in ("-", ".", "-."):
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _safe_int(val) -> Optional[int]:
    f = _safe_float(val)
    if f is None:
        return None
    try:
        return int(round(f))
    except Exception:
        return None


def _safe_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    return str(val).strip()


_VALID_PRODUCT_TYPES = {"module", "pcs", "battery", "other"}


def _normalize_extracted(raw: dict) -> dict:
    """Claudeの生レスポンスを公開スキーマに正規化する。"""
    if not isinstance(raw, dict):
        raw = {}

    product_type = _safe_str(raw.get("product_type")).lower()
    if product_type not in _VALID_PRODUCT_TYPES:
        # よく出てくる別名を吸収
        synonyms = {
            "panel": "module",
            "solar": "module",
            "pv": "module",
            "inverter": "pcs",
            "powerconditioner": "pcs",
            "power_conditioner": "pcs",
            "storage": "battery",
            "ess": "battery",
            "lithium": "battery",
        }
        product_type = synonyms.get(product_type.replace(" ", "").replace("-", "_"), "other")

    physical_raw = raw.get("physical") or {}
    if not isinstance(physical_raw, dict):
        physical_raw = {}
    electrical_raw = raw.get("electrical") or {}
    if not isinstance(electrical_raw, dict):
        electrical_raw = {}
    warranty_raw = raw.get("warranty") or {}
    if not isinstance(warranty_raw, dict):
        warranty_raw = {}

    # model_aliases
    aliases_raw = raw.get("model_aliases") or []
    if isinstance(aliases_raw, str):
        aliases_raw = [aliases_raw]
    if not isinstance(aliases_raw, list):
        aliases_raw = []
    aliases = [_safe_str(a) for a in aliases_raw if _safe_str(a)]

    warnings_raw = raw.get("extracted_warnings") or []
    if isinstance(warnings_raw, str):
        warnings_raw = [warnings_raw]
    if not isinstance(warnings_raw, list):
        warnings_raw = []
    warnings = [_safe_str(w) for w in warnings_raw if _safe_str(w)]

    raw_excerpt = _safe_str(raw.get("raw_text_excerpt"))
    if len(raw_excerpt) > 500:
        raw_excerpt = raw_excerpt[:500]

    # electrical はキーが製品タイプによって変わる可能性があるため辞書ごと持つ
    electrical: dict = {}
    if product_type == "module":
        electrical = {
            "vmp": _safe_float(electrical_raw.get("vmp")),
            "imp": _safe_float(electrical_raw.get("imp")),
            "voc": _safe_float(electrical_raw.get("voc")),
            "isc": _safe_float(electrical_raw.get("isc")),
            "efficiency_pct": _safe_float(electrical_raw.get("efficiency_pct")),
        }
    elif product_type == "pcs":
        electrical = {
            "rated_input_v": _safe_float(electrical_raw.get("rated_input_v")),
            "rated_output_kva": _safe_float(electrical_raw.get("rated_output_kva")),
            "efficiency_pct": _safe_float(electrical_raw.get("efficiency_pct")),
        }
    elif product_type == "battery":
        electrical = {
            "rated_capacity_kwh": _safe_float(electrical_raw.get("rated_capacity_kwh")),
            "usable_capacity_kwh": _safe_float(electrical_raw.get("usable_capacity_kwh")),
            "cycle_life": _safe_int(electrical_raw.get("cycle_life")),
        }
    else:
        # other: 渡された値をfloat化して持つだけ
        electrical = {k: _safe_float(v) for k, v in electrical_raw.items()}

    return {
        "product_type": product_type,
        "maker": _safe_str(raw.get("maker")),
        "model": _safe_str(raw.get("model")),
        "model_aliases": aliases,
        "output_w": _safe_float(raw.get("output_w")),
        "physical": {
            "length_mm": _safe_float(physical_raw.get("length_mm")),
            "width_mm": _safe_float(physical_raw.get("width_mm")),
            "thickness_mm": _safe_float(physical_raw.get("thickness_mm")),
            "weight_kg": _safe_float(physical_raw.get("weight_kg")),
        },
        "electrical": electrical,
        "warranty": {
            "product_years": _safe_int(warranty_raw.get("product_years")),
            "output_years": _safe_int(warranty_raw.get("output_years")),
        },
        "catalog_image_path": _safe_str(raw.get("catalog_image_path")) or "",
        "extracted_warnings": warnings,
        "raw_text_excerpt": raw_excerpt,
    }


def _empty_result(warnings: Optional[list[str]] = None) -> dict:
    """抽出失敗時の空レスポンス。"""
    return {
        "product_type": "other",
        "maker": "",
        "model": "",
        "model_aliases": [],
        "output_w": None,
        "physical": {
            "length_mm": None,
            "width_mm": None,
            "thickness_mm": None,
            "weight_kg": None,
        },
        "electrical": {},
        "warranty": {"product_years": None, "output_years": None},
        "catalog_image_path": "",
        "extracted_warnings": list(warnings or []),
        "raw_text_excerpt": "",
    }


if __name__ == "__main__":
    # API呼び出しは行わず、_normalize_extracted のサニタイズロジックだけ確認する
    sample = {
        "product_type": "MODULE",
        "maker": "  Canadian Solar  ",
        "model": "CS7L-MS",
        "model_aliases": "CS7L-MS-660",
        "output_w": "660W",
        "physical": {
            "length_mm": "2384",
            "width_mm": "1303 mm",
            "thickness_mm": "35",
            "weight_kg": "33.5kg",
        },
        "electrical": {
            "vmp": "38.5V",
            "imp": 17.15,
            "voc": "46.0",
            "isc": "18.31",
            "efficiency_pct": "21.3%",
        },
        "warranty": {"product_years": "12年", "output_years": "25年"},
        "extracted_warnings": ["サンプル"],
        "raw_text_excerpt": "Canadian Solar CS7L-MS 660W ...",
    }
    out = _normalize_extracted(sample)
    print(json.dumps(out, ensure_ascii=False, indent=2))
