"""図面差分学習のテスト（API不要・スクリプト式）

実行: python3 tests/test_learning_drawing.py

カバー範囲:
- diff_drawing_specs: gap/margin/orientation の学習可能差分の検出、
  枚数/寸法/架台/系統/型番の参考差分（learnable=False）、
  golden_example 提案が必ず1件付くこと、面の name 対応付け
- apply_learned_drawing_rules: 既定値のみ上書き（手動編集の尊重）、
  roof_type 条件（"*" は全面）、2回目適用の冪等性
- learned_golden_examples: 新しい順・limit
- store 経由の diff→承認→apply のラウンドトリップ
- spec_extractor プロンプトへの学習済みお手本注入

store は一時ディレクトリに差し替えて実行する（実 knowledge/ を汚さない）。
"""
import copy
import os
import sys
import tempfile
from pathlib import Path

# 本番Supabase（共有学習データ）への書込をimport前に遮断する。
# スクリプト実行では PYTEST_CURRENT_TEST が無く、.env.local の実クレデンシャルで
# kv_set が本番 learned_drawing_rules を上書きする事故が実発生した（2026-08-10）。
os.environ["SANEI_DISABLE_SUPABASE"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import learning.store as store

# --- 学習ストアを一時ディレクトリへ差し替え（本物の knowledge/ を汚さない） ---
_TMP = tempfile.TemporaryDirectory()
_TMP_DIR = Path(_TMP.name)
store.ESTIMATE_RULES_PATH = _TMP_DIR / "learned_estimate_rules.json"
store.DRAWING_RULES_PATH = _TMP_DIR / "learned_drawing_rules.json"
store.LEARNING_LOG_PATH = _TMP_DIR / "learning_history.json"

from drafting import sample_specs
from drafting.models import (
    DraftingSpec, PanelSpec, RoofFace, Orientation, RoofType, spec_to_dict,
)
from learning.drawing_diff import diff_drawing_specs
from learning.apply_drawing import apply_learned_drawing_rules, learned_golden_examples


def _reset_store():
    """テスト間の独立性のため図面ルールを空にする。"""
    store.save_rules("drawing", [])


def _by_type(diffs):
    """diff_type → list[DrawingDiffItem] のグループ化。"""
    grouped = {}
    for d in diffs:
        grouped.setdefault(d.diff_type, []).append(d)
    return grouped


# =============================================================
# diff_drawing_specs
# =============================================================

def test_diff_detects_changes():
    """ゴールデンを官製版と見立て、AI版との差分が全種別で検出されること。"""
    # 正規版 = spice_house（全面 折板）を「人が修正した値」に改変
    official = spec_to_dict(sample_specs.spice_house_layout())
    official["panel"]["gap_long_mm"] = 15
    official["panel"]["gap_short_mm"] = 8
    for f in official["roof_faces"]:
        f["margin_mm"] = 800
        f["orientation"] = Orientation.PORTRAIT
    official["roof_faces"][0]["target_panel_count"] = 40   # 枚数（参考）
    official["roof_faces"][1]["width_mm"] = 26500           # ±2%超（参考）
    official["mount_type"] = "屋根用架台"                    # 架台（参考）

    # AI版 = 既定値のままの生成物（向きはAUTO、実配置は横置き）
    ai = spec_to_dict(sample_specs.spice_house_layout())
    ai["panel"]["gap_long_mm"] = 25
    ai["panel"]["gap_short_mm"] = 10
    for f in ai["roof_faces"]:
        f["margin_mm"] = 500
        f["orientation"] = Orientation.AUTO
        f["panels"] = [{"x_mm": 0, "y_mm": 0, "w_mm": 2278, "h_mm": 1134,
                        "orientation": Orientation.LANDSCAPE, "string_id": None}]

    diffs = diff_drawing_specs(ai, official)
    grouped = _by_type(diffs)

    # gap: 縦25→15・横10→8、全面折板なので roof_type="setsuban"
    assert "gap_changed" in grouped, "gap_changed が検出されない"
    gap = grouped["gap_changed"][0]
    assert gap.learnable and gap.proposed_rule is not None
    assert gap.proposed_rule["kind"] == "gap_override"
    assert gap.proposed_rule["target"] == "drawing"
    assert gap.proposed_rule["payload"]["gap_long_mm"] == 15
    assert gap.proposed_rule["payload"]["gap_short_mm"] == 8
    assert gap.proposed_rule["payload"]["roof_type"] == RoofType.SETSUBAN

    # margin: 500→800（面ごとに検出。同キー同値の提案は1件目のみ学習可能・以降は参考）
    assert "margin_changed" in grouped, "margin_changed が検出されない"
    m_learnable = [m for m in grouped["margin_changed"] if m.learnable]
    assert len(m_learnable) == 1, "同値マージンの学習提案は1件に集約されるべき"
    m = m_learnable[0]
    assert m.proposed_rule["kind"] == "margin_override"
    assert m.proposed_rule["payload"]["margin_mm"] == 800
    assert m.proposed_rule["payload"]["roof_type"] == RoofType.SETSUBAN
    for ref in grouped["margin_changed"]:
        if not ref.learnable:
            assert ref.proposed_rule is None

    # orientation: AI実配置=横置き → 正規=縦置き
    assert "orientation_changed" in grouped, "orientation_changed が検出されない"
    ori = grouped["orientation_changed"][0]
    assert ori.learnable and ori.proposed_rule["kind"] == "orientation_preference"
    assert ori.proposed_rule["payload"]["orientation"] == Orientation.PORTRAIT

    # 参考表示（learnable=False, proposed_rule なし）
    for ref_type in ("panel_count_changed", "face_dimension_changed", "mount_type_changed"):
        assert ref_type in grouped, f"{ref_type} が検出されない"
        for item in grouped[ref_type]:
            assert item.learnable is False, f"{ref_type} は参考表示のはず"
            assert item.proposed_rule is None, f"{ref_type} に proposed_rule は不要"

    # golden_example: 必ず1件・末尾・official 全体を payload に持つ
    goldens = grouped.get("golden_example", [])
    assert len(goldens) == 1, "golden_example 提案は必ず1件のはず"
    g = goldens[0]
    assert diffs[-1].diff_type == "golden_example", "golden_example は末尾に付くはず"
    assert g.learnable and g.proposed_rule["kind"] == "golden_example"
    assert g.proposed_rule["payload"]["spec"] == official
    assert "スパイスハウス" in g.proposed_rule["payload"]["name"]

    # 全 diff が summary を持つ
    assert all(d.summary for d in diffs), "summary が空の差分がある"


def test_identical_specs_only_golden():
    """完全一致でも golden_example 提案だけは必ず1件付くこと。"""
    official = spec_to_dict(sample_specs.kurihara_layout())
    ai = copy.deepcopy(official)
    diffs = diff_drawing_specs(ai, official)
    assert len(diffs) == 1, f"完全一致なら golden_example のみ1件のはず: {[d.diff_type for d in diffs]}"
    assert diffs[0].diff_type == "golden_example"


def test_face_matching_by_name():
    """面の対応付けが name 一致で行われること（順序が入れ替わっても誤検出しない）。"""
    def _make(margins):
        spec = DraftingSpec(
            panel=PanelSpec(long_mm=1762, short_mm=1134),
            roof_faces=[
                RoofFace(name="面A", roof_type=RoofType.KAWARA, width_mm=10000,
                         depth_mm=6000, margin_mm=margins[0],
                         orientation=Orientation.PORTRAIT),
                RoofFace(name="面B", roof_type=RoofType.KAWARA, width_mm=8000,
                         depth_mm=5000, margin_mm=margins[1],
                         orientation=Orientation.PORTRAIT),
            ],
        )
        return spec_to_dict(spec)

    ai = _make([500, 700])
    official = _make([500, 700])
    official["roof_faces"].reverse()  # 順序を入れ替えても name で対応付く

    diffs = diff_drawing_specs(ai, official)
    grouped = _by_type(diffs)
    assert "margin_changed" not in grouped, "name一致で対応付けば margin 差分は出ないはず"
    assert "face_dimension_changed" not in grouped, "name一致で対応付けば寸法差分は出ないはず"


# =============================================================
# apply_learned_drawing_rules
# =============================================================

def test_apply_default_only_and_roof_type():
    """既定値のみ上書き・roof_type 条件（"*" は全面）・手動値の尊重。"""
    _reset_store()
    store.add_rules("drawing", [
        {"kind": "gap_override",
         "payload": {"gap_long_mm": 15, "roof_type": RoofType.SETSUBAN}},
        {"kind": "margin_override",
         "payload": {"margin_mm": 300, "roof_type": RoofType.SETSUBAN}},
        {"kind": "orientation_preference",
         "payload": {"orientation": Orientation.PORTRAIT, "roof_type": "*"}},
    ])

    spec = DraftingSpec(
        panel=PanelSpec(gap_long_mm=25, gap_short_mm=10),
        roof_faces=[
            RoofFace(name="面1", roof_type=RoofType.SETSUBAN,
                     margin_mm=500, orientation=Orientation.AUTO),
            RoofFace(name="面2", roof_type=RoofType.KAWARA,
                     margin_mm=500, orientation=Orientation.AUTO),
            RoofFace(name="面3", roof_type=RoofType.SETSUBAN,
                     margin_mm=600, orientation=Orientation.LANDSCAPE),  # 手動編集済み
        ],
    )
    spec, msgs = apply_learned_drawing_rules(spec)

    assert spec.panel.gap_long_mm == 15, "折板面があるので gap_long は学習値になるはず"
    assert spec.panel.gap_short_mm == 10, "ルールに無い gap_short は不変のはず"
    assert spec.roof_faces[0].margin_mm == 300, "折板+既定値 → 学習マージン適用"
    assert spec.roof_faces[1].margin_mm == 500, "瓦面には折板ルールを適用しない"
    assert spec.roof_faces[2].margin_mm == 600, "手動編集したマージンは尊重"
    assert spec.roof_faces[0].orientation == Orientation.PORTRAIT, '"*" は全面の AUTO に適用'
    assert spec.roof_faces[1].orientation == Orientation.PORTRAIT, '"*" は全面の AUTO に適用'
    assert spec.roof_faces[2].orientation == Orientation.LANDSCAPE, "手動指定の向きは尊重"
    assert msgs and all("学習値" in m for m in msgs), f"適用説明が不正: {msgs}"

    # 2回目の適用: 値はもう既定値でないため何も起きない（冪等）
    spec, msgs2 = apply_learned_drawing_rules(spec)
    assert msgs2 == [], f"2回目の適用は no-op のはず: {msgs2}"
    assert spec.panel.gap_long_mm == 15 and spec.roof_faces[0].margin_mm == 300


def test_apply_respects_manual_gap():
    """手動変更した gap_long=30 は学習値で上書きされないこと。"""
    _reset_store()
    store.add_rules("drawing", [
        {"kind": "gap_override", "payload": {"gap_long_mm": 15, "roof_type": "*"}},
    ])
    spec = DraftingSpec(
        panel=PanelSpec(gap_long_mm=30, gap_short_mm=10),  # gap_long は手動編集済み
        roof_faces=[RoofFace(name="面1", roof_type=RoofType.SETSUBAN)],
    )
    spec, msgs = apply_learned_drawing_rules(spec)
    assert spec.panel.gap_long_mm == 30, "手動編集した gap_long は上書きされないはず"
    assert msgs == [], f"適用が無いので説明も空のはず: {msgs}"


def test_apply_roof_type_mismatch_gap():
    """roof_type 不一致の gap ルールは適用されないこと。"""
    _reset_store()
    store.add_rules("drawing", [
        {"kind": "gap_override",
         "payload": {"gap_long_mm": 15, "roof_type": RoofType.SETSUBAN}},
    ])
    spec = DraftingSpec(
        panel=PanelSpec(gap_long_mm=25),
        roof_faces=[RoofFace(name="面1", roof_type=RoofType.KAWARA)],  # 瓦のみ
    )
    spec, msgs = apply_learned_drawing_rules(spec)
    assert spec.panel.gap_long_mm == 25, "折板ルールは瓦のみの図面に適用されないはず"
    assert msgs == []


def test_disabled_rule_not_applied():
    """無効化（enabled=False）したルールは適用されないこと。"""
    _reset_store()
    rules = store.add_rules("drawing", [
        {"kind": "margin_override", "payload": {"margin_mm": 300, "roof_type": "*"}},
    ])
    store.set_rule_enabled("drawing", rules[0]["id"], False)
    spec = DraftingSpec(roof_faces=[RoofFace(name="面1", margin_mm=500)])
    spec, msgs = apply_learned_drawing_rules(spec)
    assert spec.roof_faces[0].margin_mm == 500 and msgs == []


# =============================================================
# learned_golden_examples
# =============================================================

def test_learned_golden_examples():
    """golden_example が新しい順・limit 付きで取得できること。"""
    _reset_store()
    store.add_rules("drawing", [
        {"kind": "golden_example",
         "payload": {"name": "案件A 太陽光配置図", "spec": {"customer_name": "A"}},
         "evidence": {"learned_at": "2026-07-01 10:00"}},
        {"kind": "golden_example",
         "payload": {"name": "案件B ストリングス図", "spec": {"customer_name": "B"}},
         "evidence": {"learned_at": "2026-07-14 09:00"}},
        {"kind": "margin_override",  # golden 以外は混ざらないこと
         "payload": {"margin_mm": 300, "roof_type": "*"}},
    ])
    examples = learned_golden_examples(2)
    assert len(examples) == 2
    assert examples[0]["name"] == "案件B ストリングス図", "新しい順のはず"
    assert examples[0]["spec"] == {"customer_name": "B"}
    assert len(learned_golden_examples(1)) == 1
    assert learned_golden_examples(0) == []


# =============================================================
# ラウンドトリップ + プロンプト注入
# =============================================================

def test_diff_to_store_to_apply_roundtrip():
    """diff → 承認（add_rules）→ apply の一気通貫が機能すること。"""
    _reset_store()
    official = spec_to_dict(sample_specs.spice_house_layout())
    official["panel"]["gap_long_mm"] = 15
    for f in official["roof_faces"]:
        f["margin_mm"] = 800

    ai = spec_to_dict(sample_specs.spice_house_layout())
    ai["panel"]["gap_long_mm"] = 25
    for f in ai["roof_faces"]:
        f["margin_mm"] = 500

    diffs = diff_drawing_specs(ai, official)
    approved = [d.proposed_rule for d in diffs if d.learnable and d.proposed_rule]
    store.add_rules("drawing", approved)

    # 既定値のままの新しい折板案件に自動反映される
    spec = DraftingSpec(
        panel=PanelSpec(gap_long_mm=25, gap_short_mm=10),
        roof_faces=[RoofFace(name="面1", roof_type=RoofType.SETSUBAN, margin_mm=500)],
    )
    spec, msgs = apply_learned_drawing_rules(spec)
    assert spec.panel.gap_long_mm == 15
    assert spec.roof_faces[0].margin_mm == 800
    assert msgs, "適用説明が返るはず"

    # golden_example も store に入り few-shot として取得できる
    examples = learned_golden_examples(2)
    assert len(examples) == 1 and examples[0]["spec"] == official


def test_prompt_injection():
    """spec_extractor のプロンプトに学習済みお手本が注入されること（無ければ従来通り）。"""
    from drafting.spec_extractor import build_user_prompt

    _reset_store()
    prompt_before = build_user_prompt()
    assert "学習済みお手本" not in prompt_before, "学習ゼロ件なら注入されないはず"

    store.add_rules("drawing", [
        {"kind": "golden_example",
         "payload": {"name": "テスト商事様 太陽光配置図",
                     "spec": spec_to_dict(sample_specs.kurihara_layout())}},
    ])
    prompt_after = build_user_prompt()
    assert "学習済みお手本" in prompt_after, "学習済みお手本が注入されるはず"
    assert "テスト商事様" in prompt_after
    assert "正解出力例" in prompt_after, "既存のゴールデン例は残るはず"


# =============================================================
# 実行
# =============================================================

def main() -> bool:
    tests = [
        test_diff_detects_changes,
        test_identical_specs_only_golden,
        test_face_matching_by_name,
        test_apply_default_only_and_roof_type,
        test_apply_respects_manual_gap,
        test_apply_roof_type_mismatch_gap,
        test_disabled_rule_not_applied,
        test_learned_golden_examples,
        test_diff_to_store_to_apply_roundtrip,
        test_prompt_injection,
    ]
    print("=== 図面差分学習テスト（API不要） ===")
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
