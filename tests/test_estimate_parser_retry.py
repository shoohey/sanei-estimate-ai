"""見積パーサーのAPIリトライ・途中切れ対応のテスト（API不要・スクリプト式）

実行: python3 tests/test_estimate_parser_retry.py

背景（2026-08-07 の本番障害）:
明細行が多い正規見積（原価表）で応答が max_tokens=8192 に達して JSON が途中で切れ、
temperature=0 のため3回のリトライ全てが同じ位置で切れて
「JSONが見つかりません」で差分抽出が失敗した。

カバー範囲:
- stop_reason=max_tokens（途中切れ）検知 → 上限を CEILING に引き上げて再試行 → 成功
- CEILING でも途中切れ → 無駄な再試行をせず「明細が多すぎる」エラーで即失敗
- JSON解析エラー → 従来どおりリトライして2回目で成功
- APIエラー → リトライして2回目で成功
- 全滅時は従来の「3回失敗しました」メッセージ
- 途中切れ応答（閉じフェンス無し・波括弧不均衡）で _extract_json が
  ValueError を出す既存挙動の確認（本障害の再現形）

Claude API はフェイククライアントに差し替えるため API キー不要で実行できる。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import learning.estimate_parser as ep
from extraction.survey_extractor import _extract_json

# --- 外部依存の差し替え（APIキー・待機を無効化） ---
# 差し替え前の実体を保持し、実行後に復元する（pytest の収集順対策）
_REAL_ANTHROPIC = ep.anthropic
_REAL_GET_API_KEY = ep.get_api_key
_REAL_TIME = ep.time

ep.get_api_key = lambda: "test-key"
ep.time = SimpleNamespace(sleep=lambda s: None)


def teardown_module(module=None):
    """pytest がモジュール終了時に呼ぶ復元フック。"""
    ep.anthropic = _REAL_ANTHROPIC
    ep.get_api_key = _REAL_GET_API_KEY
    ep.time = _REAL_TIME


class _FakeAPIError(Exception):
    pass


class _FakeStream:
    """client.messages.stream(...) が返すコンテキストマネージャの代替。"""

    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _FakeMessages:
    def __init__(self, script, calls):
        self._script = script
        self._calls = calls

    def stream(self, **kwargs):
        self._calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeStream(item)


def _patch_api(script):
    """応答シナリオ（メッセージ or 例外のリスト）を仕込み、呼び出し記録リストを返す。"""
    calls = []

    def _factory(api_key=None):
        return SimpleNamespace(messages=_FakeMessages(script, calls))

    ep.anthropic = SimpleNamespace(Anthropic=_factory, APIError=_FakeAPIError)
    return calls


def _msg(text, stop_reason="end_turn", output_tokens=100):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(output_tokens=output_tokens),
    )


_FULL_JSON = (
    '```json\n'
    '{"estimate_id":"49080380-4598775","client_name":"ちふれホールディングス株式会社",'
    '"project_name":"太陽光発電設備設置工事","issue_date":"2026/08/06",'
    '"items":[{"category":"支給品","no":1,"description":"太陽光モジュール",'
    '"remarks":"LR7-72HVH-645M","quantity_value":120,"quantity_unit":"枚",'
    '"unit_price":0,"amount":0}],'
    '"subtotal":100,"discount":null,"total_before_tax":100,"tax":10,'
    '"total_with_tax":110,"warnings":[]}\n'
    '```'
)

# 本番障害の再現形: 閉じフェンスが無く、文字列リテラルの途中で切れている応答
_TRUNCATED_JSON = _FULL_JSON[:180]

_CONTENT = [{"type": "text", "text": "dummy"}]


def test_extract_json_raises_on_truncated_response():
    """途中切れ応答は _extract_json が ValueError（既存挙動＝本障害の直接原因）。"""
    try:
        _extract_json(_TRUNCATED_JSON)
    except ValueError as e:
        assert "JSONが見つかりません" in str(e), f"想定外のメッセージ: {e}"
    else:
        raise AssertionError("途中切れ応答で ValueError が出ませんでした")


def test_truncation_escalates_then_succeeds():
    """途中切れ → 上限を CEILING に引き上げて再試行 → 成功。"""
    calls = _patch_api([
        _msg(_TRUNCATED_JSON, stop_reason="max_tokens"),
        _msg(_FULL_JSON),
    ])
    result = ep._call_claude_with_retry(_CONTENT)
    assert result["estimate_id"] == "49080380-4598775"
    assert len(result["items"]) == 1
    assert len(calls) == 2, f"呼び出し回数: {len(calls)}"
    assert calls[0]["max_tokens"] == ep.MAX_OUTPUT_TOKENS
    assert calls[1]["max_tokens"] == ep.MAX_OUTPUT_TOKENS_CEILING


def test_truncation_at_ceiling_fails_fast():
    """CEILING でも途中切れなら、3回目を浪費せず明確なメッセージで即失敗。"""
    calls = _patch_api([
        _msg(_TRUNCATED_JSON, stop_reason="max_tokens"),
        _msg(_TRUNCATED_JSON, stop_reason="max_tokens"),
        _msg(_FULL_JSON),  # ここまで到達しないことを確認する番兵
    ])
    try:
        ep._call_claude_with_retry(_CONTENT)
    except RuntimeError as e:
        assert "明細が多すぎる" in str(e), f"想定外のメッセージ: {e}"
    else:
        raise AssertionError("CEILING 途中切れで RuntimeError が出ませんでした")
    assert len(calls) == 2, f"即失敗のはずが {len(calls)} 回呼ばれました"


def test_truncation_on_last_attempt_still_escalates():
    """一時エラー2回の後、最終試行で初めて途中切れ → 引き上げはリトライ予算外なので
    64K での再試行が実行されて成功する（敵対的検証で見つかった経路の回帰テスト）。"""
    calls = _patch_api([
        _FakeAPIError("overloaded"),
        _FakeAPIError("overloaded"),
        _msg(_TRUNCATED_JSON, stop_reason="max_tokens"),  # 試行3で初めて途中切れ
        _msg(_FULL_JSON),  # エスカレーション後の呼び出し（予算外の4回目）
    ])
    result = ep._call_claude_with_retry(_CONTENT)
    assert result["estimate_id"] == "49080380-4598775"
    assert len(calls) == 4, f"呼び出し回数: {len(calls)}"
    assert calls[3]["max_tokens"] == ep.MAX_OUTPUT_TOKENS_CEILING


def test_json_error_then_success():
    """JSON無し応答は従来どおりリトライし、2回目の正常応答で成功。"""
    calls = _patch_api([
        _msg("すみません、この画像からは見積書を読み取れませんでした。"),
        _msg(_FULL_JSON),
    ])
    result = ep._call_claude_with_retry(_CONTENT)
    assert result["client_name"] == "ちふれホールディングス株式会社"
    assert len(calls) == 2
    # JSONエラーのリトライでは上限は引き上げない
    assert calls[1]["max_tokens"] == ep.MAX_OUTPUT_TOKENS


def test_api_error_then_success():
    """APIエラーはリトライし、2回目の正常応答で成功。"""
    calls = _patch_api([
        _FakeAPIError("overloaded"),
        _msg(_FULL_JSON),
    ])
    result = ep._call_claude_with_retry(_CONTENT)
    assert result["total_with_tax"] == 110
    assert len(calls) == 2


def test_all_attempts_fail_raises():
    """全リトライ失敗時は従来の「3回失敗しました」メッセージ。"""
    calls = _patch_api([
        _msg("JSONなし1"),
        _msg("JSONなし2"),
        _msg("JSONなし3"),
    ])
    try:
        ep._call_claude_with_retry(_CONTENT)
    except RuntimeError as e:
        assert f"{ep.MAX_RETRIES}回失敗" in str(e), f"想定外のメッセージ: {e}"
    else:
        raise AssertionError("全滅時に RuntimeError が出ませんでした")
    assert len(calls) == ep.MAX_RETRIES


def main():
    tests = [
        test_extract_json_raises_on_truncated_response,
        test_truncation_escalates_then_succeeds,
        test_truncation_at_ceiling_fails_fast,
        test_truncation_on_last_attempt_still_escalates,
        test_json_error_then_success,
        test_api_error_then_success,
        test_all_attempts_fail_raises,
    ]
    print("=== 見積パーサー リトライ・途中切れ対応テスト（API不要） ===")
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
