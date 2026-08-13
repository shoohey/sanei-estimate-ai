"""最大枚数自動配置 + 屋根種別連動の離隔のテスト（API不要）

実行: python3 tests/test_layout_maxfill.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drafting.models import (
    DraftingSpec, PanelSpec, RoofFace, RoofType, Orientation, DrawingType,
)
from drafting.layout_engine import place_panels


def _make_spec(roof_type=RoofType.KAWARA, target=None, margin=500.0,
               gap_long=25.0, gap_short=10.0):
    return DraftingSpec(
        customer_name="テスト",
        drawing_type=DrawingType.LAYOUT,
        panel=PanelSpec(maker="TEST", model="T-100", output_w=465,
                        long_mm=1762, short_mm=1134,
                        gap_long_mm=gap_long, gap_short_mm=gap_short),
        roof_faces=[RoofFace(name="面1", roof_type=roof_type,
                             width_mm=10000, depth_mm=8000,
                             margin_mm=margin,
                             # 10%自動離隔ルール(2026-08-13)から独立させるため
                             # 500(旧標準)のときは方向別で明示する
                             margin_ns_mm=(margin if margin == 500.0 else 0.0),
                             margin_ew_mm=(margin if margin == 500.0 else 0.0),
                             orientation=Orientation.AUTO,
                             target_panel_count=target)],
    )


def test_none_target_places_max():
    """target=None なら最大枚数が配置されること。"""
    spec = place_panels(_make_spec(target=None))
    assert spec.roof_faces[0].panel_count > 0, "None で1枚も置かれない"


def test_zero_target_means_max():
    """target=0 は「未指定」= None と同じ最大枚数になること。"""
    max_spec = place_panels(_make_spec(target=None))
    zero_spec = place_panels(_make_spec(target=0))
    assert zero_spec.roof_faces[0].panel_count == max_spec.roof_faces[0].panel_count, \
        f"0指定: {zero_spec.roof_faces[0].panel_count} != 最大: {max_spec.roof_faces[0].panel_count}"


def test_negative_target_means_max():
    """負値も未指定扱いで最大枚数になること。"""
    max_spec = place_panels(_make_spec(target=None))
    neg_spec = place_panels(_make_spec(target=-1))
    assert neg_spec.roof_faces[0].panel_count == max_spec.roof_faces[0].panel_count


def test_positive_target_still_limits():
    """正の枚数指定は従来通り上限として効くこと。"""
    spec = place_panels(_make_spec(target=5))
    assert spec.roof_faces[0].panel_count == 5


def test_rikuyane_defaults_reduce_count():
    """陸屋根は行間235mm（TUG段間）の既定値が効いて瓦屋根より枚数が減ること。"""
    kawara = place_panels(_make_spec(roof_type=RoofType.KAWARA))
    riku = place_panels(_make_spec(roof_type=RoofType.RIKUYANE))
    assert riku.roof_faces[0].panel_count < kawara.roof_faces[0].panel_count, \
        f"陸屋根: {riku.roof_faces[0].panel_count} >= 瓦: {kawara.roof_faces[0].panel_count}"


def test_explicit_gap_wins_over_roof_defaults():
    """行間を明示（≠25）した場合は陸屋根既定値で上書きされないこと。"""
    explicit = place_panels(_make_spec(roof_type=RoofType.RIKUYANE, gap_long=30.0))
    kawara30 = place_panels(_make_spec(roof_type=RoofType.KAWARA, gap_long=30.0))
    assert explicit.roof_faces[0].panel_count == kawara30.roof_faces[0].panel_count, \
        "明示した行間30mmが陸屋根既定値235mmで上書きされている"


def test_explicit_margin_wins():
    """マージンを明示（≠500）した場合は既定値で上書きされないこと。"""
    spec = place_panels(_make_spec(roof_type=RoofType.RIKUYANE, margin=300.0))
    assert spec.roof_faces[0].margin_mm == 300.0


def test_target_specified_keeps_measured_gaps():
    """枚数指定がある陸屋根面では離隔既定値を適用しない（実設計の再現）こと。"""
    spec = place_panels(_make_spec(roof_type=RoofType.RIKUYANE, target=5))
    assert spec.roof_faces[0].panel_count == 5, "枚数指定が離隔既定値で崩れている"


def test_zero_values_treated_as_unspecified():
    """AI抽出の「記載なし=0」の隙間に屋根種別既定値が効き、
    マージン=0 は10%ルールの自動離隔になること（2026-08-13 作図ルール対応）。"""
    spec = _make_spec(roof_type=RoofType.RIKUYANE, target=None,
                      margin=0.0, gap_long=0.0, gap_short=0.0)
    spec = place_panels(spec)
    assert spec.roof_faces[0].margin_mm == 0.0, \
        "margin=0（未指定）は0のまま＝10%ルールが配置時に自動適用される"
    # 10%ルール適用の実証: 屋根10000×8000 → EW1000/NS800 の離隔内に全パネル
    f = spec.roof_faces[0]
    for pr in f.panels:
        assert pr.x_mm >= 1000 - 1 and pr.x_mm + pr.w_mm <= 10000 - 1000 + 1
        assert pr.y_mm >= 800 - 1 and pr.y_mm + pr.h_mm <= 8000 - 800 + 1
    assert spec.panel.gap_long_mm == 235.0, "gap=0（未指定）に陸屋根行間235（TUG段間）が適用されるはず"


def test_effective_gap_reflected_in_spec():
    """陸屋根既定値が効いた場合、spec.panel の隙間にも反映され
    図面の間隔注記と実配置が食い違わないこと（レビュー指摘 med）。"""
    spec = place_panels(_make_spec(roof_type=RoofType.RIKUYANE, target=None))
    assert spec.panel.gap_long_mm == 235.0, \
        f"spec.panel.gap_long_mm が {spec.panel.gap_long_mm}（凡例が実配置235mmと不一致になる）"
    # 既定値が効かないケース（瓦・枚数指定あり）では 25 のまま
    spec2 = place_panels(_make_spec(roof_type=RoofType.KAWARA, target=None))
    assert spec2.panel.gap_long_mm == 25.0


def test_golden_samples_unchanged():
    """既存ゴールデンサンプルの配置枚数が変わらないこと（回帰確認）。"""
    from drafting import sample_specs
    expected = {
        "kurihara_layout": 10,
        "yagi_layout": 12,
        "spice_house_layout": 72,
        "tok_string": 202,
    }
    for key, count in expected.items():
        spec = place_panels(sample_specs.get_golden(key))
        assert spec.total_panels == count, \
            f"{key}: {spec.total_panels}枚 != 期待 {count}枚"


def main():
    tests = [
        test_none_target_places_max,
        test_zero_target_means_max,
        test_negative_target_means_max,
        test_positive_target_still_limits,
        test_rikuyane_defaults_reduce_count,
        test_explicit_gap_wins_over_roof_defaults,
        test_explicit_margin_wins,
        test_target_specified_keeps_measured_gaps,
        test_zero_values_treated_as_unspecified,
        test_effective_gap_reflected_in_spec,
        test_golden_samples_unchanged,
    ]
    print("=== 最大枚数配置・屋根種別離隔テスト（API不要） ===")
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
