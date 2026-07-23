"""修正③（2026-07-23 会議）: パネル設置工事の枚数ベース計算テスト

パネル取付工事・架台取付工事（枚数連動項目）が
「数量=実枚数・単位=枚・単価×枚数」の明細になり（「一式」廃止）、
枚数変更に金額が比例して追従すること、学習ルール（unit_price_override）
適用後も「単価への学習」として枚数比例が保たれること（金額固定化なし）を検証する。

サンプル: テックランド掛川店（660W × 288枚 = 190.08kW）
  現行 kW連動: 190.08kW × ¥3,300/kW = ¥627,264
  枚数連動:    288枚 × ¥2,178/枚   = ¥627,264（総額の回帰なし）

実行: python3 -m pytest tests/test_pricing_per_panel.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import learning.storage_backend as storage_backend
import learning.store as store
from learning.estimate_diff import normalize_desc
from models.survey_data import SurveyData
from pricing.pricing_engine import generate_estimate

PANEL_ITEM = "パネル取付工事"  # 会議で言う「パネル設置工事（設置費用）」の明細項目
RACK_ITEM = "架台取付工事"    # 枚数連動の関連項目（架台）
PER_PANEL_PRICE = 2178        # ¥3,300/kW × 0.66kW（660W）= ¥2,178/枚


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """学習ストアを一時ディレクトリへ差し替え、Supabase を無効化する。

    本番の学習済みルール（knowledge/・Supabase app_storage）に影響されず、
    かつ実 knowledge/ を汚さないための分離。monkeypatch が終了時に自動復元する。
    """
    monkeypatch.setattr(store, "ESTIMATE_RULES_PATH",
                        tmp_path / "learned_estimate_rules.json")
    monkeypatch.setattr(store, "DRAWING_RULES_PATH",
                        tmp_path / "learned_drawing_rules.json")
    monkeypatch.setattr(store, "LEARNING_LOG_PATH",
                        tmp_path / "learning_history.json")
    monkeypatch.setattr(storage_backend, "is_enabled", lambda: False)
    yield


def _survey(panels: int, output_w: float = 660) -> SurveyData:
    """掛川店サンプル相当の現調データを作る。"""
    s = SurveyData()
    s.project.project_name = "テックランド掛川店"
    s.equipment.module_maker = "LONGI"
    s.equipment.module_model = "LR7-72HVH-660M"
    s.equipment.module_output_w = output_w
    s.equipment.planned_panels = panels
    s.equipment.pv_capacity_kw = panels * output_w / 1000
    return s


def _construction_item(estimate, description: str):
    """施工費セクションから摘要一致の明細を1件返す。"""
    for cat in estimate.summary.categories:
        if cat.category.value != "施工費":
            continue
        matches = [it for it in cat.items if it.description == description]
        assert len(matches) == 1, \
            f"{description} は施工費に1件のはず（実際: {len(matches)}件）"
        return matches[0]
    pytest.fail("施工費セクションが見つからない")


# =============================================================
# 要件1: 数量=実枚数・単位=枚・単価×枚数（「一式」廃止）
# =============================================================

def test_panel_install_quantity_is_actual_panels():
    """660W×288枚で パネル取付工事 が「288枚 × ¥2,178/枚 = ¥627,264」になる。"""
    est = generate_estimate(_survey(288))
    item = _construction_item(est, PANEL_ITEM)

    assert item.quantity_value == 288, "数量は実枚数"
    assert item.quantity_unit == "枚", "単位は「枚」（「式」禁止）"
    assert item.quantity == "288枚"
    assert item.unit_price == PER_PANEL_PRICE, "単価は1枚単価"
    assert item.amount == item.unit_price * 288, "金額 = 単価 × 枚数"
    assert item.amount == 627_264, "掛川店サンプルの総額は従来kW連動と一致（回帰なし）"


def test_rack_install_quantity_is_actual_panels():
    """枚数連動の関連項目（架台取付工事）も同様に枚数ベースになる。"""
    est = generate_estimate(_survey(288))
    item = _construction_item(est, RACK_ITEM)

    assert item.quantity_value == 288
    assert item.quantity_unit == "枚"
    assert item.quantity == "288枚"
    assert item.amount == item.unit_price * 288
    assert item.amount == 627_264


# =============================================================
# 要件3: 枚数を半分（288→144）にすると金額もほぼ半分（単価同一）
# =============================================================

def test_half_panels_gives_half_amount_same_unit_price():
    """288枚→144枚で設置費・架台費が単価同一のままちょうど半分になる。"""
    est_288 = generate_estimate(_survey(288))
    est_144 = generate_estimate(_survey(144))

    for desc in (PANEL_ITEM, RACK_ITEM):
        i288 = _construction_item(est_288, desc)
        i144 = _construction_item(est_144, desc)
        assert i144.unit_price == i288.unit_price, f"{desc}: 単価は枚数に依らず同一"
        assert i144.quantity_value == 144
        assert i144.quantity == "144枚"
        assert i144.amount == i144.unit_price * 144
        assert i144.amount * 2 == i288.amount, f"{desc}: 金額は枚数に正比例"


# =============================================================
# 要件2: 学習ルールは「単価への学習」— 適用後も枚数比例が保たれる
# =============================================================

def test_learned_unit_price_override_keeps_panel_proportionality():
    """unit_price_override 適用後も 金額 = 枚数 × 学習単価 で比例が保たれる。

    金額固定化（枚数が変わっても金額が変わらない）の再発を検出する。
    """
    # 学習: パネル取付工事の1枚単価を ¥2,178 → ¥2,500 に補正
    # （既存ルールデータと同じキー構成: kind/category/match_description/payload）
    store.add_rules("estimate", [{
        "kind": "unit_price_override",
        "category": "施工費",
        "match_description": normalize_desc(PANEL_ITEM),
        "display_description": PANEL_ITEM,
        "payload": {"unit_price": 2500, "old_unit_price": PER_PANEL_PRICE},
    }])

    est_288 = generate_estimate(_survey(288))
    est_144 = generate_estimate(_survey(144))
    i288 = _construction_item(est_288, PANEL_ITEM)
    i144 = _construction_item(est_144, PANEL_ITEM)

    assert i288.unit_price == 2500, "学習は単価に効く"
    assert i144.unit_price == 2500, "枚数を変えても学習単価は同一"
    assert i288.amount == 2500 * 288, "金額 = 枚数 × 学習単価"
    assert i144.amount == 2500 * 144, "金額 = 枚数 × 学習単価"
    assert i144.amount * 2 == i288.amount, "学習適用後も枚数に正比例（金額固定化なし）"

    # 学習していない架台側は基準単価のまま（誤爆なし）
    rack = _construction_item(est_288, RACK_ITEM)
    assert rack.unit_price == PER_PANEL_PRICE


# =============================================================
# 防御: 設置枚数が未取得（抽出0値）の場合は従来のkW連動へ退避
# =============================================================

def test_zero_panels_falls_back_to_kw_rate():
    """planned_panels=0 でも ¥0 明細にならず kW 連動額で計上される。"""
    s = _survey(288)
    s.equipment.planned_panels = 0  # 抽出失敗を模擬（PV容量 190.08kW は保持）
    est = generate_estimate(s)
    item = _construction_item(est, PANEL_ITEM)

    assert item.amount == int(190.08 * 3300), "従来のkW連動額（¥0明細の防止）"
    assert item.unit_price == 3300
    assert item.quantity_unit == "式"
