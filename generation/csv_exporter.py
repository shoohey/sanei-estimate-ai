"""見積データをCSV形式でエクスポートするモジュール

EstimateData を Excel/Numbers/Googleスプレッドシートで文字化けせず開ける
UTF-8 with BOM 形式の CSV バイト列に変換する。

公開関数:
    - export_estimate_to_csv(estimate)         : シンプル版（明細のみ）
    - export_estimate_to_csv_detailed(estimate): 詳細版（表紙情報・明細・集計・根拠を網羅）

使用例:
    >>> from generation.csv_exporter import export_estimate_to_csv_detailed
    >>> csv_bytes = export_estimate_to_csv_detailed(estimate)
    >>> st.download_button(
    ...     "📊 内訳CSV ダウンロード",
    ...     data=csv_bytes,
    ...     file_name="見積_内訳.csv",
    ...     mime="text/csv",
    ... )
"""
from __future__ import annotations

import csv
import io

from models.estimate_data import EstimateData, CategoryType, PricingMethod


# UTF-8 BOM (Excel が UTF-8 CSV を正しく日本語表示するために必要)
_BOM = "﻿"

# 計算方法の英語Enum値 → 日本語ラベル
_PRICING_METHOD_LABELS: dict[str, str] = {
    PricingMethod.KW_RATE.value: "kW単価",
    PricingMethod.FIXED.value: "固定",
    PricingMethod.CONDITIONAL.value: "条件付き",
    PricingMethod.DISTANCE.value: "距離連動",
    PricingMethod.MANUAL.value: "手動入力",
    PricingMethod.SUPPLIED.value: "支給品",
}


def _pricing_method_label(method: PricingMethod | str | None) -> str:
    """PricingMethod を日本語ラベルに変換"""
    if method is None:
        return ""
    value = method.value if isinstance(method, PricingMethod) else str(method)
    return _PRICING_METHOD_LABELS.get(value, value)


def _make_writer(buffer: io.StringIO) -> "csv.writer":
    """Excel互換のCSV writer を生成（CRLF区切り、最小クォート）"""
    return csv.writer(
        buffer,
        delimiter=",",
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\r\n",
    )


def _to_bytes(buffer: io.StringIO) -> bytes:
    """BOM付きでUTF-8エンコードしたbytesを返す"""
    return (_BOM + buffer.getvalue()).encode("utf-8")


# ---------------------------------------------------------------------------
# シンプル版
# ---------------------------------------------------------------------------
def export_estimate_to_csv(estimate: EstimateData) -> bytes:
    """EstimateData を シンプルなCSVバイト列に変換する。

    明細行だけを並べた最小構成のCSV。Excelで開いてサクッと中身を確認したい時に使う。

    Args:
        estimate: 見積データ。

    Returns:
        UTF-8 with BOM でエンコードされたCSVのbytes。
    """
    buffer = io.StringIO()
    writer = _make_writer(buffer)

    # ヘッダー行
    writer.writerow([
        "カテゴリ", "No", "摘要", "備考", "数量", "単価", "金額",
    ])

    # 明細行
    for category in estimate.summary.categories:
        cat_label = f"{category.category_number}. {category.category.value}"
        for item in category.items:
            writer.writerow([
                cat_label,
                item.no,
                item.description,
                item.remarks,
                item.quantity,
                item.unit_price,
                item.amount,
            ])

    # 集計行
    writer.writerow([])
    writer.writerow(["小計", "", "", "", "", "", estimate.summary.subtotal])
    writer.writerow(["お値引き", "", "", "", "", "", estimate.summary.discount])
    writer.writerow(["税抜合計", "", "", "", "", "", estimate.summary.total_before_tax])
    writer.writerow(["消費税(10%)", "", "", "", "", "", estimate.summary.tax])
    writer.writerow(["税込合計", "", "", "", "", "", estimate.summary.total_with_tax])

    return _to_bytes(buffer)


# ---------------------------------------------------------------------------
# 詳細版（デフォルト）
# ---------------------------------------------------------------------------
def export_estimate_to_csv_detailed(estimate: EstimateData) -> bytes:
    """EstimateData を 詳細CSVバイト列に変換する。

    見積書情報・明細(全カテゴリ・全行)・集計・見積根拠一覧 を1ファイルにまとめた
    Excel/Numbers/Googleスプレッドシートで文字化けせずに開けるCSVを生成する。

    Args:
        estimate: 見積データ。

    Returns:
        UTF-8 with BOM でエンコードされたCSVのbytes。
    """
    buffer = io.StringIO()
    writer = _make_writer(buffer)

    cover = estimate.cover

    # === 見積書情報 ===
    writer.writerow(["=== 見積書情報 ==="])
    writer.writerow(["見積ID", cover.estimate_id])
    writer.writerow(["工事名", cover.project_name])
    client_display = f"{cover.client_name} 御中" if cover.client_name else ""
    writer.writerow(["宛先", client_display])
    writer.writerow(["工事場所", cover.project_location])
    writer.writerow(["発行日", cover.issue_date])
    writer.writerow(["有効期限", cover.validity_period])
    writer.writerow(["担当者", cover.representative])
    writer.writerow([])

    # === 見積明細 ===
    writer.writerow(["=== 見積明細 ==="])
    writer.writerow([
        "カテゴリ", "No", "摘要", "備考", "数量", "単価", "金額",
        "計算方法", "計算式", "根拠元", "補足",
    ])

    for category in estimate.summary.categories:
        cat_label = f"{category.category_number}. {category.category.value}"
        for item in category.items:
            reasoning = item.reasoning
            if reasoning is not None:
                method_label = _pricing_method_label(reasoning.method)
                formula = reasoning.formula
                source = reasoning.source
                note = reasoning.note
            else:
                method_label = ""
                formula = ""
                source = ""
                note = ""

            writer.writerow([
                cat_label,
                item.no,
                item.description,
                item.remarks,
                item.quantity,
                item.unit_price,
                item.amount,
                method_label,
                formula,
                source,
                note,
            ])
    writer.writerow([])

    # === 集計 ===
    writer.writerow(["=== 集計 ==="])
    writer.writerow(["小計", estimate.summary.subtotal])
    writer.writerow(["お値引き", estimate.summary.discount])
    writer.writerow(["税抜合計", estimate.summary.total_before_tax])
    writer.writerow(["消費税(10%)", estimate.summary.tax])
    writer.writerow(["税込合計", estimate.summary.total_with_tax])
    writer.writerow([])

    # === 見積根拠一覧 ===
    writer.writerow(["=== 見積根拠一覧 ==="])
    for idx, reasoning_text in enumerate(estimate.reasoning_list, start=1):
        writer.writerow([idx, reasoning_text])

    return _to_bytes(buffer)


# ---------------------------------------------------------------------------
# 動作確認用
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from models.estimate_data import (
        EstimateCover,
        EstimateSummary,
        CategorySection,
        LineItem,
        LineItemReasoning,
    )

    sample = EstimateData(
        cover=EstimateCover(
            estimate_id="EST-2026-0001",
            issue_date="2026-04-28",
            client_name="株式会社サンプル",
            project_name="サンプル太陽光発電所 設置工事",
            project_location="東京都千代田区サンプル1-1-1",
            validity_period="発行日より30日間",
            representative="根本　雄介",
        ),
        summary=EstimateSummary(
            categories=[
                CategorySection(
                    category=CategoryType.SUPPLIED,
                    category_number=1,
                    items=[
                        LineItem(
                            no=1,
                            description="太陽光パネル 540W",
                            remarks="国内メーカー",
                            quantity="288枚",
                            unit_price=0,
                            amount=0,
                            reasoning=LineItemReasoning(
                                method=PricingMethod.SUPPLIED,
                                formula="-",
                                source="現調シート",
                                note="お客様支給品",
                            ),
                        ),
                    ],
                    subtotal=0,
                    total=0,
                ),
                CategorySection(
                    category=CategoryType.MATERIAL,
                    category_number=2,
                    items=[
                        LineItem(
                            no=1,
                            description="架台一式",
                            remarks="陸屋根用",
                            quantity="1式",
                            unit_price=5_000_000,
                            amount=5_000_000,
                            reasoning=LineItemReasoning(
                                method=PricingMethod.KW_RATE,
                                formula="155.52kW × ¥32,150/kW ≒ ¥5,000,000",
                                source="価格マスタ M-001",
                                note="陸屋根設置の標準単価",
                            ),
                        ),
                    ],
                    subtotal=5_000_000,
                    total=5_000_000,
                ),
                CategorySection(
                    category=CategoryType.CONSTRUCTION,
                    category_number=3,
                    items=[
                        LineItem(
                            no=1,
                            description="据付工事 一式",
                            remarks="",
                            quantity="1式",
                            unit_price=3_000_000,
                            amount=3_000_000,
                            reasoning=LineItemReasoning(
                                method=PricingMethod.FIXED,
                                formula="一式 ¥3,000,000",
                                source="標準工事費",
                                note="",
                            ),
                        ),
                    ],
                    subtotal=3_000_000,
                    total=3_000_000,
                ),
            ],
            subtotal=8_000_000,
            discount=-200_000,
            total_before_tax=7_800_000,
            tax=780_000,
            total_with_tax=8_580_000,
        ),
        reasoning_list=[
            "太陽光パネル: お客様支給品のため金額0円",
            "架台一式: 155.52kW × ¥32,150/kW ≒ ¥5,000,000（陸屋根用標準単価）",
            "据付工事: 一式 ¥3,000,000（標準工事費）",
            "お値引き: ¥-200,000（端数調整）",
        ],
    )

    csv_bytes = export_estimate_to_csv_detailed(sample)

    output_path = "/tmp/test.csv"
    with open(output_path, "wb") as fp:
        fp.write(csv_bytes)
    print(f"[OK] CSV を書き出しました: {output_path} ({len(csv_bytes)} bytes)\n")

    text = csv_bytes.decode("utf-8-sig")
    print("--- 先頭20行 ---")
    for line in text.splitlines()[:20]:
        print(line)
