"""見積書の全構造をモデル化"""
from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class CategoryType(str, Enum):
    SUPPLIED = "支給品"
    MATERIAL = "材料費"
    CONSTRUCTION = "施工費"
    OVERHEAD = "その他・諸経費等"
    ADDITIONAL = "付帯工事"
    SPECIAL_NOTES = "特記事項"


class PricingMethod(str, Enum):
    """価格計算方法"""
    KW_RATE = "kw_rate"            # kW単価
    FIXED = "fixed"                 # 固定額
    CONDITIONAL = "conditional"     # 条件付き
    DISTANCE = "distance"           # 距離連動
    MANUAL = "manual"               # 手動入力
    SUPPLIED = "supplied"           # 支給品（金額0）


class LineItemReasoning(BaseModel):
    """見積項目の根拠"""
    method: PricingMethod = Field(description="計算方法")
    formula: str = Field(default="", description="計算式（例: 190.08kW × ¥3,300/kW）")
    source: str = Field(default="", description="根拠元（例: 現調シート PV容量より）")
    note: str = Field(default="", description="補足説明")


class LineItem(BaseModel):
    """見積明細行"""
    no: int = Field(description="行番号")
    description: str = Field(description="摘要")
    remarks: str = Field(default="", description="備考")
    quantity: str = Field(default="", description="数量（例: 1式, 288枚, 30m）")
    quantity_value: float = Field(default=0, description="数量の数値部分")
    quantity_unit: str = Field(default="", description="数量の単位部分")
    unit_price: int = Field(default=0, description="見積単価")
    amount: int = Field(default=0, description="見積額")
    reasoning: Optional[LineItemReasoning] = Field(default=None, description="根拠")
    is_manual_input: bool = Field(default=False, description="手動入力が必要か")


class CategorySection(BaseModel):
    """カテゴリセクション"""
    category: CategoryType = Field(description="カテゴリ種別")
    category_number: int = Field(description="カテゴリ番号 (1-6)")
    items: list[LineItem] = Field(default_factory=list, description="明細行リスト")
    subtotal: int = Field(default=0, description="小計")
    total: int = Field(default=0, description="合計")

    def calculate_totals(self):
        """小計・合計を再計算"""
        self.subtotal = sum(item.amount for item in self.items)
        self.total = self.subtotal


class EstimateSummary(BaseModel):
    """見積内訳書（サマリ）"""
    categories: list[CategorySection] = Field(default_factory=list)
    subtotal: int = Field(default=0, description="小計（6カテゴリ合計）")
    discount: int = Field(default=0, description="お値引き")
    total_before_tax: int = Field(default=0, description="税抜合計")
    tax: int = Field(default=0, description="消費税")
    total_with_tax: int = Field(default=0, description="税込合計")

    def calculate_totals(self):
        """全体合計を再計算"""
        self.subtotal = sum(cat.total for cat in self.categories)
        self.total_before_tax = self.subtotal + self.discount  # discountは負の値
        self.tax = int(self.total_before_tax * 0.10)
        self.total_with_tax = self.total_before_tax + self.tax


class EstimateCover(BaseModel):
    """見積書表紙情報"""
    estimate_id: str = Field(default="", description="見積ID")
    issue_date: str = Field(default="", description="発行日")
    client_name: str = Field(default="", description="宛先会社名")
    project_name: str = Field(default="", description="工事名")
    project_location: str = Field(default="", description="工事場所")
    project_period: str = Field(default="～", description="工事期間")
    validity_period: str = Field(default="", description="有効期限")
    notes: str = Field(default="", description="備考")
    representative: str = Field(default="根本　雄介", description="担当者")
    total_with_tax: int = Field(default=0, description="御見積金額（税込）")
    total_before_tax: int = Field(default=0, description="税抜合計")
    tax: int = Field(default=0, description="消費税")


class EstimateData(BaseModel):
    """見積書全体のデータモデル"""
    cover: EstimateCover = Field(default_factory=EstimateCover)
    summary: EstimateSummary = Field(default_factory=EstimateSummary)
    reasoning_list: list[str] = Field(default_factory=list, description="全根拠テキスト一覧")
