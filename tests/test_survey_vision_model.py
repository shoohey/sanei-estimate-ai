"""画像読み取りモデル（CLAUDE_VISION_MODEL）切替のテスト（API不要・スクリプト式）

実行: python3 tests/test_survey_vision_model.py

背景（2026-08-10）:
現調シート・図面の読み取り精度向上のため、Vision抽出のみ claude-fable-5 を
試験導入した。Fable 系はAPI仕様が異なる:
- temperature 等のサンプリングパラメータを送ると 400
- thinking 常時ONのため content 先頭が thinking ブロックになり得る
- 組織設定等で利用できない場合がある（400/403/404）→ CLAUDE_MODEL に自動退避

カバー範囲:
- Fable への送信に temperature が含まれず max_tokens=16000 であること
- 通常モデル（sonnet）への送信は従来どおり temperature + max_tokens=8192
- thinking ブロックが先頭でもテキストブロックからJSONを取れること
- BadRequest / refusal 時に CLAUDE_MODEL へフォールバックすること
- レート制限（RateLimitError相当）はフォールバックせず上位リトライに任せること
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extraction.survey_extractor as se

# 差し替え前の実体を保持し、実行後に復元する（pytest の収集順が変わった際に
# 後続テストが偽SDKを掴む事故の防止）
_REAL_ANTHROPIC = se.anthropic
_REAL_GET_API_KEY = se.get_api_key
_REAL_VISION_MODEL = se.CLAUDE_VISION_MODEL


def _restore_module():
    se.anthropic = _REAL_ANTHROPIC
    se.get_api_key = _REAL_GET_API_KEY
    se.CLAUDE_VISION_MODEL = _REAL_VISION_MODEL


def teardown_module(module=None):
    """pytest がモジュール終了時に呼ぶ復元フック。"""
    _restore_module()


class _FakeBadRequestError(Exception):
    pass


class _FakeNotFoundError(Exception):
    pass


class _FakePermissionError(Exception):
    pass


class _FakeRateLimitError(Exception):
    pass


class _FakeAPIError(Exception):
    pass


def _patch_api(script):
    """応答シナリオ（メッセージ or 例外のリスト）を仕込み、呼び出し記録を返す。"""
    calls = []

    class _FakeMessages:
        def create(self, **kwargs):
            calls.append(kwargs)
            item = script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    def _factory(api_key=None):
        return SimpleNamespace(messages=_FakeMessages())

    se.anthropic = SimpleNamespace(
        Anthropic=_factory,
        APIError=_FakeAPIError,
        BadRequestError=_FakeBadRequestError,
        NotFoundError=_FakeNotFoundError,
        PermissionDeniedError=_FakePermissionError,
        RateLimitError=_FakeRateLimitError,
    )
    se.get_api_key = lambda: "test-key"
    return calls


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _thinking_block():
    return SimpleNamespace(type="thinking", thinking="")


def _msg(blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


_JSON = '{"site_name": "テスト現場"}'
_CONTENT = [{"type": "text", "text": "dummy"}]


def test_fable_request_omits_temperature():
    """Fable への送信は temperature 無し・max_tokens=16000 であること。"""
    se.CLAUDE_VISION_MODEL = "claude-fable-5"
    calls = _patch_api([_msg([_text_block(_JSON)])])
    result = se._call_claude_api(_CONTENT, attempt=1, temperature=0.0)
    assert result == {"site_name": "テスト現場"}
    assert len(calls) == 1
    assert calls[0]["model"] == "claude-fable-5"
    assert "temperature" not in calls[0], "Fableにtemperatureを送ると400になる"
    assert calls[0]["max_tokens"] == 20000, \
        "thinking込み上限。SDK非ストリーミング上限(約21,333)未満であること"


def test_sonnet_request_keeps_temperature():
    """通常モデルへの送信は従来どおり temperature + max_tokens=8192。"""
    se.CLAUDE_VISION_MODEL = "claude-sonnet-4-6"
    calls = _patch_api([_msg([_text_block(_JSON)])])
    se._call_claude_api(_CONTENT, attempt=1, temperature=0.3)
    assert calls[0]["model"] == "claude-sonnet-4-6"
    assert calls[0]["temperature"] == 0.3
    assert calls[0]["max_tokens"] == 8192


def test_thinking_block_first():
    """content 先頭が thinking ブロックでもテキストからJSONを取れること。"""
    se.CLAUDE_VISION_MODEL = "claude-fable-5"
    calls = _patch_api([_msg([_thinking_block(), _text_block(_JSON)])])
    result = se._call_claude_api(_CONTENT, attempt=1)
    assert result == {"site_name": "テスト現場"}


def test_fallback_on_bad_request():
    """Fable が 400（組織設定等）→ CLAUDE_MODEL にフォールバックすること。"""
    se.CLAUDE_VISION_MODEL = "claude-fable-5"
    calls = _patch_api([
        _FakeBadRequestError("retention config"),
        _msg([_text_block(_JSON)]),
    ])
    result = se._call_claude_api(_CONTENT, attempt=1)
    assert result == {"site_name": "テスト現場"}
    assert len(calls) == 2
    assert calls[0]["model"] == "claude-fable-5"
    assert calls[1]["model"] == se.CLAUDE_MODEL
    assert "temperature" in calls[1], "フォールバック先は通常モデルの仕様で呼ぶ"


def test_fallback_on_refusal():
    """Fable が stop_reason=refusal → CLAUDE_MODEL にフォールバックすること。"""
    se.CLAUDE_VISION_MODEL = "claude-fable-5"
    calls = _patch_api([
        _msg([], stop_reason="refusal"),
        _msg([_text_block(_JSON)]),
    ])
    result = se._call_claude_api(_CONTENT, attempt=1)
    assert result == {"site_name": "テスト現場"}
    assert len(calls) == 2
    assert calls[1]["model"] == se.CLAUDE_MODEL


def test_fallback_on_max_tokens_truncation():
    """Fable が stop_reason=max_tokens（thinking込みで途中切れ）
    → CLAUDE_MODEL にフォールバックすること（2026-08-07障害と同型の防御）。"""
    se.CLAUDE_VISION_MODEL = "claude-fable-5"
    calls = _patch_api([
        _msg([_text_block('{"site_na')], stop_reason="max_tokens"),
        _msg([_text_block(_JSON)]),
    ])
    result = se._call_claude_api(_CONTENT, attempt=1)
    assert result == {"site_name": "テスト現場"}
    assert len(calls) == 2
    assert calls[1]["model"] == se.CLAUDE_MODEL


def test_rate_limit_not_swallowed():
    """レート制限はフォールバックせず、そのまま上位のリトライに伝播すること。"""
    se.CLAUDE_VISION_MODEL = "claude-fable-5"
    calls = _patch_api([
        _FakeRateLimitError("429"),
        _msg([_text_block(_JSON)]),  # ここに到達しない番兵
    ])
    try:
        se._call_claude_api(_CONTENT, attempt=1)
    except _FakeRateLimitError:
        pass
    else:
        raise AssertionError("レート制限がフォールバックに吸収されてはいけない")
    assert len(calls) == 1, "フォールバック呼び出しが発生してはいけない"


def main():
    tests = [
        test_fable_request_omits_temperature,
        test_sonnet_request_keeps_temperature,
        test_thinking_block_first,
        test_fallback_on_bad_request,
        test_fallback_on_refusal,
        test_fallback_on_max_tokens_truncation,
        test_rate_limit_not_swallowed,
    ]
    print("=== 画像読み取りモデル切替テスト（API不要） ===")
    ok = True
    try:
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
    finally:
        _restore_module()
    print("=== 結果:", "全パス" if ok else "一部失敗", "===")
    return ok


if __name__ == "__main__":
    success = main()
    raise SystemExit(0 if success else 1)
