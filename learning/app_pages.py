"""学習センター モードの Streamlit UI（3ステップ）

app.py から呼ばれる。見積作成・簡易製図フローとは独立した「別系統」のページ群
（drafting/app_pages.py と同じパターン）。

フロー:
  step1: 学習タイプ選択（見積 / 図面）+ AI版・正規版のアップロード → 差分抽出
  step2: 差分の確認・承認（チェックボックスで選択して学習）
  step3: 学習完了 + 学習済みルールの確認

セッションキー（learning_ プレフィックスで名前空間を分離）:
  learning_kind             : "estimate" | "drawing"（今回の学習対象）
  learning_ai_parsed        : AI側パース結果の dict（model_dump / spec dict）
  learning_official_parsed  : 正規側パース結果の dict
  learning_diffs            : 差分リスト（EstimateDiffItem / DrawingDiffItem の dict）
  learning_saved_count      : step3 で表示する学習件数
  learning_tmp_paths        : アップロード由来の一時ファイルパス
  learning_warnings         : パース時の警告（step2 冒頭で表示）
  learning_source_files     : 学習ログに残す入力ファイル名
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import streamlit as st

from drafting.models import DrawingType
from learning import history, store


# =============================================================
# 表示用ラベル
# =============================================================

# 差分種別の日本語ラベル（EstimateDiffItem / DrawingDiffItem の diff_type）
_DIFF_TYPE_LABEL = {
    "price_changed": "単価変更",
    "quantity_changed": "数量変更",
    "item_added": "項目追加",
    "item_removed": "項目削除",
    "gap_changed": "パネル間隔",
    "margin_changed": "マージン",
    "orientation_changed": "パネル向き",
    "panel_count_changed": "枚数・配置",
    "face_dimension_changed": "屋根寸法",
    "mount_type_changed": "架台種別",
    "string_config_changed": "系統構成",
    "panel_spec_changed": "モジュール仕様",
    "golden_example": "お手本登録",
}

# 学習ルール kind の日本語ラベル（store に保存される LearnedRule の kind）
_RULE_KIND_LABEL = {
    "unit_price_override": "単価上書き",
    "item_add": "項目追加",
    "item_suppress": "項目抑止",
    "gap_override": "パネル間隔",
    "margin_override": "マージン",
    "orientation_preference": "向き既定",
    "golden_example": "お手本(few-shot)",
}

# 学習対象の日本語ラベル
_TARGET_LABEL = {"estimate": "見積", "drawing": "図面"}


def _fmt_yen(v) -> str:
    """金額の表示用文字列（¥3,300 形式。None・非数値は空文字）。"""
    try:
        return f"¥{int(v):,}"
    except (TypeError, ValueError):
        return ""


# =============================================================
# セッション初期化
# =============================================================

def init_learning_session():
    defaults = {
        "learning_kind": None,
        "learning_ai_parsed": None,
        "learning_official_parsed": None,
        "learning_diffs": [],
        "learning_saved_count": 0,
        "learning_tmp_paths": [],
        "learning_warnings": [],
        "learning_source_files": [],
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def learning_step_names() -> list:
    return ["入力方法", "アップロード", "差分確認・学習", "完了"]


def _cleanup_tmp_files():
    """アップロード由来の一時ファイルを削除する（残留防止）。"""
    for p in (st.session_state.get("learning_tmp_paths") or []):
        try:
            if p and os.path.isfile(p):
                os.unlink(p)
        except Exception:
            pass
    st.session_state["learning_tmp_paths"] = []


def _clear_diff_checkbox_state():
    """差分承認チェックボックス（ld_*）のウィジェット状態を破棄する。

    key 付き widget の値は session_state に残るため、別の差分セットを
    読み込む前に消さないと前回のチェック状態が誤って引き継がれる。
    """
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith("ld_"):
            del st.session_state[k]


def _reset_learning():
    """学習系セッションを初期状態に戻す（一時ファイルも掃除）。"""
    _cleanup_tmp_files()
    _clear_diff_checkbox_state()
    st.session_state["learning_kind"] = None
    st.session_state["learning_ai_parsed"] = None
    st.session_state["learning_official_parsed"] = None
    st.session_state["learning_diffs"] = []
    st.session_state["learning_saved_count"] = 0
    st.session_state["learning_warnings"] = []
    st.session_state["learning_source_files"] = []


def _save_tmp(uploaded) -> str:
    """アップロードファイルを一時保存し、パスを learning_tmp_paths に記録する。"""
    suffix = os.path.splitext(uploaded.name)[1] or ".bin"
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tf.write(uploaded.getbuffer())
    tf.close()
    st.session_state.learning_tmp_paths = \
        (st.session_state.get("learning_tmp_paths") or []) + [tf.name]
    return tf.name


# =============================================================
# Step0 カード（app.py の step0 から呼ぶ）
# =============================================================

def render_mode_card():
    """step0 のモード選択に出す『学習センター』カード本体（ボタン込み）。"""
    st.markdown("""
    <div class="mode-card" style="border-color:#5B21B6;">
        <div style="font-size:4.5rem;margin-bottom:0.5rem;line-height:1;">🧠</div>
        <h3>学習センター</h3>
        <p>AI見積・AI図面と正規版の差分を学習し、<br/>次回の生成精度を向上</p>
        <div style="margin-top:0.6rem;font-size:0.78rem;color:#475569;line-height:1.6;">対象: <b>見積（単価・項目）／図面（間隔・向き）</b></div>
        <div style="margin-top:0.6rem;">
            <span style="background:#F5F3FF;color:#5B21B6;font-size:0.75rem;padding:3px 10px;border-radius:12px;font-weight:600;">差分学習</span>
            <span style="background:#F0FFF4;color:#276749;font-size:0.75rem;padding:3px 10px;border-radius:12px;font-weight:600;">自動反映</span>
        </div>
    </div>
    """, unsafe_allow_html=True)
    st.markdown("")
    if st.button("🧠 学習センターを開く", type="secondary", use_container_width=True, key="start_learning_btn"):
        st.session_state.input_mode = "learning"
        init_learning_session()
        _reset_learning()
        st.session_state.step = 1
        st.rerun()


def _render_persistence_indicator():
    """学習データの永続化状態（Supabase共有 / ローカル揮発）を表示する。

    is_enabled() は接続情報の存在チェックのみ（ネットワーク往復なし）で軽量。
    """
    try:
        from learning import storage_backend
        if storage_backend.is_enabled():
            st.success("🟢 学習データは全員で共有されています（クラウド保存）")
        else:
            st.warning("🟡 この環境ではローカル保存です — 再起動で消える可能性・"
                       "全員共有にはSupabase設定が必要")
    except Exception:
        pass


# =============================================================
# Step 1: 学習タイプ選択 + アップロード → 差分抽出
# =============================================================

def render_step1_upload():
    init_learning_session()
    st.markdown("### 🧠 学習センター｜比較データのアップロード")
    st.caption("AIが作成した見積・図面と、担当者が仕上げた正規版を比較し、"
               "差分（単価・項目・図面規約）を次回の生成に活かします。")
    _render_persistence_indicator()

    tab_est, tab_dwg = st.tabs(["📊 見積の学習", "📐 図面の学習"])

    with tab_est:
        _render_estimate_upload_tab()

    with tab_dwg:
        _render_drawing_upload_tab()

    # ---- 学習済みルールの管理（ページ下部・共通） ----
    st.divider()
    _render_rule_management()

    st.markdown("")
    if st.button("← モード選択へ", use_container_width=False, key="learning_back_to_mode"):
        _reset_learning()  # 一時ファイルの残留・前回状態の引き継ぎを防止
        st.session_state.input_mode = None
        st.session_state.step = 0
        st.rerun()


def _render_estimate_upload_tab():
    """見積の学習タブ: AI見積（履歴/CSV/PDF）+ 正規見積PDF → 差分抽出。"""
    col_ai, col_of = st.columns(2)

    hist_sel = None
    csv_file = None
    ai_pdf = None

    with col_ai:
        st.markdown("##### 🤖 AI見積（このツールの出力）")
        method = st.radio(
            "AI見積の入力方法",
            ["保存履歴から選択", "CSVアップロード", "PDFアップロード"],
            key="learning_est_method",
        )
        if method == "保存履歴から選択":
            hist = history.list_estimate_history()
            if not hist:
                st.info("保存履歴がまだありません。見積を作成すると自動で履歴が貯まります。")
            else:
                hist_sel = st.selectbox(
                    "保存履歴（新しい順）", hist,
                    format_func=lambda h: f"{h.get('saved_at', '')}｜"
                                          f"{h.get('project_name') or h.get('client_name') or h.get('estimate_id') or '無題'}｜"
                                          f"{_fmt_yen(h.get('total_with_tax')) or '¥0'}",
                    key="learning_est_hist",
                )
        elif method == "CSVアップロード":
            csv_file = st.file_uploader(
                "AI見積のCSV（本ツールの内訳CSV）", type=["csv"], key="learning_est_csv")
        else:
            ai_pdf = st.file_uploader(
                "AI見積のPDF", type=["pdf"], key="learning_est_ai_pdf")

    with col_of:
        st.markdown("##### ✅ 正規見積（担当者の最終版）")
        official_pdf = st.file_uploader(
            "正規見積のPDF", type=["pdf"], key="learning_est_official")

    ai_ready = (hist_sel is not None) or (csv_file is not None) or (ai_pdf is not None)
    st.markdown("")
    if st.button("🔍 差分を抽出", type="primary", use_container_width=True,
                 disabled=not (ai_ready and official_pdf is not None),
                 key="learning_est_extract"):
        _run_estimate_diff(method, hist_sel, csv_file, ai_pdf, official_pdf)


def _run_estimate_diff(method, hist_sel, csv_file, ai_pdf, official_pdf):
    """AI見積・正規見積をパース → 差分抽出 → step2 へ。"""
    # 見積側パーサー/差分エンジンは並行実装のため遅延 import
    # （モジュール欠落時も UI を落とさず案内する）
    try:
        from learning.estimate_parser import parse_estimate_pdf, parse_estimate_csv
        from learning.estimate_diff import diff_estimates
    except ImportError as e:
        st.error(f"⚠️ 見積学習モジュールを読み込めませんでした: {e}")
        st.info("learning/estimate_parser.py・learning/estimate_diff.py の配置後に再度お試しください。")
        return

    _cleanup_tmp_files()  # 前回抽出の一時ファイルを残さない

    with st.spinner("見積を読み取り、差分を抽出しています..."):
        try:
            # --- AI見積のパース ---
            if method == "保存履歴から選択":
                est = history.load_estimate_history(hist_sel["path"])
                if est is None:
                    st.error("⚠️ 保存履歴の読み込みに失敗しました。別の履歴を選択してください。")
                    return
                ai_parsed = history.estimate_to_parsed(
                    est, file_name=Path(hist_sel["path"]).name)
            elif method == "CSVアップロード":
                ai_parsed = parse_estimate_csv(
                    csv_file.getvalue(), source="ai", file_name=csv_file.name)
            else:
                ai_path = _save_tmp(ai_pdf)
                ai_parsed = parse_estimate_pdf(ai_path, source="ai")
                # パーサーは一時ファイル名（tmpXXXX.pdf）を file_name に入れるため、
                # evidence・学習ログに残るのはアップロード時の元ファイル名に必ず上書きする
                ai_parsed.file_name = ai_pdf.name

            # --- 正規見積のパース ---
            official_path = _save_tmp(official_pdf)
            official_parsed = parse_estimate_pdf(official_path, source="official")
            official_parsed.file_name = official_pdf.name

            # --- 差分抽出 ---
            diffs = diff_estimates(ai_parsed, official_parsed)
        except Exception as e:
            st.error(f"⚠️ 差分の抽出に失敗しました: {e}")
            st.info("PDFの読み取りにはClaude APIを使用します。"
                    "APIキー（ANTHROPIC_API_KEY）の設定と通信環境をご確認ください。")
            return

    st.session_state.learning_kind = "estimate"
    st.session_state.learning_ai_parsed = ai_parsed.model_dump()
    st.session_state.learning_official_parsed = official_parsed.model_dump()
    st.session_state.learning_diffs = [d.model_dump() for d in diffs]
    st.session_state.learning_warnings = \
        list(ai_parsed.warnings or []) + list(official_parsed.warnings or [])
    st.session_state.learning_source_files = \
        [p for p in (ai_parsed.file_name, official_parsed.file_name) if p]
    _cleanup_tmp_files()  # パース済みのため一時PDFは以後不要 → 即時削除（残留防止）
    _clear_diff_checkbox_state()
    st.session_state.step = 2
    st.rerun()


def _render_drawing_upload_tab():
    """図面の学習タブ: AI図面（履歴/JSON）+ 正規図面PDF → 差分抽出。"""
    col_ai, col_of = st.columns(2)

    hist_sel = None
    json_file = None

    with col_ai:
        st.markdown("##### 🤖 AI図面（このツールの出力）")
        method = st.radio(
            "AI図面の入力方法",
            ["保存履歴から選択", "スペックJSONアップロード"],
            key="learning_dwg_method",
        )
        if method == "保存履歴から選択":
            hist = history.list_drawing_history()
            if not hist:
                st.info("保存履歴がまだありません。簡易製図AIで図面を生成すると自動で履歴が貯まります。")
            else:
                hist_sel = st.selectbox(
                    "保存履歴（新しい順）", hist,
                    format_func=lambda h: f"{h.get('saved_at', '')}｜"
                                          f"{h.get('customer_name') or '無題'}｜"
                                          f"{DrawingType.LABEL.get(h.get('drawing_type'), h.get('drawing_type') or '-')}｜"
                                          f"{h.get('total_panels', 0)}枚/{h.get('total_kw', 0)}kW",
                    key="learning_dwg_hist",
                )
        else:
            json_file = st.file_uploader(
                "AI図面のスペックJSON", type=["json"], key="learning_dwg_json")

    with col_of:
        st.markdown("##### ✅ 正規図面（担当者の最終版）")
        official_pdf = st.file_uploader(
            "正規図面のPDF", type=["pdf"], key="learning_dwg_official")
        dtype = st.radio(
            "図面種別",
            options=[DrawingType.LAYOUT, DrawingType.STRING],
            format_func=lambda x: DrawingType.LABEL.get(x, x),
            key="learning_dwg_dtype",
            horizontal=True,
        )

    ai_ready = (hist_sel is not None) or (json_file is not None)
    st.markdown("")
    if st.button("🔍 差分を抽出", type="primary", use_container_width=True,
                 disabled=not (ai_ready and official_pdf is not None),
                 key="learning_dwg_extract"):
        _run_drawing_diff(method, hist_sel, json_file, official_pdf, dtype)


def _run_drawing_diff(method, hist_sel, json_file, official_pdf, dtype):
    """AI図面スペック取得 → 正規図面PDFをAI抽出 → 差分抽出 → step2 へ。"""
    try:
        from learning.drawing_diff import diff_drawing_specs
    except ImportError as e:
        st.error(f"⚠️ 図面学習モジュールを読み込めませんでした: {e}")
        return

    _cleanup_tmp_files()

    # --- AI図面スペックの取得（履歴 or JSON） ---
    source_files = []
    try:
        if method == "保存履歴から選択":
            ai_spec = history.load_drawing_history(hist_sel["path"])
            if not isinstance(ai_spec, dict):
                st.error("⚠️ 図面履歴の読み込みに失敗しました。別の履歴を選択してください。")
                return
            source_files.append(Path(hist_sel["path"]).name)
        else:
            data = json.loads(json_file.getvalue().decode("utf-8-sig"))
            # 履歴JSON（{"spec": {...}}）とスペック単体の両形式を受け付ける
            if isinstance(data, dict) and isinstance(data.get("spec"), dict):
                ai_spec = data["spec"]
            elif isinstance(data, dict):
                ai_spec = data
            else:
                st.error("⚠️ スペックJSONの形式が不正です（dict である必要があります）。")
                return
            source_files.append(json_file.name)
    except Exception as e:
        st.error(f"⚠️ AI図面スペックの読み込みに失敗しました: {e}")
        return

    # --- 正規図面PDFのAI抽出 → 差分 ---
    with st.spinner("正規図面を読み取り、差分を抽出しています..."):
        try:
            from drafting.spec_extractor import extract_drafting_spec
            from drafting.models import spec_to_dict
            official_path = _save_tmp(official_pdf)
            official_spec_obj = extract_drafting_spec([official_path], drawing_type=dtype)
            official_spec = spec_to_dict(official_spec_obj)
            # 一時ファイル名（tmpXXXX.pdf）が evidence に残らないよう、
            # 正規図面の元ファイル名をスペックに保持してから差分抽出する
            official_spec["source_file"] = official_pdf.name
            diffs = diff_drawing_specs(ai_spec, official_spec)
        except Exception as e:
            st.error(f"⚠️ 差分の抽出に失敗しました: {e}")
            st.info("図面PDFの読み取りにはClaude APIを使用します。"
                    "APIキー（ANTHROPIC_API_KEY）の設定と通信環境をご確認ください。")
            return

    source_files.append(official_pdf.name)
    st.session_state.learning_kind = "drawing"
    st.session_state.learning_ai_parsed = ai_spec
    st.session_state.learning_official_parsed = official_spec
    st.session_state.learning_diffs = [d.model_dump() for d in diffs]
    st.session_state.learning_warnings = \
        list(getattr(official_spec_obj, "warnings", []) or [])
    st.session_state.learning_source_files = source_files
    _cleanup_tmp_files()  # パース済みのため一時PDFは以後不要 → 即時削除（残留防止）
    _clear_diff_checkbox_state()
    st.session_state.step = 2
    st.rerun()


# =============================================================
# Step 2: 差分確認・承認
# =============================================================

def _diff_headline(d: dict) -> str:
    """差分1件の見出し文字列（種別ラベル + カテゴリ/対象 + summary）。"""
    label = _DIFF_TYPE_LABEL.get(d.get("diff_type", ""), d.get("diff_type", ""))
    scope = d.get("category") or d.get("target") or ""
    desc = d.get("description") or ""
    head = "／".join(x for x in (scope, desc) if x)
    parts = [f"【{label}】"]
    if head:
        parts.append(f"{head}｜")
    parts.append(d.get("summary", ""))
    return "".join(parts)


def _diff_detail_caption(d: dict) -> str:
    """差分行の補足キャプション（金額・数量。¥{v:,} 形式）。"""
    item = d.get("official_item") or d.get("ai_item")
    if not isinstance(item, dict):
        return ""
    parts = []
    qv = item.get("quantity_value")
    if qv is not None:
        q = f"{qv:g}" if isinstance(qv, float) else str(qv)
        parts.append(f"数量 {q}{item.get('quantity_unit', '')}")
    up = _fmt_yen(item.get("unit_price"))
    if up:
        parts.append(f"単価 {up}")
    am = _fmt_yen(item.get("amount"))
    if am:
        parts.append(f"金額 {am}")
    return "／".join(parts)


def render_step2_review():
    init_learning_session()
    kind = st.session_state.get("learning_kind") or "estimate"
    diffs = st.session_state.get("learning_diffs") or []

    st.markdown("### 🔍 差分確認・学習")
    st.caption(f"対象: {_TARGET_LABEL.get(kind, kind)}の学習｜"
               f"入力: {'、'.join(st.session_state.get('learning_source_files') or []) or '-'}")

    warnings = st.session_state.get("learning_warnings") or []
    if warnings:
        st.warning("**読み取り時の注意：**\n\n" + "\n".join(f"- {w}" for w in warnings))

    if not diffs:
        st.info("差分はありませんでした。AI版と正規版は一致しています。")
        if st.button("← アップロードに戻る", key="learning_review_back_empty"):
            st.session_state.learning_diffs = []
            _clear_diff_checkbox_state()
            st.session_state.step = 1
            st.rerun()
        return

    learnable = [(i, d) for i, d in enumerate(diffs) if d.get("learnable")]
    reference = [(i, d) for i, d in enumerate(diffs) if not d.get("learnable")]

    # ---- 学習可能な差分（チェックボックスで承認） ----
    st.markdown(f"#### ✅ 学習可能な差分（{len(learnable)}件）")
    if not learnable:
        st.caption("学習可能な差分はありません。")
    else:
        c1, c2, _sp = st.columns([1, 1, 3])
        with c1:
            if st.button("全選択", use_container_width=True, key="learning_check_all"):
                for i, _d in learnable:
                    st.session_state[f"ld_{i}"] = True
        with c2:
            if st.button("全解除", use_container_width=True, key="learning_uncheck_all"):
                for i, _d in learnable:
                    st.session_state[f"ld_{i}"] = False

        for i, d in learnable:
            key = f"ld_{i}"
            if key not in st.session_state:
                st.session_state[key] = True  # 既定ON
            st.checkbox(_diff_headline(d), key=key)
            detail = _diff_detail_caption(d)
            if detail:
                st.caption(f"　└ {detail}")

    # ---- 参考差分（数量・寸法など案件固有 → 自動学習の対象外） ----
    if reference:
        st.markdown(f"#### ℹ️ 参考（自動学習の対象外・{len(reference)}件）")
        st.caption("数量・枚数・屋根寸法などは案件ごとに変わるため、学習には使いません。")
        for _i, d in reference:
            st.markdown(f"- {_diff_headline(d)}")

    # ---- 学習実行 / 戻る ----
    st.divider()
    nav1, nav2 = st.columns([2, 1])
    with nav1:
        if st.button("✅ 選択した差分を学習する", type="primary",
                     use_container_width=True, key="learning_save_btn"):
            approved_rules = [
                d["proposed_rule"] for i, d in learnable
                if st.session_state.get(f"ld_{i}") and d.get("proposed_rule")
            ]
            if not approved_rules:
                st.warning("学習する差分が選択されていません。チェックを入れてから実行してください。")
            else:
                try:
                    store.add_rules(kind, approved_rules)
                    store.append_learning_log({
                        "kind": kind,
                        "source_files": st.session_state.get("learning_source_files") or [],
                        "approved": len(approved_rules),
                        "total_diffs": len(diffs),
                    })
                except Exception as e:
                    st.error(f"⚠️ 学習ルールの保存に失敗しました: {e}")
                    return
                st.session_state.learning_saved_count = len(approved_rules)
                _clear_diff_checkbox_state()
                st.session_state.step = 3
                st.rerun()
    with nav2:
        if st.button("← アップロードに戻る", use_container_width=True, key="learning_review_back"):
            # checkbox キーの残留で次回の差分に前回状態が付くのを防ぐ
            st.session_state.learning_diffs = []
            _clear_diff_checkbox_state()
            st.session_state.step = 1
            st.rerun()


# =============================================================
# Step 3: 学習完了
# =============================================================

def render_step3_done():
    init_learning_session()
    kind = st.session_state.get("learning_kind") or "estimate"
    n = st.session_state.get("learning_saved_count", 0)

    st.markdown("### 🎓 学習完了")
    st.success(f"✅ {n}件の差分を学習しました（対象: {_TARGET_LABEL.get(kind, kind)}）。")
    st.caption("学習内容は次回の見積作成・図面生成から自動で反映されます")

    # 対象ストアの学習済みルール一覧
    try:
        rules = store.load_rules(kind)
    except Exception:
        rules = []
    if rules:
        st.markdown(f"#### 📚 現在の学習済みルール（{_TARGET_LABEL.get(kind, kind)}: {len(rules)}件）")
        for r in rules:
            _render_rule_summary_line(r)

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if st.button("📚 続けて学習する", type="primary", use_container_width=True,
                     key="learning_continue_btn"):
            # 差分・パース結果をリセットして再アップロードへ（履歴は保持）
            _cleanup_tmp_files()
            _clear_diff_checkbox_state()
            st.session_state.learning_ai_parsed = None
            st.session_state.learning_official_parsed = None
            st.session_state.learning_diffs = []
            st.session_state.learning_warnings = []
            st.session_state.learning_source_files = []
            st.session_state.step = 1
            st.rerun()
    with c2:
        if st.button("🏠 モード選択へ", use_container_width=True, key="learning_home_btn"):
            _reset_learning()
            st.session_state.input_mode = None
            st.session_state.step = 0
            st.rerun()


# =============================================================
# 学習済みルールの管理（step1 下部の expander）
# =============================================================

def _render_rule_summary_line(rule: dict):
    """学習済みルール1件の要約表示（有効マーク + 種別 + 説明 + 根拠）。"""
    ev = rule.get("evidence", {}) or {}
    kind_label = _RULE_KIND_LABEL.get(rule.get("kind", ""), rule.get("kind", ""))
    mark = "🟢" if rule.get("enabled", True) else "⚪"
    st.markdown(f"{mark} **[{kind_label}]** {rule.get('display_description', '')}")
    st.caption(f"　学習日時: {ev.get('learned_at', '-')}｜案件: {ev.get('project_name') or '-'}")


def _render_rule_management():
    """estimate/drawing 両ストアの一覧・有効/無効・削除 + 学習ログ。"""
    with st.expander("🗂 学習済みルールの管理", expanded=False):
        for target in ("estimate", "drawing"):
            try:
                rules = store.load_rules(target)
            except Exception as e:
                st.warning(f"{_TARGET_LABEL[target]}ルールの読み込みに失敗しました: {e}")
                continue
            st.markdown(f"**{_TARGET_LABEL[target]}のルール（{len(rules)}件）**")
            if not rules:
                st.caption("まだ学習されたルールはありません。")
            for r in rules:
                rid = r.get("id", "")
                ev = r.get("evidence", {}) or {}
                kind_label = _RULE_KIND_LABEL.get(r.get("kind", ""), r.get("kind", ""))
                c1, c2, c3 = st.columns([6, 1.3, 0.8])
                with c1:
                    mark = "🟢" if r.get("enabled", True) else "⚪"
                    st.markdown(f"{mark} **[{kind_label}]** {r.get('display_description', '')}")
                    st.caption(f"学習日時: {ev.get('learned_at', '-')}｜"
                               f"案件: {ev.get('project_name') or '-'}")
                with c2:
                    enabled = bool(r.get("enabled", True))
                    new_enabled = st.toggle("有効", value=enabled, key=f"tgl_{rid}")
                    if new_enabled != enabled:
                        # store 更新成功時のみ rerun（失敗時に rerun すると
                        # エラー表示が即消え＆無限リランループになるため）。
                        # ※ st.rerun() は例外送出で実装されているため except に
                        #   吸われないよう else 節に置く。
                        try:
                            store.set_rule_enabled(target, rid, new_enabled)
                        except Exception as e:
                            st.error(f"更新に失敗しました: {e}")
                        else:
                            st.rerun()
                with c3:
                    if st.button("🗑", key=f"del_{rid}", help="このルールを削除"):
                        try:
                            store.delete_rule(target, rid)
                        except Exception as e:
                            st.error(f"削除に失敗しました: {e}")
                        else:
                            st.session_state.pop(f"tgl_{rid}", None)
                            st.rerun()
            st.markdown("")

        # ---- 学習ログ（最新10件） ----
        try:
            logs = store.load_learning_log()
        except Exception:
            logs = []
        if logs:
            st.markdown("**🕐 学習ログ（最新10件）**")
            for entry in reversed(logs[-10:]):
                files = "、".join(entry.get("source_files") or []) or "-"
                st.caption(f"{entry.get('logged_at', '-')}｜"
                           f"{_TARGET_LABEL.get(entry.get('kind'), entry.get('kind') or '-')}｜"
                           f"承認 {entry.get('approved', 0)}/{entry.get('total_diffs', 0)}件｜{files}")
