"""設計確定情報（図面側→見積側の引き継ぎデータ）

2026-08-15 顧客ルールブック:
- 【図面側】10条: 図面完成時に「設計確定情報」を作る。不明は推測せず「未確認」。
- 【見積側】1条: 見積側は設計確定情報を正とし、枚数・容量・PCS台数等を
  勝手に再判断しない。矛盾があれば「設計確認」とする。

build_design_handoff(spec) が DraftingSpec（配置済み）から設計確定情報 dict を
組み立てる。キーは顧客ルールブック10条の項目に準拠。
"""
from __future__ import annotations

from drafting.models import DraftingSpec

UNCONFIRMED = "未確認"

# 顧客ルールブック10条の項目（順序も表示順として使う）
HANDOFF_KEYS = [
    "案件名",
    "電圧区分",            # 低圧／高圧
    "事業区分",            # 自家消費／FIT／FIP
    "売電区分",            # 全量売電／余剰売電／売電なし
    "逆潮流",
    "工事区分",            # 新設／増設／改修
    "蓄電池",
    "モジュールメーカー",
    "モジュール型式",
    "モジュールW数",
    "モジュール枚数",
    "PV容量",
    "PCSメーカー",
    "PCS型式",
    "PCS容量",
    "PCS台数",
    "屋根種類",
    "架台・固定方法",
    "PCS設置位置",
    "接続先",
    "主幹容量",
    "配管ルート",
    "DC配線概算距離",
    "AC配線概算距離",
    "貫通箇所",
    "盤改造・交換の有無",
    "支給品",
    "その他現調条件",
]

_ROOF_TYPE_LABELS = {
    "kawara": "瓦屋根",
    "slate": "スレート屋根",
    "setsuban": "折板屋根",
    "rikuyane": "陸屋根",
}

_MOUNT_LABELS = {
    "yane": "屋根用架台",
    "rikuyane_tug": "陸屋根用架台（TUG）",
}


def _placed_total(spec: DraftingSpec) -> int:
    return sum(int(f.panel_count or 0) for f in (spec.roof_faces or []) if f)


def build_design_handoff(spec: DraftingSpec) -> dict:
    """配置済み DraftingSpec から設計確定情報 dict を組み立てる。

    - 枚数・容量は「実配置」を正とする（配置0枚の場合は指示値を使わず未確認）
    - spec.handoff（抽出時に取得した区分・配線情報等）をマージ
    - 不明な項目はすべて「未確認」
    """
    h = {k: UNCONFIRMED for k in HANDOFF_KEYS}

    if spec.title.project_name:
        h["案件名"] = spec.title.project_name
    elif spec.customer_name:
        h["案件名"] = spec.customer_name

    p = spec.panel
    if p.maker:
        h["モジュールメーカー"] = p.maker
    if p.model:
        h["モジュール型式"] = p.model
    if (p.output_w or 0) > 0:
        h["モジュールW数"] = f"{int(p.output_w)}W"

    placed = _placed_total(spec)
    if placed > 0:
        # 枚数は実配置だけで確定する（W数未読取でも枚数は分かっている）。
        # 容量はW数がある場合のみ計算（推測しない）
        h["モジュール枚数"] = f"{placed}枚"
        if (p.output_w or 0) > 0:
            h["PV容量"] = f"{placed * p.output_w / 1000.0:.3f}kW"

    if spec.pcs_model:
        h["PCS型式"] = spec.pcs_model
    if (spec.pcs_count or 0) > 0:
        h["PCS台数"] = f"{int(spec.pcs_count)}台"

    roof_types = {f.roof_type for f in (spec.roof_faces or []) if f and f.roof_type}
    if roof_types:
        h["屋根種類"] = "・".join(
            _ROOF_TYPE_LABELS.get(rt, rt) for rt in sorted(roof_types))
    if spec.mount_type:
        h["架台・固定方法"] = _MOUNT_LABELS.get(spec.mount_type, spec.mount_type)

    # 抽出時の handoff（電圧区分・配線ルート等）をマージ。空値・None は未確認のまま。
    # 実配置・スペックから確定済みの項目は上書きしない（図面側の実配置が正。
    # 抽出テキストの指示値で確定値を潰さない — Codexレビュー指摘）。
    # 枚数・容量は「実配置からのみ」確定する項目のため、マージ対象から常に
    # 除外する（配置0枚のとき抽出の指示値が「設計確定」に化けるのを防ぐ）
    _placement_only_keys = ("モジュール枚数", "PV容量")
    for k, v in (spec.handoff or {}).items():
        if k in _placement_only_keys:
            continue
        if (k in h and h[k] == UNCONFIRMED
                and v is not None and str(v).strip()):
            h[k] = str(v).strip()

    # 図面側の完成状態（見積側の「設計確認」判定材料）
    h["_配置不可"] = any("指示枚数配置不可" in str(w) for w in (spec.warnings or []))
    h["_確認事項件数"] = len(spec.warnings or [])
    return h


def handoff_display_rows(handoff: dict) -> list:
    """UI表示用に (項目, 値) の行リストを返す（内部キー _ 始まりは除く）。"""
    return [(k, handoff.get(k, UNCONFIRMED)) for k in HANDOFF_KEYS]


def unconfirmed_items(handoff: dict) -> list:
    """未確認のままの項目名リスト（見積側の「要確認」表示に使う）。"""
    return [k for k in HANDOFF_KEYS if handoff.get(k, UNCONFIRMED) == UNCONFIRMED]
