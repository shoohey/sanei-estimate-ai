"""点検通路（2列ごと800mm）配置のテスト（API不要）

2026-07-23 会議 修正①: 2列ごとに800mm〜1,000mmの点検通路を
配置計算（drafting/layout_engine）と最大枚数算出（roof/panel_layout）の
両方に反映する。walkway_mm=0 は従来配置と完全一致（後方互換）。

実行: python3 tests/test_corridor.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drafting.models import (
    DraftingSpec, PanelSpec, RoofFace, RoofType, Orientation, DrawingType,
    spec_to_dict, spec_from_dict,
)
from drafting.layout_engine import place_panels


# 検証寸法（手計算で列数を確定済み）:
#   屋根 12000x8000 / margin 500 → 有効幅 11000
#   縦置き panel_w=1134 / 短辺側隙間10
#   通路なし: cols=9 rows=3 → 27枚
#   通路800・2列ごと: 7*1134+3*10+3*800=10368 ≤ 11000 → cols=7 rows=3 → 21枚
#   通路800・3列ごと: 8*1134+5*10+2*800=10722 ≤ 11000 → cols=8 rows=3 → 24枚
def _make_spec(walkway=0.0, group=2, orient=Orientation.PORTRAIT, target=None):
    return DraftingSpec(
        customer_name="テスト",
        drawing_type=DrawingType.LAYOUT,
        panel=PanelSpec(maker="TEST", model="T-100", output_w=465,
                        long_mm=1762, short_mm=1134,
                        gap_long_mm=25.0, gap_short_mm=10.0,
                        walkway_mm=walkway, walkway_every_n_cols=group),
        roof_faces=[RoofFace(name="面1", roof_type=RoofType.KAWARA,
                             width_mm=12000, depth_mm=8000,
                             # 10%自動離隔ルール(2026-08-13)から独立させるため明示指定
                             margin_ns_mm=500.0, margin_ew_mm=500.0,
                             orientation=orient, target_panel_count=target)],
    )


def _col_gaps(face, panel_w=1134.0):
    """実配置から隣接列間のクリア隙間リストを返す。"""
    xs = sorted({p.x_mm for p in face.panels})
    return [round(xs[i + 1] - xs[i] - panel_w, 3) for i in range(len(xs) - 1)]


def test_walkway_reduces_max_cols():
    """通路800mmで最大列数が 9→7 に減ること（最大枚数算出への反映）。"""
    base = place_panels(_make_spec(walkway=0.0)).roof_faces[0]
    walk = place_panels(_make_spec(walkway=800.0)).roof_faces[0]
    assert base.cols == 9 and base.panel_count == 27, \
        f"通路なし基準が想定と不一致: {base.rows}x{base.cols}={base.panel_count}"
    assert walk.cols == 7 and walk.panel_count == 21, \
        f"通路800mm: {walk.rows}x{walk.cols}={walk.panel_count}（期待 3x7=21）"


def test_walkway_every_2_cols_positions():
    """2列ごとに通路800mmが入り、それ以外は短辺側隙間10mmであること。"""
    face = place_panels(_make_spec(walkway=800.0)).roof_faces[0]
    assert _col_gaps(face) == [10.0, 800.0, 10.0, 800.0, 10.0, 800.0], \
        f"列間パターンが想定外: {_col_gaps(face)}"


def test_walkway_every_3_cols():
    """N列ごと（N=3）の指定が効くこと。"""
    face = place_panels(_make_spec(walkway=800.0, group=3)).roof_faces[0]
    assert face.cols == 8 and face.panel_count == 24, \
        f"3列ごと通路: {face.rows}x{face.cols}={face.panel_count}（期待 3x8=24）"
    assert _col_gaps(face) == [10.0, 10.0, 800.0, 10.0, 10.0, 800.0, 10.0], \
        f"列間パターンが想定外: {_col_gaps(face)}"


def test_walkway_zero_identical_to_baseline():
    """walkway=0 は従来配置と座標まで完全一致すること（後方互換）。"""
    base = place_panels(_make_spec(walkway=0.0)).roof_faces[0]
    zero = place_panels(_make_spec(walkway=0.0, group=2)).roof_faces[0]
    assert [(p.x_mm, p.y_mm) for p in base.panels] == \
           [(p.x_mm, p.y_mm) for p in zero.panels]


def test_old_json_without_walkway_keys():
    """walkway キーの無い旧保存JSONは通路なし（従来動作）で復元されること。"""
    d = spec_to_dict(_make_spec(walkway=800.0))
    d["panel"].pop("walkway_mm", None)
    d["panel"].pop("walkway_every_n_cols", None)
    spec = spec_from_dict(d)
    assert spec.panel.walkway_mm == 0.0, "旧JSONの欠損キーは 0.0（通路なし）のはず"
    assert spec.panel.walkway_every_n_cols == 2
    base = place_panels(_make_spec(walkway=0.0)).roof_faces[0]
    old = place_panels(spec).roof_faces[0]
    assert [(p.x_mm, p.y_mm) for p in old.panels] == \
           [(p.x_mm, p.y_mm) for p in base.panels], "旧JSONの配置が従来と不一致"


def test_walkway_json_roundtrip():
    """walkway_mm は JSON ラウンドトリップで保存・復元されること。"""
    spec = spec_from_dict(spec_to_dict(_make_spec(walkway=850.0, group=3)))
    assert spec.panel.walkway_mm == 850.0
    assert spec.panel.walkway_every_n_cols == 3


def test_walkway_with_target_still_limits():
    """通路ありでも枚数指定（target）は従来通り上限として効くこと。"""
    face = place_panels(_make_spec(walkway=800.0, target=5)).roof_faces[0]
    assert face.panel_count == 5


def test_walkway_auto_orientation():
    """AUTO向きでも通路込みで正常に配置されること（クラッシュ回帰）。"""
    face = place_panels(_make_spec(walkway=800.0, orient=Orientation.AUTO)).roof_faces[0]
    assert face.panel_count > 0
    base = place_panels(_make_spec(walkway=0.0, orient=Orientation.AUTO)).roof_faces[0]
    assert face.panel_count <= base.panel_count, "通路ありで枚数が増えるのはおかしい"


def test_roof_panel_layout_walkway():
    """見積アプリ側 compute_panel_layout にも同じ通路ルールが効くこと。"""
    from roof.panel_layout import compute_panel_layout
    base = compute_panel_layout(12.0, 8.0, 1.762, 1.134,
                                edge_margin_m=0.5, gap_m=0.02,
                                orientation="portrait")
    walk = compute_panel_layout(12.0, 8.0, 1.762, 1.134,
                                edge_margin_m=0.5, gap_m=0.02,
                                orientation="portrait",
                                walkway_m=0.8, walkway_every_n_cols=2)
    assert base["cols"] == 9 and base["panel_count"] == 27
    assert walk["cols"] == 7 and walk["panel_count"] == 21, \
        f"roof側通路: {walk['rows']}x{walk['cols']}={walk['panel_count']}（期待 3x7=21）"
    assert walk["walkway_m"] == 0.8
    xs = sorted({p["x"] for p in walk["positions"]})
    gaps = [round(xs[i + 1] - xs[i] - 1.134, 4) for i in range(len(xs) - 1)]
    assert gaps == [0.02, 0.8, 0.02, 0.8, 0.02, 0.8], f"roof側列間パターン: {gaps}"


def test_roof_panel_layout_default_unchanged():
    """walkway_m 未指定の compute_panel_layout は従来結果のままであること。"""
    from roof.panel_layout import compute_panel_layout
    default = compute_panel_layout(12.0, 8.0, 1.762, 1.134,
                                   edge_margin_m=0.5, gap_m=0.02,
                                   orientation="portrait")
    explicit0 = compute_panel_layout(12.0, 8.0, 1.762, 1.134,
                                     edge_margin_m=0.5, gap_m=0.02,
                                     orientation="portrait", walkway_m=0.0)
    assert default["panel_count"] == explicit0["panel_count"] == 27
    assert default["positions"] == explicit0["positions"]


def test_renderer_walkway_dimension_no_crash():
    """通路入りスペックの製図が生成でき、通路寸法描画でも落ちないこと。"""
    from drafting.drawing_renderer import render_drawing_png
    spec = place_panels(_make_spec(walkway=800.0))
    png = render_drawing_png(spec, dpi=72)
    assert isinstance(png, bytes) and len(png) > 1000
    # 通路なしも従来どおり描けること
    spec0 = place_panels(_make_spec(walkway=0.0))
    png0 = render_drawing_png(spec0, dpi=72)
    assert isinstance(png0, bytes) and len(png0) > 1000


def main():
    tests = [
        test_walkway_reduces_max_cols,
        test_walkway_every_2_cols_positions,
        test_walkway_every_3_cols,
        test_walkway_zero_identical_to_baseline,
        test_old_json_without_walkway_keys,
        test_walkway_json_roundtrip,
        test_walkway_with_target_still_limits,
        test_walkway_auto_orientation,
        test_roof_panel_layout_walkway,
        test_roof_panel_layout_default_unchanged,
        test_renderer_walkway_dimension_no_crash,
    ]
    print("=== 点検通路（2列ごと800mm）配置テスト（API不要） ===")
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[OK] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"[NG] {t.__name__}: {e}")
    print("=== 結果: " + ("全パス" if failed == 0 else "一部失敗") + " ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
