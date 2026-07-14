"""差分学習ループのデータモデル（契約）

このファイルの型は estimate_parser / estimate_diff / drawing_diff / app_pages の
共通契約。フィールドの追加は可、変更・削除は不可。
"""
from typing import Optional
from pydantic import BaseModel, Field

# 見積書の6カテゴリ（models/estimate_data.py の CategoryType と同じ表記）
ESTIMATE_CATEGORIES = [
    "支給品",
    "材料費",
    "施工費",
    "その他・諸経費等",
    "付帯工事",
    "特記事項",
]


class ParsedLineItem(BaseModel):
    """パース済み見積明細行（AI/正規 共通）"""
    category: str = Field(default="", description="6カテゴリ名のいずれか。不明は空")
    no: int = Field(default=0, description="行番号")
    description: str = Field(default="", description="摘要")
    remarks: str = Field(default="", description="備考")
    quantity_value: Optional[float] = Field(default=None, description="数量の数値")
    quantity_unit: str = Field(default="", description="数量の単位")
    unit_price: Optional[int] = Field(default=None, description="単価（円）")
    amount: Optional[int] = Field(default=None, description="金額（円）")


class ParsedEstimate(BaseModel):
    """パース済み見積書（AI見積・正規見積 共通の中間表現）"""
    source: str = Field(default="", description='"ai" | "official"')
    origin: str = Field(default="", description='"history" | "csv" | "pdf"')
    file_name: str = Field(default="")
    estimate_id: str = Field(default="")
    client_name: str = Field(default="")
    project_name: str = Field(default="")
    issue_date: str = Field(default="")
    items: list[ParsedLineItem] = Field(default_factory=list)
    subtotal: Optional[int] = Field(default=None, description="小計（税抜・値引前）")
    discount: Optional[int] = Field(default=None, description="値引き（負値）")
    total_before_tax: Optional[int] = Field(default=None)
    tax: Optional[int] = Field(default=None)
    total_with_tax: Optional[int] = Field(default=None)
    warnings: list[str] = Field(default_factory=list)


class EstimateDiffItem(BaseModel):
    """見積差分1件。承認されたら proposed_rule が store に入る。"""
    diff_type: str = Field(
        description='"price_changed"|"quantity_changed"|"item_added"|"item_removed"')
    category: str = Field(default="")
    description: str = Field(default="", description="対象項目の摘要")
    ai_item: Optional[ParsedLineItem] = Field(default=None)
    official_item: Optional[ParsedLineItem] = Field(default=None)
    match_score: float = Field(default=0.0)
    summary: str = Field(default="", description='人間向け説明 例: "単価 ¥3,300 → ¥3,100"')
    learnable: bool = Field(default=True, description="False は参考表示のみ（数量差分など）")
    proposed_rule: Optional[dict] = Field(default=None, description="承認時に store.add_rules へ渡す")


class DrawingDiffItem(BaseModel):
    """図面スペック差分1件。"""
    diff_type: str = Field(
        description='"gap_changed"|"margin_changed"|"orientation_changed"|"panel_count_changed"|'
                    '"string_config_changed"|"mount_type_changed"|"panel_spec_changed"|'
                    '"face_dimension_changed"|"golden_example"')
    target: str = Field(default="", description="対象（面名など）")
    ai_value: str = Field(default="")
    official_value: str = Field(default="")
    summary: str = Field(default="")
    learnable: bool = Field(default=True)
    proposed_rule: Optional[dict] = Field(default=None)
