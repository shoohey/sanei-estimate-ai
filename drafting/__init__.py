"""簡易製図AI（drafting）モジュール

手書き現調資料（スケッチ／航空写真＋手書き寸法／建築図面）を入力に、
株式会社サンエー標準テンプレートの太陽光発電 製図（配置図・ストリングス図）を
自動生成する。見積作成AIとは別系統の「簡易製図AI」サブシステム。

パイプライン:
    survey images
        │  spec_extractor.extract_drafting_spec()   ← Claude Vision
        ▼
    DraftingSpec（屋根面・モジュール・枚数・系統）
        │  layout_engine.place_panels()             ← 幾何計算（複数屋根面）
        ▼
    DraftingSpec（パネル座標まで確定）
        │  drawing_renderer.render_drawing()        ← matplotlib CADテンプレ
        ▼
    PNG / PDF（A4・A3横）

公開モデルは drafting.models を参照。
"""

from drafting.models import (  # noqa: F401
    DraftingSpec,
    RoofFace,
    PanelSpec,
    PanelRect,
    StringGroup,
    Equipment,
    TitleBlock,
    DrawingType,
    RoofType,
    Orientation,
    MountType,
    default_spec,
    spec_to_dict,
    spec_from_dict,
)

__all__ = [
    "DraftingSpec",
    "RoofFace",
    "PanelSpec",
    "PanelRect",
    "StringGroup",
    "Equipment",
    "TitleBlock",
    "DrawingType",
    "RoofType",
    "Orientation",
    "MountType",
    "default_spec",
    "spec_to_dict",
    "spec_from_dict",
]
