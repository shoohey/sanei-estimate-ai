"""音声コマンド解析モジュール

ユーザーの自然言語指示（音声から文字起こしされたテキスト）を Claude API で
構造化コマンドのリストに変換する。

サポートするアクション:
- update_unit_price / update_quantity / update_amount
- update_description / update_remarks
- delete_item / add_item
- set_discount / set_client_name / set_project_name / set_validity_period
- unknown (解釈できない場合)

使い方:
    from voice.voice_command_parser import parse_voice_command
    commands = parse_voice_command("太陽光パネルの単価を5万円にして", estimate)
    # → [{"action": "update_unit_price", "category": "支給品",
    #     "item_match": "太陽光パネル", "new_value": 50000, "reason": "..."}]
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import anthropic

from models.estimate_data import EstimateData
from config import get_api_key, CLAUDE_MODEL

logger = logging.getLogger(__name__)

# Claude API設定
MAX_TOKENS = 2048
TEMPERATURE = 0.0

# サポートするアクション一覧（プロンプトと検証で共有）
SUPPORTED_ACTIONS = [
    "update_unit_price",
    "update_quantity",
    "update_amount",
    "update_description",
    "update_remarks",
    "delete_item",
    "add_item",
    "set_discount",
    "set_client_name",
    "set_project_name",
    "set_validity_period",
    "unknown",
]

# サポートするカテゴリ名
SUPPORTED_CATEGORIES = [
    "支給品",
    "材料費",
    "施工費",
    "その他・諸経費等",
    "付帯工事",
    "特記事項",
]


def parse_voice_command(text: str, estimate: EstimateData) -> list[dict]:
    """自然言語テキストを構造化コマンドのリストに変換する

    Args:
        text: ユーザーの自然言語指示（例: "太陽光パネルの単価を5万円にして"）
        estimate: 現在の見積データ（item_match を正確にするための参照）

    Returns:
        構造化コマンドのリスト。各要素は dict で以下のキーを持つ:
        - action: アクション名（SUPPORTED_ACTIONS の値）
        - category: カテゴリ名（該当する場合）
        - item_match: 明細項目の部分一致文字列（該当する場合）
        - new_value: 新しい値（該当する場合）
        - new_unit: 数量変更時の単位（update_quantity の場合のみ）
        - reason: コマンドの理由・要約

        解釈できない場合は [{"action": "unknown", "reason": "解釈できませんでした"}] を返す。
    """
    if not text or not text.strip():
        return [{"action": "unknown", "reason": "解釈できませんでした（入力テキストが空です）"}]

    estimate_summary = _summarize_estimate(estimate)
    prompt = _build_command_extraction_prompt(text, estimate_summary)

    try:
        client = anthropic.Anthropic(api_key=get_api_key())
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            messages=[
                {"role": "user", "content": prompt},
                # Prefill "[" で配列のみを返すよう誘導
                {"role": "assistant", "content": "["},
            ],
        )
        raw_text = response.content[0].text
        if not raw_text.lstrip().startswith("["):
            raw_text = "[" + raw_text

        json_str = _extract_json_array(raw_text)
        commands = json.loads(json_str)

        if not isinstance(commands, list):
            logger.warning(f"Claude API応答が配列ではありません: {type(commands)}")
            return [{"action": "unknown", "reason": "解釈できませんでした（応答形式が不正）"}]

        validated = _validate_commands(commands)
        if not validated:
            return [{"action": "unknown", "reason": "解釈できませんでした"}]
        return validated

    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"音声コマンドのJSON解析エラー: {e}")
        return [{"action": "unknown", "reason": f"解釈できませんでした（JSON解析エラー: {e}）"}]
    except anthropic.APIError as e:
        logger.warning(f"Claude APIエラー: {e}")
        return [{"action": "unknown", "reason": f"解釈できませんでした（APIエラー: {e}）"}]
    except Exception as e:
        logger.warning(f"音声コマンド解析の予期せぬエラー: {e}")
        return [{"action": "unknown", "reason": f"解釈できませんでした（{e}）"}]


def _build_command_extraction_prompt(text: str, estimate_summary: dict) -> str:
    """Claude API用のプロンプトを構築する

    Args:
        text: ユーザーの自然言語指示
        estimate_summary: EstimateData をflatにしたカテゴリ・項目一覧

    Returns:
        Claude API に渡すプロンプト文字列
    """
    # 現在の見積概要をテキスト化
    summary_lines = []
    cover = estimate_summary.get("cover", {})
    if cover:
        summary_lines.append("【見積基本情報】")
        if cover.get("client_name"):
            summary_lines.append(f"- 宛先会社名: {cover.get('client_name')}")
        if cover.get("project_name"):
            summary_lines.append(f"- 工事名: {cover.get('project_name')}")
        if cover.get("validity_period"):
            summary_lines.append(f"- 有効期限: {cover.get('validity_period')}")
        summary_lines.append("")

    summary_lines.append("【現在の見積明細一覧】")
    for cat in estimate_summary.get("categories", []):
        cat_name = cat.get("category", "")
        items = cat.get("items", [])
        summary_lines.append(f"■ {cat_name}")
        if not items:
            summary_lines.append("  (明細なし)")
        for item in items:
            no = item.get("no", "")
            desc = item.get("description", "")
            qty = item.get("quantity", "")
            up = item.get("unit_price", 0)
            amt = item.get("amount", 0)
            summary_lines.append(
                f"  {no}. {desc} | 数量:{qty} | 単価:¥{up:,} | 金額:¥{amt:,}"
            )
        summary_lines.append("")

    discount = estimate_summary.get("discount", 0)
    if discount:
        summary_lines.append(f"お値引き: ¥{discount:,}")

    summary_text = "\n".join(summary_lines) if summary_lines else "(見積データなし)"

    actions_desc = """
【サポートするアクション一覧】
- update_unit_price: 特定明細の単価を変更
  必須fields: category, item_match, new_value (整数)
- update_quantity: 特定明細の数量を変更
  必須fields: category, item_match, new_value (数値)
  任意fields: new_unit (文字列、例: "枚", "式", "m")
- update_amount: 特定明細の金額を直接変更
  必須fields: category, item_match, new_value (整数)
- update_description: 摘要文を変更
  必須fields: category, item_match, new_value (文字列)
- update_remarks: 備考を変更
  必須fields: category, item_match, new_value (文字列)
- delete_item: 明細を削除
  必須fields: category, item_match
- add_item: 明細を追加
  必須fields: category, description (文字列), quantity (文字列), unit_price (整数), amount (整数)
- set_discount: 値引き額を設定（負の値で渡す）
  必須fields: new_value (整数、通常は負の値)
- set_client_name: 宛先会社名を変更
  必須fields: new_value (文字列)
- set_project_name: 工事名を変更
  必須fields: new_value (文字列)
- set_validity_period: 有効期限を変更
  必須fields: new_value (文字列)
- unknown: 解釈できない場合
  必須fields: reason ("解釈できませんでした" を入れる)
"""

    rules = """
【重要なルール】
1. 必ず JSON 配列のみを返してください。前置き・説明文・コメントは不要です。
2. 単一指示でも複数指示でも、複数のコマンドが含まれていれば配列に複数の要素を入れてください。
3. category は以下のいずれかの完全一致または前方一致を採用してください:
   "支給品" / "材料費" / "施工費" / "その他・諸経費等" / "付帯工事" / "特記事項"
4. item_match は明細の摘要(description)に対する部分一致文字列です。
   例: 摘要が「太陽光パネル 540W 単結晶」なら item_match: "太陽光パネル" でも一致します。
   見積の【現在の見積明細一覧】を参照し、できるだけ既存項目と一致する単語を選んでください。
5. 金額・数値の表記ゆれを正しく解釈してください:
   - 「5万円」「ご万円」「五万円」 → 50000
   - 「ななまんごせんえん」「7万5千円」「七万五千円」 → 75000
   - 「3千円」「三千円」「サンゼンエン」 → 3000
   - 「100円」 → 100
   - 「3百万」「三百万円」 → 3000000
   - 「マイナス5万」「-50000」「5万円引き」 → -50000
6. 値引きは負の値で表現します。「お値引きを5万円に設定」→ new_value: -50000
   「値引きをなしに」「値引きをゼロに」→ new_value: 0
7. 「上げて」「下げて」のような相対指示の場合、現在値+/-指定額で計算してください。
   例: 現在単価60000円のものを「3千円下げて」→ new_value: 57000
8. 解釈できない指示や、対象が見積に見つからない場合は、unknown アクションを返してください。
9. 各コマンドには必ず reason フィールドを入れて、コマンドの内容を日本語で要約してください。
   例: "支給品の太陽光パネル単価を5万円に変更"
"""

    output_format = """
【出力フォーマット例】
[
  {"action": "update_unit_price", "category": "支給品", "item_match": "太陽光パネル", "new_value": 50000, "reason": "支給品の太陽光パネル単価を5万円に変更"},
  {"action": "delete_item", "category": "材料費", "item_match": "ケーブルラック", "reason": "ケーブルラックを削除"},
  {"action": "set_discount", "new_value": -50000, "reason": "お値引きを5万円に設定"}
]
"""

    prompt = f"""あなたは太陽光発電の見積編集アシスタントです。
ユーザーの自然言語指示を解析し、構造化コマンドのJSON配列に変換してください。

{summary_text}

{actions_desc}

{rules}

{output_format}

【ユーザーの指示】
{text}

上記の指示を構造化コマンドのJSON配列に変換し、配列のみを返してください。
"""
    return prompt


def _summarize_estimate(estimate: EstimateData) -> dict:
    """EstimateData をプロンプト用の辞書に変換する"""
    summary: dict[str, Any] = {
        "cover": {
            "client_name": estimate.cover.client_name,
            "project_name": estimate.cover.project_name,
            "validity_period": estimate.cover.validity_period,
        },
        "categories": [],
        "discount": estimate.summary.discount,
    }
    for cat in estimate.summary.categories:
        cat_dict = {
            "category": cat.category.value if hasattr(cat.category, "value") else str(cat.category),
            "items": [
                {
                    "no": item.no,
                    "description": item.description,
                    "remarks": item.remarks,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                    "amount": item.amount,
                }
                for item in cat.items
            ],
        }
        summary["categories"].append(cat_dict)
    return summary


def _extract_json_array(text: str) -> str:
    """レスポンステキストから JSON 配列を抽出する"""
    # ```json ... ``` ブロック対応
    block = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if block:
        candidate = block.group(1).strip()
        if candidate.startswith("["):
            return candidate

    # 最初の [ から対応する ] までを抽出
    text = text.strip()
    start = text.find("[")
    if start < 0:
        raise ValueError(f"JSON配列が見つかりません: {text[:200]}")

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
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    raise ValueError(f"JSON配列の終端が見つかりません: {text[:200]}")


def _validate_commands(commands: list) -> list[dict]:
    """コマンドリストを検証し、無効なコマンドを除外する"""
    validated: list[dict] = []
    for cmd in commands:
        if not isinstance(cmd, dict):
            continue
        action = cmd.get("action")
        if not action or action not in SUPPORTED_ACTIONS:
            # 不明なアクションは unknown に変換
            validated.append({
                "action": "unknown",
                "reason": cmd.get("reason", "解釈できませんでした（未対応のアクション）"),
            })
            continue
        # reason が無い場合はデフォルトを補う
        if "reason" not in cmd or not cmd.get("reason"):
            cmd["reason"] = action
        validated.append(cmd)
    return validated


if __name__ == "__main__":
    # 動作確認: API呼び出しなしでプロンプト生成のみテスト
    from models.estimate_data import (
        EstimateData, EstimateCover, EstimateSummary,
        CategorySection, LineItem, CategoryType,
    )

    sample = EstimateData(
        cover=EstimateCover(
            client_name="株式会社サンプル",
            project_name="太陽光発電設備設置工事",
            validity_period="発行日より60日間",
        ),
        summary=EstimateSummary(
            categories=[
                CategorySection(
                    category=CategoryType.SUPPLIED,
                    category_number=1,
                    items=[
                        LineItem(no=1, description="太陽光パネル 540W 単結晶",
                                 quantity="288枚", unit_price=0, amount=0),
                    ],
                ),
                CategorySection(
                    category=CategoryType.MATERIAL,
                    category_number=2,
                    items=[
                        LineItem(no=1, description="ケーブルラック",
                                 quantity="30m", unit_price=5000, amount=150000),
                    ],
                ),
            ],
            discount=0,
        ),
    )

    prompt = _build_command_extraction_prompt(
        "太陽光パネルの単価を5万円にして、ケーブルラックを削除して、お値引きを5万円に設定",
        _summarize_estimate(sample),
    )
    print("===== プロンプト生成テスト =====")
    print(prompt[:1500])
    print("...(以下略)...")
    print()
    print("===== サポートアクション =====")
    for a in SUPPORTED_ACTIONS:
        print(f"  - {a}")
