"""製図パイプラインの品質ガードのテスト（API不要・スクリプト式）

実行: python3 tests/test_drafting_guards.py

背景（2026-08-11 分析。顧客提供のAI図面2件の根本原因）:
1. 抽出プロンプト「不明な数値は0」→ パネル寸法0 → 0枚配置がサイレント通過
   → レンダラーの試験用フォールバックが既定寸法1762×1134のダミーグリッドを
   本番図面として描画（はみ出し・0.000kWの直接原因）
2. 複数屋根面の origin が全て(0,0)付近 → 外形の二重描画・パネル行の交錯
3. ポリゴン面の格納座標が外接矩形ローカルのままで min_x/min_y 分ズレる
4. 内包判定がパネル中心のみで斜辺沿いに角がはみ出す

カバー範囲:
- place_panels: 寸法0ガード（警告＋空配置）/ 面重なり自動整列 / 目標未達警告
- ポリゴン: 0起点でない頂点列でも座標が一致 / 4隅内包ではみ出しゼロ
- レンダラー: 寸法0ではダミーグリッドを敷かない
- 正規化: 型式→寸法補完 / 枚数・kW検算警告
- プロンプト: 新ルール（キー省略・origin・検算）の存在、旧「数値は0」ルールの不在
- 回帰: 正解①相当の入力で68枚を再現（エンジンの到達度保証）
"""
import os
import sys
from pathlib import Path

os.environ["SANEI_DISABLE_SUPABASE"] = "1"  # 本番Supabase遮断（学習お手本の読込経路対策）

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drafting.models import DraftingSpec, PanelSpec, RoofFace
from drafting.layout_engine import place_panels, _point_in_polygon
from drafting.spec_extractor import _normalize_parsed, build_user_prompt


def _xsol_panel(**kw):
    return PanelSpec(maker="XSOL", model="XLN120G-510X", output_w=510,
                     long_mm=1903, short_mm=1134, **kw)


def _face(w, d, target=None, name="面1", **kw):
    f = RoofFace()
    f.name = name
    f.width_mm = w
    f.depth_mm = d
    if target is not None:
        f.target_panel_count = target
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def test_zero_dims_guard():
    """パネル寸法0 → 配置スキップ＋警告（サイレント0枚配置の防止）。"""
    spec = DraftingSpec()
    spec.panel = PanelSpec(model="不明", output_w=0, long_mm=0, short_mm=0)
    spec.roof_faces = [_face(10000, 8000, target=16)]
    place_panels(spec)
    assert spec.roof_faces[0].panels == [], "寸法0で配置してはいけない"
    assert any("モジュール寸法" in w for w in spec.warnings), \
        f"警告が出るはず: {spec.warnings}"
    # パネル0枚なのに図面注記が「16枚/◯kW」を示す誤解を残さない（Codex指摘）
    assert spec.total_panels == 0 and spec.total_kw == 0.0, \
        f"配置0枚時の集計は0であるべき: {spec.total_panels}枚/{spec.total_kw}kW"


def test_renderer_no_fake_grid_on_zero_dims():
    """寸法0のままレンダリングしてもダミーグリッドを敷かないこと。"""
    from drafting.drawing_renderer import render_drawing_png
    spec = DraftingSpec()
    spec.panel = PanelSpec(long_mm=0, short_mm=0)
    spec.roof_faces = [_face(10000, 8000, target=16)]
    render_drawing_png(spec, dpi=60)
    assert spec.roof_faces[0].panels == [], \
        "既定寸法1762×1134のダミーグリッドが描かれてはいけない（実障害の再発）"


def test_auto_arrange_overlapping_faces():
    """origin衝突（全面0,0）→ 横並びに自動整列＋警告。"""
    spec = DraftingSpec()
    spec.panel = _xsol_panel()
    spec.roof_faces = [_face(10000, 8000, name="面1"),
                       _face(12000, 6000, name="面2")]
    place_panels(spec)
    f1, f2 = spec.roof_faces
    assert f2.origin_x_mm >= f1.origin_x_mm + 10000 + 1500, \
        f"面2が右に整列されるはず: {f2.origin_x_mm}"
    assert any("自動整列" in w for w in spec.warnings)


def test_no_rearrange_when_origins_valid():
    """originが正しく分離されている場合は変更しないこと。"""
    spec = DraftingSpec()
    spec.panel = _xsol_panel()
    f1 = _face(10000, 8000, name="面1")
    f2 = _face(12000, 6000, name="面2")
    f2.origin_x_mm = 15000.0
    f2.origin_y_mm = 3000.0
    spec.roof_faces = [f1, f2]
    place_panels(spec)
    assert (f2.origin_x_mm, f2.origin_y_mm) == (15000.0, 3000.0), \
        "正しいoriginを勝手に動かしてはいけない"
    assert not any("自動整列" in w for w in spec.warnings)


def test_target_shortfall_warning():
    """屋根に入り切らない目標枚数 → 未達警告（silent不足の防止）。"""
    spec = DraftingSpec()
    spec.panel = _xsol_panel()
    spec.roof_faces = [_face(5000, 4000, target=100)]
    place_panels(spec)
    assert spec.roof_faces[0].panel_count < 100
    assert any("しか配置できません" in w for w in spec.warnings), \
        f"目標未達の警告が出るはず: {spec.warnings}"


def test_polygon_offset_vertices_no_shift():
    """0起点でない頂点列でも、パネルがポリゴン外接矩形内に描かれること
    （min_x/min_y ズレ格納バグの回帰テスト）。"""
    spec = DraftingSpec()
    spec.panel = _xsol_panel()
    f = RoofFace()
    f.name = "多角形面"
    f.shape = "polygon"
    f.polygon_mm = [[2000, 1000], [12000, 1000], [12000, 7000], [2000, 7000]]
    spec.roof_faces = [f]
    place_panels(spec)
    assert f.panels, "配置0枚では検証にならない"
    for pr in f.panels:
        assert 2000 - 1 <= pr.x_mm and pr.x_mm + pr.w_mm <= 12000 + 1, \
            f"x方向はみ出し: {pr.x_mm}..{pr.x_mm + pr.w_mm}"
        assert 1000 - 1 <= pr.y_mm and pr.y_mm + pr.h_mm <= 7000 + 1, \
            f"y方向はみ出し: {pr.y_mm}..{pr.y_mm + pr.h_mm}"


def test_polygon_corner_containment():
    """台形屋根で、採用された全パネルの4隅がポリゴン内にあること
    （中心のみ判定による斜辺はみ出しの回帰テスト）。"""
    spec = DraftingSpec()
    spec.panel = _xsol_panel()
    f = RoofFace()
    f.name = "台形面"
    f.shape = "polygon"
    poly = [[0, 0], [14000, 0], [11000, 8000], [3000, 8000]]
    f.polygon_mm = poly
    spec.roof_faces = [f]
    place_panels(spec)
    assert f.panels, "配置0枚では検証にならない"
    eps = 1.0
    for pr in f.panels:
        corners = [(pr.x_mm + eps, pr.y_mm + eps),
                   (pr.x_mm + pr.w_mm - eps, pr.y_mm + eps),
                   (pr.x_mm + eps, pr.y_mm + pr.h_mm - eps),
                   (pr.x_mm + pr.w_mm - eps, pr.y_mm + pr.h_mm - eps)]
        for cx, cy in corners:
            assert _point_in_polygon(cx, cy, poly), \
                f"パネル角がポリゴン外: ({cx}, {cy})"


def test_normalize_completes_panel_dims_from_model():
    """型式XLN120G-510X・寸法0 → マスターから1903×1134に補完されること。"""
    d, warns, conf = _normalize_parsed(
        {"panel": {"maker": "XSOL", "model": "XLN120G-510X",
                   "output_w": 0, "long_mm": 0, "short_mm": 0}}, "layout")
    assert d["panel"]["output_w"] == 510.0
    assert d["panel"]["long_mm"] == 1903.0, d["panel"]
    assert d["panel"]["short_mm"] == 1134.0, d["panel"]
    assert conf.get("panel.dimensions") == "low"


def test_normalize_totals_mismatch_warning():
    """面ごと枚数の合計 ≠ 総枚数 → 検算警告。"""
    d, warns, conf = _normalize_parsed({
        "total_panels": 68,
        "roof_faces": [
            {"name": "面1", "target_panel_count": 16},
            {"name": "面2", "target_panel_count": 40},
        ],
    }, "layout")
    assert any("一致しません" in w for w in warns), warns
    assert conf.get("total_panels") == "low"


def test_normalize_kw_mismatch_warning():
    """kW記載 ≠ 枚数×W → 検算警告（一致時は警告なし）。"""
    base = {"total_panels": 68, "total_kw": 34.68,
            "panel": {"output_w": 510},
            "roof_faces": [{"name": "面1", "target_panel_count": 68}]}
    d, warns, conf = _normalize_parsed(dict(base), "layout")
    assert not any("設置容量" in w for w in warns), f"一致時は警告なし: {warns}"
    bad = dict(base); bad["total_kw"] = 50.0
    d, warns, conf = _normalize_parsed(bad, "layout")
    assert any("設置容量" in w for w in warns), warns


def test_prompt_new_rules():
    """プロンプトに新ルールが入り、旧「数値は0」既定ルールが消えていること。"""
    p = build_user_prompt("layout")
    assert "キー自体を出力しない" in p, "不明キー省略ルールが無い"
    assert "origin_x_mm" in p, "複数面originの指示が無い"
    assert "検算" in p, "枚数・kW検算の指示が無い"
    assert "区画ごとに別の roof_face" in p, "枚数ラベル→面分割の指示が無い"
    assert "数値は 0、配列は []" not in p, "旧『不明は0』ルールが残っている"
    assert "0 起点で出力" in p, "ポリゴン0起点の指示が無い"


def test_margin_10_percent_rule():
    """離隔既定 = 各方向寸法の10%（上限2m）。作図ルール3条（2026-08-13）。"""
    from drafting.layout_engine import _resolve_margins
    f = RoofFace()  # margin未指定（既定500=センチネル）
    ns, ew = _resolve_margins(f, 5980, 4470)  # 鎌倉ゴールデンケースの屋根
    assert ns == 447.0, f"南北=奥行の10%のはず: {ns}"
    assert ew == 598.0, f"東西=幅の10%のはず: {ew}"
    ns, ew = _resolve_margins(f, 30000, 25000)  # 大屋根 → 2m上限
    assert ns == 2000.0 and ew == 2000.0, f"10%が2m超なら2m: {ns}/{ew}"


def test_margin_explicit_wins():
    """現調で明示された離隔は10%ルールより優先されること（作図ルール: 現調記載>標準）。"""
    from drafting.layout_engine import _resolve_margins
    f = RoofFace(); f.margin_ns_mm = 450.0; f.margin_ew_mm = 1000.0
    assert _resolve_margins(f, 30000, 25000) == (450.0, 1000.0)
    f2 = RoofFace(); f2.margin_mm = 300.0  # 共通明示
    assert _resolve_margins(f2, 30000, 25000) == (300.0, 300.0)
    # 500 の明示も尊重される（Codex指摘: 旧センチネル方式の曖昧さ解消の回帰）
    f3 = RoofFace(); f3.margin_mm = 500.0
    assert _resolve_margins(f3, 30000, 25000) == (500.0, 500.0)


def test_target_priority_relaxes_auto_margin():
    """目標枚数が10%離隔で収まらない場合、離隔を500mmまで緩めて配置すること
    （作図ルール優先順位: ②現調指定 > ③標準離隔）。明示離隔は緩めない。"""
    spec = DraftingSpec()
    spec.panel = _xsol_panel()
    # 14200×9247 に40枚: 10%離隔(1420/924.7)では収まらないが500なら収まる
    spec.roof_faces = [_face(14200, 9247, target=40, roof_type="rikuyane")]
    place_panels(spec)
    assert spec.roof_faces[0].panel_count == 40, \
        f"目標優先で離隔緩和されるはず: {spec.roof_faces[0].panel_count}"
    # 離隔が明示されている場合は緩めない（収まらない→未達警告）
    spec2 = DraftingSpec()
    spec2.panel = _xsol_panel()
    f = _face(14200, 9247, target=40, roof_type="rikuyane")
    f.margin_ns_mm = 1500.0
    f.margin_ew_mm = 1500.0
    spec2.roof_faces = [f]
    place_panels(spec2)
    assert spec2.roof_faces[0].panel_count < 40, "明示離隔は緩和されないはず"
    assert any("しか配置できません" in w for w in spec2.warnings)


def test_kamakura_golden_case():
    """鎌倉警察署 滑川交番（低圧・465W×4枚）: 5980×4470 の陸屋根に
    2×2 横置きで4枚が10%離隔内に収まること（正解図面 2026-07-04 準拠）。"""
    spec = DraftingSpec()
    spec.panel = PanelSpec(maker="NextEnergy", model="NER108M465B-NE",
                           output_w=465, long_mm=1762, short_mm=1134,
                           gap_long_mm=235, gap_short_mm=10)
    spec.roof_faces = [_face(5980, 4470, target=4, name="陸屋根",
                             roof_type="rikuyane", orientation="landscape")]
    place_panels(spec)
    f = spec.roof_faces[0]
    assert f.panel_count == 4, f"4枚配置のはず: {f.panel_count}"
    assert f.cols == 2 and f.rows == 2, f"2列×2行のはず: {f.cols}×{f.rows}"
    assert abs(spec.total_kw - 1.86) < 0.001, spec.total_kw
    # 全パネルが屋根内・離隔(10%=EW598/NS447)以上を確保
    for pr in f.panels:
        assert pr.x_mm >= 598 - 1 and pr.x_mm + pr.w_mm <= 5980 - 598 + 1
        assert pr.y_mm >= 447 - 1 and pr.y_mm + pr.h_mm <= 4470 - 447 + 1


def test_engine_reproduces_correct_counts():
    """正解①相当の入力（面ごと寸法＋目標枚数）で68枚を正確に再現できること
    （エンジン到達度の回帰保証。2026-08-11 分析の実測に基づく）。"""
    spec = DraftingSpec()
    spec.panel = _xsol_panel()
    spec.roof_faces = [
        _face(11090, 14300, target=16, name="上部", roof_type="rikuyane"),
        _face(14200, 9247, target=40, name="下部左", roof_type="rikuyane"),
        _face(10130, 5550, target=12, name="下部右", roof_type="rikuyane"),
    ]
    # origin衝突の自動整列が入っても枚数には影響しない
    place_panels(spec)
    counts = [f.panel_count for f in spec.roof_faces]
    assert counts == [16, 40, 12], f"正解①の枚数を再現できない: {counts}"
    assert spec.total_panels == 68
    assert abs(spec.total_kw - 34.68) < 0.01, spec.total_kw


def main():
    tests = [
        test_zero_dims_guard,
        test_renderer_no_fake_grid_on_zero_dims,
        test_auto_arrange_overlapping_faces,
        test_no_rearrange_when_origins_valid,
        test_target_shortfall_warning,
        test_polygon_offset_vertices_no_shift,
        test_polygon_corner_containment,
        test_normalize_completes_panel_dims_from_model,
        test_normalize_totals_mismatch_warning,
        test_normalize_kw_mismatch_warning,
        test_prompt_new_rules,
        test_margin_10_percent_rule,
        test_margin_explicit_wins,
        test_target_priority_relaxes_auto_margin,
        test_kamakura_golden_case,
        test_engine_reproduces_correct_counts,
    ]
    print("=== 製図品質ガードテスト（API不要） ===")
    ok = True
    for fn in tests:
        try:
            fn()
            print(f"[OK] {fn.__name__}")
        except AssertionError as e:
            ok = False
            print(f"[NG] {fn.__name__}: {e}")
        except Exception as e:
            ok = False
            print(f"[NG] {fn.__name__}: 予期しないエラー: {type(e).__name__}: {e}")
    print("=== 結果:", "全パス" if ok else "一部失敗", "===")
    return ok


if __name__ == "__main__":
    success = main()
    raise SystemExit(0 if success else 1)
