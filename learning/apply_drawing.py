"""学習済み図面ルールの DraftingSpec への適用 + few-shot お手本の提供

store（knowledge/learned_drawing_rules.json）の有効ルールを製図直前の
DraftingSpec に反映する（docs/LEARNING_LOOP_DESIGN.md §13）。

設計原則「手動編集を尊重」:
  値が既定値のままの場合のみ上書きする。
  既定値 = gap_long_mm=25 / gap_short_mm=10 / margin_mm=500 / orientation=AUTO
  （drafting/models.py の PanelSpec / RoofFace の初期値と対）。
  ユーザーが確認フォームで変えた値（既定値以外）には触れない。

呼び出し側（drafting/app_pages._generate_drawing）は try/except で保護されるが、
本モジュール内でもストア読込失敗を握り、製図フローを止めない。
"""
import logging

from drafting.models import Orientation, RoofType
from learning import store

logger = logging.getLogger(__name__)

# 既定値（drafting/models.py の dataclass 初期値と一致させること）
DEFAULT_GAP_LONG_MM = 25.0
DEFAULT_GAP_SHORT_MM = 10.0
DEFAULT_MARGIN_MM = 500.0

_EPS = 1e-6


def _is_default(value, default) -> bool:
    """数値が既定値のままか（＝手動編集されていないか）を判定する。"""
    try:
        return abs(float(value) - float(default)) < _EPS
    except (TypeError, ValueError):
        return False


def _differs(a, b) -> bool:
    """2値が実質的に異なるか（同値なら適用・表示をスキップ）。"""
    try:
        return abs(float(a) - float(b)) > _EPS
    except (TypeError, ValueError):
        return False


def _fmt_mm(v) -> str:
    """mm 値の表示用文字列（整数なら小数点を出さない）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _roof_label(roof_type: str) -> str:
    """roof_type の日本語ラベル（"*" は全屋根共通）。"""
    if not roof_type or roof_type == "*":
        return "全屋根"
    return RoofType.LABEL.get(roof_type, roof_type)


def apply_learned_drawing_rules(spec):
    """有効な学習済み図面ルールを spec に適用する（破壊的変更OK）。

    呼び出し側は spec_from_dict 直後（place_panels 前）の spec を渡す前提。

    Args:
        spec: DraftingSpec。

    Returns:
        (spec, 適用内容の日本語説明リスト)
        例: ["折板屋根のマージン 500→300mm（学習値）"]
    """
    messages: list = []
    try:
        rules = store.enabled_rules("drawing")
    except Exception as e:
        logger.warning("学習済み図面ルールの読込に失敗（適用をスキップ）: %s", e)
        return spec, messages

    faces = [f for f in (getattr(spec, "roof_faces", None) or []) if f is not None]
    face_types = {getattr(f, "roof_type", "") for f in faces}

    for rule in rules:
        try:
            kind = rule.get("kind", "")
            payload = rule.get("payload", {}) or {}
            roof_type = payload.get("roof_type", "*") or "*"

            if kind == "gap_override":
                # gap はパネル仕様（spec全体で1つ）。roof_type 一致面がある場合のみ適用。
                if roof_type != "*" and roof_type not in face_types:
                    continue
                parts = []
                gl = payload.get("gap_long_mm")
                if gl is not None and _is_default(spec.panel.gap_long_mm, DEFAULT_GAP_LONG_MM) \
                        and _differs(gl, spec.panel.gap_long_mm):
                    parts.append(f"縦 {_fmt_mm(spec.panel.gap_long_mm)}→{_fmt_mm(gl)}mm")
                    spec.panel.gap_long_mm = float(gl)
                gs = payload.get("gap_short_mm")
                if gs is not None and _is_default(spec.panel.gap_short_mm, DEFAULT_GAP_SHORT_MM) \
                        and _differs(gs, spec.panel.gap_short_mm):
                    parts.append(f"横 {_fmt_mm(spec.panel.gap_short_mm)}→{_fmt_mm(gs)}mm")
                    spec.panel.gap_short_mm = float(gs)
                if parts:
                    messages.append(
                        f"{_roof_label(roof_type)}のパネル間隔 {'・'.join(parts)}（学習値）")

            elif kind == "margin_override":
                new_margin = payload.get("margin_mm")
                if new_margin is None:
                    continue
                applied = False
                for face in faces:
                    if roof_type != "*" and getattr(face, "roof_type", "") != roof_type:
                        continue
                    if _is_default(face.margin_mm, DEFAULT_MARGIN_MM) \
                            and _differs(new_margin, face.margin_mm):
                        face.margin_mm = float(new_margin)
                        applied = True
                if applied:
                    messages.append(
                        f"{_roof_label(roof_type)}のマージン "
                        f"{_fmt_mm(DEFAULT_MARGIN_MM)}→{_fmt_mm(new_margin)}mm（学習値）")

            elif kind == "orientation_preference":
                new_ori = payload.get("orientation", "")
                if new_ori not in (Orientation.PORTRAIT, Orientation.LANDSCAPE):
                    continue
                applied = False
                for face in faces:
                    if roof_type != "*" and getattr(face, "roof_type", "") != roof_type:
                        continue
                    if face.orientation == Orientation.AUTO:
                        face.orientation = new_ori
                        applied = True
                if applied:
                    messages.append(
                        f"{_roof_label(roof_type)}のパネル向き "
                        f"自動→{Orientation.LABEL.get(new_ori, new_ori)}（学習値）")

            # golden_example は spec には適用しない（spec_extractor の few-shot 用）
        except Exception as e:
            # 1ルールの不備で他ルールの適用・製図を止めない
            logger.warning("学習ルールの適用に失敗（id=%s）: %s", rule.get("id", "?"), e)
            continue

    return spec, messages


def learned_golden_examples(limit: int = 2) -> list:
    """有効な golden_example ルールの payload（{name, spec}）を新しい順に返す。

    spec_extractor.build_user_prompt の few-shot 注入用。プロンプト肥大を防ぐため
    呼び出し側は limit=2 を既定とする。ストア読込失敗時は []（本体を止めない）。
    """
    try:
        rules = [r for r in store.enabled_rules("drawing")
                 if r.get("kind") == "golden_example"]
    except Exception as e:
        logger.warning("学習済みお手本の読込に失敗: %s", e)
        return []

    limit = max(0, int(limit))
    if limit == 0:
        return []

    # 追加順（末尾が最新）を新しい順に。learned_at があればそれを最優先。
    rules.reverse()
    rules.sort(key=lambda r: (r.get("evidence") or {}).get("learned_at", ""), reverse=True)

    out = []
    for r in rules:
        payload = r.get("payload", {}) or {}
        spec = payload.get("spec")
        if isinstance(spec, dict):
            out.append({"name": payload.get("name", ""), "spec": spec})
        if len(out) >= limit:
            break
    return out
