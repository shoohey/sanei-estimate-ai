"""簡易製図AI モードの Streamlit UI（3ステップ）

app.py から呼ばれる。見積作成フローとは独立した「別系統」のページ群。

フロー:
  step1: 現調資料アップロード → AI抽出（or 手入力）
  step2: 抽出結果の確認・修正フォーム
  step3: 製図プレビュー + PNG/PDF ダウンロード

セッションキー（drafting_ プレフィックスで名前空間を分離）:
  drafting_spec_dict : DraftingSpec の dict（編集中の真実）
  drafting_files     : アップロードされた一時ファイルパス
  drafting_png       : 生成済み PNG bytes
  drafting_pdf       : 生成済み PDF bytes
  drafting_warnings  : 抽出時の要確認事項
"""

from __future__ import annotations

import os
import tempfile

import streamlit as st

from drafting.models import (
    DraftingSpec, RoofFace, PanelSpec, StringGroup, TitleBlock,
    DrawingType, RoofType, Orientation, MountType,
    default_spec, spec_to_dict, spec_from_dict,
)
from drafting import sample_specs


# =============================================================
# セッション初期化
# =============================================================

def init_drafting_session():
    defaults = {
        "drafting_spec_dict": None,
        "drafting_files": [],
        "drafting_png": None,
        "drafting_pdf": None,
        "drafting_warnings": [],
        "drafting_drawing_type": DrawingType.LAYOUT,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def drafting_step_names() -> list:
    return ["モード選択", "資料アップロード", "確認・修正", "製図プレビュー"]


def _cleanup_temp_files():
    """アップロード由来の一時ファイルを削除する（残留防止）。"""
    for p in (st.session_state.get("drafting_files") or []):
        try:
            if p and os.path.isfile(p):
                os.unlink(p)
        except Exception:
            pass
    st.session_state["drafting_files"] = []


def _clear_form_widget_state():
    """確認フォームの df_* ウィジェット状態を破棄する。

    Streamlit は key 付き widget の値を session_state 優先で保持するため、
    新しい下書きを読み込む際にこれを消さないと前案件の値が残り、
    その古い値が dict に書き戻されて誤った図面を生成してしまう。
    """
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith("df_"):
            del st.session_state[k]


def _load_draft(spec_dict: dict, warnings: list):
    """新しい下書きをセッションに読み込む（フォーム状態・生成物をリセット）。"""
    _clear_form_widget_state()
    st.session_state.drafting_spec_dict = spec_dict
    st.session_state.drafting_warnings = list(warnings or [])
    st.session_state.drafting_png = None
    st.session_state.drafting_pdf = None


def _reset_drafting():
    _cleanup_temp_files()
    _clear_form_widget_state()
    for k in ("drafting_spec_dict", "drafting_png", "drafting_pdf"):
        st.session_state[k] = None
    st.session_state["drafting_warnings"] = []


# =============================================================
# Step0 カード（app.py の step0 から呼ぶ）
# =============================================================

def render_mode_card():
    """step0 のモード選択に出す『簡易製図AI』カード本体（ボタン込み）。"""
    st.markdown("""
    <div class="mode-card" style="border-color:#1e3a5f;">
        <div style="font-size:4.5rem;margin-bottom:0.5rem;line-height:1;">📐</div>
        <h3>簡易製図AI</h3>
        <p>手書き現調・航空写真から<br/>太陽光配置図／ストリングス図を自動作図</p>
        <div style="margin-top:0.6rem;font-size:0.78rem;color:#475569;line-height:1.6;">出力: <b>配置図・ストリングス図（A4/A3 PDF）</b></div>
        <div style="margin-top:0.6rem;">
            <span style="background:#EBF5FF;color:#2B6CB0;font-size:0.75rem;padding:3px 10px;border-radius:12px;font-weight:600;">AI抽出</span>
            <span style="background:#F0FFF4;color:#276749;font-size:0.75rem;padding:3px 10px;border-radius:12px;font-weight:600;">CAD風出力</span>
        </div>
    </div>
    """, unsafe_allow_html=True)
    st.markdown("")
    if st.button("📐 簡易製図AIで開始", type="secondary", use_container_width=True, key="start_drafting_btn"):
        st.session_state.input_mode = "drafting"
        init_drafting_session()
        _reset_drafting()
        st.session_state.step = 1
        st.rerun()


# =============================================================
# Step 1: アップロード → AI抽出
# =============================================================

def render_step1_upload():
    init_drafting_session()
    st.markdown("### 📐 簡易製図AI｜現調資料のアップロード")
    st.caption("現地調査の手書きスケッチ・航空写真（赤ペン寸法入り）・建築図面・現地写真をアップロードしてください。"
               "AIが屋根寸法・モジュール・枚数・系統を読み取り、確認フォームに反映します。")

    col1, col2 = st.columns([3, 2])
    with col1:
        files = st.file_uploader(
            "現調資料（PDF / PNG / JPG・複数可）",
            type=["pdf", "png", "jpg", "jpeg"],
            accept_multiple_files=True,
            key="drafting_uploader",
        )
    with col2:
        dtype = st.radio(
            "作成する図面",
            options=[DrawingType.LAYOUT, DrawingType.STRING],
            format_func=lambda x: DrawingType.LABEL.get(x, x),
            key="drafting_drawing_type",
            horizontal=False,
        )

    st.divider()
    c1, c2, c3 = st.columns([2, 2, 1.2])
    with c1:
        extract_btn = st.button("🤖 AIで読み取って下書きを作成", type="primary", use_container_width=True,
                                disabled=not files)
    with c2:
        manual_btn = st.button("✏️ 手入力で1から作成", use_container_width=True)
    with c3:
        if st.button("← モード選択へ", use_container_width=True):
            st.session_state.input_mode = None
            st.session_state.step = 0
            st.rerun()

    # --- デモ: ゴールデン仕様から下書き ---
    with st.expander("🧪 デモデータから作成（サンプル案件で動作確認）", expanded=False):
        demo_choices = {
            "kurihara_layout": "栗原様（住宅・瓦・配置図・10枚）",
            "yagi_layout": "八木様（住宅・ポリゴン屋根・配置図・12枚）",
            "spice_house_layout": "スパイスハウス様（法人・折板・配置図・72枚）",
            "tok_string": "東京応化様（法人・ストリングス図・202枚）",
        }
        demo_key = st.selectbox("サンプル案件", list(demo_choices.keys()),
                                format_func=lambda k: demo_choices[k], key="drafting_demo_sel")
        if st.button("このサンプルで下書きを作成", key="drafting_demo_btn"):
            spec = sample_specs.get_golden(demo_key)
            _load_draft(spec_to_dict(spec), [])
            st.session_state.step = 2
            st.rerun()

    if extract_btn and files:
        _run_extraction(files, dtype)

    if manual_btn:
        spec = default_spec()
        spec.drawing_type = dtype
        # 新規下書きの点検通路は既定800mm（2026-07-23 会議 修正①。0で無効化可能）
        spec.panel.walkway_mm = 800.0
        _load_draft(spec_to_dict(spec), ["手入力モードです。各項目を入力してください。"])
        st.session_state.step = 2
        st.rerun()


def _run_extraction(files, dtype: str):
    """アップロードファイルを一時保存 → spec_extractor で抽出 → step2 へ。"""
    _cleanup_temp_files()  # 前回抽出の一時ファイルを残さない
    tmp_paths = []
    try:
        for f in files:
            suffix = os.path.splitext(f.name)[1] or ".bin"
            tf = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tf.write(f.getbuffer())
            tf.close()
            tmp_paths.append(tf.name)
    except Exception as e:
        st.error(f"ファイルの保存に失敗しました: {e}")
        return

    with st.spinner("AIが現調資料を読み取っています（屋根寸法・モジュール・枚数・系統）..."):
        try:
            from drafting.spec_extractor import extract_drafting_spec
            spec = extract_drafting_spec(tmp_paths, drawing_type=dtype)
            # 新規下書きの点検通路は既定800mm（AI抽出対象外の項目。0で無効化可能）
            if not float(getattr(spec.panel, "walkway_mm", 0) or 0):
                spec.panel.walkway_mm = 800.0
        except Exception as e:
            st.error(f"⚠️ 抽出に失敗しました: {e}")
            st.info("「手入力で1から作成」から手動で入力することもできます。")
            for p in tmp_paths:
                try:
                    os.unlink(p)
                except Exception:
                    pass
            return

    _load_draft(spec_to_dict(spec), list(getattr(spec, "warnings", []) or []))
    st.session_state.drafting_files = tmp_paths
    st.session_state.step = 2
    st.rerun()


# =============================================================
# Step 2: 確認・修正フォーム
# =============================================================

def render_step2_confirm():
    init_drafting_session()
    d = st.session_state.get("drafting_spec_dict")
    if not d:
        st.warning("下書きがありません。アップロードからやり直してください。")
        if st.button("← アップロードへ"):
            st.session_state.step = 1
            st.rerun()
        return

    st.markdown("### 📝 抽出結果の確認・修正")
    st.caption("AIが読み取った内容です。手書きの寸法は誤読の可能性があるため、必ず確認・修正してください。")

    warnings = st.session_state.get("drafting_warnings") or []
    if warnings:
        st.warning("**要確認：**\n\n" + "\n".join(f"- {w}" for w in warnings))

    # ---- 基本情報 ----
    st.markdown("#### 基本情報")
    b1, b2, b3, b4 = st.columns(4)
    with b1:
        d["customer_name"] = st.text_input("施主名／工事名", value=d.get("customer_name", ""), key="df_customer")
    with b2:
        d["drawing_type"] = st.selectbox(
            "図面種別", options=[DrawingType.LAYOUT, DrawingType.STRING],
            index=[DrawingType.LAYOUT, DrawingType.STRING].index(d.get("drawing_type", DrawingType.LAYOUT))
            if d.get("drawing_type") in (DrawingType.LAYOUT, DrawingType.STRING) else 0,
            format_func=lambda x: DrawingType.LABEL.get(x, x), key="df_dtype")
    with b3:
        d["paper"] = st.selectbox("用紙", options=["A4", "A3"],
                                  index=0 if d.get("paper", "A4") == "A4" else 1, key="df_paper")
    with b4:
        d["mount_type"] = st.selectbox(
            "架台種別", options=list(MountType.ALL),
            index=list(MountType.ALL).index(d.get("mount_type")) if d.get("mount_type") in MountType.ALL else 0,
            key="df_mount")

    # ---- パネル ----
    st.markdown("#### モジュール（パネル）")
    panel = d.get("panel", {}) or {}
    p1, p2, p3 = st.columns(3)
    with p1:
        panel["maker"] = st.text_input("メーカー", value=panel.get("maker", ""), key="df_pmaker")
        panel["output_w"] = st.number_input("出力 (W/枚)", value=float(panel.get("output_w", 0) or 0),
                                            min_value=0.0, step=5.0, key="df_pw")
    with p2:
        panel["model"] = st.text_input("型番", value=panel.get("model", ""), key="df_pmodel")
        panel["long_mm"] = st.number_input("パネル長辺 (mm)", value=float(panel.get("long_mm", 0) or 0),
                                          min_value=0.0, step=1.0, key="df_plong")
    with p3:
        panel["gap_long_mm"] = st.number_input("長辺側隙間 (mm)", value=float(panel.get("gap_long_mm", 25) or 0),
                                              min_value=0.0, step=1.0, key="df_pgl",
                                              help="段と段の間（上下方向）の隙間。標準25mm（陸屋根は500mm）")
        panel["short_mm"] = st.number_input("パネル短辺 (mm)", value=float(panel.get("short_mm", 0) or 0),
                                           min_value=0.0, step=1.0, key="df_pshort")
    panel["gap_short_mm"] = st.number_input("短辺側隙間 (mm)", value=float(panel.get("gap_short_mm", 10) or 0),
                                          min_value=0.0, step=1.0, key="df_pgs",
                                          help="列と列の間（左右方向）の隙間。標準10mm")
    # 点検通路（2026-07-23 会議 修正①: 2列ごとに800mm〜1,000mmの通路）
    w1, w2 = st.columns(2)
    with w1:
        panel["walkway_mm"] = st.number_input(
            "点検通路幅 (mm)", value=float(panel.get("walkway_mm", 0) or 0),
            min_value=0.0, step=50.0, key="df_pwalk",
            help="N列ごとに確保する点検・メンテナンス用の通路幅。標準800〜1,000mm。"
                 "0にすると通路なし（従来どおりの配置）")
    with w2:
        panel["walkway_every_n_cols"] = int(st.number_input(
            "通路の間隔（N列ごと）", value=int(panel.get("walkway_every_n_cols", 2) or 2),
            min_value=1, max_value=20, step=1, key="df_pwalkn",
            help="何列ごとに点検通路を入れるか。標準は2列ごと"))
    d["panel"] = panel

    # ---- 屋根面 ----
    st.markdown("#### 屋根面")
    faces = d.get("roof_faces", []) or []
    _FACE_MAX = 16
    n_faces = st.number_input("屋根面の数", min_value=1, max_value=_FACE_MAX,
                              value=min(_FACE_MAX, max(1, len(faces))),
                              step=1, key="df_nfaces")
    # 面数の増減に合わせて配列を調整
    while len(faces) < n_faces:
        faces.append(spec_to_dict(RoofFace(name=f"面{len(faces)+1}", width_mm=10000, depth_mm=6000)))
    faces = faces[:n_faces]

    for i, face in enumerate(faces):
        with st.expander(f"🏠 {face.get('name') or f'面{i+1}'}", expanded=(i == 0)):
            f1, f2, f3 = st.columns(3)
            with f1:
                face["name"] = st.text_input("面の名称", value=face.get("name", f"面{i+1}"), key=f"df_fname_{i}")
                face["shape"] = st.selectbox("形状", options=["rectangle", "polygon"],
                                             index=0 if face.get("shape", "rectangle") == "rectangle" else 1,
                                             format_func=lambda x: "矩形" if x == "rectangle" else "ポリゴン",
                                             key=f"df_fshape_{i}")
            with f2:
                face["roof_type"] = st.selectbox(
                    "屋根種別", options=list(RoofType.ALL),
                    index=list(RoofType.ALL).index(face.get("roof_type")) if face.get("roof_type") in RoofType.ALL else 0,
                    format_func=lambda x: RoofType.LABEL.get(x, x), key=f"df_frtype_{i}")
                face["orientation"] = st.selectbox(
                    "パネル向き", options=list(Orientation.ALL),
                    index=list(Orientation.ALL).index(face.get("orientation")) if face.get("orientation") in Orientation.ALL else 2,
                    format_func=lambda x: Orientation.LABEL.get(x, x), key=f"df_forient_{i}")
            with f3:
                _cnt = st.number_input(
                    "設置枚数（0=最大枚数を自動配置）",
                    value=int(face.get("target_panel_count") or 0),
                    min_value=0, step=1, key=f"df_fcount_{i}",
                    help="0 のままにすると、屋根面に収まる最大枚数を自動で配置します。")
                face["target_panel_count"] = int(_cnt) or None
                face["margin_mm"] = st.number_input("離隔マージン (mm)", value=float(face.get("margin_mm", 500) or 0),
                                                   min_value=0.0, step=50.0, key=f"df_fmargin_{i}")

            g1, g2 = st.columns(2)
            with g1:
                face["width_mm"] = st.number_input("屋根 幅 (mm, 東西)", value=float(face.get("width_mm", 0) or 0),
                                                  min_value=0.0, step=100.0, key=f"df_fw_{i}")
            with g2:
                face["depth_mm"] = st.number_input("屋根 奥行 (mm, 南北)", value=float(face.get("depth_mm", 0) or 0),
                                                  min_value=0.0, step=100.0, key=f"df_fd_{i}")
            if face.get("shape") == "polygon":
                st.caption("ポリゴン頂点は AI 抽出値を使用します（このフォームでは編集不可）。"
                           "矩形に変更すると幅×奥行で再配置されます。")
            # 複数面の図面上オフセット（任意・上級者向け）
            o1, o2 = st.columns(2)
            with o1:
                face["origin_x_mm"] = st.number_input("図面配置X (mm)", value=float(face.get("origin_x_mm", 0) or 0),
                                                     step=500.0, key=f"df_fox_{i}")
            with o2:
                face["origin_y_mm"] = st.number_input("図面配置Y (mm)", value=float(face.get("origin_y_mm", 0) or 0),
                                                     step=500.0, key=f"df_foy_{i}")
    d["roof_faces"] = faces

    # ---- ストリングス図のときのみ: PCS / 系統表 ----
    if d.get("drawing_type") == DrawingType.STRING:
        st.markdown("#### PCS・ストリング系統")
        s1, s2 = st.columns(2)
        with s1:
            d["pcs_model"] = st.text_input("PCS 型番", value=d.get("pcs_model", ""), key="df_pcsmodel")
        with s2:
            d["pcs_count"] = st.number_input("PCS 台数", value=int(d.get("pcs_count", 0) or 0),
                                            min_value=0, step=1, key="df_pcscount")
        strings = d.get("strings", []) or []
        _STR_MAX = 40
        n_str = st.number_input("系統数", min_value=0, max_value=_STR_MAX,
                                value=min(_STR_MAX, len(strings)), step=1, key="df_nstr")
        while len(strings) < n_str:
            strings.append(spec_to_dict(StringGroup(pcs_label=f"PCS{len(strings)+1}")))
        strings = strings[:n_str]
        for j, sg in enumerate(strings):
            cc1, cc2 = st.columns([1, 2])
            with cc1:
                sg["pcs_label"] = st.text_input("番号", value=sg.get("pcs_label", f"PCS{j+1}"), key=f"df_slabel_{j}")
            with cc2:
                sg["config_text"] = st.text_input("系統（例 12直×5並）", value=sg.get("config_text", ""), key=f"df_sconf_{j}")
        d["strings"] = strings

    # ---- タイトルブロック ----
    st.markdown("#### タイトルブロック")
    title = d.get("title", {}) or {}
    t1, t2, t3 = st.columns(3)
    with t1:
        title["drawing_no"] = st.text_input("図番", value=title.get("drawing_no", ""), key="df_tno")
        title["scale"] = st.text_input("縮尺（空欄で自動）", value=title.get("scale", ""), key="df_tscale")
    with t2:
        title["drawing_name"] = st.text_input("図面名", value=title.get("drawing_name", "太陽光配置図"), key="df_tname")
        title["created_date"] = st.text_input("作成日", value=title.get("created_date", ""), key="df_tdate")
    with t3:
        title["system_text"] = st.text_input("システム表記", value=title.get("system_text", ""), key="df_tsys")
        title["install_angle"] = st.text_input("設置角度", value=title.get("install_angle", ""), key="df_tangle")
    title["project_name"] = st.text_input("工事名称", value=title.get("project_name", d.get("customer_name", "")), key="df_tproj")
    d["title"] = title

    # 保存
    st.session_state.drafting_spec_dict = d

    st.divider()
    nav1, nav2, nav3 = st.columns([1.4, 1.4, 1])
    with nav1:
        if st.button("🖨️ この内容で製図を生成", type="primary", use_container_width=True):
            _generate_drawing(d)
    with nav2:
        if st.button("← アップロードに戻る", use_container_width=True):
            st.session_state.step = 1
            st.rerun()
    with nav3:
        if st.button("最初から", use_container_width=True):
            _reset_drafting()
            st.session_state.input_mode = None
            st.session_state.step = 0
            st.rerun()


def _generate_drawing(d: dict):
    """dict → DraftingSpec → 学習ルール適用 → place_panels → render_drawing → step3。"""
    learned_notes = []
    with st.spinner("製図を生成しています..."):
        try:
            from drafting.layout_engine import place_panels
            from drafting.drawing_renderer import render_drawing
            spec = spec_from_dict(d)
            # 学習済み図面ルールの適用（既定値のみ上書き。失敗しても製図は続行）
            try:
                from learning.apply_drawing import apply_learned_drawing_rules
                spec, learned_notes = apply_learned_drawing_rules(spec)
            except Exception:
                learned_notes = []
            spec = place_panels(spec)
            out = render_drawing(spec)
        except Exception as e:
            st.error(f"⚠️ 製図の生成に失敗しました: {e}")
            return
    st.session_state.drafting_png = out.get("png_bytes")
    st.session_state.drafting_pdf = out.get("pdf_bytes")
    st.session_state.drafting_spec_dict = spec_to_dict(spec)  # 配置・集計反映後
    st.session_state.drafting_learning_notes = learned_notes  # step3 で表示する
    # 学習の材料として図面履歴を自動保存（失敗しても本体フローは止めない）
    try:
        from learning.history import save_drawing_history
        save_drawing_history(spec_to_dict(spec))
    except Exception:
        pass
    st.session_state.step = 3
    st.rerun()


# =============================================================
# Step 3: プレビュー + ダウンロード
# =============================================================

def render_step3_result():
    init_drafting_session()
    png = st.session_state.get("drafting_png")
    pdf = st.session_state.get("drafting_pdf")
    d = st.session_state.get("drafting_spec_dict") or {}

    st.markdown("### 🖼️ 製図プレビュー")
    if not png:
        st.warning("製図がまだ生成されていません。")
        if st.button("← 確認画面に戻る"):
            st.session_state.step = 2
            st.rerun()
        return

    # サマリ
    title = d.get("title", {}) or {}
    dtype = d.get("drawing_type", DrawingType.LAYOUT)
    st.markdown(
        f"**{d.get('customer_name','')}** ｜ {DrawingType.LABEL.get(dtype, dtype)} ｜ "
        f"{d.get('total_panels',0)}枚 / {d.get('total_kw',0)}kW ｜ {d.get('paper','A4')}横"
    )

    # 生成時に適用された学習済みルール（st.rerun 後もここで見える）
    learned_notes = st.session_state.get("drafting_learning_notes") or []
    if learned_notes:
        st.caption("🧠 学習済みルール適用: " + " / ".join(learned_notes))

    st.image(png, use_container_width=True)

    cust = (d.get("customer_name") or "drawing").replace(" ", "_").replace("　", "_")
    dname = title.get("drawing_name", "製図")
    fbase = f"{cust}_{dname}"

    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button("📥 PDF をダウンロード", data=pdf or b"", file_name=f"{fbase}.pdf",
                           mime="application/pdf", use_container_width=True, type="primary",
                           disabled=not pdf)
    with c2:
        st.download_button("📥 PNG をダウンロード", data=png, file_name=f"{fbase}.png",
                           mime="image/png", use_container_width=True)
    with c3:
        if st.button("✏️ 内容を修正して再生成", use_container_width=True):
            st.session_state.step = 2
            st.rerun()

    st.divider()
    if st.button("📐 別の案件を作成（最初から）"):
        _reset_drafting()
        st.session_state.input_mode = None
        st.session_state.step = 0
        st.rerun()
