"""PDF読み取り精度評価CLI。

入力済みExcelを正解(ground truth)、現調シートPDFをLLM抽出結果として
比較し、フィールド単位の正解率を計算する。

使い方:
    python3 tests/eval_accuracy.py             # 正解データ表示のみ
    python3 tests/eval_accuracy.py --api       # 実APIで精度評価
    python3 tests/eval_accuracy.py --case 三精産業
    python3 tests/eval_accuracy.py --output report.json
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any, Optional

# プロジェクトルートをパスに追加
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models.survey_data import SurveyData  # noqa: E402
from tests.ground_truth import (  # noqa: E402
    discover_ground_truth_dir,
    find_pdf_for_xlsx,
    load_ground_truth,
)


# ----------------------------------------------------------------------
# 比較ロジック
# ----------------------------------------------------------------------

# 重要フィールド（太字で強調表示するキー）
IMPORTANT_FIELDS = {
    "project.project_name",
    "project.address",
    "equipment.module_output_w",
    "equipment.planned_panels",
    "equipment.pv_capacity_kw",
}

# 比較対象フィールドの定義: (キー, 型タグ)
COMPARE_FIELDS: list[tuple[str, str]] = [
    ("project.project_name", "str"),
    ("project.address", "str"),
    ("project.survey_date", "str"),
    ("project.weather", "str"),
    ("project.surveyor", "str"),
    ("equipment.module_maker", "str"),
    ("equipment.module_model", "str"),
    ("equipment.module_output_w", "num"),
    ("equipment.planned_panels", "num"),
    ("equipment.pv_capacity_kw", "num"),
    ("equipment.design_status", "enum"),
    ("high_voltage.building_drawing", "bool"),
    ("high_voltage.single_line_diagram", "bool"),
    ("high_voltage.ground_type", "enum"),
    ("high_voltage.c_installation", "enum"),
    ("high_voltage.vt_available", "bool"),
    ("high_voltage.ct_available", "bool"),
    ("high_voltage.relay_space", "bool"),
    ("high_voltage.pcs_space", "bool"),
    ("high_voltage.pcs_location", "enum"),
    ("high_voltage.bt_space", "enum"),
    ("high_voltage.tr_capacity", "str"),
    ("high_voltage.pre_use_self_check", "bool"),
    ("high_voltage.separation_ns_mm", "num"),
    ("high_voltage.separation_ew_mm", "num"),
    ("supplementary.crane_available", "bool"),
    ("supplementary.scaffold_location", "str"),
    ("supplementary.pole_number", "str"),
    ("supplementary.wiring_route", "str"),
    ("supplementary.cubicle_location", "bool"),
    ("supplementary.bt_location", "str"),
]


def _get_field(obj: Any, dotted: str) -> Any:
    """'project.project_name' のようなドット記法で属性を取得."""
    cur = obj
    for part in dotted.split("."):
        if cur is None:
            return None
        cur = getattr(cur, part, None)
    return cur


def _norm_str(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("　", " ").replace(" ", " ").replace("\xa0", " ")
    return s.strip().lower()


def _enum_value(v: Any) -> str:
    if v is None:
        return ""
    if hasattr(v, "value"):
        return str(v.value)
    return str(v)


def _compare_value(predicted: Any, expected: Any, kind: str) -> tuple[bool, bool]:
    """完全一致と部分一致の (correct, partial) を返す."""
    if kind == "num":
        try:
            p = float(predicted) if predicted is not None else 0.0
            e = float(expected) if expected is not None else 0.0
        except (TypeError, ValueError):
            return False, False
        if e == 0:
            return p == 0, False
        # ±2%以内
        diff_ratio = abs(p - e) / abs(e)
        return diff_ratio <= 0.02, diff_ratio <= 0.05
    if kind == "bool":
        return bool(predicted) == bool(expected), False
    if kind == "enum":
        return _enum_value(predicted) == _enum_value(expected), False
    # str
    p = _norm_str(predicted)
    e = _norm_str(expected)
    if p == e:
        return True, True
    if e and (e in p or p in e):
        return False, True
    return False, False


def compare_survey_data(predicted: SurveyData, ground_truth: SurveyData) -> dict:
    """フィールド単位の比較結果を返す."""
    field_results: dict[str, dict] = {}
    correct = 0
    partial = 0
    total = 0
    for key, kind in COMPARE_FIELDS:
        p = _get_field(predicted, key)
        e = _get_field(ground_truth, key)
        ok, partial_ok = _compare_value(p, e, kind)
        field_results[key] = {
            "correct": ok,
            "partial": partial_ok and not ok,
            "predicted": _enum_value(p) if kind == "enum" else (p if p is not None else ""),
            "expected": _enum_value(e) if kind == "enum" else (e if e is not None else ""),
            "kind": kind,
            "important": key in IMPORTANT_FIELDS,
        }
        total += 1
        if ok:
            correct += 1
        elif partial_ok:
            partial += 1
    return {
        "correct": correct,
        "partial": partial,
        "total": total,
        "accuracy": correct / total if total else 0.0,
        "field_results": field_results,
    }


# ----------------------------------------------------------------------
# レポート出力
# ----------------------------------------------------------------------

# ANSI 太字/色
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"


def _mark(ok: bool, partial: bool) -> str:
    if ok:
        return f"{GREEN}OK{RESET}"
    if partial:
        return f"{YELLOW}~~{RESET}"
    return f"{RED}NG{RESET}"


def _truncate(s: Any, n: int = 40) -> str:
    text = str(s) if s is not None else ""
    if len(text) > n:
        return text[: n - 1] + "…"
    return text


def print_accuracy_report(results: list[dict]) -> None:
    """精度レポートを標準出力に印字."""
    if not results:
        print("評価対象なし")
        return

    # ケース別サマリ
    print()
    print("=" * 78)
    print(f"{BOLD}精度評価レポート{RESET}  ({len(results)}件)")
    print("=" * 78)

    field_correct_count: dict[str, int] = {}
    field_total_count: dict[str, int] = {}
    overall_correct = 0
    overall_partial = 0
    overall_total = 0

    for r in results:
        case = r.get("case_name", "?")
        cmp = r.get("compare", {})
        if not cmp:
            print(f"\n--- {case} (SKIP: {r.get('error', '比較不可')}) ---")
            continue

        correct = cmp["correct"]
        partial = cmp["partial"]
        total = cmp["total"]
        acc = correct / total if total else 0.0
        overall_correct += correct
        overall_partial += partial
        overall_total += total

        print(f"\n--- {BOLD}{case}{RESET}  ({correct}/{total} = {acc*100:.1f}%) ---")
        for key, fr in cmp["field_results"].items():
            field_total_count[key] = field_total_count.get(key, 0) + 1
            if fr["correct"]:
                field_correct_count[key] = field_correct_count.get(key, 0) + 1
            label = f"{BOLD}{key}{RESET}" if fr["important"] else key
            mark = _mark(fr["correct"], fr.get("partial", False))
            pred = _truncate(fr["predicted"])
            exp = _truncate(fr["expected"])
            print(f"  {mark}  {label:<42}  pred='{pred}'  exp='{exp}'")

    # 全体精度
    print()
    print("=" * 78)
    if overall_total > 0:
        print(f"{BOLD}全体精度{RESET}: {overall_correct}/{overall_total} = "
              f"{overall_correct/overall_total*100:.2f}%  "
              f"(部分一致 {overall_partial}件)")
    else:
        print("全体精度: 評価対象なし")
    print("=" * 78)

    # フィールド別ランキング
    if field_total_count:
        print(f"\n{BOLD}フィールド別精度ランキング{RESET}")
        ranking = []
        for key, total in field_total_count.items():
            correct = field_correct_count.get(key, 0)
            ranking.append((key, correct, total, correct / total))
        ranking.sort(key=lambda x: x[3])
        # 弱い順に表示
        print(f"{DIM}(精度の低い順){RESET}")
        for key, correct, total, ratio in ranking:
            tag = f" {BOLD}*{RESET}" if key in IMPORTANT_FIELDS else "  "
            print(f" {tag} {key:<42}  {correct}/{total}  {ratio*100:5.1f}%")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def _filter_cases(
    cases: list[tuple[str, str]],
    case_filter: Optional[str],
) -> list[tuple[str, str]]:
    if not case_filter:
        return cases
    needle = case_filter.lower()
    return [(n, p) for n, p in cases if needle in n.lower()]


def _print_ground_truth_only(cases: list[tuple[str, str]]) -> list[dict]:
    """正解データのみ表示し、結果用の辞書も返す."""
    out = []
    print(f"\n{BOLD}正解データ一覧{RESET} ({len(cases)}件)")
    print("=" * 78)
    for case_name, xlsx in cases:
        try:
            gt = load_ground_truth(xlsx)
        except Exception as e:
            print(f"\n--- {case_name} ---  ERROR: {e}")
            out.append({"case_name": case_name, "error": str(e)})
            continue
        print(f"\n--- {BOLD}{case_name}{RESET} ---")
        print(f"  案件名         : {gt.project.project_name}")
        print(f"  所在地         : {gt.project.address}")
        print(f"  モジュール出力 : {gt.equipment.module_output_w} W")
        print(f"  設置枚数       : {gt.equipment.planned_panels} 枚")
        print(f"  PV容量         : {gt.equipment.pv_capacity_kw} kW")
        pdfs = find_pdf_for_xlsx(xlsx)
        print(f"  関連PDF        : {len(pdfs)}件")
        out.append({
            "case_name": case_name,
            "xlsx": xlsx,
            "pdfs": pdfs,
            "ground_truth": gt.model_dump(mode="json"),
        })
    return out


def _run_with_api(cases: list[tuple[str, str]]) -> list[dict]:
    """各ケースをLLM抽出して比較."""
    # 遅延import（API無し時にAPIキーチェックを避ける）
    from extraction.survey_extractor import extract_survey_data_multi  # noqa

    results = []
    for case_name, xlsx in cases:
        print(f"\n>>> [{case_name}] 抽出中...")
        try:
            gt = load_ground_truth(xlsx)
        except Exception as e:
            print(f"  ground_truth ERROR: {e}")
            results.append({"case_name": case_name, "error": f"GT: {e}"})
            continue
        pdfs = find_pdf_for_xlsx(xlsx)
        if not pdfs:
            print("  PDFが見つかりません。スキップ")
            results.append({"case_name": case_name, "error": "no PDF"})
            continue
        try:
            predicted = extract_survey_data_multi(pdfs)
        except Exception as e:
            print(f"  抽出ERROR: {e}")
            results.append({"case_name": case_name, "error": f"extract: {e}"})
            continue
        cmp = compare_survey_data(predicted, gt)
        results.append({
            "case_name": case_name,
            "xlsx": xlsx,
            "pdfs": pdfs,
            "ground_truth": gt.model_dump(mode="json"),
            "predicted": predicted.model_dump(mode="json"),
            "compare": cmp,
        })
        print(f"  {cmp['correct']}/{cmp['total']} = {cmp['accuracy']*100:.1f}%")
    return results


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="現調シートPDF読み取り精度評価")
    parser.add_argument("--api", action="store_true", help="実APIで抽出して比較する")
    parser.add_argument("--case", type=str, default=None, help="案件名でフィルタ(部分一致)")
    parser.add_argument("--output", type=str, default=None, help="JSON出力先パス")
    parser.add_argument(
        "--base-dir",
        type=str,
        default="見積AI入力資料/入力済み",
        help="入力済みディレクトリ",
    )
    args = parser.parse_args(argv)

    cases = discover_ground_truth_dir(args.base_dir)
    cases = _filter_cases(cases, args.case)
    if not cases:
        print("対象ケースが見つかりません")
        return 1

    if args.api:
        results = _run_with_api(cases)
        print_accuracy_report(results)
    else:
        results = _print_ground_truth_only(cases)
        print()
        print("(--api オプションでLLM抽出と比較を実行できます)")

    if args.output:
        out_path = Path(args.output)
        # SurveyData は model_dump 済み、辞書化可能なものだけ書き出す
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nJSON出力: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
