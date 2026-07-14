"""AI図面スペックと正規図面スペックの差分抽出（図面の学習）

spec_to_dict 形式の dict 同士（AI生成版 / 人が修正した正規版）を比較し、
DrawingDiffItem のリストを返す。

設計方針（docs/LEARNING_LOOP_DESIGN.md §12）:
- 学習可能な差分（パネル間隔 gap / マージン margin / 向き orientation）には
  store.add_rules へそのまま渡せる proposed_rule を付ける。
- 枚数・屋根寸法・架台・系統・モジュール型番は案件固有のため参考表示のみ
  （learnable=False, proposed_rule=None）。誤学習で全案件が壊れることを防ぐ。
- 最後に必ず1件、正規スペック全体を few-shot お手本として登録する
  golden_example 提案を付ける（spec_extractor のプロンプトに注入される）。
"""
from datetime import datetime

from drafting.models import DrawingType, Orientation, RoofType
from learning.models import DrawingDiffItem

_EPS = 1e-6
# 屋根寸法の差分とみなす閾値（正規値に対する比率。±2%超で案件相違とみなす）
_DIMENSION_TOLERANCE = 0.02


# =============================================================
# 内部ユーティリティ
# =============================================================

def _num(val) -> float:
    """数値へ寛容変換（None/文字列/数値）。失敗時は 0。"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _fmt_num(v) -> str:
    """数値の表示用文字列（整数なら小数点を出さない）。"""
    f = _num(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _roof_label(roof_type: str) -> str:
    """roof_type の日本語ラベル（"*" は全屋根共通）。"""
    if not roof_type or roof_type == "*":
        return "全屋根"
    return RoofType.LABEL.get(roof_type, roof_type)


def _common_roof_type(spec: dict) -> str:
    """spec の屋根面が単一種別ならその roof_type、混在/不明なら "*" を返す。

    パネル間隔（gap）は spec 全体で1つのため、どの屋根種別の学習かは
    面の種別が一意なときだけ特定できる。混在時は全屋根共通 "*" とする。
    """
    types = {
        (f or {}).get("roof_type", "")
        for f in (spec.get("roof_faces") or [])
        if (f or {}).get("roof_type")
    }
    return types.pop() if len(types) == 1 else "*"


def _placed_orientation(face: dict) -> str:
    """面の「実配置向き」を返す（AUTO は実際に置かれたパネルの向きで解決）。

    orientation が AUTO のままで配置結果（panels）も無い場合は ""（比較対象外）。
    """
    ori = (face or {}).get("orientation") or ""
    if ori and ori != Orientation.AUTO:
        return ori
    panels = (face or {}).get("panels") or []
    if panels and isinstance(panels[0], dict):
        return panels[0].get("orientation", "") or ""
    return ""


def _effective_count(face: dict) -> int:
    """面の実効枚数（配置済みなら panel_count、抽出段階なら target_panel_count）。"""
    face = face or {}
    n = int(_num(face.get("panel_count")))
    if n > 0:
        return n
    return int(_num(face.get("target_panel_count")))


def _string_display(s: dict) -> str:
    """StringGroup dict の表示文字列（config_text 優先、無ければ 直×並 から生成）。"""
    s = s or {}
    ct = s.get("config_text") or ""
    if ct:
        return ct
    series = int(_num(s.get("series")))
    parallel = int(_num(s.get("parallel")))
    return f"{series}直×{parallel}並" if series and parallel else ""


def _evidence(official_spec: dict, note: str) -> dict:
    """proposed_rule に付ける根拠情報（どの案件から学んだか）。"""
    title = official_spec.get("title") or {}
    return {
        "project_name": title.get("project_name") or official_spec.get("customer_name", ""),
        "learned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "note": note,
    }


def _make_rule(kind: str, payload: dict, display_description: str,
               note: str, official_spec: dict) -> dict:
    """store.add_rules にそのまま渡せる LearnedRule dict を組み立てる。"""
    return {
        "target": "drawing",
        "kind": kind,
        "category": "",
        "match_description": "",
        "display_description": display_description,
        "payload": payload,
        "evidence": _evidence(official_spec, note),
        "enabled": True,
        "applied_count": 0,
    }


def _pair_faces(ai_faces: list, of_faces: list) -> list:
    """屋根面の対応付け。name 完全一致を優先し、残りは順序でフォールバック。"""
    pairs = []
    used_ai, used_of = set(), set()
    for i, af in enumerate(ai_faces):
        name = (af or {}).get("name") or ""
        if not name:
            continue
        for j, off in enumerate(of_faces):
            if j in used_of:
                continue
            if ((off or {}).get("name") or "") == name:
                pairs.append((af, off))
                used_ai.add(i)
                used_of.add(j)
                break
    rest_ai = [af for i, af in enumerate(ai_faces) if i not in used_ai]
    rest_of = [off for j, off in enumerate(of_faces) if j not in used_of]
    pairs.extend(zip(rest_ai, rest_of))
    return pairs


# =============================================================
# 公開関数
# =============================================================

def diff_drawing_specs(ai_spec: dict, official_spec: dict) -> list:
    """AI図面スペックと正規図面スペックの差分を抽出する。

    Args:
        ai_spec: AI生成版の spec dict（spec_to_dict 形式。履歴 or JSON由来）。
        official_spec: 正規版の spec dict（extract_drafting_spec の結果等）。

    Returns:
        list[DrawingDiffItem]。末尾に必ず golden_example 提案が1件付く。
    """
    ai_spec = ai_spec if isinstance(ai_spec, dict) else {}
    official_spec = official_spec if isinstance(official_spec, dict) else {}
    diffs: list = []

    ai_panel = ai_spec.get("panel") or {}
    of_panel = official_spec.get("panel") or {}

    # --- パネル間隔 gap_long / gap_short（学習可能） ---
    gl_ai = _num(ai_panel.get("gap_long_mm", 25))
    gl_of = _num(of_panel.get("gap_long_mm", 25))
    gs_ai = _num(ai_panel.get("gap_short_mm", 10))
    gs_of = _num(of_panel.get("gap_short_mm", 10))
    gap_payload = {}
    gap_parts = []
    if abs(gl_ai - gl_of) > _EPS:
        gap_payload["gap_long_mm"] = gl_of
        gap_parts.append(f"縦 {_fmt_num(gl_ai)}→{_fmt_num(gl_of)}mm")
    if abs(gs_ai - gs_of) > _EPS:
        gap_payload["gap_short_mm"] = gs_of
        gap_parts.append(f"横 {_fmt_num(gs_ai)}→{_fmt_num(gs_of)}mm")
    if gap_payload:
        roof_type = _common_roof_type(official_spec)
        gap_payload["roof_type"] = roof_type
        summary = "パネル間隔 " + "・".join(gap_parts)
        diffs.append(DrawingDiffItem(
            diff_type="gap_changed",
            target="パネル間隔",
            ai_value=f"縦{_fmt_num(gl_ai)} / 横{_fmt_num(gs_ai)}mm",
            official_value=f"縦{_fmt_num(gl_of)} / 横{_fmt_num(gs_of)}mm",
            summary=summary,
            learnable=True,
            proposed_rule=_make_rule(
                "gap_override", gap_payload,
                f"{_roof_label(roof_type)}のパネル間隔を学習値に",
                summary, official_spec),
        ))

    # --- 屋根面ごとの比較（name 一致 → 順序フォールバック） ---
    pairs = _pair_faces(ai_spec.get("roof_faces") or [],
                        official_spec.get("roof_faces") or [])

    # 同一承認バッチ内で同キー (kind, roof_type) のルールが複数生まれると
    # store 側の dedup で先勝ち以外が無警告に消えるため、2件目以降は参考表示に落とす
    proposed_keys: dict = {}

    def _face_rule(kind: str, roof_type: str, payload: dict, summary: str):
        """面単位ルールの提案。roof_type不明・同キー競合は参考表示（None）に落とす。"""
        if not roof_type:
            return None, "（屋根種別不明のため参考表示）"
        key = (kind, roof_type)
        if key in proposed_keys:
            if proposed_keys[key] == payload:
                return None, "（同内容の学習提案が既にあるため省略）"
            return None, "（同種ルールの提案が競合するため参考表示。別々に学習してください）"
        proposed_keys[key] = payload
        return payload, ""

    for af, off in pairs:
        af = af or {}
        off = off or {}
        name = off.get("name") or af.get("name") or "面"
        roof_type = off.get("roof_type") or ""

        # マージン（学習可能）
        m_ai = _num(af.get("margin_mm", 500))
        m_of = _num(off.get("margin_mm", 500))
        if abs(m_ai - m_of) > _EPS:
            summary = f"マージン {_fmt_num(m_ai)}→{_fmt_num(m_of)}mm"
            payload, note = _face_rule(
                "margin_override", roof_type,
                {"margin_mm": m_of, "roof_type": roof_type}, summary)
            diffs.append(DrawingDiffItem(
                diff_type="margin_changed",
                target=name,
                ai_value=f"{_fmt_num(m_ai)}mm",
                official_value=f"{_fmt_num(m_of)}mm",
                summary=summary + note,
                learnable=payload is not None,
                proposed_rule=_make_rule(
                    "margin_override", payload,
                    f"{_roof_label(roof_type)}のマージンを{_fmt_num(m_of)}mmに",
                    f"{name}: {summary}", official_spec) if payload else None,
            ))

        # 向き（学習可能。AUTO は実配置向きで解決してから比較）
        o_ai = _placed_orientation(af)
        o_of = _placed_orientation(off)
        if o_ai and o_of and o_ai != o_of:
            label_ai = Orientation.LABEL.get(o_ai, o_ai)
            label_of = Orientation.LABEL.get(o_of, o_of)
            summary = f"パネル向き {label_ai}→{label_of}"
            payload, note = _face_rule(
                "orientation_preference", roof_type,
                {"orientation": o_of, "roof_type": roof_type}, summary)
            diffs.append(DrawingDiffItem(
                diff_type="orientation_changed",
                target=name,
                ai_value=label_ai,
                official_value=label_of,
                summary=summary + note,
                learnable=payload is not None,
                proposed_rule=_make_rule(
                    "orientation_preference", payload,
                    f"{_roof_label(roof_type)}の向き既定を{label_of}に",
                    f"{name}: {summary}", official_spec) if payload else None,
            ))

        # 枚数・行列（案件固有 → 参考表示のみ）
        c_ai = _effective_count(af)
        c_of = _effective_count(off)
        r_ai, r_of = int(_num(af.get("rows"))), int(_num(off.get("rows")))
        col_ai, col_of = int(_num(af.get("cols"))), int(_num(off.get("cols")))
        count_diff = c_ai > 0 and c_of > 0 and c_ai != c_of
        grid_diff = (r_ai > 0 and r_of > 0 and r_ai != r_of) or \
                    (col_ai > 0 and col_of > 0 and col_ai != col_of)
        if count_diff or grid_diff:
            parts = []
            if count_diff:
                parts.append(f"枚数 {c_ai}→{c_of}枚")
            if grid_diff:
                parts.append(f"配置 {r_ai}行×{col_ai}列→{r_of}行×{col_of}列")
            diffs.append(DrawingDiffItem(
                diff_type="panel_count_changed",
                target=name,
                ai_value=f"{c_ai}枚",
                official_value=f"{c_of}枚",
                summary="・".join(parts) + "（案件固有のため参考表示）",
                learnable=False,
            ))

        # 屋根寸法 ±2%超（案件固有 → 参考表示のみ）
        dim_parts = []
        for key, jp in (("width_mm", "幅"), ("depth_mm", "奥行")):
            v_ai = _num(af.get(key, 0))
            v_of = _num(off.get(key, 0))
            if v_ai > 0 and v_of > 0 and abs(v_ai - v_of) > _DIMENSION_TOLERANCE * v_of:
                dim_parts.append(f"{jp} {_fmt_num(v_ai)}→{_fmt_num(v_of)}mm")
        if dim_parts:
            diffs.append(DrawingDiffItem(
                diff_type="face_dimension_changed",
                target=name,
                ai_value=f"{_fmt_num(af.get('width_mm', 0))}×{_fmt_num(af.get('depth_mm', 0))}mm",
                official_value=f"{_fmt_num(off.get('width_mm', 0))}×{_fmt_num(off.get('depth_mm', 0))}mm",
                summary="屋根寸法 " + "・".join(dim_parts) + "（案件固有のため参考表示）",
                learnable=False,
            ))

    # --- 架台種別（参考表示のみ） ---
    mt_ai = ai_spec.get("mount_type") or ""
    mt_of = official_spec.get("mount_type") or ""
    if mt_ai and mt_of and mt_ai != mt_of:
        diffs.append(DrawingDiffItem(
            diff_type="mount_type_changed",
            target="架台",
            ai_value=mt_ai,
            official_value=mt_of,
            summary=f"架台種別 {mt_ai}→{mt_of}（参考表示）",
            learnable=False,
        ))

    # --- ストリング系統（参考表示のみ） ---
    ai_strings = ai_spec.get("strings") or []
    of_strings = official_spec.get("strings") or []
    for idx in range(max(len(ai_strings), len(of_strings))):
        s_ai = ai_strings[idx] if idx < len(ai_strings) else {}
        s_of = of_strings[idx] if idx < len(of_strings) else {}
        d_ai = _string_display(s_ai)
        d_of = _string_display(s_of)
        if d_ai != d_of:
            label = (s_of or {}).get("pcs_label") or (s_ai or {}).get("pcs_label") or f"系統{idx + 1}"
            diffs.append(DrawingDiffItem(
                diff_type="string_config_changed",
                target=label,
                ai_value=d_ai or "（なし）",
                official_value=d_of or "（なし）",
                summary=f"{label} 系統 {d_ai or '（なし）'}→{d_of or '（なし）'}（参考表示）",
                learnable=False,
            ))

    # --- モジュール型番・出力（参考表示のみ） ---
    model_ai = ai_panel.get("model") or ""
    model_of = of_panel.get("model") or ""
    w_ai = _num(ai_panel.get("output_w"))
    w_of = _num(of_panel.get("output_w"))
    model_diff = bool(model_ai and model_of and model_ai != model_of)
    output_diff = w_ai > 0 and w_of > 0 and abs(w_ai - w_of) > _EPS
    if model_diff or output_diff:
        diffs.append(DrawingDiffItem(
            diff_type="panel_spec_changed",
            target="モジュール",
            ai_value=f"{model_ai} {_fmt_num(w_ai)}W",
            official_value=f"{model_of} {_fmt_num(w_of)}W",
            summary=f"モジュール {model_ai} {_fmt_num(w_ai)}W→{model_of} {_fmt_num(w_of)}W（参考表示）",
            learnable=False,
        ))

    # --- 必ず1件: 正規スペック全体を few-shot お手本として登録する提案 ---
    cust = official_spec.get("customer_name") or \
        (official_spec.get("title") or {}).get("project_name") or "案件名不明"
    dtype = official_spec.get("drawing_type") or DrawingType.LAYOUT
    example_name = f"{cust} {DrawingType.LABEL.get(dtype, dtype)}"
    diffs.append(DrawingDiffItem(
        diff_type="golden_example",
        target="全体",
        ai_value="",
        official_value=example_name,
        summary=f"正規図面のスペック全体をAIのお手本（few-shot）として登録: {example_name}",
        learnable=True,
        proposed_rule=_make_rule(
            "golden_example",
            {"name": example_name, "spec": official_spec},
            f"お手本として登録: {example_name}",
            "正規図面スペックを few-shot お手本として登録", official_spec),
    ))

    return diffs
