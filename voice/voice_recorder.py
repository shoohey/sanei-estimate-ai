"""
音声録音 → 文字起こしモジュール（Streamlit用）

見積もりに対して音声で変更箇所を伝えるためのコンポーネント。
スマホ/PCの両方で動作する。

使用例:
    import streamlit as st
    from voice.voice_recorder import record_and_transcribe

    text = record_and_transcribe(
        key="estimate_edit_voice",
        help_text="変更内容を話してください",
    )
    if text:
        st.success(f"認識結果: {text}")

優先順位:
    録音方式:
        1) streamlit-mic-recorder (pip install streamlit-mic-recorder)
        2) st.audio_input()  (Streamlit 1.31+)
        3) HTML+JS Web Speech API（ブラウザネイティブ音声認識）
        4) st.text_input() による手入力（最終フォールバック）

    文字起こし:
        1) OpenAI Whisper API (whisper-1)
        2) Web Speech API（クライアント側でテキスト化されたものを受け取る）

注意:
    - iOS Safari は Web Speech API 非対応のため、その場合は手入力フォールバック
    - Anthropic SDK は現状音声入力非対応のため Whisper を採用
"""
from __future__ import annotations

import os
from typing import Optional

import streamlit as st


# ============================================================================
# キー取得ユーティリティ
# ============================================================================

def _get_openai_key() -> Optional[str]:
    """OpenAI APIキーを取得する。

    優先順位:
        1) Streamlit secrets (.streamlit/secrets.toml)
        2) 環境変数 OPENAI_API_KEY

    Returns:
        APIキー文字列 / 取得できなければ None
    """
    # Streamlit secrets を優先
    try:
        if hasattr(st, "secrets"):
            try:
                key = st.secrets.get("OPENAI_API_KEY")  # type: ignore[attr-defined]
                if key:
                    return str(key).strip() or None
            except Exception:
                # secrets.toml が無い、または読み込み不可
                pass
    except Exception:
        pass

    # 環境変数フォールバック
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key.strip() or None
    return None


def _get_anthropic_key() -> Optional[str]:
    """Anthropic APIキーを取得する（フック用。現状未使用）。

    Returns:
        APIキー文字列 / 取得できなければ None
    """
    try:
        if hasattr(st, "secrets"):
            try:
                key = st.secrets.get("ANTHROPIC_API_KEY")  # type: ignore[attr-defined]
                if key:
                    return str(key).strip() or None
            except Exception:
                pass
    except Exception:
        pass

    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip() or None
    return None


def is_whisper_available() -> bool:
    """OpenAI Whisper API が利用可能か（APIキーが設定されているか）チェックする。

    Returns:
        True: APIキーが設定されている
        False: APIキー未設定 or openai パッケージ未インストール
    """
    if _get_openai_key() is None:
        return False
    try:
        import openai  # noqa: F401
        return True
    except ImportError:
        return False


# ============================================================================
# 文字起こし
# ============================================================================

def transcribe_audio(audio_bytes: bytes, language: str = "ja") -> str:
    """音声バイト列を文字起こしする（OpenAI Whisper API）。

    Args:
        audio_bytes: 音声データ（webm/wav/mp3/m4a 等）
        language: ISO-639-1 言語コード（"ja" = 日本語）

    Returns:
        文字起こし結果のテキスト

    Raises:
        RuntimeError: APIキー未設定 or openai パッケージ未インストール
        Exception:    Whisper API 呼び出しの失敗
    """
    api_key = _get_openai_key()
    if not api_key:
        raise RuntimeError(
            "OpenAI APIキーが設定されていません。"
            "環境変数 OPENAI_API_KEY または .streamlit/secrets.toml に設定してください。"
        )

    try:
        import openai
    except ImportError as e:
        raise RuntimeError(
            "openai パッケージがインストールされていません。"
            "`pip install openai` でインストールしてください。"
        ) from e

    client = openai.OpenAI(api_key=api_key)
    resp = client.audio.transcriptions.create(
        model="whisper-1",
        file=("recording.webm", audio_bytes, "audio/webm"),
        language=language,
    )
    return resp.text


# ============================================================================
# Web Speech API 埋込HTML
# ============================================================================

def _render_html_speech_recognition() -> str:
    """Web Speech API（ブラウザネイティブ音声認識）の埋込HTMLを返す。

    Returns:
        HTML文字列。`<textarea>` に認識結果を書き込み、ユーザーが
        その内容をコピー or Streamlit側のテキストエリアに転記する想定。
    """
    return """
<div style="font-family: 'Noto Sans JP', sans-serif; padding: 12px;
            background: #f5f7fa; border: 1px solid #e2e8f0; border-radius: 8px;">
  <div id="speech-status" style="font-size: 13px; color: #4a5568; margin-bottom: 8px;">
    🎤 「録音開始」を押して話してください（Chrome/Edge/Android対応）
  </div>
  <div style="display: flex; gap: 8px; margin-bottom: 8px;">
    <button id="speech-start" type="button"
            style="background:#1e3a5f;color:#fff;border:none;border-radius:6px;
                   padding:8px 16px;cursor:pointer;font-size:14px;">
      🎤 録音開始
    </button>
    <button id="speech-stop" type="button" disabled
            style="background:#c53030;color:#fff;border:none;border-radius:6px;
                   padding:8px 16px;cursor:pointer;font-size:14px;opacity:0.5;">
      ■ 停止
    </button>
    <button id="speech-clear" type="button"
            style="background:#fff;color:#4a5568;border:1px solid #e2e8f0;
                   border-radius:6px;padding:8px 16px;cursor:pointer;font-size:14px;">
      クリア
    </button>
  </div>
  <textarea id="speech-result" rows="4"
            style="width:100%;padding:8px;border:1px solid #e2e8f0;
                   border-radius:6px;font-size:14px;font-family:inherit;resize:vertical;"
            placeholder="ここに認識結果が表示されます。下のテキストエリアにコピーして「確定」してください。"></textarea>
  <div style="font-size: 12px; color: #975a16; margin-top: 6px;">
    ⚠️ 認識完了後、上のテキストをコピーして下のフォームに貼り付け、Enterキーで確定してください。
  </div>
</div>
<script>
(function() {
  const startBtn = document.getElementById('speech-start');
  const stopBtn  = document.getElementById('speech-stop');
  const clearBtn = document.getElementById('speech-clear');
  const result   = document.getElementById('speech-result');
  const status   = document.getElementById('speech-status');

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    status.innerHTML = '⚠️ お使いのブラウザはWeb Speech APIに対応していません。'
                     + '下のテキストエリアに手入力してください。'
                     + '<br>(iOS Safari は非対応。Chrome/Edge/Android Chrome を推奨)';
    startBtn.disabled = true;
    startBtn.style.opacity = 0.5;
    return;
  }

  let recognition = null;
  let finalText = '';

  function start() {
    finalText = result.value || '';
    recognition = new SR();
    recognition.lang = 'ja-JP';
    recognition.continuous = true;
    recognition.interimResults = true;

    recognition.onstart = function() {
      status.textContent = '🔴 録音中... 話し終わったら「停止」を押してください';
      startBtn.disabled = true;
      startBtn.style.opacity = 0.5;
      stopBtn.disabled = false;
      stopBtn.style.opacity = 1.0;
    };

    recognition.onresult = function(event) {
      let interim = '';
      let appended = '';
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript;
        if (event.results[i].isFinal) {
          appended += transcript;
        } else {
          interim += transcript;
        }
      }
      if (appended) {
        finalText += appended;
      }
      result.value = (finalText + interim).trim();
    };

    recognition.onerror = function(event) {
      status.textContent = '❌ エラー: ' + event.error
        + '（マイク権限を許可しているか確認してください）';
      stop();
    };

    recognition.onend = function() {
      status.textContent = '✅ 録音終了。テキストをコピーして下のフォームに貼り付けてください。';
      startBtn.disabled = false;
      startBtn.style.opacity = 1.0;
      stopBtn.disabled = true;
      stopBtn.style.opacity = 0.5;
    };

    try {
      recognition.start();
    } catch (e) {
      status.textContent = '❌ 録音開始に失敗しました: ' + e.message;
    }
  }

  function stop() {
    if (recognition) {
      try { recognition.stop(); } catch (e) {}
      recognition = null;
    }
    startBtn.disabled = false;
    startBtn.style.opacity = 1.0;
    stopBtn.disabled = true;
    stopBtn.style.opacity = 0.5;
  }

  startBtn.addEventListener('click', start);
  stopBtn.addEventListener('click', stop);
  clearBtn.addEventListener('click', function() {
    finalText = '';
    result.value = '';
    status.textContent = '🎤 「録音開始」を押して話してください';
  });
})();
</script>
"""


# ============================================================================
# 録音方式の検出
# ============================================================================

def _try_mic_recorder(key: str) -> Optional[bytes]:
    """streamlit-mic-recorder で音声を取得する。

    Returns:
        音声バイト列 / 録音されていない or パッケージ無し → None
    """
    try:
        from streamlit_mic_recorder import mic_recorder
    except ImportError:
        return None

    try:
        audio = mic_recorder(
            start_prompt="🎤 録音開始",
            stop_prompt="■ 停止",
            just_once=False,
            use_container_width=False,
            format="webm",
            key=f"{key}_mic_recorder",
        )
    except Exception as e:
        st.warning(f"streamlit-mic-recorder の呼び出しに失敗しました: {e}")
        return None

    if audio and isinstance(audio, dict) and audio.get("bytes"):
        return audio["bytes"]
    return None


def _try_audio_input(key: str, help_text: str) -> Optional[bytes]:
    """st.audio_input() で音声を取得する（Streamlit 1.31+）。

    Returns:
        音声バイト列 / 録音されていない or 機能無し → None
    """
    if not hasattr(st, "audio_input"):
        return None

    try:
        audio_file = st.audio_input(help_text, key=f"{key}_audio_input")
    except Exception as e:
        st.warning(f"st.audio_input の呼び出しに失敗しました: {e}")
        return None

    if audio_file is None:
        return None

    try:
        return audio_file.getvalue()
    except Exception:
        try:
            return audio_file.read()
        except Exception:
            return None


# ============================================================================
# 公開メイン関数
# ============================================================================

def record_and_transcribe(
    key: str = "voice_input",
    help_text: str = "🎤 マイクボタンを押して話してください",
) -> Optional[str]:
    """Streamlit上で音声録音→文字起こしを行い、テキストを返す。

    内部で st.session_state を使い、再実行をまたいで結果を保持する。

    Args:
        key:        st.session_state のキープレフィクス（複数配置時はユニークに）
        help_text:  録音ボタンに添えるヘルプ文言

    Returns:
        文字起こし結果のテキスト。録音されていない場合は None。
    """
    state_key = f"{key}_transcribed_text"
    if state_key not in st.session_state:
        st.session_state[state_key] = None

    whisper_ok = is_whisper_available()

    # ----- 録音UI（Whisperが使える場合のみ意味がある） -----
    if whisper_ok:
        audio_bytes: Optional[bytes] = None

        # 1) streamlit-mic-recorder
        audio_bytes = _try_mic_recorder(key)

        # 2) st.audio_input フォールバック
        if audio_bytes is None:
            audio_bytes = _try_audio_input(key, help_text)

        if audio_bytes is None:
            # どちらも使えない → Web Speech API へフォールバック
            st.info(
                "💡 録音コンポーネントが利用できないため、ブラウザの音声認識を使用します。"
            )
            return _fallback_web_speech(key, help_text)

        # 録音されたら文字起こし
        if audio_bytes:
            with st.spinner("🎙️ 文字起こし中..."):
                try:
                    text = transcribe_audio(audio_bytes, language="ja")
                    st.session_state[state_key] = text
                except Exception as e:
                    st.warning(f"文字起こしに失敗しました: {e}")

        return st.session_state[state_key]

    # ----- Whisper未利用 → Web Speech API + 手入力フォールバック -----
    return _fallback_web_speech(key, help_text)


def _fallback_web_speech(key: str, help_text: str) -> Optional[str]:
    """Web Speech API（ブラウザネイティブ）+ 手入力フォールバックUI。

    HTML+JS で音声認識し、認識結果はテキストエリアに表示。
    ユーザーがそれをコピーして Streamlit のテキスト入力欄に貼り付けて確定する。

    Returns:
        確定されたテキスト / 未入力 → None
    """
    state_key = f"{key}_transcribed_text"
    if state_key not in st.session_state:
        st.session_state[state_key] = None

    st.caption(help_text)

    # Web Speech API 埋込（ブラウザがChromeなら音声認識可、iOS Safariなら非対応）
    try:
        from streamlit.components.v1 import html as st_html
        st_html(_render_html_speech_recognition(), height=260, scrolling=False)
    except Exception:
        st.info(
            "💡 ブラウザ音声認識UIを表示できません。下のテキスト欄に手入力してください。"
        )

    # 確定用テキスト入力欄（音声認識結果のコピペ or 手入力）
    user_text = st.text_input(
        "↓ 認識結果をここに貼り付けて Enter キーで確定（または直接入力）",
        key=f"{key}_text_input",
        placeholder="例: 太陽光パネルを20枚に変更してください",
    )

    if user_text and user_text.strip():
        st.session_state[state_key] = user_text.strip()

    return st.session_state[state_key]


# ============================================================================
# 簡易動作確認
# ============================================================================

if __name__ == "__main__":
    print(f"is_whisper_available(): {is_whisper_available()}")
    print(f"OpenAI API key configured: {_get_openai_key() is not None}")
    print(f"Anthropic API key configured: {_get_anthropic_key() is not None}")
