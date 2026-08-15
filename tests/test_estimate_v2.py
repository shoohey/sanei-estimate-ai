"""v4.0 見積v2（4大分類）・設計確定情報・支給品属性のテスト

実行: SANEI_DISABLE_SUPABASE=1 python3 -m pytest tests/test_estimate_v2.py -q

2026-08-15 顧客ルールブック準拠の検証:
【見積側】
- 3条: 大分類は「1.共通仮設工事 2.太陽光発電システム機器 3.電材 4.設置工事」固定
- 1条: 設計確定情報（handoff）を正とし、枚数を再判断しない
- 4条: 支給品は属性（機器カテゴリに残す・備考「御支給品」・金額0円・工事費は0にしない）
- 6条: 原価×(1+粗利率) → 客出し単価（100円丸め）
- 7条: 不足情報は補完せず 手動入力＋要確認
- 学習: 単価のみ（item_add/item_suppress は4大分類に適用されない）
【図面側】
- 10条: 図面完成時の設計確定情報（不明は「未確認」・配置不可フラグ）
"""
import os
import sys
from pathlib import Path

os.environ["SANEI_DISABLE_SUPABASE"] = "1"  # 本番Supabase遮断（import前に必須）

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pricing.estimate_v2 as ev2
from models.estimate_data import CategoryType
from models.survey_data import SurveyData
from pricing.estimate_v2 import generate_estimate_v2
from pricing.knowledge_base import load_pricing_rules


def _survey(panels: int = 8, watt: float = 465) -> SurveyData:
    s = SurveyData()
    s.project.project_name = "鶴見警察署市場交番"
    s.equipment.module_maker = "ネクストエナジー"
    s.equipment.module_model = "NER108M465B-NE"
    s.equipment.module_output_w = watt
    s.equipment.planned_panels = panels
    s.equipment.pv_capacity_kw = round(panels * watt / 1000, 3)
    return s


def _rules(**over):
    rules = dict(load_pricing_rules())
    rules.update(over)
    return rules


def _section(est, cat):
    return next(c for c in est.summary.categories if c.category == cat)


# =============================================================
# 3条: 4大分類の構成
# =============================================================

def test_v2_four_categories_fixed_order():
    est = generate_estimate_v2(_survey(), _rules(), "テスト株式会社")
    cats = [c.category for c in est.summary.categories]
    assert cats == [CategoryType.SETUP, CategoryType.EQUIPMENT,
                    CategoryType.WIRING, CategoryType.INSTALL], cats
    assert [c.category_number for c in est.summary.categories] == [1, 2, 3, 4]
    assert [c.category.value for c in est.summary.categories] == \
        ["共通仮設工事", "太陽光発電システム機器", "電材", "設置工事"]


def test_v2_is_default_via_generate_estimate():
    """pricing_engine.generate_estimate の既定が v2 であること。"""
    from pricing.pricing_engine import generate_estimate
    est = generate_estimate(_survey(), client_name="テスト株式会社")
    cats = [c.category for c in est.summary.categories]
    assert CategoryType.EQUIPMENT in cats
    assert CategoryType.SUPPLIED not in cats, "v1の支給品大分類は作らない"


def test_v2_wiring_base_items():
    """電材の基本構成（ブレーカー＋盤・ケーブル類＋配管）が載ること。"""
    est = generate_estimate_v2(_survey(), _rules(), "テスト株式会社")
    wiring = _section(est, CategoryType.WIRING)
    descs = [it.description for it in wiring.items]
    assert any("ブレーカー" in d for d in descs), descs
    assert any("配管" in d for d in descs), descs


# =============================================================
# 1条: 設計確定情報を正とする（枚数の再判断禁止）
# =============================================================

def test_v2_module_uses_handoff_panel_count(monkeypatch):
    """図面側の実配置枚数（handoff）が現調の指示枚数より優先されること。"""
    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (16550, "単価マスター: TEST"))
    est = generate_estimate_v2(
        _survey(panels=10), _rules(margin_rate=0.0), "テスト",
        handoff={"モジュール枚数": "8枚"})
    eq = _section(est, CategoryType.EQUIPMENT)
    module = next(it for it in eq.items if "モジュール" in it.description)
    assert module.quantity_value == 8, "図面側の8枚が正（現調10枚を再判断しない）"
    assert module.unit_price == 16550
    assert module.amount == 8 * 16550


def test_v2_module_manual_when_master_missing(monkeypatch):
    """単価マスター未登録 → 手動入力行＋金額0（勝手に補完しない・7条）。"""
    monkeypatch.setattr(ev2, "_master_price", lambda model, description="": (None, ""))
    est = generate_estimate_v2(_survey(panels=8), _rules(), "テスト")
    eq = _section(est, CategoryType.EQUIPMENT)
    module = next(it for it in eq.items if "モジュール" in it.description)
    assert module.is_manual_input
    assert module.unit_price == 0 and module.amount == 0


# =============================================================
# 6条: 原価 → 粗利 → 客出し
# =============================================================

def test_v2_margin_rate_applied_and_rounded(monkeypatch):
    """原価×(1+粗利率) を100円単位に丸めて客出し単価にすること。"""
    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (16550, "単価マスター: TEST"))
    est = generate_estimate_v2(
        _survey(panels=8), _rules(margin_rate=0.2), "テスト",
        handoff={"モジュール枚数": "8枚"})
    eq = _section(est, CategoryType.EQUIPMENT)
    module = next(it for it in eq.items if "モジュール" in it.description)
    # 16550 × 1.2 = 19860 → 100円丸めで 19900
    assert module.unit_price == 19900, module.unit_price
    assert module.amount == 8 * 19900


def test_v2_sell_price_zero_margin_is_cost():
    assert ev2._sell_price(16550, 0.0) == 16550
    assert ev2._sell_price(None, 0.2) is None


# =============================================================
# 7条: 未確認・設計確認の明示
# =============================================================

def test_v2_unconfirmed_handoff_noted_in_reasoning():
    est = generate_estimate_v2(
        _survey(), _rules(), "テスト",
        handoff={"モジュール枚数": "8枚", "主幹容量": "未確認"})
    joined = "\n".join(est.reasoning_list)
    assert "【要確認】" in joined and "主幹容量" in joined


def test_v2_placement_failure_flagged_as_design_check():
    est = generate_estimate_v2(
        _survey(), _rules(), "テスト", handoff={"_配置不可": True})
    joined = "\n".join(est.reasoning_list)
    assert "【設計確認】" in joined and "指示枚数配置不可" in joined


# =============================================================
# 4条: 支給品は属性（機器カテゴリに残す・工事費は0にしない）
# =============================================================

def test_v2_supply_attribute_toggle(monkeypatch):
    import pricing.supply_selection as ss

    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (16550, "単価マスター: TEST"))
    est = generate_estimate_v2(
        _survey(panels=8), _rules(margin_rate=0.0), "テスト",
        handoff={"モジュール枚数": "8枚"})
    eq = _section(est, CategoryType.EQUIPMENT)
    install = _section(est, CategoryType.INSTALL)
    install_total_before = install.total
    module = next(it for it in eq.items if "モジュール" in it.description)
    key = ss.item_key(module)
    snap = ss.snapshot_equipment(est)

    # ON: 御支給品 → 機器カテゴリに残り ¥0・備考「御支給品」
    ss.apply_supply_attribute(est, snap, {key: True})
    eq = _section(est, CategoryType.EQUIPMENT)
    module = next(it for it in eq.items if "モジュール" in it.description)
    assert module.unit_price == 0 and module.amount == 0
    assert "御支給品" in module.remarks
    assert _section(est, CategoryType.INSTALL).total == install_total_before, \
        "支給品にしても設置工事（工事費）は0にしない"

    # OFF: 元の購入価格に完全復元（冪等）
    ss.apply_supply_attribute(est, snap, {key: False})
    module = next(it for it in _section(est, CategoryType.EQUIPMENT).items
                  if "モジュール" in it.description)
    assert module.unit_price == 16550 and module.amount == 8 * 16550
    assert "御支給品" not in module.remarks


def test_v2_supply_toggle_recomputes_discount(monkeypatch):
    """支給品化で小計が減ったとき値引きを再計算し、税抜がマイナスにならないこと
    （Codexレビュー指摘: 旧値引きのままだと大口機器の支給品化で合計が壊れる）。"""
    import pricing.supply_selection as ss

    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (16550, "単価マスター: TEST"))
    est = generate_estimate_v2(
        _survey(panels=8), _rules(margin_rate=0.0), "テスト",
        handoff={"モジュール枚数": "8枚"})
    eq = _section(est, CategoryType.EQUIPMENT)
    module = next(it for it in eq.items if "モジュール" in it.description)
    snap = ss.snapshot_equipment(est)
    ss.apply_supply_attribute(est, snap, {ss.item_key(module): True})
    assert est.summary.total_before_tax >= 0
    assert est.summary.total_before_tax == \
        (est.summary.subtotal // 10000) * 10000, \
        "値引きは新しい小計で切捨て再計算されるはず"
    assert est.cover.total_with_tax == est.summary.total_with_tax


def test_v2_parse_count_no_digit_concatenation(monkeypatch):
    """内訳付き枚数「東4枚・西4枚（合計8枚）」を448枚と誤読しないこと
    （Codexレビュー指摘: 全数字連結の防止）。"""
    assert ev2._parse_count("8枚", "枚") == 8
    assert ev2._parse_count("東4枚・西4枚（合計8枚）", "枚") == 8
    assert ev2._parse_count("東4枚・西4枚", "枚") == 0, "合計表記なしの内訳は不採用"
    assert ev2._parse_count("8", "枚") == 8
    assert ev2._parse_count("2台", "台") == 2
    assert ev2._parse_count("未確認", "台") == 0

    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (16550, "単価マスター: TEST"))
    est = generate_estimate_v2(
        _survey(panels=10), _rules(margin_rate=0.0), "テスト",
        handoff={"モジュール枚数": "東4枚・西4枚（合計8枚）"})
    eq = _section(est, CategoryType.EQUIPMENT)
    module = next(it for it in eq.items if "モジュール" in it.description)
    assert module.quantity_value == 8, module.quantity_value


def test_v2_pcs_manual_row_when_model_unknown():
    """PCS型式が未確認でも台数等が分かっていれば手動入力行を立てること
    （Codexレビュー指摘: 明細の欠落防止。補完はしない）。"""
    est = generate_estimate_v2(
        _survey(), _rules(), "テスト",
        handoff={"PCS台数": "2台", "PCSメーカー": "HUAWEI"})
    eq = _section(est, CategoryType.EQUIPMENT)
    pcs = [it for it in eq.items if "パワーコンディショナ" in it.description]
    assert len(pcs) == 1, "型式不明でもPCS行が欠落しない"
    assert pcs[0].is_manual_input
    assert pcs[0].quantity_value == 2 and pcs[0].amount == 0


def test_v2_handoff_supplied_auto_applied(monkeypatch):
    """設計確定情報「支給品」に含まれる機器は自動で御支給品¥0になること
    （Codexレビュー指摘: 引き継いだのに購入品として過大計上される）。"""
    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (16550, "単価マスター: TEST"))
    est = generate_estimate_v2(
        _survey(panels=8), _rules(margin_rate=0.0), "テスト",
        handoff={"モジュール枚数": "8枚",
                 "支給品": "太陽光モジュール・PCSは御支給品"})
    eq = _section(est, CategoryType.EQUIPMENT)
    module = next(it for it in eq.items if "モジュール" in it.description)
    assert module.unit_price == 0 and module.amount == 0
    assert "御支給品" in module.remarks


def test_v2_handoff_supplied_can_revert_to_purchase(monkeypatch):
    """設計確定情報で自動支給品化された明細をUIでOFFに戻すと購入価格に復元
    できること（Codexレビュー指摘: ¥0化後スナップショットでは復元不能）。"""
    import pricing.supply_selection as ss

    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (16550, "単価マスター: TEST"))
    est = generate_estimate_v2(
        _survey(panels=8), _rules(margin_rate=0.0), "テスト",
        handoff={"モジュール枚数": "8枚", "支給品": "太陽光モジュール"})
    eq = _section(est, CategoryType.EQUIPMENT)
    module = next(it for it in eq.items if "モジュール" in it.description)
    assert module.amount == 0, "生成時点で自動支給品化されている前提"

    snap = ss.snapshot_equipment(est)
    flags = ss.initial_supply_flags(est)
    key = next(iter(flags))
    assert flags[key] is True, "自動支給品は初期チェックON"

    ss.apply_supply_attribute(est, snap, {key: False})
    module = next(it for it in _section(est, CategoryType.EQUIPMENT).items
                  if "モジュール" in it.description)
    assert module.unit_price == 16550 and module.amount == 8 * 16550, \
        "OFFに戻すと購入価格（単価マスター）に復元される"
    assert "御支給品" not in module.remarks


def test_v2_unconfirmed_count_becomes_manual_row(monkeypatch):
    """図面側の枚数が「未確認」なら現調枚数で確定計上せず手動行にすること
    （Codexレビュー指摘: 未確認の図面を確定枚数として見積もらない）。"""
    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (16550, "単価マスター: TEST"))
    est = generate_estimate_v2(
        _survey(panels=10), _rules(), "テスト",
        handoff={"モジュール枚数": "未確認"})
    eq = _section(est, CategoryType.EQUIPMENT)
    module = next(it for it in eq.items if "モジュール" in it.description)
    assert module.is_manual_input
    assert module.amount == 0
    assert "（仮）" in module.quantity

    # handoff なし（図面フローを使わない従来運用）は現調枚数で計上してよい
    est2 = generate_estimate_v2(_survey(panels=10), _rules(margin_rate=0.0),
                                "テスト", handoff=None)
    module2 = next(it for it in _section(est2, CategoryType.EQUIPMENT).items
                   if "モジュール" in it.description)
    assert module2.amount == 10 * 16550


def test_v2_master_price_exact_match_only(monkeypatch):
    """部分型式（SUN2000等）の曖昧一致で別型式の価格を拾わないこと
    （Codexレビュー指摘: 金額に直結する照合は完全一致のみ）。"""
    from product import price_master as pm

    calls = {}

    def _fake_find(q, fuzzy=True):
        calls["fuzzy"] = fuzzy
        return []

    monkeypatch.setattr(pm, "find_by_model", _fake_find)
    cost, source = ev2._master_price("SUN2000", "パワーコンディショナ")
    assert cost is None
    assert calls.get("fuzzy") is False, "fuzzy=False で照合するはず"


def test_v2_supply_toggle_preserves_manual_edits(monkeypatch):
    """支給品チェックの付け外しで、他の機器行の手動編集（架台単価等）が
    消えないこと（Codexレビュー指摘: 全行の原本再構築は編集を失う）。"""
    import pricing.supply_selection as ss

    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (16550, "単価マスター: TEST"))
    est = generate_estimate_v2(
        _survey(panels=8), _rules(margin_rate=0.0), "テスト",
        handoff={"モジュール枚数": "8枚"})
    snap = ss.snapshot_equipment(est)
    eq = _section(est, CategoryType.EQUIPMENT)
    module = next(it for it in eq.items if "モジュール" in it.description)
    rack = next(it for it in eq.items if it.description == "架台")

    # 架台の単価を手動編集（UIのカテゴリ編集相当）
    rack.unit_price = 200000
    rack.amount = 200000
    eq.calculate_totals()

    # モジュールを支給品化 → 架台の編集は残る
    ss.apply_supply_attribute(est, snap, {ss.item_key(module): True})
    rack2 = next(it for it in _section(est, CategoryType.EQUIPMENT).items
                 if it.description == "架台")
    assert rack2.unit_price == 200000 and rack2.amount == 200000, \
        "支給品切替で架台の手動編集が消えてはいけない"


def test_v2_learned_price_not_marked_up_again(monkeypatch):
    """学習単価（客出し実績値）に粗利率を重ね掛けしないこと（Codexレビュー指摘）。"""
    import learning.apply_estimate as ae
    from learning.estimate_diff import normalize_desc

    rules = {"wiring_items": [
        {"no": 1, "description": "ブレーカー", "remarks": "",
         "quantity": 1, "quantity_unit": "式", "unit_price": 49000,
         "pricing_method": "fixed", "note": ""}]}
    learned = [{"id": "t9", "kind": "unit_price_override", "category": "電材",
                "match_description": normalize_desc("ブレーカー"),
                "payload": {"unit_price": 52000, "old_unit_price": 49000}}]
    monkeypatch.setattr(ae.store, "enabled_rules", lambda kind: learned)
    out = ae.apply_learned_rules(rules)
    assert out["wiring_items"][0].get("learned_price") is True

    est = generate_estimate_v2(
        _survey(), {"setup_items": [], "install_items": [],
                    "wiring_items": out["wiring_items"],
                    "margin_rate": 0.2, "discount_method": "none"}, "テスト")
    wiring = _section(est, CategoryType.WIRING)
    breaker = next(it for it in wiring.items if it.description == "ブレーカー")
    assert breaker.unit_price == 52000, \
        f"学習値のまま（粗利20%を重ねて¥62,400にしない）: {breaker.unit_price}"


def test_handoff_panel_count_survives_missing_wattage():
    """W数未読取でも実配置枚数は設計確定情報に引き継ぐこと（Codexレビュー指摘）。"""
    from drafting.design_handoff import build_design_handoff
    spec = _placed_spec()
    spec.panel.output_w = 0
    h = build_design_handoff(spec)
    assert h["モジュール枚数"] == "8枚"
    assert h["PV容量"] == "未確認", "容量はW数が無ければ計算しない（推測禁止）"


def test_v2_cover_capacity_uses_handoff():
    """表紙のkW表記も設計確定情報のPV容量を正とすること（Codexレビュー指摘）。"""
    est = generate_estimate_v2(
        _survey(panels=10), _rules(), "テスト",
        handoff={"モジュール枚数": "8枚", "PV容量": "3.720kW"})
    assert "3.72" in est.cover.project_name, est.cover.project_name


def test_v2_supply_toggle_preserves_manual_discount(monkeypatch):
    """手動設定した値引きが支給品切替で上書きされないこと（Codexレビュー指摘）。"""
    import pricing.supply_selection as ss

    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (16550, "単価マスター: TEST"))
    est = generate_estimate_v2(
        _survey(panels=8), _rules(margin_rate=0.0), "テスト",
        handoff={"モジュール枚数": "8枚"})
    module = next(it for it in _section(est, CategoryType.EQUIPMENT).items
                  if "モジュール" in it.description)
    snap = ss.snapshot_equipment(est)

    # 値引き調整UI相当: 手動で値引きを設定
    est.summary.discount = -50000
    est.summary.total_before_tax = est.summary.subtotal - 50000

    ss.apply_supply_attribute(est, snap, {ss.item_key(module): True})
    assert est.summary.discount == -50000, "手動値引きは支給品切替後も維持"
    assert est.summary.total_before_tax == est.summary.subtotal - 50000


def test_v2_manual_edit_recomputes_auto_discount(monkeypatch):
    """手動明細の金額入力後、自動値引き（端数切捨て）が新小計で再計算される
    こと。手動で設定した値引きは維持されること（Codexレビュー指摘）。"""
    from generation.estimate_builder import update_line_item

    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (16550, "単価マスター: TEST"))
    est = generate_estimate_v2(
        _survey(panels=8), _rules(margin_rate=0.0), "テスト",
        handoff={"モジュール枚数": "8枚"})
    assert est.summary.discount == \
        (est.summary.subtotal // 10000) * 10000 - est.summary.subtotal

    # 電材の手動行（盤・スペースボックス）に金額を入力 → 値引きを再計算
    wiring_idx = next(i for i, c in enumerate(est.summary.categories)
                      if c.category == CategoryType.WIRING)
    item_idx = next(i for i, it in enumerate(
        est.summary.categories[wiring_idx].items) if it.is_manual_input)
    update_line_item(est, wiring_idx, item_idx, unit_price=106000)
    assert est.summary.total_before_tax == \
        (est.summary.subtotal // 10000) * 10000, \
        "編集後も切捨て値引きが新小計で再計算されるはず"

    # 手動値引き（値引き調整UI相当）を設定 → 以後の編集で上書きされない
    est.summary.discount = -50000
    est.summary.total_before_tax = est.summary.subtotal - 50000
    update_line_item(est, wiring_idx, item_idx, unit_price=110000)
    assert est.summary.discount == -50000, "手動値引きは維持されるはず"


# =============================================================
# 学習: 単価のみ（v2の4大分類は構成固定）
# =============================================================

def test_v2_learning_price_override_applies_to_wiring(monkeypatch):
    """unit_price_override は電材（wiring_items）に適用されること。"""
    import learning.apply_estimate as ae
    from learning.estimate_diff import normalize_desc

    rules = {"wiring_items": [
        {"no": 1, "description": "ブレーカー", "remarks": "",
         "quantity": 1, "quantity_unit": "式", "unit_price": 49000,
         "pricing_method": "fixed", "note": ""},
    ]}
    learned = [{"id": "t1", "kind": "unit_price_override", "category": "電材",
                "match_description": normalize_desc("ブレーカー"),
                "payload": {"unit_price": 52000, "old_unit_price": 49000}}]
    monkeypatch.setattr(ae.store, "enabled_rules", lambda kind: learned)
    out = ae.apply_learned_rules(rules)
    assert out["wiring_items"][0]["unit_price"] == 52000


def test_v2_learning_add_and_suppress_skipped(monkeypatch):
    """item_add / item_suppress は4大分類に適用されないこと（構成固定）。"""
    import learning.apply_estimate as ae
    from learning.estimate_diff import normalize_desc

    rules = {
        "setup_items": [
            {"no": 1, "description": "足場", "remarks": "", "quantity": 1,
             "quantity_unit": "式", "unit_price": 180000,
             "pricing_method": "fixed", "note": ""}],
        "wiring_items": [],
    }
    learned = [
        {"id": "t2", "kind": "item_suppress", "category": "共通仮設工事",
         "match_description": normalize_desc("足場"), "payload": {}},
        {"id": "t3", "kind": "item_add", "category": "電材",
         "match_description": normalize_desc("PVケーブル"),
         "payload": {"category": "電材", "description": "PVケーブル",
                     "unit_price": 30000}},
    ]
    monkeypatch.setattr(ae.store, "enabled_rules", lambda kind: learned)
    out = ae.apply_learned_rules(rules)
    assert [it["description"] for it in out["setup_items"]] == ["足場"], \
        "item_suppress は共通仮設工事に適用しない"
    assert out["wiring_items"] == [], "item_add は電材に適用しない"


def test_v2_pcs_row_present_in_survey_only_flow():
    """設計確定情報が無い現調のみのフローでもPCS行が欠落しないこと
    （Codexレビュー指摘: PCSは必須機器。不明なら手動行）。"""
    est = generate_estimate_v2(_survey(), _rules(), "テスト", handoff=None)
    pcs = [it for it in _section(est, CategoryType.EQUIPMENT).items
           if "パワーコンディショナ" in it.description]
    assert len(pcs) == 1
    assert pcs[0].is_manual_input and pcs[0].amount == 0


def test_v2_pcs_count_unconfirmed_not_priced(monkeypatch):
    """PCS型式が分かっても台数未確認なら1台で確定計上しないこと
    （Codexレビュー指摘: 未確認は補完しない）。"""
    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (500000, "単価マスター: TEST"))
    est = generate_estimate_v2(
        _survey(), _rules(), "テスト",
        handoff={"PCS型式": "SUN2000-10KTL", "PCS台数": "未確認"})
    pcs = next(it for it in _section(est, CategoryType.EQUIPMENT).items
               if "パワーコンディショナ" in it.description)
    assert pcs.is_manual_input and pcs.amount == 0
    assert "（仮）" in pcs.quantity

    # 台数確定なら台数×マスター価格で計上
    est2 = generate_estimate_v2(
        _survey(), _rules(margin_rate=0.0), "テスト",
        handoff={"PCS型式": "SUN2000-10KTL", "PCS台数": "2台"})
    pcs2 = next(it for it in _section(est2, CategoryType.EQUIPMENT).items
                if "パワーコンディショナ" in it.description)
    assert pcs2.amount == 2 * 500000


def test_v2_cover_capacity_unconfirmed_not_borrowed_from_survey():
    """図面側でPV容量が未確認なら表紙に現調容量を流用せず「容量未確認」と
    表示すること（Codexレビュー指摘）。"""
    est = generate_estimate_v2(
        _survey(panels=10), _rules(), "テスト",
        handoff={"PV容量": "未確認", "モジュール枚数": "未確認"})
    assert "容量未確認" in est.cover.project_name
    # handoff なしの従来運用は現調容量のまま
    est2 = generate_estimate_v2(_survey(panels=10), _rules(), "テスト")
    assert "kW" in est2.cover.project_name


def test_v2_negated_supply_text_not_treated_as_supplied(monkeypatch):
    """「モジュール支給なし」等の否定表現で支給品化しないこと（Codexレビュー指摘）。"""
    monkeypatch.setattr(ev2, "_master_price",
                        lambda model, description="": (16550, "単価マスター: TEST"))
    for text in ("モジュール支給なし", "PCS・モジュールは支給品ではない",
                 "パネルは支給しない"):
        est = generate_estimate_v2(
            _survey(panels=8), _rules(margin_rate=0.0), "テスト",
            handoff={"モジュール枚数": "8枚", "PCS型式": "SUN2000-10KTL",
                     "支給品": text})
        eq_items = _section(est, CategoryType.EQUIPMENT).items
        module = next(it for it in eq_items if "モジュール" in it.description)
        assert module.amount == 8 * 16550, f"{text!r} で¥0化してはいけない"
        assert "御支給品" not in (module.remarks or "")
        # 中黒列挙の否定（PCS・モジュールは〜ではない）でPCS側も¥0化しない
        pcs = next(it for it in eq_items
                   if "パワーコンディショナ" in it.description)
        assert "御支給品" not in (pcs.remarks or ""), \
            f"{text!r} でPCSを支給品化してはいけない"

    # 肯定表現は従来どおり支給品化される
    est = generate_estimate_v2(
        _survey(panels=8), _rules(margin_rate=0.0), "テスト",
        handoff={"モジュール枚数": "8枚", "支給品": "太陽光モジュール・PCSは御支給品"})
    module = next(it for it in _section(est, CategoryType.EQUIPMENT).items
                  if "モジュール" in it.description)
    assert module.amount == 0 and "御支給品" in module.remarks


def test_handoff_extracted_count_not_promoted_when_zero_placed():
    """配置0枚のとき抽出テキストの指示枚数が設計確定に化けないこと
    （Codexレビュー指摘: 枚数・容量は実配置からのみ確定する）。"""
    from drafting.design_handoff import build_design_handoff
    spec = _placed_spec()
    spec.roof_faces[0].panels = []
    spec.roof_faces[0].panel_count = 0
    spec.handoff["モジュール枚数"] = "10枚"  # 抽出された指示値
    spec.handoff["PV容量"] = "4.650kW"
    h = build_design_handoff(spec)
    assert h["モジュール枚数"] == "未確認"
    assert h["PV容量"] == "未確認"


def test_v2_equipment_price_diff_not_learnable():
    """機器カテゴリの単価差分は学習提案せず単価マスター修正へ誘導すること
    （Codexレビュー指摘: 反映先リストが無くno-opになるルールを承認させない）。"""
    from learning.estimate_diff import diff_estimates
    from learning.models import ParsedEstimate, ParsedLineItem

    def _pe(source, price):
        return ParsedEstimate(source=source, items=[ParsedLineItem(
            category="太陽光発電システム機器", no=1,
            description="太陽光モジュール", remarks="NER108M465B-NE",
            quantity_value=8, quantity_unit="枚",
            unit_price=price, amount=price * 8)])

    diffs = diff_estimates(_pe("ai", 16550), _pe("official", 17000))
    price_diffs = [d for d in diffs if d.diff_type == "price_changed"]
    assert price_diffs, "単価差分自体は検出される"
    assert all(not d.learnable for d in price_diffs)
    assert all("単価マスター" in d.summary for d in price_diffs)


# =============================================================
# 図面側10条: 設計確定情報の組み立て
# =============================================================

def _placed_spec():
    from drafting.models import (
        DraftingSpec, PanelRect, PanelSpec, RoofFace, RoofType)
    spec = DraftingSpec()
    spec.customer_name = "鶴見警察署市場交番"
    spec.panel = PanelSpec(maker="ネクストエナジー", model="NER108M465B-NE",
                           output_w=465, long_mm=1722, short_mm=1134)
    face = RoofFace(width_mm=8000, depth_mm=6750, roof_type=RoofType.SLATE)
    face.panels = [PanelRect(x_mm=0, y_mm=0, w_mm=1722, h_mm=1134)
                   for _ in range(8)]
    face.panel_count = 8
    spec.roof_faces = [face]
    spec.pcs_model = "SUN2000-10KTL"
    spec.pcs_count = 1
    spec.handoff = {"電圧区分": "低圧", "配管ルート": "外壁沿い→点検口"}
    return spec


def test_handoff_build_from_placed_spec():
    from drafting.design_handoff import build_design_handoff, unconfirmed_items
    h = build_design_handoff(_placed_spec())
    assert h["案件名"] == "鶴見警察署市場交番"
    assert h["モジュール枚数"] == "8枚"
    assert h["PV容量"] == "3.720kW"
    assert h["PCS型式"] == "SUN2000-10KTL"
    assert h["電圧区分"] == "低圧", "抽出時handoffがマージされる"
    assert h["配管ルート"] == "外壁沿い→点検口"
    assert h["主幹容量"] == "未確認", "不明は未確認（推測しない）"
    assert h["_配置不可"] is False
    assert "主幹容量" in unconfirmed_items(h)


def test_handoff_merge_does_not_overwrite_placed_values():
    """抽出テキスト由来の枚数・容量が実配置から確定した値を上書きしないこと
    （Codexレビュー指摘: 図面側の実配置が正）。"""
    from drafting.design_handoff import build_design_handoff
    spec = _placed_spec()
    spec.handoff["モジュール枚数"] = "10枚"  # 指示値（実配置は8枚）
    spec.handoff["PV容量"] = "4.650kW"
    h = build_design_handoff(spec)
    assert h["モジュール枚数"] == "8枚", "実配置8枚が正（指示10枚で潰さない）"
    assert h["PV容量"] == "3.720kW"
    assert h["電圧区分"] == "低圧", "未確認の項目は抽出値で埋まる"


def test_handoff_zero_placed_stays_unconfirmed():
    """配置0枚のとき枚数・容量は指示値で埋めず未確認のままにすること。"""
    from drafting.design_handoff import build_design_handoff
    spec = _placed_spec()
    spec.roof_faces[0].panels = []
    spec.roof_faces[0].panel_count = 0
    h = build_design_handoff(spec)
    assert h["モジュール枚数"] == "未確認"
    assert h["PV容量"] == "未確認"


def test_handoff_placement_failure_flag():
    from drafting.design_handoff import build_design_handoff
    spec = _placed_spec()
    spec.warnings = ["【指示枚数配置不可】指示8枚に対し配置可能6枚"]
    h = build_design_handoff(spec)
    assert h["_配置不可"] is True
    assert h["_確認事項件数"] == 1


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
