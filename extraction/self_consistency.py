"""Self-Consistency による Claude Vision API の高精度抽出

手書き現調シートの読み取り精度を上げるため、複数回（temperature を変えて）
Claude Vision API を呼び出し、各フィールドについて多数決またはconfidence統合で
ベストアンサーを選ぶ。これにより手書き文字の誤読をハルシネーションとして検出・除去できる。

使い方:

    def call_api(content, temperature):
        # ... claude api ...
        return parsed_dict

    merged, confs = extract_with_self_consistency(call_api, content, n_samples=3, temperatures=[0.0, 0.2, 0.4])
    # merged: 多数決で選ばれたフィールド値の dict（既存スキーマと同じ構造）
    # confs:  {"project.project_name": "high", "equipment.module_output_w": "medium", ...}

設計方針:
- 数値フィールドは ±1% 以内の値で一致する場合は同じとみなす（660 と 661 は同じ）
- 文字列は normalize（trim, 全角/半角, 大文字/小文字）してから比較
- 全結果一致 → high、過半数一致 → medium、バラバラ → low（最頻値を採用）
- bool は単純多数決
- dict（ネスト構造）は再帰的にマージ
"""
import logging
import re
import unicodedata
from collections import Counter
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# デフォルトの temperature リスト（決定論ベース1回 + 多様性2回）
DEFAULT_TEMPERATURES = [0.0, 0.2, 0.3]

# 数値フィールドの一致判定許容誤差（±1%）
NUMERIC_TOLERANCE_RATIO = 0.01


def extract_with_self_consistency(
    api_call_func: Callable[[list, float], dict],
    content: list,
    n_samples: int = 3,
    temperatures: Optional[list[float]] = None,
) -> tuple[dict, dict]:
    """複数回 API を呼び出し、多数決で最もありそうな抽出結果を返す。

    Args:
        api_call_func: (content, temperature) -> dict のコールバック。
                       内部で Claude Vision API を呼び出し、JSON パース済みの dict を返す。
        content: API に渡すコンテンツ（画像 + テキスト）
        n_samples: サンプリング回数（デフォルト 3）
        temperatures: 各サンプルの temperature。None の場合は DEFAULT_TEMPERATURES を使用。
                      n_samples より長い場合は先頭 n_samples 個のみを使用。
                      短い場合は最後の値を繰り返し使用。

    Returns:
        (merged_result, field_confidences) のタプル
        - merged_result: 各フィールドの最頻値で構成された dict
        - field_confidences: {"パス": "high|medium|low"} の dict
    """
    if n_samples < 1:
        raise ValueError(f"n_samples は 1 以上である必要があります（指定値: {n_samples}）")

    # temperatures の調整
    if temperatures is None:
        temperatures = DEFAULT_TEMPERATURES
    if len(temperatures) >= n_samples:
        temps = temperatures[:n_samples]
    else:
        # 不足分は最後の値で埋める
        temps = list(temperatures) + [temperatures[-1]] * (n_samples - len(temperatures))

    results: list[dict] = []
    for i, temp in enumerate(temps, start=1):
        try:
            result = api_call_func(content, temp)
            if not isinstance(result, dict):
                logger.warning(
                    f"Self-Consistency サンプル {i}/{n_samples} (temp={temp}): "
                    f"dict 以外が返されました ({type(result).__name__})。スキップします。"
                )
                continue
            results.append(result)
            logger.debug(
                f"Self-Consistency サンプル {i}/{n_samples} (temp={temp}): "
                f"成功（トップレベルキー数={len(result)}）"
            )
        except Exception as e:
            logger.warning(
                f"Self-Consistency サンプル {i}/{n_samples} (temp={temp}): "
                f"API呼び出し失敗: {e}"
            )

    if not results:
        raise RuntimeError(
            "Self-Consistency: 全サンプルが失敗しました。API呼び出しを確認してください。"
        )

    if len(results) == 1:
        # 1サンプルしか得られなかった場合は信頼度を全体的に下げて返す
        logger.warning("Self-Consistency: 有効サンプルが1件のみ。多数決ができないため medium で返します。")
        merged = results[0]
        confs = {p: "medium" for p in _collect_all_paths(merged)}
        return merged, confs

    return merge_extractions(results)


def merge_extractions(results: list[dict]) -> tuple[dict, dict]:
    """複数の抽出結果をマージし、各フィールドの最頻値と信頼度を返す。

    Args:
        results: API から得られた dict のリスト

    Returns:
        (merged, confidences) のタプル
        - merged: 各フィールドの最頻値で構成された dict（ネスト構造を保持）
        - confidences: {"パス": "high|medium|low"} の dict
    """
    if not results:
        return {}, {}

    # 全結果からフィールドパスを収集
    all_paths: set[str] = set()
    for r in results:
        for p in _collect_all_paths(r):
            all_paths.add(p)

    merged: dict = {}
    confidences: dict = {}

    for path in sorted(all_paths):
        # 各サンプルから値を取得（無いサンプルはスキップ）
        values = []
        for r in results:
            try:
                v = _get_nested(r, path)
                # MISSING マーカーで無い場合のみ含める
                if v is _MISSING:
                    continue
                values.append(v)
            except (KeyError, TypeError):
                continue

        if not values:
            continue

        best_value, conf = vote_field(values, field_path=path)
        _set_nested(merged, path, best_value)
        confidences[path] = conf

    return merged, confidences


def vote_field(values: list, field_path: str = "") -> tuple[Any, str]:
    """複数の値から最頻値と信頼度レベルを決定する。

    Args:
        values: 同じフィールドに対する複数サンプルの値リスト
        field_path: フィールドパス（ログ出力用）

    Returns:
        (best_value, confidence_level) のタプル
        - best_value: 最頻値
        - confidence_level: "high"（全一致）, "medium"（過半数一致）, "low"（バラバラ）
    """
    if not values:
        return None, "low"

    if len(values) == 1:
        # 1サンプルのみ → medium
        return values[0], "medium"

    # list 型は要素のタプル化で比較
    # dict はパス展開済みのため、ここに来ない想定だが念のため
    # 文字列フィールドは空文字列を投票から除外
    is_string_field = all(isinstance(v, str) or v is None for v in values)
    if is_string_field:
        non_empty = [v for v in values if v and (not isinstance(v, str) or v.strip())]
        if not non_empty:
            # 全部空 → 空文字列を返して low
            return "", "low"
        voting_values = non_empty
    else:
        voting_values = values

    # 各値を「正規化キー」に変換してカウント
    # 元の値も保持して、勝った正規化キーから代表値を返す
    normalized_groups: dict[str, list] = {}
    for v in voting_values:
        key = _normalize_for_compare(v)
        normalized_groups.setdefault(key, []).append(v)

    # 数値の場合は ±1% 以内のグループを統合
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in voting_values):
        normalized_groups = _merge_close_numeric_groups(normalized_groups)

    # 最大グループを選択
    best_key = max(normalized_groups.keys(), key=lambda k: len(normalized_groups[k]))
    best_group = normalized_groups[best_key]
    # 代表値: グループ内の最頻値（同数なら先頭）
    rep_counter = Counter(_value_hash_key(v) for v in best_group)
    most_common_hash, _ = rep_counter.most_common(1)[0]
    best_value = next(v for v in best_group if _value_hash_key(v) == most_common_hash)

    # 信頼度判定
    n_total = len(voting_values)
    n_winning = len(best_group)
    if n_winning == n_total:
        conf = "high"
    elif n_winning * 2 > n_total:
        # 過半数（半分超）
        conf = "medium"
    else:
        conf = "low"
        if field_path:
            logger.debug(
                f"Self-Consistency: フィールド「{field_path}」で意見が割れました "
                f"(勝者={n_winning}/{n_total}, グループ数={len(normalized_groups)})。"
                f"最頻値={best_value!r}"
            )

    return best_value, conf


def _merge_close_numeric_groups(groups: dict[str, list]) -> dict[str, list]:
    """数値グループのうち、±NUMERIC_TOLERANCE_RATIO 以内のものを統合する。

    例: {"660": [660], "661": [661]} → {"660": [660, 661]}（差が1%以内）
    """
    keys = list(groups.keys())
    # 数値変換
    numeric_keys: list[tuple[str, float]] = []
    for k in keys:
        try:
            numeric_keys.append((k, float(k)))
        except (ValueError, TypeError):
            # 変換できないキーはそのまま
            return groups

    # ソートして近いもの同士をマージ
    numeric_keys.sort(key=lambda x: x[1])
    merged: dict[str, list] = {}
    used: set[str] = set()
    for i, (k1, v1) in enumerate(numeric_keys):
        if k1 in used:
            continue
        merged[k1] = list(groups[k1])
        used.add(k1)
        for j in range(i + 1, len(numeric_keys)):
            k2, v2 = numeric_keys[j]
            if k2 in used:
                continue
            # ±1% 以内かを判定（基準値0の場合は絶対誤差0.01以内）
            base = max(abs(v1), abs(v2), 1e-9)
            if abs(v1 - v2) / base <= NUMERIC_TOLERANCE_RATIO:
                merged[k1].extend(groups[k2])
                used.add(k2)
    return merged


def _value_hash_key(v: Any) -> str:
    """値のハッシュキーを返す（同値判定用）。dict/list は repr で代用。"""
    try:
        if isinstance(v, (dict, list)):
            return repr(v)
        return f"{type(v).__name__}:{v}"
    except Exception:
        return repr(v)


def _normalize_for_compare(val: Any) -> str:
    """比較のための正規化を行う。

    - None → "<NONE>"
    - bool → "true"/"false"
    - int/float → 適切に丸めた文字列
    - str → trim + 全角→半角 + 大文字→小文字 + 連続空白を1つに
    - その他 → str(val)
    """
    if val is None:
        return "<NONE>"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        # 浮動小数点誤差を吸収するため小数第4位で丸め
        # ただし整数値の場合は小数を付けない
        if val == int(val):
            return str(int(val))
        return f"{val:.4f}".rstrip("0").rstrip(".")
    if isinstance(val, str):
        # NFKC で全角→半角統一、trim、小文字化、連続空白圧縮
        s = unicodedata.normalize("NFKC", val)
        s = s.strip().lower()
        s = re.sub(r"\s+", " ", s)
        return s
    if isinstance(val, (list, tuple)):
        return "[" + ",".join(_normalize_for_compare(x) for x in val) + "]"
    if isinstance(val, dict):
        items = sorted((k, _normalize_for_compare(v)) for k, v in val.items())
        return "{" + ",".join(f"{k}:{v}" for k, v in items) + "}"
    return str(val)


# パスがそのキーで存在しないことを示す内部マーカー
_MISSING = object()


def _get_nested(d: dict, path: str) -> Any:
    """ドット区切りパスでネスト辞書から値を取得する。

    存在しない場合は _MISSING を返す（None と区別するため）。
    """
    parts = path.split(".")
    current: Any = d
    for p in parts:
        if not isinstance(current, dict):
            return _MISSING
        if p not in current:
            return _MISSING
        current = current[p]
    return current


def _set_nested(d: dict, path: str, value: Any) -> None:
    """ドット区切りパスでネスト辞書に値を設定する。

    途中の dict が無い場合は自動で作成する。
    """
    parts = path.split(".")
    current = d
    for p in parts[:-1]:
        if p not in current or not isinstance(current[p], dict):
            current[p] = {}
        current = current[p]
    current[parts[-1]] = value


def _collect_all_paths(d: dict, prefix: str = "") -> list[str]:
    """dict 内の全フィールドパスを列挙する（ネストはドット区切り）。

    リスト・スカラー値の葉ノードのパスのみを返す（中間 dict は含めない）。
    例: {"a": {"b": 1, "c": [1,2]}} → ["a.b", "a.c"]
    """
    paths: list[str] = []
    if not isinstance(d, dict):
        return paths
    for k, v in d.items():
        full_path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            sub = _collect_all_paths(v, prefix=full_path)
            if sub:
                paths.extend(sub)
            else:
                # 空dictは葉として扱う
                paths.append(full_path)
        else:
            paths.append(full_path)
    return paths


# ----------------------------------------------------------------------
# 動作テスト
# ----------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    # モック API レスポンス（3サンプル）
    mock_responses = [
        {
            "project": {
                "project_name": "テックランド掛川店",
                "address": "静岡県掛川市",
                "postal_code": "436-0001",
            },
            "equipment": {
                "module_output_w": 660,
                "planned_panels": 288,
                "pv_capacity_kw": 190.08,
            },
            "high_voltage": {
                "vt_available": True,
                "ground_type": "C",
            },
        },
        {
            "project": {
                "project_name": "テックランド掛川店",
                "address": "静岡県掛川市",
                "postal_code": "436-0001",
            },
            "equipment": {
                "module_output_w": 661,  # ±1%以内なので660と同じとみなす
                "planned_panels": 288,
                "pv_capacity_kw": 190.08,
            },
            "high_voltage": {
                "vt_available": True,
                "ground_type": "C",
            },
        },
        {
            "project": {
                "project_name": "テックランド掛川店",
                "address": "静岡県掛川市",
                "postal_code": "436-0007",  # 1サンプルだけ違う
            },
            "equipment": {
                "module_output_w": 600,  # 1サンプルだけ違う（誤読想定）
                "planned_panels": 288,
                "pv_capacity_kw": 190.08,
            },
            "high_voltage": {
                "vt_available": False,  # 1サンプルだけ違う
                "ground_type": "D",  # 1サンプルだけ違う
            },
        },
    ]

    print("=" * 60)
    print("Test 1: merge_extractions 単体テスト")
    print("=" * 60)
    merged, confs = merge_extractions(mock_responses)
    print("\n--- Merged Result ---")
    import json
    print(json.dumps(merged, indent=2, ensure_ascii=False))
    print("\n--- Field Confidences ---")
    for path, conf in sorted(confs.items()):
        print(f"  {path}: {conf}")

    # 期待値の検証
    print("\n--- Assertion Checks ---")
    assert merged["project"]["project_name"] == "テックランド掛川店", "project_name 全一致"
    assert confs["project.project_name"] == "high", "project_name 信頼度=high"
    print("  OK: project_name 全一致 → high")

    assert merged["project"]["postal_code"] == "436-0001", "postal_code 過半数"
    assert confs["project.postal_code"] == "medium", "postal_code 信頼度=medium"
    print("  OK: postal_code 過半数 → medium")

    # module_output_w: 660, 661, 600 → 660と661は±1%以内で統合され、勝つ
    assert merged["equipment"]["module_output_w"] in (660, 661), "module_output_w 数値統合"
    assert confs["equipment.module_output_w"] == "medium", "数値統合後の信頼度"
    print(f"  OK: module_output_w={merged['equipment']['module_output_w']} (660/661統合) → medium")

    assert merged["high_voltage"]["vt_available"] is True, "vt_available 多数決"
    assert confs["high_voltage.vt_available"] == "medium", "bool 多数決信頼度"
    print("  OK: vt_available 多数決 (True 2/3) → medium")

    print("\n" + "=" * 60)
    print("Test 2: extract_with_self_consistency（モックコールバック）")
    print("=" * 60)

    call_count = [0]
    def mock_api_call(content, temperature):
        idx = call_count[0]
        call_count[0] += 1
        # temperature をログ
        logger.debug(f"[mock] temperature={temperature}, returning sample {idx}")
        return mock_responses[idx % len(mock_responses)]

    merged2, confs2 = extract_with_self_consistency(
        mock_api_call, content=[], n_samples=3, temperatures=[0.0, 0.2, 0.4]
    )
    print("\n--- Merged Result ---")
    print(json.dumps(merged2, indent=2, ensure_ascii=False))
    print("\n--- Field Confidences ---")
    for path, conf in sorted(confs2.items()):
        print(f"  {path}: {conf}")

    print("\n" + "=" * 60)
    print("Test 3: 全サンプル一致 → high")
    print("=" * 60)
    same_responses = [{"a": {"b": 100}} for _ in range(3)]
    merged3, confs3 = merge_extractions(same_responses)
    assert confs3["a.b"] == "high", "全一致時 high"
    print(f"  OK: a.b={merged3['a']['b']}, conf={confs3['a.b']}")

    print("\n" + "=" * 60)
    print("Test 4: バラバラ → low（最頻値採用）")
    print("=" * 60)
    diverse = [{"x": "A"}, {"x": "B"}, {"x": "C"}]
    merged4, confs4 = merge_extractions(diverse)
    assert confs4["x"] == "low", "バラバラ時 low"
    print(f"  OK: x={merged4['x']!r}, conf={confs4['x']}")

    print("\n" + "=" * 60)
    print("Test 5: 文字列正規化テスト（全角/半角・大文字/小文字）")
    print("=" * 60)
    normalize_test = [
        {"name": "Canadian Solar"},
        {"name": "canadian solar"},  # 大文字小文字違い
        {"name": "Ｃａｎａｄｉａｎ　Ｓｏｌａｒ"},  # 全角
    ]
    merged5, confs5 = merge_extractions(normalize_test)
    assert confs5["name"] == "high", "正規化後一致"
    print(f"  OK: name={merged5['name']!r} (正規化後3つとも一致) → {confs5['name']}")

    print("\n全テスト合格")
