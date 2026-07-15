"""幾何計算エンジン: DraftingSpec の各屋根面にパネル座標を割り付ける。

パイプライン上の役割:
    spec_extractor が屋根面・モジュール・目標枚数・系統を埋める
      → layout_engine.place_panels() が各 RoofFace に PanelRect 群を配置（本モジュール）
        → drawing_renderer が描画する

座標系（drafting/models.py の契約に準拠）:
    屋根面ローカル mm。原点は左上、x=右(東)、y=下(南)。
    PanelRect.x_mm / y_mm は屋根面ローカル原点からのオフセット。
    PanelRect.w_mm / h_mm は描画寸法（パネル向きを反映済み）。

設計方針:
    - 矩形面はグリッド配置（中央寄せ）。AUTO は portrait/landscape の枚数が多い方を採用。
    - ポリゴン面は外接矩形にグリッドを敷き、各パネル中心がポリゴン内部にあるものだけ残す
      （ray casting による点内包判定を自前実装）。
    - target_panel_count があれば上限として末尾から間引く（行優先＝面→行→列の走査順で残す）。
    - spec.strings が非空なら、全パネルを面→行→列順に走査して各 StringGroup へ
      series×parallel 枚ずつ string_id を付与する。

寸法はすべて mm。
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import List, Optional, Tuple

from drafting.models import (
    DraftingSpec,
    RoofFace,
    PanelRect,
    PanelSpec,
    StringGroup,
    Orientation,
)


# =============================================================
# 内部ユーティリティ: グリッド枚数計算
# =============================================================

def _fit_count(avail: float, panel: float, gap: float) -> int:
    """利用可能長 avail に panel（隙間 gap）が何個並ぶかを返す。

    並び条件: n*panel + (n-1)*gap <= avail
      → n <= (avail + gap) / (panel + gap)

    Args:
        avail: 利用可能長 mm（既にマージンを差し引いた値）。
        panel: パネル1個の当該方向寸法 mm。
        gap: パネル間の隙間 mm。

    Returns:
        並べられる個数（0 以上）。入力が不正なら 0。
    """
    if panel <= 0 or avail <= 0:
        return 0
    denom = panel + gap
    if denom <= 0:
        return 0
    return max(0, int(math.floor((avail + gap) / denom)))


def _grid_positions(
    avail_w: float,
    avail_d: float,
    panel_w: float,
    panel_h: float,
    gap_col: float,
    gap_row: float,
    margin: float,
) -> Tuple[int, int, List[Tuple[float, float]]]:
    """中央寄せグリッドの (rows, cols, 左上座標リスト) を返す。

    座標は屋根面ローカル（左上原点）。走査順は行優先（上の行から、各行は左→右）。

    Args:
        avail_w: マージン控除後の幅 mm（屋根幅方向）。
        avail_d: マージン控除後の奥行 mm（屋根奥行方向）。
        panel_w: パネルの幅方向寸法 mm。
        panel_h: パネルの奥行方向寸法 mm。
        gap_col: 列間（幅方向）の隙間 mm。
        gap_row: 行間（奥行方向）の隙間 mm。
        margin: 屋根エッジからの離隔 mm（左上オフセットの基準）。

    Returns:
        (rows, cols, [(x, y), ...])。配置不能なら (0, 0, [])。
    """
    cols = _fit_count(avail_w, panel_w, gap_col)
    rows = _fit_count(avail_d, panel_h, gap_row)
    if rows <= 0 or cols <= 0:
        return 0, 0, []

    used_w = cols * panel_w + (cols - 1) * gap_col
    used_d = rows * panel_h + (rows - 1) * gap_row
    # 水平・垂直とも中央寄せ（マージン内で余った分を均等に振る）
    offset_x = margin + max(0.0, (avail_w - used_w) / 2.0)
    offset_y = margin + max(0.0, (avail_d - used_d) / 2.0)

    positions: List[Tuple[float, float]] = []
    for r in range(rows):
        for c in range(cols):
            x = offset_x + c * (panel_w + gap_col)
            y = offset_y + r * (panel_h + gap_row)
            positions.append((x, y))
    return rows, cols, positions


# =============================================================
# 内部ユーティリティ: 点内包判定（ray casting）
# =============================================================

def _point_in_polygon(x: float, y: float, polygon: List[List[float]]) -> bool:
    """点 (x, y) がポリゴン内部にあるかを ray casting で判定する。

    polygon は閉じない頂点列 [[x, y], ...]。辺上の点はおおむね内部として扱う
    （厳密な辺判定は v1 では行わない）。

    Args:
        x, y: 判定する点の座標 mm。
        polygon: 頂点列（3 点以上）。

    Returns:
        内部なら True。頂点数が 3 未満なら False。
    """
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        # 水平レイ（右向き）が辺 (i, j) と交差するか
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


# =============================================================
# 内部ユーティリティ: 1 屋根面の配置
# =============================================================

def _orientation_dims(orientation: str, panel: PanelSpec) -> Tuple[float, float, float, float]:
    """向きに応じたパネル描画寸法と列/行の隙間を返す。

    LANDSCAPE: パネル長辺を屋根幅方向に → w=long, h=short。
    PORTRAIT : パネル長辺を屋根奥行方向に → w=short, h=long。

    隙間の規約（実サンプル準拠・向きに依らず一定）:
        列間（横・隣り合う列の間） = gap_short_mm（標準10mm。モジュール同士の小隙間）
        行間（縦・段と段の間）     = gap_long_mm（標準25mm。架台レール/水勾配用）
        ※実図面（栗原/八木/スパイス）では横10mm・縦25mmが一貫して使われ、
          drawing_renderer のパネル詳細図・簡易グリッドとも一致する。

    Returns:
        (panel_w, panel_h, gap_col, gap_row)
    """
    long_mm = max(panel.long_mm, panel.short_mm)
    short_mm = min(panel.long_mm, panel.short_mm)
    gap_col = panel.gap_short_mm   # 列間（横）= 小さい方（標準10）
    gap_row = panel.gap_long_mm    # 行間（縦）= 大きい方（標準25）
    if orientation == Orientation.LANDSCAPE:
        return long_mm, short_mm, gap_col, gap_row
    # PORTRAIT（既定）
    return short_mm, long_mm, gap_col, gap_row


def _place_rectangle_one(
    face: RoofFace, panel: PanelSpec, orientation: str
) -> Tuple[int, int, List[PanelRect]]:
    """矩形屋根面を指定向きで配置し (rows, cols, PanelRect群) を返す。"""
    width = float(face.width_mm or 0)
    depth = float(face.depth_mm or 0)
    margin = float(face.margin_mm or 0)
    avail_w = width - 2 * margin
    avail_d = depth - 2 * margin

    panel_w, panel_h, gap_col, gap_row = _orientation_dims(orientation, panel)
    rows, cols, positions = _grid_positions(
        avail_w, avail_d, panel_w, panel_h, gap_col, gap_row, margin
    )
    rects = [
        PanelRect(x_mm=x, y_mm=y, w_mm=panel_w, h_mm=panel_h, orientation=orientation)
        for (x, y) in positions
    ]
    return rows, cols, rects


def _place_polygon_one(
    face: RoofFace, panel: PanelSpec, orientation: str
) -> Tuple[int, int, List[PanelRect]]:
    """ポリゴン屋根面を指定向きで配置し (rows, cols, PanelRect群) を返す。

    外接矩形にグリッドを敷き、各パネル中心がポリゴン内部のものだけ残す。
    rows/cols は「敷いたグリッドの段数/列数」を返す（残った枚数とは別）。
    margin はポリゴンの内側オフセットではなく外接矩形からの margin で近似（v1）。
    """
    polygon = face.polygon_mm or []
    if len(polygon) < 3:
        return 0, 0, []

    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    bbox_w = max_x - min_x
    bbox_d = max_y - min_y
    margin = float(face.margin_mm or 0)
    avail_w = bbox_w - 2 * margin
    avail_d = bbox_d - 2 * margin

    panel_w, panel_h, gap_col, gap_row = _orientation_dims(orientation, panel)
    rows, cols, positions = _grid_positions(
        avail_w, avail_d, panel_w, panel_h, gap_col, gap_row, margin
    )

    rects: List[PanelRect] = []
    for (x, y) in positions:
        # グリッドは外接矩形ローカル（min_x, min_y を 0 とした座標）で算出されている。
        # 中心を外接矩形→ポリゴン絶対座標へ戻して内包判定する。
        cx = min_x + x + panel_w / 2.0
        cy = min_y + y + panel_h / 2.0
        if _point_in_polygon(cx, cy, polygon):
            # 座標は屋根面ローカル（=外接矩形ローカル, 左上原点）でそのまま保持
            rects.append(
                PanelRect(
                    x_mm=x, y_mm=y, w_mm=panel_w, h_mm=panel_h, orientation=orientation
                )
            )
    return rows, cols, rects


def _apply_roof_type_defaults(face: RoofFace, panel: PanelSpec) -> PanelSpec:
    """屋根種別ごとの離隔既定値（knowledge/layout_defaults.yaml）を適用する。

    枚数未指定（target_panel_count が None/0 = 最大枚数の自動算出）の面に
    限って適用する。枚数指定がある面は実設計に基づく図面のため、
    実測由来の離隔（既定値と同値のこともある）を上書きしない。
    さらにフォーム・AI抽出・学習ルールで明示された値を尊重し、標準既定値
    （行間25 / 列間10 / マージン500）のままの項目だけ差し替える。
    face.margin_mm は in-place 更新、パネル隙間は差し替え済みコピーを返す。
    """
    if face.target_panel_count and face.target_panel_count > 0:
        return panel  # 枚数指定あり = 実設計の再現 → 離隔は触らない
    try:
        from drafting.layout_defaults import roof_type_defaults
        d = roof_type_defaults(face.roof_type)
    except Exception:
        return panel
    if not d:
        return panel
    try:
        # 標準既定値のまま or 0（AI抽出の「記載なし」は0で返る）を未指定とみなす
        if face.margin_mm in (500.0, 0, 0.0) and d.get("margin_mm") is not None:
            face.margin_mm = float(d["margin_mm"])
        gl, gs = panel.gap_long_mm, panel.gap_short_mm
        if gl in (25.0, 0, 0.0) and d.get("gap_long_mm") is not None:
            gl = float(d["gap_long_mm"])
        if gs in (10.0, 0, 0.0) and d.get("gap_short_mm") is not None:
            gs = float(d["gap_short_mm"])
        if (gl, gs) == (panel.gap_long_mm, panel.gap_short_mm):
            return panel
        return replace(panel, gap_long_mm=gl, gap_short_mm=gs)
    except Exception:
        return panel


def _layout_face(face: RoofFace, panel: PanelSpec) -> None:
    """1 屋根面に対してパネルを配置し、face を in-place で更新する。

    - orientation=AUTO は portrait/landscape を両方計算して枚数の多い方を採用。
    - target_panel_count があれば上限として末尾から間引く（行優先で前から残す）。
      None/0/負値は「枚数未指定」= 収まる最大枚数をそのまま採用。
    """
    shape = (face.shape or "rectangle").lower()

    def _compute(orientation: str) -> Tuple[int, int, List[PanelRect]]:
        if shape == "polygon":
            return _place_polygon_one(face, panel, orientation)
        return _place_rectangle_one(face, panel, orientation)

    want = (face.orientation or Orientation.AUTO).lower()
    if want == Orientation.AUTO:
        r_p, c_p, rects_p = _compute(Orientation.PORTRAIT)
        r_l, c_l, rects_l = _compute(Orientation.LANDSCAPE)
        if len(rects_l) > len(rects_p):
            rows, cols, rects = r_l, c_l, rects_l
        else:
            rows, cols, rects = r_p, c_p, rects_p
    elif want == Orientation.LANDSCAPE:
        rows, cols, rects = _compute(Orientation.LANDSCAPE)
    else:  # PORTRAIT または未知 → portrait 既定
        rows, cols, rects = _compute(Orientation.PORTRAIT)

    # target 上限で間引く（行優先＝走査順で前から残し、末尾を切る）
    target = face.target_panel_count
    if target is not None and target <= 0:
        target = None  # 0/負値 = 枚数未指定 → 最大枚数を自動配置
    if target is not None and len(rects) > target:
        rects = rects[:target]
        # 間引き後は最終行が不完全になり得る。cols は維持し、
        # rows は「実枚数 ÷ cols の切り上げ」で再導出する（端数行を含む実段数）。
        if cols > 0:
            rows = int(math.ceil(len(rects) / cols))
        else:
            rows = 0

    face.panels = rects
    face.rows = rows
    face.cols = cols
    face.panel_count = len(rects)


# =============================================================
# 内部ユーティリティ: ストリング割当
# =============================================================

def _parse_config_text(text: str) -> List[Tuple[int, int]]:
    """系統文字列（"12直×5並" / "12直×4並＋10直×2並"）から (直列, 並列) の組を抽出する。

    "＋"/"+" 区切りの複合系統に対応。数値が取れない場合は空リスト。
    全角×/x、全角＋/+、全角数字も許容する。
    """
    if not text:
        return []
    import re
    s = str(text)
    # 全角数字 → 半角
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    pairs: List[Tuple[int, int]] = []
    # 「<直列>直 × <並列>並」のパターンを全て拾う
    for m in re.finditer(r"(\d+)\s*直\s*[×xX*]\s*(\d+)\s*並", s):
        series = int(m.group(1))
        parallel = int(m.group(2))
        if series > 0 and parallel > 0:
            pairs.append((series, parallel))
    return pairs


def _string_subconfigs(sg: StringGroup) -> List[Tuple[int, int]]:
    """StringGroup から (直列, 並列) の組リストを得る。

    series/parallel が数値で取れればそれを優先。無ければ config_text を解析する。
    """
    series = int(sg.series or 0)
    parallel = int(sg.parallel or 0)
    if series > 0 and parallel > 0:
        return [(series, parallel)]
    return _parse_config_text(sg.config_text)


def _assign_strings(spec: DraftingSpec) -> None:
    """spec.strings に基づき全パネルへ string_id を付与する。

    全パネルを 面→行→列 の順に走査し、各 StringGroup の (直列×並列) 枚ずつ
    順番に割り当てる。series/parallel が空でも config_text（"12直×5並" 等）を
    解析して枚数を導出する（実運用では config_text のみが埋まることが多いため）。

    string_id は色分け・系統識別に使える文字列とする（"<pcs_label>-<k>"）。
    strings が空、または枚数が一切導出できないときは何もしない（string_id は None）。
    """
    strings: List[StringGroup] = [s for s in (spec.strings or []) if s is not None]
    if not strings:
        return

    # 走査順で全パネルを 1 列に並べる（面→行→列。panels は既に行優先で並んでいる）
    flat: List[PanelRect] = []
    for face in spec.roof_faces:
        if face is None:
            continue
        for rect in face.panels:
            flat.append(rect)
    if not flat:
        return

    idx = 0
    n = len(flat)
    for sg in strings:
        label = sg.pcs_label or "PCS"
        subs = _string_subconfigs(sg)
        if not subs:
            # 直列/並列も config_text も読めない系統はスキップ
            continue
        branch_no = 0
        for series, parallel in subs:
            # この系統（サブ含む）に属する枚へ、並列ごとに枝番号を振る
            for _p in range(parallel):
                if idx >= n:
                    break
                branch_no += 1
                branch = f"{label}-{branch_no}"
                for _s in range(series):
                    if idx >= n:
                        break
                    flat[idx].string_id = branch
                    idx += 1
            if idx >= n:
                break
        if idx >= n:
            break


# =============================================================
# 公開 API
# =============================================================

def place_panels(spec: DraftingSpec) -> DraftingSpec:
    """DraftingSpec の各屋根面にパネル座標を割り付けて返す（in-place）。

    各 RoofFace の panels / rows / cols / panel_count を確定し、
    spec.strings が非空なら string_id を付与し、最後に
    spec.recompute_totals() を呼んで total_panels / total_kw を再計算する。

    Args:
        spec: 配置対象の DraftingSpec。roof_faces と panel を参照する。

    Returns:
        同一の spec オブジェクト（配置済み）。spec が None や不正な場合は
        可能な範囲で安全に処理し、例外は投げない。
    """
    if spec is None:
        raise ValueError("spec is None: place_panels には DraftingSpec が必要です")

    panel = spec.panel or PanelSpec()
    faces = spec.roof_faces or []

    effective_panels = []  # 各面で実際に使った隙間（凡例表示との整合用）
    for face in faces:
        if face is None:
            continue
        try:
            eff_panel = _apply_roof_type_defaults(face, panel)
            effective_panels.append(eff_panel)
            _layout_face(face, eff_panel)
        except Exception as exc:  # 1 面の失敗で全体を止めない
            # 当該面は空配置として続行し、所見に残す
            face.panels = []
            face.rows = 0
            face.cols = 0
            face.panel_count = 0
            try:
                spec.warnings.append(
                    f"面『{getattr(face, 'name', '?')}』の配置に失敗: {exc}"
                )
            except Exception:
                pass

    # 屋根種別既定値で隙間を差し替えた場合、全面が同一値なら spec.panel にも
    # 反映する（drawing_renderer の間隔注記・凡例が実配置値と食い違わないように）
    try:
        gaps = {(p.gap_long_mm, p.gap_short_mm) for p in effective_panels}
        if len(gaps) == 1:
            gl, gs = gaps.pop()
            if (gl, gs) != (panel.gap_long_mm, panel.gap_short_mm):
                spec.panel.gap_long_mm = gl
                spec.panel.gap_short_mm = gs
    except Exception:
        pass

    # ストリング割当（strings が空なら no-op）
    try:
        _assign_strings(spec)
    except Exception as exc:
        try:
            spec.warnings.append(f"ストリング割当に失敗: {exc}")
        except Exception:
            pass

    return spec.recompute_totals()


# =============================================================
# 自己テスト
# =============================================================

if __name__ == "__main__":
    from drafting.sample_specs import GOLDEN_SPECS, get_golden

    def _string_summary(spec: DraftingSpec) -> str:
        ids = {}
        for f in spec.roof_faces:
            for p in f.panels:
                ids[p.string_id] = ids.get(p.string_id, 0) + 1
        if list(ids.keys()) == [None]:
            return "（系統割当なし）"
        labeled = sum(v for k, v in ids.items() if k is not None)
        return f"系統付与 {labeled}枚 / 系統数 {len([k for k in ids if k])}"

    print("=" * 68)
    print("layout_engine 自己テスト: 4 ゴールデン仕様に place_panels を適用")
    print("=" * 68)

    all_ok = True
    for name in GOLDEN_SPECS:
        spec = get_golden(name)
        target_total = sum(int(f.target_panel_count or 0) for f in spec.roof_faces)
        place_panels(spec)

        print()
        print(f"■ {name}  (paper={spec.paper}, panel={spec.panel.maker} "
              f"{spec.panel.long_mm}x{spec.panel.short_mm})")
        face_total = 0
        for f in spec.roof_faces:
            # 不変条件: panel_count は実 PanelRect 数と一致（必須）
            count_ok = (f.panel_count == len(f.panels))
            # グリッド整合: 矩形は (rows-1)*cols < count <= rows*cols（端数行許容）
            #              ポリゴンは間引きのため rows*cols >= count を満たせばよい
            if f.cols > 0:
                grid_ok = ((f.rows - 1) * f.cols < f.panel_count <= f.rows * f.cols)
            else:
                grid_ok = (f.panel_count == 0)
            consistent = count_ok and grid_ok
            shape_note = ""
            if f.shape == "polygon":
                shape_note = "  [polygon: 端数あり]"
            flag = "OK" if consistent else "NG"
            if not consistent:
                all_ok = False
            print(f"   - {f.name}: shape={f.shape} orient={f.orientation} "
                  f"{f.rows}行×{f.cols}列 → {f.panel_count}枚 "
                  f"(target={f.target_panel_count}) [{flag}]{shape_note}")
            face_total += f.panel_count

        print(f"   合計: {face_total}枚 (target合計={target_total}) "
              f"/ total_panels={spec.total_panels} / total_kw={spec.total_kw}")
        print(f"   {_string_summary(spec)}")

        # 整合チェック: total_panels == 各面 panel_count 合計
        if spec.total_panels != face_total:
            print("   [NG] total_panels が面合計と不一致")
            all_ok = False

    # ポリゴン専用チェック1: ray casting の点内包判定が正しいか（直接検証）
    print()
    print("-" * 68)
    print("ポリゴン内包判定チェック (ray casting)")
    yagi_poly = get_golden("yagi_layout").roof_faces[0].polygon_mm
    # 切り欠き内部の点（x<2900, y<1450）は外、下段の点は内
    pt_notch = _point_in_polygon(1000, 500, yagi_poly)      # L字の欠けた角 → 外
    pt_inside = _point_in_polygon(5000, 3000, yagi_poly)    # 主部 → 内
    pt_outside = _point_in_polygon(-100, -100, yagi_poly)   # 完全に外 → 外
    print(f"   切欠き(1000,500)=内側? {pt_notch} (期待 False) / "
          f"主部(5000,3000)=内側? {pt_inside} (期待 True) / "
          f"範囲外(-100,-100)=内側? {pt_outside} (期待 False)")
    if (not pt_notch) and pt_inside and (not pt_outside):
        print("   [OK] ray casting 内包判定は正常")
    else:
        print("   [NG] ray casting 内包判定が不正")
        all_ok = False

    # ポリゴン専用チェック2: 切り欠きにグリッドが届くケースで枚数が外接矩形より減ることを確認
    print()
    print("-" * 68)
    print("ポリゴン縮小チェック (切り欠きにグリッドが届く合成ポリゴン)")
    from drafting.models import RoofFace as _RF, PanelSpec as _PS, DraftingSpec as _DS
    # 10000x10000 の正方形の右下 5000x5000 を欠いた L 字（中央寄せでも欠けに掛かる）
    notched = [[0, 0], [10000, 0], [10000, 5000], [5000, 5000], [5000, 10000], [0, 10000]]
    syn = _DS(
        panel=_PS(long_mm=1750, short_mm=1170, gap_long_mm=10, gap_short_mm=10),
        roof_faces=[_RF(
            name="合成", shape="polygon", polygon_mm=notched,
            width_mm=10000, depth_mm=10000, margin_mm=200,
            orientation=Orientation.PORTRAIT, target_panel_count=None,
        )],
    )
    place_panels(syn)
    fseg = syn.roof_faces[0]
    grid_cells = fseg.rows * fseg.cols
    poly_count = fseg.panel_count
    print(f"   外接矩形グリッド総セル: {grid_cells} / ポリゴン内に残った枚数: {poly_count}")
    if poly_count < grid_cells:
        print("   [OK] ポリゴン内包判定で外接矩形より枚数が減少")
    else:
        print("   [NG] ポリゴンで枚数が減っていない")
        all_ok = False
    print("   (参考) yagi_layout の L 字は中央寄せグリッドが切欠きに掛からないため全数残存")

    # ストリング割当チェック: tok_string に series/parallel を入れて確認
    print()
    print("-" * 68)
    print("ストリング割当チェック (tok_string, series/parallel 注入)")
    tok = get_golden("tok_string")
    # config_text のみ → series/parallel を補完して割当を有効化
    tok.strings = [
        StringGroup(pcs_label="PCS1", series=12, parallel=5),  # 60枚
        StringGroup(pcs_label="PCS2", series=12, parallel=4),  # 48枚
        StringGroup(pcs_label="PCS3", series=12, parallel=6),  # 72枚
    ]
    place_panels(tok)
    assigned = 0
    sample_ids = []
    for f in tok.roof_faces:
        for p in f.panels:
            if p.string_id:
                assigned += 1
                if len(sample_ids) < 6:
                    sample_ids.append(p.string_id)
    total_panels = tok.total_panels
    print(f"   配置総枚数: {total_panels} / string_id 付与: {assigned}")
    print(f"   string_id サンプル: {sample_ids}")
    if assigned > 0:
        print("   [OK] series×parallel に基づき string_id を付与")
    else:
        print("   [NG] string_id が付与されていない")
        all_ok = False

    print()
    print("=" * 68)
    print(f"自己テスト結果: {'ALL OK' if all_ok else 'NG あり（上記参照）'}")
    print("=" * 68)
