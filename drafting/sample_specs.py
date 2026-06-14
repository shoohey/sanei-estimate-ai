"""ゴールデン仕様（6サンプル解析から起こした DraftingSpec の実例）

用途:
- drawing_renderer / layout_engine のテストフィクスチャ（実サンプルと見比べる基準）
- アプリのデモ／フォーム初期値
- spec_extractor が返すべき JSON の正解例（few-shot 参考）

これらは「抽出レベル」の仕様（屋根面・モジュール・目標枚数・系統・タイトル）であり、
パネル座標（PanelRect）は持たない。layout_engine.place_panels() で座標を確定してから
renderer に渡すのが本来のパイプライン。

寸法は実サンプルPDFから読み取った実測値（mm）。
"""

from __future__ import annotations

from drafting.models import (
    DraftingSpec, RoofFace, PanelSpec, StringGroup, TitleBlock,
    DrawingType, RoofType, Orientation, MountType,
)


def kurihara_layout() -> DraftingSpec:
    """栗原 英信様（住宅・瓦・配置図・横置き 10枚 4.650kW）

    元: 新しいフォルダー(3) パターン㈰ 太陽光配置図 2026.06.05
    屋根 10800×6000、パネル域 8850×2293（左右975マージン）、NEXT ENERGY 465W。
    """
    return DraftingSpec(
        customer_name="栗原 英信様",
        drawing_type=DrawingType.LAYOUT,
        paper="A4",
        mount_type=MountType.YANE,
        panel=PanelSpec(
            maker="NEXT ENERGY", model="NER108M465B-NE", output_w=465,
            long_mm=1762, short_mm=1134, gap_long_mm=25, gap_short_mm=10,
        ),
        roof_faces=[
            RoofFace(
                name="面1", roof_type=RoofType.KAWARA, shape="rectangle",
                width_mm=10800, depth_mm=6000, margin_mm=975,
                orientation=Orientation.LANDSCAPE, target_panel_count=10,
                hatch="horizontal",
            ),
        ],
        title=TitleBlock(
            drawing_no="202605-88TH",
            project_name="栗原 英信様 太陽光システム増設工事",
            drawing_name="太陽光配置図",
            system_text="NEXT ENERGY 465W×10枚　4.650kW",
            scale="1/60(A4)",
            created_date="2026年06月05日",
        ),
        total_panels=10, total_kw=4.650,
    )


def yagi_layout() -> DraftingSpec:
    """八木 秀作様（住宅・陸屋根・配置図・縦置き 12枚 4.800kW・ポリゴン屋根）

    元: 新しいフォルダー(2) 八木 秀作様のレイアウト図 2026.03.18
    L字（上段小+下段主）屋根。下段にパネル 2行×6列=12。SHARP SILFINE 400W。
    """
    # ポリゴン（mm）: 上段が左に張り出すL字。左上原点・x右・y下。
    poly = [
        [2900, 0], [10750, 0], [10750, 5050], [0, 5050], [0, 1450], [2900, 1450],
    ]
    return DraftingSpec(
        customer_name="八木 秀作様",
        drawing_type=DrawingType.LAYOUT,
        paper="A4",
        mount_type=MountType.RIKU,
        panel=PanelSpec(
            maker="SHARP", model="SFJ-400-EWH", output_w=400,
            long_mm=1750, short_mm=1170, gap_long_mm=10, gap_short_mm=10,
        ),
        roof_faces=[
            RoofFace(
                name="面1", roof_type=RoofType.RIKUYANE, shape="polygon",
                polygon_mm=poly, width_mm=10750, depth_mm=5050, margin_mm=425,
                orientation=Orientation.PORTRAIT, target_panel_count=12,
                hatch="vertical",
            ),
        ],
        title=TitleBlock(
            drawing_no="202603-62N",
            project_name="八木 秀作様太陽光システム設置工事",
            drawing_name="レイアウト図",
            system_text="SILFINE 400W×12枚　4.800kW",
            scale="1/75(A4)",
            created_date="2026年03月18日",
        ),
        total_panels=12, total_kw=4.800,
    )


def spice_house_layout() -> DraftingSpec:
    """株式会社スパイスハウス様（法人・折板・配置図・縦置き 72枚 42.480kW・2屋根面）

    元: スパイスハウス 太陽光配置図＆ストリングス図 2026.01.27
    上下2面、各面36枚（折板・縦置き）。JINKO 590W。
    """
    def _face(name: str, oy: float) -> RoofFace:
        return RoofFace(
            name=name, roof_type=RoofType.SETSUBAN, shape="rectangle",
            width_mm=25101, depth_mm=6966, margin_mm=1100,
            origin_y_mm=oy,
            orientation=Orientation.PORTRAIT, target_panel_count=36,
            hatch="vertical",
        )
    return DraftingSpec(
        customer_name="株式会社スパイスハウス様",
        drawing_type=DrawingType.LAYOUT,
        paper="A3",
        mount_type=MountType.SETSUBAN,
        panel=PanelSpec(
            maker="JINKO", model="JKM590N-72HL4-BDV", output_w=590,
            long_mm=2278, short_mm=1134, gap_long_mm=25, gap_short_mm=10,
        ),
        roof_faces=[
            _face("面1（上）", 0),
            _face("面2（下）", 7500),
        ],
        title=TitleBlock(
            drawing_no="202601-TH48",
            project_name="株式会社スパイスハウス様太陽光システム設置工事",
            drawing_name="レイアウト図",
            system_text="JINKO 590W×72枚　42.480kW",
            scale="1/100",
            created_date="2026年01月27日",
        ),
        total_panels=72, total_kw=42.480,
    )


def tok_string() -> DraftingSpec:
    """東京応化工業株式会社 TOK技術革新センター様（法人・ストリングス図・202枚 102.010kW）

    元: 新しいフォルダー(1) 東京応化 ストリングス図 2026.03.05
    複数屋根面・PCS3台。簡略化のため主要2面で代表（108枚 + 94枚）。XSOL 505W。
    系統表: PCS1 12直×5並 / PCS2 12直×4並+10直×2並 / PCS3 12直×6並。
    """
    return DraftingSpec(
        customer_name="東京応化工業株式会社　TOK技術革新センター様",
        drawing_type=DrawingType.STRING,
        paper="A3",
        mount_type=MountType.TEIJUSHIN,
        panel=PanelSpec(
            maker="XSOL", model="XLN120-505S", output_w=505,
            long_mm=1903, short_mm=1134, gap_long_mm=25, gap_short_mm=10,
        ),
        pcs_model="CEPT-P3AB2025B", pcs_count=3,
        roof_faces=[
            RoofFace(
                name="面1", roof_type=RoofType.RIKUYANE, shape="rectangle",
                width_mm=26630, depth_mm=19280, margin_mm=2000,
                origin_x_mm=0, origin_y_mm=0,
                orientation=Orientation.LANDSCAPE, target_panel_count=108,
                hatch="none",
            ),
            RoofFace(
                name="面2", roof_type=RoofType.RIKUYANE, shape="rectangle",
                width_mm=19500, depth_mm=18030, margin_mm=2000,
                origin_x_mm=30000, origin_y_mm=0,
                orientation=Orientation.LANDSCAPE, target_panel_count=94,
                hatch="none",
            ),
        ],
        strings=[
            StringGroup(pcs_label="PCS1", config_text="12直×5並"),
            StringGroup(pcs_label="PCS2", config_text="12直×4並＋10直×2並"),
            StringGroup(pcs_label="PCS3", config_text="12直×6並"),
        ],
        title=TitleBlock(
            drawing_no="202603-N57",
            project_name="東京応化工業株式会社　TOK技術革新センター様",
            drawing_name="ストリングス図",
            system_text="XSOL 505W×202枚　102.010kW",
            scale="1/300",
            created_date="2026年03月05日",
        ),
        total_panels=202, total_kw=102.010,
    )


# 名前→生成関数（テスト/デモ用カタログ）
GOLDEN_SPECS = {
    "kurihara_layout": kurihara_layout,
    "yagi_layout": yagi_layout,
    "spice_house_layout": spice_house_layout,
    "tok_string": tok_string,
}


def get_golden(name: str) -> DraftingSpec:
    """名前でゴールデン仕様を取得。"""
    fn = GOLDEN_SPECS.get(name)
    if fn is None:
        raise KeyError(f"unknown golden spec: {name}. choices={list(GOLDEN_SPECS)}")
    return fn()
