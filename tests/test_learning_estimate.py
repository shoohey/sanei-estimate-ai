"""見積差分学習のテスト（API不要・スクリプト式）

実行: python3 tests/test_learning_estimate.py

カバー範囲:
- normalize_desc: 表記揺れ（全角/半角・大文字小文字・空白・記号）の吸収
- diff_estimates: 単価変更（支給品除外）・項目追加・項目削除・数量参考表示・
  fuzzy一致、同一承認バッチ内の同キー衝突の抑止
- 備考違いの同名項目（材料費「PVケーブル間」×5等）: 備考完全一致ペアのみ学習可能、
  同名項目の item_removed は参考表示、¥0行（条件付き・手動入力）の削除は参考表示、
  item_add の単価補完（金額÷数量）
- apply_learned_rules: unit_price_override（note追記・lump_formula除外）・
  item_add（no採番・fixed・数量数値）・item_suppress・category空は全リスト対象・
  deepcopy による入力非破壊・複合照合（match_remarks / old_unit_price）による
  同名項目への誤爆防止（2件以上一致なら適用スキップ）
- store の add → 同キー上書き（match_remarks 込み）→ disable → delete の
  ラウンドトリップ
- pricing/knowledge_base.load_pricing_rules 経由のフック（学習ゼロ件なら従来通り）

store は一時ディレクトリに差し替えて実行する（実 knowledge/ を汚さない）。
"""
import copy
import os
import sys
import tempfile
from pathlib import Path

# 本番Supabase（共有学習データ）への書込をimport前に遮断する。
# スクリプト実行では PYTEST_CURRENT_TEST が無く、.env.local の実クレデンシャルで
# kv_set が本番 learned_estimate_rules を上書きする事故が実発生した（2026-08-10）。
os.environ["SANEI_DISABLE_SUPABASE"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import learning.store as store

# --- 学習ストアを一時ディレクトリへ差し替え（本物の knowledge/ を汚さない） ---
_TMP = tempfile.TemporaryDirectory()
_TMP_DIR = Path(_TMP.name)
store.ESTIMATE_RULES_PATH = _TMP_DIR / "learned_estimate_rules.json"
store.DRAWING_RULES_PATH = _TMP_DIR / "learned_drawing_rules.json"
store.LEARNING_LOG_PATH = _TMP_DIR / "learning_history.json"

from learning.models import ParsedEstimate, ParsedLineItem
from learning.estimate_diff import normalize_desc, diff_estimates
from learning.apply_estimate import apply_learned_rules, learned_rules_summary


def _reset_store():
    """テスト間の独立性のため見積ルールを空にする。"""
    store.save_rules("estimate", [])


def _item(category, no, desc, price=None, qty=1.0, unit="式", remarks=""):
    """テスト用の明細行を作る。"""
    return ParsedLineItem(
        category=category, no=no, description=desc, remarks=remarks,
        quantity_value=qty, quantity_unit=unit, unit_price=price,
        amount=int((qty or 0) * (price or 0)) if price is not None else None,
    )


def _estimate(source, items, project="テスト店", file_name=""):
    """テスト用の ParsedEstimate を作る。"""
    return ParsedEstimate(
        source=source, origin="history" if source == "ai" else "pdf",
        file_name=file_name, project_name=project, items=items,
    )


def _by_type(diffs):
    """diff_type → list[EstimateDiffItem] のグループ化。"""
    grouped = {}
    for d in diffs:
        grouped.setdefault(d.diff_type, []).append(d)
    return grouped


def _sample_pricing_rules():
    """pricing_rules.yaml のロード結果を模した最小の rules dict。"""
    return {
        "tax_rate": 0.10,
        "supplied_items": [
            {"no": 1, "description": "太陽光モジュール", "quantity": "1",
             "quantity_unit": "式", "note": "御支給品"},
        ],
        "material_items": [
            {"no": 1, "description": "PVケーブル間", "remarks": "配管　VE54",
             "quantity": "2", "quantity_unit": "式", "unit_price": 38000,
             "pricing_method": "fixed", "note": "PVケーブル配管"},
            {"no": 2, "description": "その他雑材費", "remarks": "",
             "quantity_formula": "max(110000, pv_capacity_kw * 600)",
             "quantity_unit": "式", "pricing_method": "lump_formula",
             "note": "PV容量連動"},
        ],
        "construction_items": [
            {"no": 1, "description": "墨出し", "remarks": "", "quantity": "1",
             "quantity_unit": "式", "unit_price": 237000,
             "pricing_method": "fixed", "note": "墨出し作業"},
        ],
        "overhead_items": [
            {"no": 1, "description": "諸経費", "remarks": "", "quantity": "1",
             "quantity_unit": "式", "unit_price": 921000,
             "pricing_method": "fixed", "note": "諸経費一式"},
        ],
        "additional_items": [
            {"no": 3, "description": "資材運搬費", "remarks": "",
             "quantity": "1", "quantity_unit": "式", "unit_price": 50000,
             "pricing_method": "fixed", "note": ""},
        ],
    }


# =============================================================
# normalize_desc
# =============================================================

def test_normalize_desc():
    """表記揺れ（全角/半角・大小文字・空白・記号）が吸収されること。"""
    assert normalize_desc("ＰＶケーブル（間）") == normalize_desc("PVケーブル間")
    assert normalize_desc("ケーブル 配線・工事。") == normalize_desc("ケーブル配線工事")
    assert normalize_desc("ABC-123") == normalize_desc("ａｂｃ　１２３")
    assert normalize_desc("架台　取付（工事）") == normalize_desc("架台取付工事")
    assert normalize_desc("PCS～QB間") == normalize_desc("PCS QB間"), "記号～は除去されるはず"
    assert normalize_desc("") == ""
    assert normalize_desc(None) == ""
    # 長音記号（ー）は文字として残る（けーぶる ≠ けぶる）
    assert normalize_desc("ケーブル") != normalize_desc("ケブル")
    # 別の文字列は別のまま
    assert normalize_desc("パワコン") != normalize_desc("パワーコン")


# =============================================================
# diff_estimates
# =============================================================

def test_diff_price_changed():
    """単価差分が検出され、支給品カテゴリの単価差分は除外されること。"""
    ai = _estimate("ai", [
        _item("施工費", 2, "架台取付工事", price=3300, qty=190.08),
        _item("支給品", 1, "太陽光モジュール", price=0, qty=432, unit="枚"),
    ])
    official = _estimate("official", [
        _item("施工費", 2, "架台取付工事", price=3100, qty=190.08),
        _item("支給品", 1, "太陽光モジュール", price=100, qty=432, unit="枚"),  # 支給品→除外
    ], project="掛川店", file_name="正規見積.pdf")

    diffs = diff_estimates(ai, official)
    grouped = _by_type(diffs)

    assert "price_changed" in grouped, "price_changed が検出されない"
    assert len(diffs) == 1, f"支給品の単価差分は出ないはず: {[d.diff_type for d in diffs]}"
    d = grouped["price_changed"][0]
    assert d.learnable and d.proposed_rule is not None
    assert d.match_score == 1.0, "完全一致ペアのスコアは1.0のはず"
    assert "¥3,300" in d.summary and "¥3,100" in d.summary
    rule = d.proposed_rule
    assert rule["target"] == "estimate"
    assert rule["kind"] == "unit_price_override"
    assert rule["category"] == "施工費"
    assert rule["match_description"] == normalize_desc("架台取付工事")
    assert rule["display_description"] == "架台取付工事"
    assert rule["payload"] == {"unit_price": 3100, "old_unit_price": 3300,
                               "basis_quantity_unit": "式"}
    ev = rule["evidence"]
    assert ev["project_name"] == "掛川店"
    assert ev["file_name"] == "正規見積.pdf"
    assert ev["summary"] and ev["learned_at"]


def test_diff_item_added_and_removed():
    """正規のみ→item_added（item_add）、AIのみ→item_removed（item_suppress）。"""
    ai = _estimate("ai", [
        _item("材料費", 1, "接地材料", price=660, qty=200, unit="m"),
    ])
    official = _estimate("official", [
        _item("材料費", 1, "防水処理材", price=12000, qty=1, remarks="シーリング材"),
    ], project="掛川店")

    diffs = diff_estimates(ai, official)
    grouped = _by_type(diffs)

    added = grouped.get("item_added", [])
    assert len(added) == 1, "item_added が検出されない"
    a = added[0]
    assert a.learnable and a.proposed_rule["kind"] == "item_add"
    assert a.proposed_rule["category"] == "材料費"
    assert a.proposed_rule["match_description"] == normalize_desc("防水処理材")
    payload = a.proposed_rule["payload"]
    assert payload["description"] == "防水処理材"
    assert payload["remarks"] == "シーリング材"
    assert payload["quantity_value"] == 1 and payload["quantity_unit"] == "式"
    assert payload["unit_price"] == 12000

    removed = grouped.get("item_removed", [])
    assert len(removed) == 1, "item_removed が検出されない"
    r = removed[0]
    assert r.learnable and r.proposed_rule["kind"] == "item_suppress"
    assert r.proposed_rule["match_description"] == normalize_desc("接地材料")
    assert r.proposed_rule["match_remarks"] == ""
    assert r.proposed_rule["payload"] == {"old_unit_price": 660}, \
        "suppress にも old_unit_price（複合照合用）が入るはず"

    # item_add には備考が match_remarks として入る（複合照合・dedupキー用）
    assert a.proposed_rule["match_remarks"] == normalize_desc("シーリング材")


def test_diff_quantity_reference_only():
    """数量差分は参考表示のみ（learnable=False, proposed_rule=None）であること。"""
    ai = _estimate("ai", [
        _item("材料費", 4, "ケーブルラック", price=15800, qty=30, unit="m"),
    ])
    official = _estimate("official", [
        _item("材料費", 4, "ケーブルラック", price=15800, qty=45, unit="m"),
    ])
    diffs = diff_estimates(ai, official)
    assert len(diffs) == 1 and diffs[0].diff_type == "quantity_changed"
    d = diffs[0]
    assert d.learnable is False, "数量差分は学習対象外のはず"
    assert d.proposed_rule is None
    assert "30m" in d.summary and "45m" in d.summary
    assert "参考" in d.summary


def test_diff_fuzzy_match():
    """表記が少し違う摘要が fuzzy 一致し、追加/削除に化けないこと。"""
    ai = _estimate("ai", [
        _item("施工費", 5, "パワコン取付工事", price=368000),
    ])
    official = _estimate("official", [
        _item("施工費", 5, "パワーコン取付工事", price=350000),
    ])
    diffs = diff_estimates(ai, official)
    grouped = _by_type(diffs)
    assert "item_added" not in grouped and "item_removed" not in grouped, \
        "fuzzy一致すれば追加/削除にはならないはず"
    assert "price_changed" in grouped
    d = grouped["price_changed"][0]
    assert 0.6 <= d.match_score < 1.0, f"fuzzy一致のスコアが不正: {d.match_score}"
    # 学習ルールの照合先はAI側（=pricing_rules.yaml の摘要）
    assert d.proposed_rule["match_description"] == normalize_desc("パワコン取付工事")


def test_diff_same_key_collision():
    """同一バッチ内で同キーの提案が複数生まれた場合、2件目以降は参考表示。

    item_removed の同名項目は重複ガードで一律参考表示になるため、
    衝突経路は item_add（正規側に同名・同備考の行が2つ）で検証する。
    """
    ai = _estimate("ai", [])
    official = _estimate("official", [
        _item("材料費", 1, "防水材　シート", price=100),
        _item("材料費", 2, "防水材・シート", price=200),  # 正規化すると同じ摘要・同じ備考("")
    ])
    diffs = diff_estimates(ai, official)
    added = [d for d in diffs if d.diff_type == "item_added"]
    assert len(added) == 2, f"追加差分は2件のはず: {[d.diff_type for d in diffs]}"
    learnables = [d for d in added if d.learnable]
    assert len(learnables) == 1, "同キーの学習提案は1件目のみ学習可能のはず"
    for d in added:
        if not d.learnable:
            assert d.proposed_rule is None
            assert "省略" in d.summary or "競合" in d.summary


def test_diff_dup_removed_not_learnable():
    """同名（正規化摘要が同一）のAI項目の削除差分は一律参考表示であること。

    正規側で1行だけ削除された場合に、摘要キーの suppress が同名全項目に
    波及する事故（レビュー指摘 high）を防ぐ。
    """
    ai = _estimate("ai", [
        _item("材料費", 1, "接地材料　雑材", price=7200),
        _item("材料費", 2, "接地材料・雑材", price=7200),  # 正規化すると同じ摘要
    ])
    official = _estimate("official", [])
    diffs = diff_estimates(ai, official)
    removed = [d for d in diffs if d.diff_type == "item_removed"]
    assert len(removed) == 2, f"削除差分は2件のはず: {[d.diff_type for d in diffs]}"
    for d in removed:
        assert d.learnable is False, "同名項目の削除は学習対象外のはず"
        assert d.proposed_rule is None
        assert "同名項目" in d.summary


# --- 備考違いの同名項目フィクスチャ（pricing_rules.yaml 材料費「PVケーブル間」×5 を模す） ---
_PV_REMARKS_PRICES = [
    ("配管", 38000), ("付属品", 86000), ("雑材", 33000),
    ("ラック", 52600), ("本体", 15800),
]


def _pv_items(overrides=None, skip=()):
    """備考だけが違う同名5項目（材料費「PVケーブル間」）の明細を作る。"""
    overrides = overrides or {}
    return [
        _item("材料費", i + 1, "PVケーブル間",
              price=overrides.get(r, p), remarks=r)
        for i, (r, p) in enumerate(_PV_REMARKS_PRICES) if r not in skip
    ]


def _pv_pricing_rules():
    """備考だけが違う同名5項目を持つ pricing rules dict を作る。"""
    return {"material_items": [
        {"no": i + 1, "description": "PVケーブル間", "remarks": r,
         "quantity": "1", "quantity_unit": "式", "unit_price": p,
         "pricing_method": "fixed", "note": ""}
        for i, (r, p) in enumerate(_PV_REMARKS_PRICES)
    ]}


def test_diff_dup_remarks_price_changed():
    """同名複数項目（備考違い）: 備考完全一致ペアの単価差分のみ学習可能で、
    apply では該当1項目のみ上書きされること（レビュー指摘 critical）。"""
    ai = _estimate("ai", _pv_items())
    official = _estimate("official", _pv_items(overrides={"配管": 40000}),
                         project="掛川店")
    diffs = diff_estimates(ai, official)
    grouped = _by_type(diffs)
    assert set(grouped) == {"price_changed"}, \
        f"差分は price_changed のみのはず: {sorted(grouped)}"
    assert len(grouped["price_changed"]) == 1, "単価差分は配管の1件のはず"
    d = grouped["price_changed"][0]
    assert d.learnable and d.proposed_rule is not None, \
        "備考完全一致ペアの単価差分は学習可能のはず"
    rule = d.proposed_rule
    assert rule["match_remarks"] == normalize_desc("配管"), \
        "同名項目のルールには match_remarks が必須"
    assert rule["payload"] == {"unit_price": 40000, "old_unit_price": 38000,
                               "basis_quantity_unit": "式"}

    # apply: 同名5項目のうち備考一致の1項目だけ上書き、他4項目は不変
    _reset_store()
    store.add_rules("estimate", [rule])
    applied = apply_learned_rules(_pv_pricing_rules())
    prices = {it["remarks"]: it["unit_price"] for it in applied["material_items"]}
    assert prices["配管"] == 40000, "備考一致項目に学習単価が反映されるはず"
    for r, p in _PV_REMARKS_PRICES[1:]:
        assert prices[r] == p, f"同名他項目（{r}）の単価が巻き添え変更された"


def test_diff_dup_row_deleted_no_false_learn():
    """同名複数項目で正規側の1行が削除されても、偽の price_changed や
    learnable な suppress が提案されないこと（レビュー指摘 high: ペアずれ）。"""
    ai = _estimate("ai", _pv_items())
    official = _estimate("official", _pv_items(skip=("配管",)))
    diffs = diff_estimates(ai, official)
    false_prices = [d for d in diffs if d.diff_type == "price_changed"]
    assert not false_prices, \
        f"ペアずれによる偽の単価差分が出ている: {[d.summary for d in false_prices]}"
    removed = [d for d in diffs if d.diff_type == "item_removed"]
    assert len(removed) == 1, "削除差分は配管の1件のはず"
    assert removed[0].learnable is False and removed[0].proposed_rule is None
    assert "同名項目" in removed[0].summary


def test_apply_suppress_dup_guard():
    """suppress の複合照合: match_remarks で1項目に特定でき、
    旧形式（照合キー無し）で同名複数一致なら何も消さないこと（レビュー指摘 high）。"""
    # match_remarks あり → 備考一致の1項目だけ除去
    _reset_store()
    store.add_rules("estimate", [
        {"kind": "item_suppress", "category": "材料費",
         "match_description": normalize_desc("PVケーブル間"),
         "match_remarks": normalize_desc("配管"),
         "payload": {"old_unit_price": 38000}},
    ])
    applied = apply_learned_rules(_pv_pricing_rules())
    remarks = [it["remarks"] for it in applied["material_items"]]
    assert "配管" not in remarks, "備考一致項目が除去されるはず"
    assert len(remarks) == 4, f"同名他項目まで消えている: {remarks}"

    # 旧形式（match_remarks も old_unit_price も無し）で同名5件一致 → 適用スキップ
    _reset_store()
    store.add_rules("estimate", [
        {"kind": "item_suppress", "category": "材料費",
         "match_description": normalize_desc("PVケーブル間"), "payload": {}},
    ])
    applied2 = apply_learned_rules(_pv_pricing_rules())
    assert len(applied2["material_items"]) == 5, \
        "対象を特定できない suppress は何も消さないはず"


def test_diff_zero_amount_removed_not_learnable():
    """金額・単価がともに0/NoneのAI行（クレーン費等の条件付き・手動入力項目）の
    削除差分は参考表示であること（レビュー指摘 high）。"""
    ai = _estimate("ai", [
        ParsedLineItem(category="付帯工事", no=1, description="クレーン費",
                       remarks="現場条件による", quantity_value=1.0,
                       quantity_unit="式", unit_price=None, amount=None),
        _item("付帯工事", 2, "外部足場", price=0, qty=1),  # 単価0・金額0 も同様
    ])
    official = _estimate("official", [])
    diffs = diff_estimates(ai, official)
    removed = [d for d in diffs if d.diff_type == "item_removed"]
    assert len(removed) == 2, f"削除差分は2件のはず: {[d.diff_type for d in diffs]}"
    for d in removed:
        assert d.learnable is False, f"¥0行は学習対象外のはず: {d.description}"
        assert d.proposed_rule is None
        assert "¥0行" in d.summary


def test_diff_item_add_price_completion():
    """item_add: 正規側の単価欄が空でも金額÷数量で単価を補完し、
    金額も無ければ参考表示であること（レビュー指摘 medium）。"""
    official = _estimate("official", [
        ParsedLineItem(category="付帯工事", no=1, description="防水処理工事",
                       remarks="", quantity_value=2.0, quantity_unit="式",
                       unit_price=None, amount=30000),
        ParsedLineItem(category="付帯工事", no=2, description="クレーン費",
                       remarks="", quantity_value=None, quantity_unit="式",
                       unit_price=None, amount=None),
    ], project="掛川店")
    diffs = diff_estimates(_estimate("ai", []), official)
    added = {d.description: d for d in diffs if d.diff_type == "item_added"}
    assert set(added) == {"防水処理工事", "クレーン費"}

    a = added["防水処理工事"]
    assert a.learnable and a.proposed_rule is not None
    assert a.proposed_rule["payload"]["unit_price"] == 15000, \
        "単価は金額30,000÷数量2=15,000で補完されるはず"
    assert "補完" in a.summary

    b = added["クレーン費"]
    assert b.learnable is False and b.proposed_rule is None, \
        "単価も金額も無い追加項目は¥0登録を防ぐため参考表示のはず"

    # 端数数量（0.5式）でも 金額÷数量 で正しく補完されること（クランプ誤差の回帰確認）
    frac = _estimate("official", [
        ParsedLineItem(category="付帯工事", no=1, description="仮設トイレ設置",
                       remarks="", quantity_value=0.5, quantity_unit="式",
                       unit_price=None, amount=48000),
    ], project="掛川店")
    fdiffs = diff_estimates(_estimate("ai", []), frac)
    f = [d for d in fdiffs if d.diff_type == "item_added"][0]
    assert f.proposed_rule["payload"]["unit_price"] == 96000, \
        f"端数数量0.5: 48,000÷0.5=96,000のはずが {f.proposed_rule['payload']['unit_price']}"

    # apply しても補完単価で登録される（¥0項目にならない）
    _reset_store()
    store.add_rules("estimate", [a.proposed_rule])
    applied = apply_learned_rules(_sample_pricing_rules())
    added_item = applied["additional_items"][-1]
    assert added_item["description"] == "防水処理工事"
    assert added_item["unit_price"] == 15000


def test_apply_old_price_guard():
    """match_remarks 無しの旧形式ルール: old_unit_price で対象を絞り、
    一意に特定できれば適用・曖昧（複数一致）なら適用しないこと。"""
    def _dup_price_rules():
        rules = _pv_pricing_rules()
        # 「本体」の単価を「配管」と同じ 38000 にして曖昧ケースを作る
        rules["material_items"][4]["unit_price"] = 38000
        return rules

    # 一意に絞れる場合（86000 は付属品のみ）→ その項目だけ更新
    _reset_store()
    store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "材料費",
         "match_description": normalize_desc("PVケーブル間"),
         "payload": {"unit_price": 90000, "old_unit_price": 86000}},
    ])
    applied = apply_learned_rules(_dup_price_rules())
    prices = {it["remarks"]: it["unit_price"] for it in applied["material_items"]}
    assert prices["付属品"] == 90000, "old_unit_price 一致の1項目には適用されるはず"
    assert prices["雑材"] == 33000 and prices["ラック"] == 52600

    # 曖昧（38000 が配管・本体の2件）→ どの項目も更新しない
    _reset_store()
    store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "材料費",
         "match_description": normalize_desc("PVケーブル間"),
         "payload": {"unit_price": 99999, "old_unit_price": 38000}},
    ])
    applied2 = apply_learned_rules(_dup_price_rules())
    prices2 = {it["remarks"]: it["unit_price"] for it in applied2["material_items"]}
    assert prices2["配管"] == 38000 and prices2["本体"] == 38000, \
        "対象を特定できない override は適用されないはず"
    assert all("学習補正" not in it["note"] for it in applied2["material_items"])


# =============================================================
# apply_learned_rules
# =============================================================

def test_apply_override_and_deepcopy():
    """単価上書き（note追記）+ 入力 rules の非破壊（deepcopy）。"""
    _reset_store()
    store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "材料費",
         "match_description": normalize_desc("PVケーブル間"),
         "payload": {"unit_price": 40000, "old_unit_price": 38000}},
    ])
    original = _sample_pricing_rules()
    snapshot = copy.deepcopy(original)
    applied = apply_learned_rules(original)

    assert original == snapshot, "入力 rules が破壊されている（deepcopyされていない）"
    item = applied["material_items"][0]
    assert item["unit_price"] == 40000
    assert "学習補正" in item["note"] and "¥38,000→¥40,000" in item["note"]
    # 他カテゴリ・他項目は不変
    assert applied["construction_items"][0]["unit_price"] == 237000


def test_apply_lump_formula_excluded():
    """lump_formula 項目は unit_price 不使用のため上書き対象外であること。"""
    _reset_store()
    store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "材料費",
         "match_description": normalize_desc("その他雑材費"),
         "payload": {"unit_price": 99999, "old_unit_price": 0}},
    ])
    applied = apply_learned_rules(_sample_pricing_rules())
    zatsuzai = applied["material_items"][1]
    assert zatsuzai["pricing_method"] == "lump_formula"
    assert "unit_price" not in zatsuzai, "lump_formula に unit_price が追加されてはいけない"
    assert "学習補正" not in zatsuzai["note"]


def test_apply_suppress():
    """item_suppress で一致項目がリストから除去されること。"""
    _reset_store()
    store.add_rules("estimate", [
        {"kind": "item_suppress", "category": "材料費",
         "match_description": normalize_desc("PVケーブル間"), "payload": {}},
    ])
    applied = apply_learned_rules(_sample_pricing_rules())
    descs = [it["description"] for it in applied["material_items"]]
    assert "PVケーブル間" not in descs, "抑止項目が除去されていない"
    assert "その他雑材費" in descs, "無関係な項目まで消えている"
    assert len(applied["construction_items"]) == 1, "他リストは不変のはず"


def test_apply_add():
    """item_add でリスト末尾に no=最大+1 の fixed 項目が追加されること。"""
    _reset_store()
    store.add_rules("estimate", [
        {"kind": "item_add", "category": "付帯工事",
         "match_description": normalize_desc("防水処理工事"),
         "display_description": "防水処理工事",
         "payload": {"category": "付帯工事", "description": "防水処理工事",
                     "remarks": "シーリング", "quantity_value": 2.0,
                     "quantity_unit": "式", "unit_price": 15000},
         "evidence": {"project_name": "掛川店"}},
    ])
    applied = apply_learned_rules(_sample_pricing_rules())
    added = applied["additional_items"][-1]
    assert added["description"] == "防水処理工事"
    assert added["no"] == 4, "no は既存最大(3)+1 のはず"
    assert added["pricing_method"] == "fixed"
    assert added["quantity"] == 2 and isinstance(added["quantity"], int), \
        "quantity は数値（整数なら int）のはず"
    assert added["quantity_unit"] == "式"
    assert added["unit_price"] == 15000
    assert "学習により追加" in added["note"] and "掛川店" in added["note"]

    # 2回適用しても二重追加されない（load_pricing_rules は毎回呼ばれるため）
    applied2 = apply_learned_rules(applied)
    descs = [it["description"] for it in applied2["additional_items"]]
    assert descs.count("防水処理工事") == 1, "item_add が二重追加されている"


def test_apply_empty_category_all_lists():
    """category 空のルールは全リスト対象であること。"""
    _reset_store()
    store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "",
         "match_description": normalize_desc("墨出し"),
         "payload": {"unit_price": 250000, "old_unit_price": 237000}},
    ])
    applied = apply_learned_rules(_sample_pricing_rules())
    assert applied["construction_items"][0]["unit_price"] == 250000, \
        "category空でも全リストを走査して一致項目に適用されるはず"


def test_apply_no_rules_passthrough():
    """学習ルールが無ければ入力がそのまま返ること（従来動作の維持）。"""
    _reset_store()
    original = _sample_pricing_rules()
    applied = apply_learned_rules(original)
    assert applied == original


def test_learned_rules_summary():
    """learned_rules_summary が有効ルールの内訳を返すこと。"""
    _reset_store()
    rules = store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "材料費",
         "match_description": "a", "payload": {"unit_price": 1}},
        {"kind": "item_add", "category": "付帯工事",
         "match_description": "b", "payload": {"category": "付帯工事", "description": "b"}},
        {"kind": "item_suppress", "category": "施工費",
         "match_description": "c", "payload": {}},
    ])
    assert learned_rules_summary() == {"total": 3, "price": 1, "add": 1, "suppress": 1}
    # 無効化すると total からも消える
    store.set_rule_enabled("estimate", rules[0]["id"], False)
    assert learned_rules_summary() == {"total": 2, "price": 0, "add": 1, "suppress": 1}


# =============================================================
# store roundtrip
# =============================================================

def test_store_roundtrip():
    """add → 同キー上書き → disable → delete のラウンドトリップ。"""
    _reset_store()
    rules = store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "材料費",
         "match_description": "pvケーブル間", "payload": {"unit_price": 40000}},
    ])
    assert len(rules) == 1
    rid = rules[0]["id"]
    assert rid and rules[0]["enabled"] is True

    # 同キー（kind+category+match_description）の再学習は上書き（件数・ID不変）
    rules = store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "材料費",
         "match_description": "pvケーブル間", "payload": {"unit_price": 41000}},
    ])
    assert len(rules) == 1, "同キーの再学習は上書きのはず"
    assert rules[0]["id"] == rid, "上書き時はIDが維持されるはず"
    assert rules[0]["payload"]["unit_price"] == 41000

    # 別キーは追加
    rules = store.add_rules("estimate", [
        {"kind": "item_suppress", "category": "材料費",
         "match_description": "pvケーブル間", "payload": {}},
    ])
    assert len(rules) == 2, "kind が違えば別ルールとして追加されるはず"

    # disable → enabled_rules から消えるが load_rules には残る
    store.set_rule_enabled("estimate", rid, False)
    assert len(store.enabled_rules("estimate")) == 1
    assert len(store.load_rules("estimate")) == 2

    # delete → 完全に消える
    store.delete_rule("estimate", rid)
    remaining = store.load_rules("estimate")
    assert len(remaining) == 1 and remaining[0]["kind"] == "item_suppress"


def test_store_dedup_match_remarks():
    """estimate の dedup キーに match_remarks が含まれること（レビュー指摘 low）。

    備考違いの同名項目（PVケーブル間×5）のルールが互いに上書きされない。
    """
    _reset_store()
    rules = store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "材料費",
         "match_description": "pvケーブル間", "match_remarks": "配管",
         "payload": {"unit_price": 40000, "old_unit_price": 38000}},
        {"kind": "unit_price_override", "category": "材料費",
         "match_description": "pvケーブル間", "match_remarks": "付属品",
         "payload": {"unit_price": 90000, "old_unit_price": 86000}},
    ])
    assert len(rules) == 2, "備考違いの同名ルールは別ルールとして保存されるはず"

    # 同じ match_remarks の再学習は上書き（件数不変・payload更新）
    rules = store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "材料費",
         "match_description": "pvケーブル間", "match_remarks": "配管",
         "payload": {"unit_price": 41000, "old_unit_price": 38000}},
    ])
    assert len(rules) == 2, "同キー（match_remarks込み）の再学習は上書きのはず"
    by_rem = {r["match_remarks"]: r for r in rules}
    assert by_rem["配管"]["payload"]["unit_price"] == 41000

    # 旧形式（match_remarks 無し）は "" として後方互換（match_remarks="" と同キー）
    rules = store.add_rules("estimate", [
        {"kind": "item_suppress", "category": "施工費",
         "match_description": "墨出し", "payload": {}},
    ])
    assert len(rules) == 3
    rules = store.add_rules("estimate", [
        {"kind": "item_suppress", "category": "施工費",
         "match_description": "墨出し", "match_remarks": "",
         "payload": {"old_unit_price": 237000}},
    ])
    assert len(rules) == 3, "match_remarks 無しと \"\" は同キー（上書き）のはず"


# =============================================================
# ラウンドトリップ + knowledge_base フック
# =============================================================

def test_diff_to_store_to_apply_roundtrip():
    """diff → 承認（add_rules）→ apply の一気通貫が機能すること。"""
    _reset_store()
    ai = _estimate("ai", [
        _item("材料費", 1, "PVケーブル間", price=38000, qty=2, remarks="配管　VE54"),
        _item("施工費", 1, "墨出し", price=237000),
    ])
    official = _estimate("official", [
        _item("材料費", 1, "PVケーブル間", price=42000, qty=2, remarks="配管　VE54"),
        # 墨出しは正規に無い（削除）+ 防水処理工事が追加
        _item("付帯工事", 1, "防水処理工事", price=15000, qty=1),
    ], project="掛川店", file_name="正規見積.pdf")

    diffs = diff_estimates(ai, official)
    approved = [d.proposed_rule for d in diffs if d.learnable and d.proposed_rule]
    assert len(approved) == 3, f"承認対象は3件のはず: {[d.diff_type for d in diffs]}"
    store.add_rules("estimate", approved)

    applied = apply_learned_rules(_sample_pricing_rules())
    assert applied["material_items"][0]["unit_price"] == 42000, "学習単価が反映されるはず"
    c_descs = [it["description"] for it in applied["construction_items"]]
    assert "墨出し" not in c_descs, "削除学習が反映されるはず"
    a_descs = [it["description"] for it in applied["additional_items"]]
    assert "防水処理工事" in a_descs, "追加学習が反映されるはず"


def test_knowledge_base_hook():
    """load_pricing_rules 経由で学習が反映され、学習ゼロ件なら従来通りであること。"""
    from pricing.knowledge_base import load_pricing_rules

    _reset_store()
    baseline = load_pricing_rules()
    base_item = [it for it in baseline["construction_items"]
                 if it["description"] == "墨出し"][0]
    base_price = base_item["unit_price"]
    n_material = len(baseline.get("material_items", []))

    store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "施工費",
         "match_description": normalize_desc("墨出し"),
         "payload": {"unit_price": base_price + 13000, "old_unit_price": base_price}},
    ])
    loaded = load_pricing_rules()
    item = [it for it in loaded["construction_items"]
            if it["description"] == "墨出し"][0]
    assert item["unit_price"] == base_price + 13000, "フック経由で学習が反映されるはず"
    assert len(loaded.get("material_items", [])) == n_material, "他リストは不変のはず"

    # 学習ゼロ件に戻すと従来通り
    _reset_store()
    reloaded = load_pricing_rules()
    item2 = [it for it in reloaded["construction_items"]
             if it["description"] == "墨出し"][0]
    assert item2["unit_price"] == base_price, "学習ゼロ件なら従来のYAML値のはず"
    assert "学習補正" not in item2.get("note", "")


# =============================================================
# 実行
# =============================================================

def test_diff_unit_mismatch_converts_to_per_panel_price():
    """AI「266枚×¥2,178」× 正規「1式 ¥1,112,400」→ 式単価をそのまま学習せず、
    金額÷枚数で1枚単価（¥4,182）に換算して学習すること（2026-08-10 実障害の再発防止）。"""
    ai = _estimate("ai", [
        _item("施工費", 2, "架台取付工事", price=2178, qty=266, unit="枚"),
    ])
    official = _estimate("official", [
        _item("施工費", 2, "架台取付工事", price=1112400, qty=1.0, unit="式"),
    ])
    diffs = diff_estimates(ai, official)
    grouped = _by_type(diffs)
    assert "price_changed" in grouped, "単価差分が検出されない"
    d = grouped["price_changed"][0]
    assert d.learnable and d.proposed_rule is not None
    payload = d.proposed_rule["payload"]
    assert payload["unit_price"] == 4182, \
        f"¥1,112,400÷266枚=¥4,182 に換算されるはず: {payload}"
    assert payload["basis_quantity_unit"] == "枚"
    assert "換算" in d.summary, f"換算した旨がsummaryに出るはず: {d.summary}"
    assert "1,112,400" not in str(payload["unit_price"]), "式単価をそのまま学習してはいけない"


def test_diff_unit_mismatch_amount_only_converts():
    """正規側が「一式・金額のみ・単価欄空」でも金額÷枚数で換算学習できること
    （Codexレビュー指摘: 一式行は単価空欄が一般的な帳票形式）。"""
    ai = _estimate("ai", [
        _item("施工費", 2, "架台取付工事", price=2178, qty=266, unit="枚"),
    ])
    o_item = _item("施工費", 2, "架台取付工事", price=1112400, qty=1.0, unit="式")
    o_item.unit_price = None  # 単価欄空欄・金額のみ（amount=1,112,400 は保持）
    official = _estimate("official", [o_item])
    diffs = diff_estimates(ai, official)
    grouped = _by_type(diffs)
    assert "price_changed" in grouped, "単価空欄でも金額から換算されるはず"
    d = grouped["price_changed"][0]
    assert d.learnable and d.proposed_rule is not None
    assert d.proposed_rule["payload"]["unit_price"] == 4182


def test_diff_unit_mismatch_without_amount_reference_only():
    """単位不一致かつ正規側の金額が無く換算できない場合は参考表示に落ちること。"""
    ai = _estimate("ai", [
        _item("施工費", 2, "架台取付工事", price=2178, qty=266, unit="枚"),
    ])
    o_item = _item("施工費", 2, "架台取付工事", price=1112400, qty=1.0, unit="式")
    o_item.amount = None  # 金額不明 → 換算不能
    official = _estimate("official", [o_item])
    diffs = diff_estimates(ai, official)
    grouped = _by_type(diffs)
    assert "price_changed" in grouped
    d = grouped["price_changed"][0]
    assert not d.learnable and d.proposed_rule is None
    assert "参考表示" in d.summary


def test_apply_panel_rate_unit_guard():
    """panel_rate 項目への単価上書きガード:
    ①学習元単位が違うルールは適用しない ②単位情報の無い旧形式ルールでも
    上限（¥100,000/枚）超は適用しない ③正常な1枚単価は適用される。"""
    def _panel_rules():
        return {
            "construction_items": [
                {"no": 2, "description": "架台取付工事", "remarks": "",
                 "quantity_source": "equipment.planned_panels",
                 "quantity_unit": "枚", "unit_price": 2178,
                 "pricing_method": "panel_rate",
                 "fallback_unit_price_per_kw": 3300, "note": ""},
            ],
        }

    # ① 学習元単位が「式」→ 枚数連動項目には適用しない
    _reset_store()
    store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "施工費",
         "match_description": normalize_desc("架台取付工事"),
         "payload": {"unit_price": 4182, "old_unit_price": 2178,
                     "basis_quantity_unit": "式"}},
    ])
    applied = apply_learned_rules(_panel_rules())
    assert applied["construction_items"][0]["unit_price"] == 2178, \
        "単位不一致ルールは適用されないはず"

    # ② 旧形式（単位情報なし）でも ¥100,000/枚 超は単位取り違えとみなす
    _reset_store()
    store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "施工費",
         "match_description": normalize_desc("架台取付工事"),
         "payload": {"unit_price": 1112400, "old_unit_price": 2178}},
    ])
    applied2 = apply_learned_rules(_panel_rules())
    assert applied2["construction_items"][0]["unit_price"] == 2178, \
        "¥1,112,400/枚 のような桁違い単価は適用されないはず（¥2.96億事故の防止）"

    # ③ 正常な1枚単価（換算済み ¥4,182・単位一致）は適用される
    _reset_store()
    store.add_rules("estimate", [
        {"kind": "unit_price_override", "category": "施工費",
         "match_description": normalize_desc("架台取付工事"),
         "payload": {"unit_price": 4182, "old_unit_price": 2178,
                     "basis_quantity_unit": "枚"}},
    ])
    applied3 = apply_learned_rules(_panel_rules())
    item = applied3["construction_items"][0]
    assert item["unit_price"] == 4182, "正常な1枚単価は適用されるはず"
    assert "学習補正" in item["note"]


def main() -> bool:
    tests = [
        test_normalize_desc,
        test_diff_price_changed,
        test_diff_item_added_and_removed,
        test_diff_quantity_reference_only,
        test_diff_fuzzy_match,
        test_diff_same_key_collision,
        test_diff_dup_removed_not_learnable,
        test_diff_dup_remarks_price_changed,
        test_diff_dup_row_deleted_no_false_learn,
        test_apply_suppress_dup_guard,
        test_diff_zero_amount_removed_not_learnable,
        test_diff_item_add_price_completion,
        test_diff_unit_mismatch_converts_to_per_panel_price,
        test_diff_unit_mismatch_amount_only_converts,
        test_diff_unit_mismatch_without_amount_reference_only,
        test_apply_panel_rate_unit_guard,
        test_apply_override_and_deepcopy,
        test_apply_lump_formula_excluded,
        test_apply_suppress,
        test_apply_add,
        test_apply_empty_category_all_lists,
        test_apply_no_rules_passthrough,
        test_apply_old_price_guard,
        test_learned_rules_summary,
        test_store_roundtrip,
        test_store_dedup_match_remarks,
        test_diff_to_store_to_apply_roundtrip,
        test_knowledge_base_hook,
    ]
    print("=== 見積差分学習テスト（API不要） ===")
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
