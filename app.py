"""見積作成AIツール - Streamlit メインエントリ（2モード対応）"""
import streamlit as st
import tempfile
import os
from datetime import date

import config
from models.survey_data import (
    SurveyData, ProjectInfo, PlannedEquipment, HighVoltageChecklist,
    SupplementarySheet, FinalConfirmation, DesignStatus, GroundType,
    LocationType, BTPlacement, CInstallation, ConfidenceLevel,
)
from models.estimate_data import EstimateData, CategoryType
from extraction.pdf_reader import pdf_to_images
from extraction.survey_extractor import extract_survey_data
from extraction.survey_validator import validate_survey_data
from generation.estimate_builder import build_estimate, update_line_item
from generation.pdf_generator import generate_pdf

# ページ設定
st.set_page_config(
    page_title="見積作成AI - 株式会社サンエー",
    page_icon="☀️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# カスタムCSS（プロフェッショナルデザイン）
st.markdown("""
<style>
    /* === グローバル === */
    .stApp {
        background: linear-gradient(135deg, #f5f7fa 0%, #e8ecf1 100%);
    }
    section[data-testid="stSidebar"] {
        background: #1B2D45;
    }

    /* === ヘッダー === */
    .app-header {
        background: linear-gradient(135deg, #1B2D45 0%, #2D4A6F 100%);
        color: white;
        padding: 1.2rem 1.8rem;
        border-radius: 12px;
        margin-bottom: 1rem;
        box-shadow: 0 4px 15px rgba(27, 45, 69, 0.2);
    }
    .app-header h1 {
        margin: 0;
        font-size: 1.6rem;
        font-weight: 700;
        letter-spacing: 0.02em;
    }
    .app-header p {
        margin: 0.3rem 0 0 0;
        font-size: 0.85rem;
        color: rgba(255,255,255,0.75);
    }

    /* === ステップインジケーター === */
    .step-bar {
        display: flex;
        justify-content: space-between;
        align-items: center;
        background: white;
        padding: 0.8rem 1.2rem;
        border-radius: 10px;
        margin-bottom: 1.2rem;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
        gap: 4px;
        flex-wrap: wrap;
    }
    .step-item {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 0.82rem;
        font-weight: 500;
        color: #a0aec0;
        white-space: nowrap;
    }
    .step-item.active {
        color: #1B2D45;
        font-weight: 700;
    }
    .step-item.done {
        color: #38A169;
    }
    .step-dot {
        width: 28px;
        height: 28px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 0.75rem;
        font-weight: 700;
        flex-shrink: 0;
    }
    .step-dot.active {
        background: linear-gradient(135deg, #F5A623, #F7C948);
        color: white;
        box-shadow: 0 2px 8px rgba(245, 166, 35, 0.4);
    }
    .step-dot.done {
        background: #38A169;
        color: white;
    }
    .step-dot.pending {
        background: #e2e8f0;
        color: #a0aec0;
    }
    .step-connector {
        flex: 1;
        height: 2px;
        background: #e2e8f0;
        min-width: 12px;
    }
    .step-connector.done {
        background: #38A169;
    }

    /* === モード選択カード === */
    .mode-card {
        background: white;
        border: 2px solid #e2e8f0;
        border-radius: 16px;
        padding: 2rem 1.5rem;
        text-align: center;
        transition: all 0.3s ease;
        cursor: pointer;
        box-shadow: 0 2px 10px rgba(0,0,0,0.04);
    }
    .mode-card:hover {
        border-color: #F5A623;
        box-shadow: 0 8px 25px rgba(245, 166, 35, 0.15);
        transform: translateY(-2px);
    }
    .mode-icon {
        font-size: 3.5rem;
        margin-bottom: 0.8rem;
        display: block;
    }
    .mode-card h3 {
        color: #1B2D45;
        font-size: 1.15rem;
        margin: 0.5rem 0;
    }
    .mode-card p {
        color: #64748b;
        font-size: 0.9rem;
        line-height: 1.6;
    }

    /* === セクションヘッダー === */
    .section-header {
        background: linear-gradient(90deg, #1B2D45 0%, #2D4A6F 100%);
        color: white;
        padding: 10px 16px;
        border-radius: 8px;
        font-weight: 600;
        font-size: 0.95rem;
        margin: 1.2rem 0 0.6rem 0;
        letter-spacing: 0.02em;
    }

    /* === カード風コンテナ === */
    .card-container {
        background: white;
        border-radius: 12px;
        padding: 1.5rem;
        margin: 0.8rem 0;
        box-shadow: 0 2px 10px rgba(0,0,0,0.06);
        border: 1px solid #e8ecf1;
    }

    /* === 警告・エラーボックス === */
    .warning-box {
        background: linear-gradient(135deg, #FFFBEB, #FEF3C7);
        border: 1px solid #F59E0B;
        border-left: 4px solid #F59E0B;
        border-radius: 8px;
        padding: 10px 14px;
        margin: 6px 0;
        color: #92400E;
        font-size: 0.9rem;
    }
    .error-box {
        background: linear-gradient(135deg, #FFF5F5, #FED7D7);
        border: 1px solid #F56565;
        border-left: 4px solid #F56565;
        border-radius: 8px;
        padding: 10px 14px;
        margin: 6px 0;
        color: #9B2C2C;
        font-size: 0.9rem;
    }
    .success-box {
        background: linear-gradient(135deg, #F0FFF4, #C6F6D5);
        border: 1px solid #48BB78;
        border-left: 4px solid #48BB78;
        border-radius: 8px;
        padding: 10px 14px;
        margin: 6px 0;
        color: #276749;
        font-size: 0.9rem;
    }

    /* === メトリクスカード === */
    div[data-testid="stMetric"] {
        background: white;
        border-radius: 10px;
        padding: 12px 16px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
        border: 1px solid #e8ecf1;
    }
    div[data-testid="stMetric"] label {
        color: #64748b;
        font-size: 0.8rem;
    }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: #1B2D45;
        font-weight: 700;
    }

    /* === ボタンスタイル === */
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #F5A623 0%, #E8961C 100%);
        border: none;
        border-radius: 8px;
        font-weight: 600;
        padding: 0.6rem 1.5rem;
        box-shadow: 0 3px 10px rgba(245, 166, 35, 0.3);
        transition: all 0.2s ease;
    }
    .stButton > button[kind="primary"]:hover {
        box-shadow: 0 5px 15px rgba(245, 166, 35, 0.4);
        transform: translateY(-1px);
    }
    .stButton > button[kind="secondary"] {
        border-radius: 8px;
        border: 1.5px solid #cbd5e0;
        font-weight: 500;
        transition: all 0.2s ease;
    }
    .stButton > button[kind="secondary"]:hover {
        border-color: #1B2D45;
        color: #1B2D45;
    }

    /* === タブ === */
    .stTabs [data-baseweb="tab-list"] {
        gap: 4px;
        background: white;
        border-radius: 10px;
        padding: 4px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px;
        font-weight: 500;
        padding: 8px 16px;
    }
    .stTabs [aria-selected="true"] {
        background: linear-gradient(135deg, #1B2D45, #2D4A6F);
        color: white !important;
    }

    /* === エクスパンダー === */
    .streamlit-expanderHeader {
        background: white;
        border-radius: 8px;
        font-weight: 600;
        color: #1B2D45;
    }

    /* === ダウンロードボタン === */
    .stDownloadButton > button {
        border-radius: 10px;
        padding: 0.8rem 1.5rem;
        font-weight: 600;
        font-size: 1rem;
    }

    /* === テーブル風の見積項目 === */
    .estimate-row {
        display: flex;
        align-items: center;
        padding: 8px 12px;
        border-bottom: 1px solid #edf2f7;
        gap: 8px;
    }
    .estimate-row:hover {
        background: #f7fafc;
    }
    .estimate-row-header {
        display: flex;
        align-items: center;
        padding: 10px 12px;
        background: #f1f5f9;
        border-radius: 8px 8px 0 0;
        font-weight: 600;
        font-size: 0.82rem;
        color: #475569;
        gap: 8px;
    }
    .estimate-total {
        background: linear-gradient(135deg, #1B2D45, #2D4A6F);
        color: white;
        padding: 12px 16px;
        border-radius: 0 0 8px 8px;
        font-weight: 700;
        font-size: 1rem;
        text-align: right;
    }

    /* === 手動入力ハイライト === */
    .manual-badge {
        display: inline-block;
        background: linear-gradient(135deg, #FFFBEB, #FEF3C7);
        color: #B45309;
        font-size: 0.7rem;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 12px;
        border: 1px solid #F59E0B;
    }

    /* === モバイル対応 === */
    @media (max-width: 768px) {
        .app-header h1 { font-size: 1.2rem; }
        .step-bar { padding: 0.5rem 0.8rem; }
        .step-item { font-size: 0.7rem; }
        .step-dot { width: 22px; height: 22px; font-size: 0.65rem; }
        .mode-card { padding: 1.2rem 1rem; }
        div[data-testid="stNumberInput"] label { font-size: 0.8rem; }
        div[data-testid="column"] { padding: 0 4px; }
    }
</style>
""", unsafe_allow_html=True)


# =============================================================
# メイン
# =============================================================
def main():
    st.markdown("""
    <div class="app-header">
        <h1>☀️ 太陽光発電設備 見積作成AI</h1>
        <p>株式会社サンエー｜現調データから見積書を自動生成</p>
    </div>
    """, unsafe_allow_html=True)

    # セッション初期化
    _init_session()

    # ステップインジケーター
    _render_step_indicator()

    # 各ステップ
    step = st.session_state.step
    if step == 0:
        _render_step0_mode_select()
    elif step == 1:
        if st.session_state.input_mode == "direct":
            _render_step1_direct_input()
        else:
            _render_step1_pdf_upload()
    elif step == 2:
        _render_step2_review()
    elif step == 3:
        _render_step3_estimate()
    elif step == 4:
        _render_step4_download()


def _init_session():
    defaults = {
        "step": 0,
        "input_mode": None,  # "direct" or "pdf"
        "survey_data": None,
        "estimate_data": None,
        "pdf_images": None,
        "pdf_bytes": None,
        "client_name": "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _render_step_indicator():
    if st.session_state.input_mode == "direct":
        steps = ["入力方法", "現調データ入力", "確認", "見積プレビュー", "ダウンロード"]
    elif st.session_state.input_mode == "pdf":
        steps = ["入力方法", "PDF読み取り", "確認・修正", "見積プレビュー", "ダウンロード"]
    else:
        steps = ["入力方法", "データ入力", "確認", "見積プレビュー", "ダウンロード"]

    current = st.session_state.step
    html_parts = ['<div class="step-bar">']
    for i, name in enumerate(steps):
        if i > 0:
            conn_cls = "done" if i <= current else ""
            html_parts.append(f'<div class="step-connector {conn_cls}"></div>')
        if i < current:
            html_parts.append(f'<div class="step-item done"><div class="step-dot done">&#10003;</div>{name}</div>')
        elif i == current:
            html_parts.append(f'<div class="step-item active"><div class="step-dot active">{i+1}</div>{name}</div>')
        else:
            html_parts.append(f'<div class="step-item"><div class="step-dot pending">{i+1}</div>{name}</div>')
    html_parts.append('</div>')
    st.markdown("".join(html_parts), unsafe_allow_html=True)


# =============================================================
# Step 0: 入力モード選択
# =============================================================
def _render_step0_mode_select():
    st.markdown("")  # spacer
    st.markdown('<p style="text-align:center;font-size:1.1rem;color:#475569;font-weight:500;">入力方法を選択してください</p>', unsafe_allow_html=True)
    st.markdown("")

    col_sp1, col1, col_gap, col2, col_sp2 = st.columns([0.5, 2, 0.3, 2, 0.5])

    with col1:
        st.markdown("""
        <div class="mode-card">
            <span class="mode-icon">📱</span>
            <h3>現場入力モード</h3>
            <p>スマホ・タブレットで<br/>現調データを直接入力</p>
            <div style="margin-top:0.8rem;">
                <span style="background:#EBF5FF;color:#2B6CB0;font-size:0.75rem;padding:3px 10px;border-radius:12px;font-weight:600;">OCR不要</span>
                <span style="background:#F0FFF4;color:#276749;font-size:0.75rem;padding:3px 10px;border-radius:12px;font-weight:600;">高速</span>
                <span style="background:#FFFBEB;color:#92400E;font-size:0.75rem;padding:3px 10px;border-radius:12px;font-weight:600;">高精度</span>
            </div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("")
        if st.button("📱 現場入力モードで開始", type="primary", use_container_width=True):
            st.session_state.input_mode = "direct"
            st.session_state.survey_data = SurveyData()
            st.session_state.step = 1
            st.rerun()

    with col2:
        st.markdown("""
        <div class="mode-card">
            <span class="mode-icon">📄</span>
            <h3>PDFアップロードモード</h3>
            <p>手書き現調シートPDFを<br/>AIが自動読み取り</p>
            <div style="margin-top:0.8rem;">
                <span style="background:#F5F3FF;color:#5B21B6;font-size:0.75rem;padding:3px 10px;border-radius:12px;font-weight:600;">AI OCR</span>
                <span style="background:#EBF5FF;color:#2B6CB0;font-size:0.75rem;padding:3px 10px;border-radius:12px;font-weight:600;">自動認識</span>
            </div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("")
        if st.button("📄 PDFアップロードで開始", use_container_width=True):
            st.session_state.input_mode = "pdf"
            st.session_state.step = 1
            st.rerun()


# =============================================================
# Step 1A: 現場入力モード
# =============================================================
def _render_step1_direct_input():
    st.markdown('<div style="margin-bottom:0.5rem;"><span style="font-size:1.25rem;font-weight:700;color:#1B2D45;">📱 現調データ入力</span><span style="margin-left:12px;color:#64748b;font-size:0.85rem;">現場で調査した内容を入力してください</span></div>', unsafe_allow_html=True)

    survey: SurveyData = st.session_state.survey_data

    # 宛先
    st.session_state.client_name = st.text_input(
        "宛先会社名", value=st.session_state.client_name,
        placeholder="例: 株式会社アローズ")

    # --- 案件基本情報 ---
    st.markdown('<div class="section-header">📋 案件基本情報</div>', unsafe_allow_html=True)
    survey.project.project_name = st.text_input(
        "案件名 *", value=survey.project.project_name,
        placeholder="例: テックランド掛川店")
    survey.project.address = st.text_input(
        "所在地 *", value=survey.project.address,
        placeholder="例: 静岡県掛川市細田231-1")
    survey.project.postal_code = st.text_input(
        "郵便番号", value=survey.project.postal_code,
        placeholder="例: 436-0048")

    c1, c2, c3 = st.columns(3)
    with c1:
        survey.project.survey_date = st.text_input(
            "調査日", value=survey.project.survey_date or date.today().strftime("%Y/%m/%d"))
    with c2:
        weather_opts = ["晴れ", "曇り", "雨", "雪"]
        w_idx = weather_opts.index(survey.project.weather) if survey.project.weather in weather_opts else 0
        survey.project.weather = st.selectbox("天気", weather_opts, index=w_idx)
    with c3:
        survey.project.surveyor = st.text_input(
            "調査者", value=survey.project.surveyor)

    # --- 計画設備情報 ---
    st.markdown('<div class="section-header">🔧 計画設備情報</div>', unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1:
        survey.equipment.module_maker = st.text_input(
            "モジュールメーカー *", value=survey.equipment.module_maker,
            placeholder="例: LONGI")
        survey.equipment.module_model = st.text_input(
            "モジュール型式 *", value=survey.equipment.module_model,
            placeholder="例: LR7-72HVH-660M")
    with c2:
        survey.equipment.module_output_w = st.number_input(
            "定格出力 (W/枚) *", value=float(survey.equipment.module_output_w),
            min_value=0.0, max_value=1000.0, step=10.0)
        survey.equipment.planned_panels = st.number_input(
            "設置予定枚数 *", value=int(survey.equipment.planned_panels),
            min_value=0, step=1)

    # PV容量の自動計算
    if survey.equipment.module_output_w > 0 and survey.equipment.planned_panels > 0:
        auto_kw = survey.equipment.planned_panels * survey.equipment.module_output_w / 1000
        survey.equipment.pv_capacity_kw = auto_kw
        st.info(f"想定PV容量: **{auto_kw:.2f} kW**（{survey.equipment.planned_panels}枚 x {survey.equipment.module_output_w:.0f}W / 1000）")
    else:
        survey.equipment.pv_capacity_kw = st.number_input(
            "想定PV容量 (kW)", value=float(survey.equipment.pv_capacity_kw),
            min_value=0.0, step=0.01, format="%.2f")

    design_options = ["確定", "仮", "未定"]
    d_idx = design_options.index(survey.equipment.design_status.value) \
        if survey.equipment.design_status.value in design_options else 2
    survey.equipment.design_status = DesignStatus(
        st.selectbox("設計確定度", design_options, index=d_idx))

    # --- 高圧チェック項目 ---
    st.markdown('<div class="section-header">⚡ 高圧チェック項目</div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        survey.high_voltage.building_drawing = st.toggle(
            "建物図面あり", value=survey.high_voltage.building_drawing)
        survey.high_voltage.single_line_diagram = st.toggle(
            "単線結線図あり", value=survey.high_voltage.single_line_diagram)
        if survey.high_voltage.single_line_diagram:
            survey.high_voltage.single_line_diagram_note = st.text_input(
                "単線結線図 備考", value=survey.high_voltage.single_line_diagram_note,
                placeholder="例: 既存")
    with c2:
        ground_opts = ["A", "C", "D"]
        g_idx = ground_opts.index(survey.high_voltage.ground_type.value) \
            if survey.high_voltage.ground_type.value in ground_opts else 0
        survey.high_voltage.ground_type = GroundType(
            st.selectbox("接地種類", ground_opts, index=g_idx))

        c_opts = ["可", "不可"]
        c_idx = 0 if survey.high_voltage.c_installation == CInstallation.POSSIBLE else 1
        survey.high_voltage.c_installation = CInstallation(
            st.selectbox("C種別設置可否", c_opts, index=c_idx))

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        survey.high_voltage.vt_available = st.toggle("VTあり", value=survey.high_voltage.vt_available)
    with c2:
        survey.high_voltage.ct_available = st.toggle("CTあり", value=survey.high_voltage.ct_available)
    with c3:
        survey.high_voltage.relay_space = st.toggle("継電器スペースあり", value=survey.high_voltage.relay_space)
    with c4:
        survey.high_voltage.pre_use_self_check = st.toggle("使用前自己確認あり", value=survey.high_voltage.pre_use_self_check)

    c1, c2 = st.columns(2)
    with c1:
        survey.high_voltage.pcs_space = st.toggle("PCS設置スペースあり", value=survey.high_voltage.pcs_space)
        if survey.high_voltage.pcs_space:
            pcs_opts = ["屋外", "屋内"]
            pcs_idx = 1 if survey.high_voltage.pcs_location == LocationType.INDOOR else 0
            survey.high_voltage.pcs_location = LocationType(
                st.selectbox("PCS設置場所", pcs_opts, index=pcs_idx))
    with c2:
        bt_opts = ["設置なし", "屋内", "屋外"]
        bt_map = {BTPlacement.NONE: 0, BTPlacement.INDOOR: 1, BTPlacement.OUTDOOR: 2}
        bt_idx = bt_map.get(survey.high_voltage.bt_space, 0)
        bt_val = st.selectbox("BT設置スペース", bt_opts, index=bt_idx)
        survey.high_voltage.bt_space = {"設置なし": BTPlacement.NONE, "屋内": BTPlacement.INDOOR, "屋外": BTPlacement.OUTDOOR}[bt_val]

    tr_opts = ["十分", "不足"]
    tr_idx = 0 if survey.high_voltage.tr_capacity != "不足" else 1
    survey.high_voltage.tr_capacity = st.selectbox("Tr容量余裕", tr_opts, index=tr_idx)

    c1, c2 = st.columns(2)
    with c1:
        survey.high_voltage.separation_ns_mm = st.number_input(
            "離隔 縦(南北) mm", value=float(survey.high_voltage.separation_ns_mm),
            min_value=0.0, step=100.0)
    with c2:
        survey.high_voltage.separation_ew_mm = st.number_input(
            "離隔 横(東西) mm", value=float(survey.high_voltage.separation_ew_mm),
            min_value=0.0, step=100.0)

    # --- 別紙チェック項目 ---
    st.markdown('<div class="section-header">📝 別紙チェック項目</div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        survey.supplementary.crane_available = st.toggle(
            "クレーンあり", value=survey.supplementary.crane_available)
        survey.supplementary.scaffold_needed = st.toggle(
            "足場必要", value=survey.supplementary.scaffold_needed)
        if survey.supplementary.scaffold_needed:
            survey.supplementary.scaffold_location = st.text_input(
                "足場設置予定位置", value=survey.supplementary.scaffold_location)
    with c2:
        survey.supplementary.cubicle_location = st.toggle(
            "キュービクル・電気室あり", value=survey.supplementary.cubicle_location)
        wiring_opts = ["確定", "未確定"]
        w_idx = 0 if survey.supplementary.wiring_route == "確定" else 1
        survey.supplementary.wiring_route = st.selectbox(
            "配管・配線ルート", wiring_opts, index=w_idx)

    survey.supplementary.pole_number = st.text_input(
        "電柱番号", value=survey.supplementary.pole_number)

    survey.supplementary.handwritten_notes = st.text_area(
        "備考・メモ", value=survey.supplementary.handwritten_notes,
        placeholder="現場で気づいた点など自由に記入", height=100)

    # --- ナビゲーション ---
    st.divider()

    # バリデーション
    validation = validate_survey_data(survey)
    if validation.errors:
        for err in validation.errors:
            st.markdown(f'<div class="error-box">❌ {err}</div>', unsafe_allow_html=True)
    if validation.warnings:
        for warn in validation.warnings:
            st.markdown(f'<div class="warning-box">⚠️ {warn}</div>', unsafe_allow_html=True)

    nav_cols = st.columns([1, 1, 2])
    with nav_cols[0]:
        if st.button("← 入力方法に戻る"):
            st.session_state.step = 0
            st.rerun()
    with nav_cols[1]:
        if st.button("確認画面へ →", type="primary"):
            st.session_state.survey_data = survey
            st.session_state.step = 2
            st.rerun()


# =============================================================
# Step 1B: PDFアップロードモード
# =============================================================
def _render_step1_pdf_upload():
    st.markdown('<div style="margin-bottom:0.5rem;"><span style="font-size:1.25rem;font-weight:700;color:#1B2D45;">📄 現調シートPDFアップロード</span></div>', unsafe_allow_html=True)

    # API Key チェック（Streamlit Cloud secrets / .env / 手動入力）
    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        # Streamlit secrets から再取得を試みる
        try:
            if "ANTHROPIC_API_KEY" in st.secrets:
                api_key = st.secrets["ANTHROPIC_API_KEY"]
                config.ANTHROPIC_API_KEY = api_key
        except Exception:
            pass
    if not api_key:
        st.error("ANTHROPIC_API_KEY が設定されていません。")
        api_key = st.text_input("API Key を入力（一時的に使用）", type="password")
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
            config.ANTHROPIC_API_KEY = api_key

    col1, col2 = st.columns([2, 1])
    with col1:
        uploaded_file = st.file_uploader(
            "現調シートPDF をアップロード", type=["pdf"],
            help="手書きの現調シートPDFをアップロードしてください")
    with col2:
        st.session_state.client_name = st.text_input(
            "宛先会社名", value=st.session_state.client_name,
            placeholder="例: 株式会社アローズ")

    if uploaded_file:
        st.subheader("アップロードされたPDF")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(uploaded_file.getvalue())
            tmp_path = tmp.name

        try:
            images = pdf_to_images(tmp_path, dpi=150)
            st.session_state.pdf_images = images
            st.session_state.tmp_pdf_path = tmp_path

            page_cols = st.columns(min(len(images), 3))
            for i, (col, img) in enumerate(zip(page_cols, images)):
                with col:
                    st.image(img["image_bytes"], caption=f"ページ {img['page']}",
                             use_container_width=True)
        except Exception as e:
            st.error(f"PDFの読み取りに失敗しました: {e}")
            return

        st.divider()
        col_back, col_read = st.columns([1, 2])
        with col_back:
            if st.button("← 入力方法に戻る"):
                st.session_state.step = 0
                st.rerun()
        with col_read:
            if st.button("🔍 AI読み取り開始", type="primary", use_container_width=True):
                with st.spinner("Claude AI で現調シートを読み取り中...（20〜30秒）"):
                    try:
                        survey = extract_survey_data(tmp_path)
                        st.session_state.survey_data = survey
                        st.session_state.step = 2
                        st.rerun()
                    except Exception as e:
                        st.error(f"読み取りに失敗しました: {e}")
                        st.exception(e)
    else:
        if st.button("← 入力方法に戻る"):
            st.session_state.step = 0
            st.rerun()


# =============================================================
# Step 2: 確認・修正画面（共通）
# =============================================================
def _render_step2_review():
    survey: SurveyData = st.session_state.survey_data
    if survey is None:
        st.warning("データがありません。")
        if st.button("← 戻る"):
            st.session_state.step = 0
            st.rerun()
        return

    is_pdf_mode = st.session_state.input_mode == "pdf"

    if is_pdf_mode:
        st.markdown('<div style="margin-bottom:0.5rem;"><span style="font-size:1.25rem;font-weight:700;color:#1B2D45;">📄 読み取り結果の確認・修正</span></div>', unsafe_allow_html=True)
    else:
        st.markdown('<div style="margin-bottom:0.5rem;"><span style="font-size:1.25rem;font-weight:700;color:#1B2D45;">📱 入力データの確認</span></div>', unsafe_allow_html=True)

    # バリデーション
    validation = validate_survey_data(survey)
    if validation.errors:
        for err in validation.errors:
            st.markdown(f'<div class="error-box">❌ {err}</div>', unsafe_allow_html=True)
    if validation.warnings:
        for warn in validation.warnings:
            st.markdown(f'<div class="warning-box">⚠️ {warn}</div>', unsafe_allow_html=True)
    if is_pdf_mode and survey.extraction_warnings:
        st.info("**AI読み取りの注意事項:**")
        for w in survey.extraction_warnings:
            st.caption(f"  - {w}")

    # レイアウト（PDFモードは左にPDF、右にフォーム。直接入力モードはフォームのみ）
    if is_pdf_mode:
        col_pdf, col_form = st.columns([1, 1])
        with col_pdf:
            st.markdown("**元のPDF**")
            if st.session_state.pdf_images:
                for img in st.session_state.pdf_images:
                    st.image(img["image_bytes"], caption=f"ページ {img['page']}",
                             use_container_width=True)
        form_container = col_form
    else:
        form_container = st.container()

    with form_container:
        st.markdown("**現調データ**")

        # 宛先会社名
        st.session_state.client_name = st.text_input(
            "宛先会社名", value=st.session_state.client_name,
            placeholder="例: 株式会社アローズ", key="review_client")

        # 案件基本情報
        with st.expander("📋 案件基本情報", expanded=True):
            survey.project.project_name = st.text_input(
                "案件名", value=survey.project.project_name, key="r_name")
            survey.project.address = st.text_input(
                "所在地", value=survey.project.address, key="r_addr")
            survey.project.postal_code = st.text_input(
                "郵便番号", value=survey.project.postal_code, key="r_zip")
            c1, c2 = st.columns(2)
            with c1:
                survey.project.survey_date = st.text_input(
                    "調査日", value=survey.project.survey_date, key="r_date")
            with c2:
                survey.project.weather = st.text_input(
                    "天気", value=survey.project.weather, key="r_weather")
            survey.project.surveyor = st.text_input(
                "調査者", value=survey.project.surveyor, key="r_surveyor")

        # 計画設備情報
        with st.expander("🔧 計画設備情報", expanded=True):
            c1, c2 = st.columns(2)
            with c1:
                survey.equipment.module_maker = st.text_input(
                    "モジュールメーカー", value=survey.equipment.module_maker, key="r_maker")
                survey.equipment.module_model = st.text_input(
                    "モジュール型式", value=survey.equipment.module_model, key="r_model")
            with c2:
                survey.equipment.module_output_w = st.number_input(
                    "定格出力 (W/枚)", value=float(survey.equipment.module_output_w),
                    min_value=0.0, step=1.0, key="r_output")
                survey.equipment.planned_panels = st.number_input(
                    "設置予定枚数", value=int(survey.equipment.planned_panels),
                    min_value=0, step=1, key="r_panels")
            survey.equipment.pv_capacity_kw = st.number_input(
                "想定PV容量 (kW)", value=float(survey.equipment.pv_capacity_kw),
                min_value=0.0, step=0.01, format="%.2f", key="r_kw")

            # PV容量チェック
            if survey.equipment.module_output_w > 0 and survey.equipment.planned_panels > 0:
                calc_kw = survey.equipment.planned_panels * survey.equipment.module_output_w / 1000
                if abs(calc_kw - survey.equipment.pv_capacity_kw) > 0.1:
                    st.warning(
                        f"計算値: {survey.equipment.planned_panels}枚 x "
                        f"{survey.equipment.module_output_w}W / 1000 = {calc_kw:.2f}kW "
                        f"（入力値: {survey.equipment.pv_capacity_kw}kW）")
                    if st.button("計算値で更新", key="r_update_kw"):
                        survey.equipment.pv_capacity_kw = calc_kw
                        st.rerun()

            design_options = ["確定", "仮", "未定"]
            d_idx = design_options.index(survey.equipment.design_status.value) \
                if survey.equipment.design_status.value in design_options else 2
            survey.equipment.design_status = DesignStatus(
                st.selectbox("設計確定度", design_options, index=d_idx, key="r_design"))

        # 高圧チェック項目
        with st.expander("⚡ 高圧チェック項目"):
            c1, c2 = st.columns(2)
            with c1:
                survey.high_voltage.building_drawing = st.checkbox(
                    "建物図面あり", value=survey.high_voltage.building_drawing, key="r_drawing")
                survey.high_voltage.single_line_diagram = st.checkbox(
                    "単線結線図あり", value=survey.high_voltage.single_line_diagram, key="r_sld")
                survey.high_voltage.vt_available = st.checkbox(
                    "VTあり", value=survey.high_voltage.vt_available, key="r_vt")
                survey.high_voltage.ct_available = st.checkbox(
                    "CTあり", value=survey.high_voltage.ct_available, key="r_ct")
                survey.high_voltage.relay_space = st.checkbox(
                    "継電器スペースあり", value=survey.high_voltage.relay_space, key="r_relay")
            with c2:
                survey.high_voltage.pcs_space = st.checkbox(
                    "PCS設置スペースあり", value=survey.high_voltage.pcs_space, key="r_pcs")
                if survey.high_voltage.pcs_space:
                    pcs_opts = ["屋内", "屋外"]
                    pcs_idx = 0 if survey.high_voltage.pcs_location == LocationType.INDOOR else 1
                    survey.high_voltage.pcs_location = LocationType(
                        st.selectbox("PCS設置場所", pcs_opts, index=pcs_idx, key="r_pcs_loc"))

                ground_opts = ["A", "C", "D"]
                g_idx = ground_opts.index(survey.high_voltage.ground_type.value) \
                    if survey.high_voltage.ground_type.value in ground_opts else 0
                survey.high_voltage.ground_type = GroundType(
                    st.selectbox("接地種類", ground_opts, index=g_idx, key="r_ground"))

                survey.high_voltage.pre_use_self_check = st.checkbox(
                    "使用前自己確認あり", value=survey.high_voltage.pre_use_self_check, key="r_selfcheck")

            c1, c2 = st.columns(2)
            with c1:
                survey.high_voltage.separation_ns_mm = st.number_input(
                    "離隔 縦(南北) mm", value=float(survey.high_voltage.separation_ns_mm),
                    min_value=0.0, step=100.0, key="r_sep_ns")
            with c2:
                survey.high_voltage.separation_ew_mm = st.number_input(
                    "離隔 横(東西) mm", value=float(survey.high_voltage.separation_ew_mm),
                    min_value=0.0, step=100.0, key="r_sep_ew")

        # 別紙チェック項目
        with st.expander("📝 別紙チェック項目"):
            c1, c2 = st.columns(2)
            with c1:
                survey.supplementary.crane_available = st.checkbox(
                    "クレーンあり", value=survey.supplementary.crane_available, key="r_crane")
                survey.supplementary.scaffold_needed = st.checkbox(
                    "足場必要", value=survey.supplementary.scaffold_needed, key="r_scaffold")
                survey.supplementary.cubicle_location = st.checkbox(
                    "キュービクル・電気室あり", value=survey.supplementary.cubicle_location, key="r_cubicle")
            with c2:
                wiring_opts = ["確定", "未確定"]
                w_idx = 0 if survey.supplementary.wiring_route == "確定" else 1
                survey.supplementary.wiring_route = st.selectbox(
                    "配管・配線ルート", wiring_opts, index=w_idx, key="r_wiring")

            survey.supplementary.handwritten_notes = st.text_area(
                "備考・メモ", value=survey.supplementary.handwritten_notes, key="r_notes")

    # フィードバック
    if validation.feedback:
        st.divider()
        st.subheader("現調シートへのフィードバック")
        for fb in validation.feedback:
            st.info(f"📝 {fb}")

    # ナビゲーション
    st.divider()
    nav_cols = st.columns([1, 1, 2])
    with nav_cols[0]:
        if st.button("← 戻る", key="r_back"):
            st.session_state.step = 1
            st.rerun()
    with nav_cols[1]:
        if st.button("見積作成に進む →", type="primary", key="r_next"):
            st.session_state.survey_data = survey
            with st.spinner("見積データを生成中..."):
                estimate = build_estimate(survey, st.session_state.client_name)
                st.session_state.estimate_data = estimate
            st.session_state.step = 3
            st.rerun()


# =============================================================
# Step 3: 見積プレビュー・編集
# =============================================================
def _render_step3_estimate():
    st.markdown('<div style="margin-bottom:0.5rem;"><span style="font-size:1.25rem;font-weight:700;color:#1B2D45;">📊 見積プレビュー・編集</span></div>', unsafe_allow_html=True)

    estimate: EstimateData = st.session_state.estimate_data
    if estimate is None:
        st.warning("見積データがありません。")
        if st.button("← 戻る"):
            st.session_state.step = 2
            st.rerun()
        return

    # 値引き調整
    with st.expander("💰 値引き調整", expanded=False):
        st.caption("税抜合計を任意の金額に調整できます（万円単位を推奨）")
        new_before_tax = st.number_input(
            "税抜合計（手動設定）", value=estimate.summary.total_before_tax,
            min_value=0, step=10000, key="manual_before_tax")
        if new_before_tax != estimate.summary.total_before_tax:
            estimate.summary.discount = new_before_tax - estimate.summary.subtotal
            estimate.summary.total_before_tax = new_before_tax
            estimate.summary.tax = int(new_before_tax * 0.10)
            estimate.summary.total_with_tax = new_before_tax + estimate.summary.tax
            estimate.cover.total_before_tax = estimate.summary.total_before_tax
            estimate.cover.tax = estimate.summary.tax
            estimate.cover.total_with_tax = estimate.summary.total_with_tax

    # サマリーカード
    st.markdown(f"""
    <div style="background:white;border-radius:12px;padding:1rem 1.5rem;box-shadow:0 2px 10px rgba(0,0,0,0.06);border:1px solid #e8ecf1;margin-bottom:1rem;">
        <div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:1rem;">
            <div style="text-align:center;flex:1;min-width:120px;">
                <div style="font-size:0.75rem;color:#64748b;margin-bottom:4px;">小計</div>
                <div style="font-size:1.1rem;font-weight:700;color:#1B2D45;">&yen;{estimate.summary.subtotal:,}</div>
            </div>
            <div style="text-align:center;flex:1;min-width:120px;">
                <div style="font-size:0.75rem;color:#64748b;margin-bottom:4px;">お値引き</div>
                <div style="font-size:1.1rem;font-weight:700;color:#E53E3E;">&yen;{estimate.summary.discount:,}</div>
            </div>
            <div style="text-align:center;flex:1;min-width:120px;">
                <div style="font-size:0.75rem;color:#64748b;margin-bottom:4px;">税抜合計</div>
                <div style="font-size:1.1rem;font-weight:700;color:#1B2D45;">&yen;{estimate.summary.total_before_tax:,}</div>
            </div>
            <div style="text-align:center;flex:1;min-width:120px;">
                <div style="font-size:0.75rem;color:#64748b;margin-bottom:4px;">消費税(10%)</div>
                <div style="font-size:1.1rem;font-weight:700;color:#64748b;">&yen;{estimate.summary.tax:,}</div>
            </div>
            <div style="text-align:center;flex:1;min-width:140px;background:linear-gradient(135deg,#1B2D45,#2D4A6F);border-radius:10px;padding:8px 12px;">
                <div style="font-size:0.75rem;color:rgba(255,255,255,0.7);margin-bottom:4px;">税込合計</div>
                <div style="font-size:1.3rem;font-weight:800;color:#F7C948;">&yen;{estimate.summary.total_with_tax:,}</div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.divider()

    # カテゴリ別タブ
    tab_names = []
    display_cats = []
    for cat in estimate.summary.categories:
        if cat.category == CategoryType.SPECIAL_NOTES and not cat.items:
            continue
        tab_names.append(f"{cat.category_number}. {cat.category.value} (¥{cat.total:,})")
        display_cats.append(cat)

    tabs = st.tabs(tab_names)
    for tab_idx, (tab, cat) in enumerate(zip(tabs, display_cats)):
        with tab:
            _render_category_editor(estimate, tab_idx, cat)

    # 根拠一覧
    st.divider()
    with st.expander("📊 根拠一覧（全項目）"):
        for r in estimate.reasoning_list:
            st.text(r)

    # ナビゲーション
    st.divider()
    nav_cols = st.columns([1, 1, 2])
    with nav_cols[0]:
        if st.button("← 確認画面に戻る"):
            st.session_state.step = 2
            st.rerun()
    with nav_cols[1]:
        if st.button("PDF生成・ダウンロードへ →", type="primary"):
            with st.spinner("PDF を生成中..."):
                pdf_bytes = generate_pdf(estimate)
                st.session_state.pdf_bytes = pdf_bytes
            st.session_state.step = 4
            st.rerun()


def _render_category_editor(estimate: EstimateData, cat_idx: int, cat):
    for item_idx, item in enumerate(cat.items):
        is_manual = item.is_manual_input

        with st.container():
            cols = st.columns([1, 4, 4, 2, 2, 2])
            with cols[0]:
                st.text(str(item.no))
            with cols[1]:
                st.text(item.description)
            with cols[2]:
                st.caption(item.remarks.replace('\n', ' / '))

            if is_manual:
                with cols[3]:
                    st.text_input("数量", value=item.quantity,
                                  key=f"qty_{cat_idx}_{item_idx}", label_visibility="collapsed")
                with cols[4]:
                    new_price = st.number_input(
                        "単価", value=item.unit_price, key=f"price_{cat_idx}_{item_idx}",
                        min_value=0, step=1000, label_visibility="collapsed")
                with cols[5]:
                    new_amount = st.number_input(
                        "金額", value=item.amount, key=f"amt_{cat_idx}_{item_idx}",
                        min_value=0, step=1000, label_visibility="collapsed")

                if new_price != item.unit_price or new_amount != item.amount:
                    item.unit_price = new_price
                    item.amount = new_amount
                    cat.calculate_totals()
                    estimate.summary.calculate_totals()
                    estimate.cover.total_with_tax = estimate.summary.total_with_tax
                    estimate.cover.total_before_tax = estimate.summary.total_before_tax
                    estimate.cover.tax = estimate.summary.tax

                st.markdown(
                    '<span class="manual-badge">⚠ 手動入力</span>',
                    unsafe_allow_html=True)
            else:
                with cols[3]:
                    st.text(item.quantity)
                with cols[4]:
                    st.text(f"¥{item.unit_price:,}" if item.unit_price else "")
                with cols[5]:
                    st.text(f"¥{item.amount:,}" if item.amount else "")

            if item.reasoning and item.reasoning.formula:
                st.caption(f"  💡 {item.reasoning.formula}")

    st.markdown(f'<div class="estimate-total">{cat.category.value} 合計: &yen;{cat.total:,}</div>', unsafe_allow_html=True)


# =============================================================
# Step 4: ダウンロード
# =============================================================
def _render_step4_download():
    estimate: EstimateData = st.session_state.estimate_data
    pdf_bytes = st.session_state.pdf_bytes

    if pdf_bytes is None:
        st.warning("PDFが生成されていません。")
        if st.button("← 見積プレビューに戻る"):
            st.session_state.step = 3
            st.rerun()
        return

    # 完了メッセージ
    st.markdown(f"""
    <div style="text-align:center;margin:1rem 0 2rem 0;">
        <div style="font-size:3rem;margin-bottom:0.5rem;">🎉</div>
        <h2 style="color:#1B2D45;margin:0;">見積書が完成しました</h2>
        <p style="color:#64748b;margin-top:0.3rem;">下のボタンからダウンロードしてください</p>
    </div>
    """, unsafe_allow_html=True)

    # 金額サマリー
    st.markdown(f"""
    <div style="max-width:420px;margin:0 auto 1.5rem auto;background:white;border-radius:12px;overflow:hidden;box-shadow:0 4px 15px rgba(0,0,0,0.08);">
        <div style="padding:14px 20px;display:flex;justify-content:space-between;border-bottom:1px solid #edf2f7;">
            <span style="color:#64748b;">税抜合計</span>
            <span style="font-weight:600;color:#1B2D45;">&yen;{estimate.summary.total_before_tax:,}</span>
        </div>
        <div style="padding:14px 20px;display:flex;justify-content:space-between;border-bottom:1px solid #edf2f7;">
            <span style="color:#64748b;">消費税 (10%)</span>
            <span style="font-weight:600;color:#1B2D45;">&yen;{estimate.summary.tax:,}</span>
        </div>
        <div style="padding:16px 20px;display:flex;justify-content:space-between;background:linear-gradient(135deg,#1B2D45,#2D4A6F);">
            <span style="color:rgba(255,255,255,0.8);font-weight:600;font-size:1.05rem;">税込合計</span>
            <span style="font-weight:800;color:#F7C948;font-size:1.3rem;">&yen;{estimate.summary.total_with_tax:,}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ダウンロードボタン
    col_sp1, col1, col2, col_sp2 = st.columns([0.5, 2, 2, 0.5])
    with col1:
        project_name = estimate.cover.project_name or "見積書"
        st.download_button(
            label="📥 見積書PDF ダウンロード", data=pdf_bytes,
            file_name=f"{project_name}.pdf", mime="application/pdf",
            type="primary", use_container_width=True)

    with col2:
        reasoning_text = "見積根拠一覧\n" + "=" * 50 + "\n\n"
        reasoning_text += f"見積ID: {estimate.cover.estimate_id}\n"
        reasoning_text += f"工事名: {estimate.cover.project_name}\n"
        reasoning_text += f"発行日: {estimate.cover.issue_date}\n"
        reasoning_text += f"税込合計: ¥{estimate.summary.total_with_tax:,}\n\n"
        reasoning_text += "-" * 50 + "\n\n"
        for r in estimate.reasoning_list:
            reasoning_text += f"  {r}\n"

        st.download_button(
            label="📝 根拠一覧テキスト ダウンロード",
            data=reasoning_text.encode("utf-8"),
            file_name=f"根拠一覧_{estimate.cover.estimate_id}.txt",
            mime="text/plain", use_container_width=True)

    st.markdown("")
    st.markdown("")

    col_sp1, col_btn, col_sp2 = st.columns([1, 2, 1])
    with col_btn:
        if st.button("🔄 新しい見積を作成", use_container_width=True):
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.rerun()
        st.markdown("")
        if st.button("← 見積プレビューに戻って編集", use_container_width=True):
            st.session_state.step = 3
            st.rerun()


if __name__ == "__main__":
    main()
