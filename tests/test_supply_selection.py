"""支給品⇄材料費切替・単価マスタ顧客上書きのテスト（API不要・スクリプト式）

実行: python3 tests/test_supply_selection.py

背景（2026-08-13 顧客要望 / 8-10会議アクション）:
- 見積プレビューで商品ごとに「支給品かどうか」を選択 → 見積に反映。
  非支給品は材料費へ移動し、単価マスターの価格 × 数量で計上する
- 材料費の基本項目は ブレーカー/スペースボックス/電線/配管/配管 のみ
- 単価マスターはお客さん側で修正できる（Supabase+ローカルに永続化）

カバー範囲:
- 生成された材料費の基本構成（5項目・手動入力フラグ）
- 支給品チェックOFF → 材料費へ移動・マスター単価×数量・合計再計算
- マスター未登録 → 手動入力行として追加
- ON へ戻すと元の構成・合計に完全復元（冪等）
- 単価マスタの上書き保存・解除（ローカル永続化。Supabaseは遮断して検証）
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["SANEI_DISABLE_SUPABASE"] = "1"  # 本番Supabase遮断

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pricing.supply_selection as ss
from models.estimate_data import CategoryType
from models.survey_data import SurveyData
from pricing.pricing_engine import generate_estimate


def _make_estimate():
    survey = SurveyData()
    survey.equipment.planned_panels = 266
    survey.equipment.pv_capacity_kw = 135.66
    survey.equipment.module_model = "XLN120G-510X"
    return generate_estimate(survey, client_name="テスト株式会社")


def _section(est, cat):
    return next(c for c in est.summary.categories if c.category == cat)


def test_material_base_structure():
    """材料費の基本項目が ブレーカー/スペースボックス/電線/配管/配管 の5件であること。"""
    est = _make_estimate()
    mat = _section(est, CategoryType.MATERIAL)
    descs = [it.description for it in mat.items]
    assert descs == ["ブレーカー", "スペースボックス", "電線", "配管", "配管"], descs
    assert mat.items[0].amount == 51000, "ブレーカーは既定単価で計上されるはず"
    for it in mat.items[1:]:
        assert it.is_manual_input, f"{it.description} は手動入力のはず"
        assert it.amount == 0


def test_toggle_off_moves_to_material_with_master_price():
    """支給品チェックOFF → 材料費へ移動し、マスター単価×数量で計上されること。"""
    est = _make_estimate()
    snap = ss.snapshot_sections(est)
    target = snap["supplied"][0]  # 太陽光モジュール（266枚）
    assert "モジュール" in target.description

    orig_lookup = ss.master_price_for
    ss.master_price_for = lambda item: (13400, "単価マスター: TEST-MODULE")
    try:
        flags = {ss.item_key(target): False}
        ss.apply_supply_selection(est, snap, flags)
    finally:
        ss.master_price_for = orig_lookup

    sup = _section(est, CategoryType.SUPPLIED)
    mat = _section(est, CategoryType.MATERIAL)
    assert all("モジュール" not in it.description for it in sup.items), \
        "OFFにした商品は支給品から消えるはず"
    moved = [it for it in mat.items if "モジュール" in it.description]
    assert len(moved) == 1, "材料費に移動しているはず"
    assert moved[0].unit_price == 13400
    assert moved[0].amount == 13400 * 266, f"数量×マスター単価のはず: {moved[0].amount}"
    assert not moved[0].is_manual_input
    assert mat.total >= 13400 * 266
    assert est.summary.subtotal == sum(c.total for c in est.summary.categories), \
        "サマリーが再計算されているはず"


def test_toggle_off_without_master_price_is_manual():
    """マスター未登録の商品をOFF → 手動入力行として材料費に追加されること。"""
    est = _make_estimate()
    snap = ss.snapshot_sections(est)
    target = snap["supplied"][0]
    orig_lookup = ss.master_price_for
    ss.master_price_for = lambda item: (None, "")
    try:
        ss.apply_supply_selection(est, snap, {ss.item_key(target): False})
    finally:
        ss.master_price_for = orig_lookup
    mat = _section(est, CategoryType.MATERIAL)
    moved = [it for it in mat.items if "モジュール" in it.description]
    assert len(moved) == 1 and moved[0].is_manual_input
    assert moved[0].amount == 0


def test_toggle_roundtrip_restores_original():
    """OFF→ONで元の構成・合計に完全復元されること（冪等）。"""
    est = _make_estimate()
    snap = ss.snapshot_sections(est)
    before_supplied = [(it.no, it.description) for it in
                       _section(est, CategoryType.SUPPLIED).items]
    before_material = [(it.no, it.description) for it in
                       _section(est, CategoryType.MATERIAL).items]
    before_total = est.summary.total_with_tax

    target = snap["supplied"][0]
    orig_lookup = ss.master_price_for
    ss.master_price_for = lambda item: (13400, "単価マスター: TEST")
    try:
        ss.apply_supply_selection(est, snap, {ss.item_key(target): False})
        ss.apply_supply_selection(est, snap, {ss.item_key(target): True})
    finally:
        ss.master_price_for = orig_lookup

    after_supplied = [(it.no, it.description) for it in
                      _section(est, CategoryType.SUPPLIED).items]
    after_material = [(it.no, it.description) for it in
                      _section(est, CategoryType.MATERIAL).items]
    assert after_supplied == before_supplied, "支給品が復元されるはず"
    assert after_material == before_material, "材料費が復元されるはず"
    assert est.summary.total_with_tax == before_total, "合計が復元されるはず"


def test_moved_item_drops_goshikyu_label():
    """材料費へ移動した行から「御支給品」表記が除去されること（Codex指摘）。"""
    est = _make_estimate()
    snap = ss.snapshot_sections(est)
    target = snap["supplied"][0]
    assert "御支給品" in (target.remarks or ""), "前提: 支給品行には御支給品表記がある"
    orig_lookup = ss.master_price_for
    ss.master_price_for = lambda item: (13400, "単価マスター: TEST")
    try:
        ss.apply_supply_selection(est, snap, {ss.item_key(target): False})
    finally:
        ss.master_price_for = orig_lookup
    mat = _section(est, CategoryType.MATERIAL)
    moved = next(it for it in mat.items if "モジュール" in it.description)
    assert "御支給品" not in (moved.remarks or ""), \
        f"購入品に御支給品表記が残っている: {moved.remarks!r}"


def test_user_edits_survive_toggle():
    """単価マスターからの明細追加・手動編集が、支給品切替で消えないこと
    （Codex指摘: スナップショット再構築による編集消失の防止）。"""
    import copy as _copy
    est = _make_estimate()
    snap = ss.snapshot_sections(est)
    mat = _section(est, CategoryType.MATERIAL)
    # ユーザー操作を再現: 手動行の単価編集 + マスターからの明細追加
    mat.items[1].unit_price = 8000
    mat.items[1].amount = 8000
    extra = _copy.deepcopy(mat.items[0])
    extra.description = "追加部材（マスターから）"
    extra.unit_price = 12345
    extra.amount = 12345
    mat.items.append(extra)
    mat.calculate_totals()

    target = snap["supplied"][0]
    orig_lookup = ss.master_price_for
    ss.master_price_for = lambda item: (13400, "単価マスター: TEST")
    try:
        ss.apply_supply_selection(est, snap, {ss.item_key(target): False})
        ss.apply_supply_selection(est, snap, {ss.item_key(target): True})
    finally:
        ss.master_price_for = orig_lookup

    mat2 = _section(est, CategoryType.MATERIAL)
    descs = [it.description for it in mat2.items]
    assert "追加部材（マスターから）" in descs, f"追加明細が消えた: {descs}"
    assert mat2.items[1].unit_price == 8000, "手動編集した単価が消えた"
    assert all("モジュール" not in d for d in descs), "ONに戻した商品が材料費に残っている"


def test_price_master_override_roundtrip():
    """単価マスタの上書き保存 → 反映 → 解除で元に戻ること（顧客側の単価修正）。"""
    from product import price_master as pm
    tmp = Path(tempfile.mkdtemp())
    orig_path = pm.OVERRIDES_PATH
    pm.OVERRIDES_PATH = tmp / "price_master_overrides.json"
    try:
        pm._OVERRIDES_CACHE["loaded"] = False
        pm._CACHE["sig"] = None
        products = pm.get_products()
        assert products, "単価マスタが読み込めるはず"
        target = next(p for p in products if p.get("unit_price"))
        pid = target["id"]
        base_price = int(target["unit_price"])

        assert pm.set_price_override(pid, base_price + 1234), "保存が成功するはず"
        p2 = next(p for p in pm.get_products() if p["id"] == pid)
        assert p2["unit_price"] == base_price + 1234, "上書きが反映されるはず"
        assert p2.get("price_overridden") is True
        assert p2.get("base_unit_price") == base_price, "元単価が保持されるはず"

        assert pm.set_price_override(pid, None), "解除が成功するはず"
        p3 = next(p for p in pm.get_products() if p["id"] == pid)
        assert p3["unit_price"] == base_price, "解除で元単価に戻るはず"
        assert not p3.get("price_overridden")
    finally:
        pm.OVERRIDES_PATH = orig_path
        pm._OVERRIDES_CACHE["loaded"] = False
        pm._CACHE["sig"] = None


def main():
    tests = [
        test_material_base_structure,
        test_toggle_off_moves_to_material_with_master_price,
        test_toggle_off_without_master_price_is_manual,
        test_toggle_roundtrip_restores_original,
        test_moved_item_drops_goshikyu_label,
        test_user_edits_survive_toggle,
        test_price_master_override_roundtrip,
    ]
    print("=== 支給品切替・単価マスタ上書きテスト（API不要） ===")
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
