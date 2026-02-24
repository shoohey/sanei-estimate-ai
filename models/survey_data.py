"""現調シートの全フィールドをPydanticモデル化"""
from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class DesignStatus(str, Enum):
    CONFIRMED = "確定"
    TENTATIVE = "仮"
    UNDECIDED = "未定"


class GroundType(str, Enum):
    A = "A"
    C = "C"
    D = "D"


class LocationType(str, Enum):
    INDOOR = "屋内"
    OUTDOOR = "屋外"


class BTPlacement(str, Enum):
    INDOOR = "屋内"
    OUTDOOR = "屋外"
    NONE = "設置なし"


class CInstallation(str, Enum):
    POSSIBLE = "可"
    IMPOSSIBLE = "不可"


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FieldConfidence(BaseModel):
    """各フィールドの読み取り信頼度"""
    value: str = ""
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH
    warning: Optional[str] = None


class ProjectInfo(BaseModel):
    """案件基本情報"""
    project_name: str = Field(default="", description="案件名")
    address: str = Field(default="", description="所在地")
    postal_code: str = Field(default="", description="郵便番号")
    survey_date: str = Field(default="", description="調査日")
    weather: str = Field(default="", description="天気")
    surveyor: str = Field(default="", description="調査者")


class PlannedEquipment(BaseModel):
    """計画設備情報（共通）"""
    module_maker: str = Field(default="", description="モジュールメーカー")
    module_model: str = Field(default="", description="モジュール型式")
    module_output_w: float = Field(default=0, description="モジュール定格出力(W/枚)")
    planned_panels: int = Field(default=0, description="設置予定枚数")
    pv_capacity_kw: float = Field(default=0, description="想定PV容量(kW)")
    design_status: DesignStatus = Field(default=DesignStatus.UNDECIDED, description="設計確定度")


class HighVoltageChecklist(BaseModel):
    """高圧チェック項目"""
    building_drawing: bool = Field(default=False, description="(1) 建物図面 あり/なし")
    single_line_diagram: bool = Field(default=False, description="(2) 単線結線図 あり/なし")
    single_line_diagram_note: str = Field(default="", description="(2) 単線結線図 備考")
    ground_type: GroundType = Field(default=GroundType.A, description="(3) 接地種類")
    c_installation: CInstallation = Field(default=CInstallation.POSSIBLE, description="(3) C種別設置可否")
    c_installation_note: str = Field(default="", description="(3) C種別 備考")
    vt_available: bool = Field(default=False, description="(4) VT有無")
    ct_available: bool = Field(default=False, description="(4) CT有無")
    relay_space: bool = Field(default=False, description="(5) 継電器スペース")
    pcs_space: bool = Field(default=False, description="(6) PCS設置スペース")
    pcs_location: Optional[LocationType] = Field(default=None, description="(6) PCS設置場所（ありの場合）")
    bt_space: Optional[BTPlacement] = Field(default=None, description="BT設置スペース")
    bt_backup_capacity: str = Field(default="", description="BTバックアップ回路容量")
    tr_capacity: str = Field(default="", description="(7) Tr容量余裕")
    pre_use_self_check: bool = Field(default=False, description="(8) 使用前自己確認")
    separation_ns_mm: float = Field(default=0, description="離隔 縦(南北) mm")
    separation_ew_mm: float = Field(default=0, description="離隔 横(東西) mm")


class SupplementarySheet(BaseModel):
    """別紙チェック項目"""
    crane_available: bool = Field(default=False, description="クレーンの有無")
    scaffold_location: str = Field(default="", description="足場設置予定位置")
    scaffold_needed: bool = Field(default=False, description="足場必要")
    pole_number: str = Field(default="", description="電柱番号")
    pole_type: str = Field(default="", description="1号柱位置 A/C/D")
    wiring_route: str = Field(default="", description="配管、配線ルート 確定/未確定")
    cubicle_location: bool = Field(default=False, description="キュービクル、電気室位置 あり/なし")
    bt_location: str = Field(default="", description="BT設置位置")
    meter_photo: str = Field(default="", description="引込柱・メーター番号撮影")
    handwritten_notes: str = Field(default="", description="手書き欄メモ")


class FinalConfirmation(BaseModel):
    """最終確認情報"""
    surveyor_name: str = Field(default="", description="調査者（現調実施）氏名")
    surveyor_date: str = Field(default="", description="調査者 日付")
    design_reviewer: str = Field(default="", description="設計確認 氏名")
    design_review_date: str = Field(default="", description="設計確認 日付")
    works_reviewer: str = Field(default="", description="ワークス部確認 氏名")
    works_review_date: str = Field(default="", description="ワークス部確認 日付")
    notes: str = Field(default="", description="備考")


class SurveyData(BaseModel):
    """現調シート全体のデータモデル"""
    project: ProjectInfo = Field(default_factory=ProjectInfo)
    equipment: PlannedEquipment = Field(default_factory=PlannedEquipment)
    high_voltage: HighVoltageChecklist = Field(default_factory=HighVoltageChecklist)
    supplementary: SupplementarySheet = Field(default_factory=SupplementarySheet)
    confirmation: FinalConfirmation = Field(default_factory=FinalConfirmation)

    # 読み取りメタデータ
    extraction_warnings: list[str] = Field(default_factory=list, description="読み取り時の警告")
    field_confidences: dict[str, ConfidenceLevel] = Field(default_factory=dict, description="フィールド別信頼度")
