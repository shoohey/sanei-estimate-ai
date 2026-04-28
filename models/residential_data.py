"""住宅(低圧/屋根上)向け現調シートのPydanticモデル定義

法人(高圧)向けの models/survey_data.py と対をなす、住宅向け現調シートの
データモデル。住宅と法人で現調シートが大きく異なるため、PDF読み取り段階で
カテゴリを判定して別スキーマで処理する。

UnifiedSurveyData をラッパーとして用意し、上位レイヤでは住宅・法人を
統一的に扱えるようにする。
"""
from pydantic import BaseModel, Field
from typing import Optional, Union
from enum import Enum

# 既存の法人(高圧)向けモデル/列挙型を再利用
from models.survey_data import ConfidenceLevel, SurveyData


class RoofType(str, Enum):
    """屋根形状"""
    GABLE = "切妻"
    HIP = "寄棟"
    SHED = "片流れ"
    FLAT = "陸屋根"
    GAMBREL = "方形"
    COMPLEX = "複雑形状"
    OTHER = "その他"


class RoofMaterial(str, Enum):
    """屋根材"""
    SLATE = "スレート"
    TILE = "瓦"
    METAL = "金属屋根"
    OTHER = "その他"


class MountingType(str, Enum):
    """架台取り付け方式"""
    ANCHOR = "アンカー"
    CLAMP = "掴み"
    HOLD = "挟み込み"
    OTHER = "その他"


class BatteryUse(str, Enum):
    """蓄電池の使用方式"""
    NONE = "なし"
    HYBRID = "ハイブリッド"
    STANDALONE = "単機能"


class ResidentialProjectInfo(BaseModel):
    """住宅向け案件基本情報（施主・住所・担当者）"""
    customer_name: str = Field(default="", description="施主名（個人名、敬称除去）")
    customer_kana: str = Field(default="", description="施主名 ふりがな")
    phone: str = Field(default="", description="連絡先電話番号")
    address: str = Field(default="", description="設置先住所")
    postal_code: str = Field(default="", description="郵便番号")
    survey_date: str = Field(default="", description="調査日 YYYY/MM/DD")
    surveyor: str = Field(default="", description="調査者")
    construction_date_target: str = Field(default="", description="工事希望日")
    referrer: str = Field(default="", description="紹介元・代理店")


class ResidentialBuilding(BaseModel):
    """住宅建物情報（屋根・構造・日射条件）"""
    construction_year: int = Field(default=0, description="築年（西暦）")
    structure: str = Field(default="", description="構造（木造/鉄骨/RC）")
    floors: int = Field(default=0, description="階数")
    roof_type: RoofType = Field(default=RoofType.OTHER, description="屋根形状")
    roof_material: RoofMaterial = Field(default=RoofMaterial.OTHER, description="屋根材")
    roof_pitch_deg: float = Field(default=0, description="屋根勾配（度）")
    roof_azimuth_deg: float = Field(default=0, description="屋根方位（南=180）")
    roof_area_sqm: float = Field(default=0, description="屋根面積（㎡）")
    has_dormer: bool = Field(default=False, description="天窓・出窓有無")
    shading_obstacles: str = Field(default="", description="日射障害物（隣家・樹木等）")


class ResidentialEquipment(BaseModel):
    """住宅向け太陽光発電設備情報"""
    module_maker: str = Field(default="", description="モジュールメーカー")
    module_model: str = Field(default="", description="モジュール型式")
    module_output_w: float = Field(default=0, description="モジュール定格出力(W/枚)")
    planned_panels: int = Field(default=0, description="設置予定枚数")
    pv_capacity_kw: float = Field(default=0, description="想定PV容量(kW)")
    layout_pattern: str = Field(default="", description="パネル配置（縦置き/横置き/混合）")
    mounting_type: MountingType = Field(default=MountingType.OTHER, description="架台取付方式")
    pcs_maker: str = Field(default="", description="PCSメーカー")
    pcs_model: str = Field(default="", description="PCS型式")
    pcs_capacity_kw: float = Field(default=0, description="PCS容量(kW)")
    pcs_indoor_outdoor: str = Field(default="", description="PCS設置場所（屋内/屋外）")


class ResidentialBattery(BaseModel):
    """住宅向け蓄電池情報"""
    has_battery: bool = Field(default=False, description="蓄電池の有無")
    battery_use: BatteryUse = Field(default=BatteryUse.NONE, description="蓄電池の使用方式")
    battery_maker: str = Field(default="", description="蓄電池メーカー")
    battery_model: str = Field(default="", description="蓄電池型式")
    battery_capacity_kwh: float = Field(default=0, description="蓄電池容量(kWh)")
    installation_location: str = Field(default="", description="蓄電池設置場所（屋内/屋外）")


class ResidentialElectrical(BaseModel):
    """住宅向け電気設備・施工条件"""
    existing_breaker_amps: int = Field(default=0, description="既設主幹ブレーカー容量(A)")
    distribution_board_space: bool = Field(default=False, description="分電盤スペース有無")
    meter_location: str = Field(default="", description="メーター位置")
    earth_resistance_ohm: float = Field(default=0, description="接地抵抗(Ω)")
    cable_route_distance_m: float = Field(default=0, description="配線距離(m)")


class ResidentialPackage(BaseModel):
    """住宅向けパッケージ情報（サンエー独自の標準パッケージ）"""
    package_code: str = Field(default="", description="パッケージコード（例: RES-5KW-A）")
    package_name: str = Field(default="", description="パッケージ名称")
    notes: str = Field(default="", description="備考")


class ResidentialSurveyData(BaseModel):
    """住宅向け現調シート全体のデータモデル"""
    project: ResidentialProjectInfo = Field(default_factory=ResidentialProjectInfo)
    building: ResidentialBuilding = Field(default_factory=ResidentialBuilding)
    equipment: ResidentialEquipment = Field(default_factory=ResidentialEquipment)
    battery: ResidentialBattery = Field(default_factory=ResidentialBattery)
    electrical: ResidentialElectrical = Field(default_factory=ResidentialElectrical)
    package: ResidentialPackage = Field(default_factory=ResidentialPackage)

    # 読み取りメタデータ
    extraction_warnings: list[str] = Field(default_factory=list, description="読み取り時の警告")
    field_confidences: dict[str, ConfidenceLevel] = Field(
        default_factory=dict, description="フィールド別信頼度"
    )


class DocumentCategory(str, Enum):
    """現調シートのカテゴリ（住宅/法人/不明）"""
    RESIDENTIAL = "residential"
    COMMERCIAL = "commercial"
    UNKNOWN = "unknown"


class UnifiedSurveyData(BaseModel):
    """住宅・法人を統合的に扱うラッパー型

    PDF読み取り段階でカテゴリ判定を行い、対応するスキーマにのみ値が入る。
    上位レイヤ（見積もり生成・UI表示）は category と is_residential() /
    is_commercial() / get_active() を使って分岐するだけで両カテゴリを扱える。
    """
    category: DocumentCategory = Field(
        default=DocumentCategory.UNKNOWN, description="現調シートのカテゴリ"
    )
    residential: Optional[ResidentialSurveyData] = Field(
        default=None, description="住宅向けデータ（categoryがRESIDENTIALの場合のみ）"
    )
    commercial: Optional[SurveyData] = Field(
        default=None, description="法人(高圧)向けデータ（categoryがCOMMERCIALの場合のみ）"
    )
    extraction_warnings: list[str] = Field(
        default_factory=list, description="カテゴリ判定や全体に関する警告"
    )

    def is_residential(self) -> bool:
        """住宅向けかどうかを判定"""
        return self.category == DocumentCategory.RESIDENTIAL

    def is_commercial(self) -> bool:
        """法人(高圧)向けかどうかを判定"""
        return self.category == DocumentCategory.COMMERCIAL

    def get_active(self) -> Optional[Union[ResidentialSurveyData, SurveyData]]:
        """カテゴリに応じてアクティブなデータを返す。UNKNOWNの場合はNone。"""
        if self.is_residential():
            return self.residential
        if self.is_commercial():
            return self.commercial
        return None
