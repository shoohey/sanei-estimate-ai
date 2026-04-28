"""AI抽出結果の後処理レイヤー（ルールベース検証＆補正）

Claude API から返った SurveyData 相当の dict に対して、ドメイン知識に基づく
ルールで検証・補正を行い、信頼度を更新する。AIの誤読を機械的に救うための
レイヤーであり、 `extraction.survey_extractor._parse_raw_data()` の前段で
呼ぶことを想定している。

公開関数:
    - validate_and_correct(raw) -> (corrected, warnings, confidence_updates)
    - validate_pv_capacity_consistency(equipment) -> (kw, warning, confidence)
    - validate_module_output_w(value) -> (value, warning, confidence)
    - validate_postal_code(value) -> (value, warning, confidence)
    - validate_separation(value) -> (mm, warning)
    - infer_module_maker(model) -> str | None
    - correct_address(address, postal_code) -> (address, postal_code)

使用例:
    >>> from extraction.post_validators import validate_and_correct
    >>> raw = {
    ...     "project": {"address": "〒530-0001 大阪府大阪市北区梅田1-2-3", "postal_code": ""},
    ...     "equipment": {"module_maker": "", "module_model": "CS7L-MS",
    ...                    "module_output_w": 661, "planned_panels": 288, "pv_capacity_kw": 190.08},
    ... }
    >>> corrected, warnings, conf = validate_and_correct(raw)
    >>> corrected["equipment"]["module_output_w"]
    660
"""
from __future__ import annotations

import copy
import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# =====================================================================
# ドメイン定数
# =====================================================================

# モジュール出力Wの典型値（市場で出回っている定格出力）
TYPICAL_MODULE_OUTPUTS = [
    400, 410, 430, 440, 450, 460, 480, 500,
    540, 550, 580, 600, 620, 660, 670, 680, 700, 720,
]

# 型式プレフィックス → メーカー名（日本市場で流通する主要モジュール）
MODULE_MAKER_PREFIXES = {
    "CS": "Canadian Solar",
    "LR": "LONGi",
    "JKM": "Jinko",
    "TSM": "Trina",
    "JAM": "JA Solar",
    "SFJ": "SILFIN JAPAN",
    "NER": "NextEnergy",
    "NU-": "シャープ",
    "Q.PEAK": "Q CELLS",
}

# メーカー名の表記揺れ → 正規化先
MAKER_NORMALIZATION = {
    "canadian solar": "Canadian Solar",
    "canadian solar inc.": "Canadian Solar",
    "canadian solar inc": "Canadian Solar",
    "canadiansolar": "Canadian Solar",
    "カナディアンソーラー": "Canadian Solar",
    "longi": "LONGi",
    "longi solar": "LONGi",
    "ロンジ": "LONGi",
    "jinko": "Jinko",
    "jinko solar": "Jinko",
    "ジンコ": "Jinko",
    "ジンコソーラー": "Jinko",
    "trina": "Trina",
    "trina solar": "Trina",
    "トリナ": "Trina",
    "ja solar": "JA Solar",
    "jasolar": "JA Solar",
    "q cells": "Q CELLS",
    "qcells": "Q CELLS",
    "q.cells": "Q CELLS",
    "hanwha q cells": "Q CELLS",
    "シャープ": "シャープ",
    "sharp": "シャープ",
    "ネクストエナジー": "NextEnergy",
    "next energy": "NextEnergy",
    "nextenergy": "NextEnergy",
    "シルフィンジャパン": "SILFIN JAPAN",
    "silfin japan": "SILFIN JAPAN",
    "silfin": "SILFIN JAPAN",
    "京セラ": "京セラ",
    "kyocera": "京セラ",
    "三菱": "三菱",
    "mitsubishi": "三菱",
    "パナソニック": "Panasonic",
    "panasonic": "Panasonic",
}

# PV容量の典型範囲（kW）
TYPICAL_PV_CAPACITY_RANGE_RESIDENTIAL = (3.0, 10.0)
TYPICAL_PV_CAPACITY_RANGE_COMMERCIAL = (10.0, 2000.0)

# 設計確定度の許容値
ALLOWED_DESIGN_STATUS = ("確定", "仮", "未定")

# 全角→半角変換テーブル（日付・住所処理で使用）
_ZENKAKU_TO_HANKAKU = str.maketrans({
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "．": ".", "，": ",", "ー": "-", "−": "-", "－": "-",
})


# =====================================================================
# 内部ユーティリティ
# =====================================================================

def _to_zenkaku_normalized(s: str) -> str:
    """全角数字・記号を半角に変換する。"""
    if not isinstance(s, str):
        return s
    return s.translate(_ZENKAKU_TO_HANKAKU)


def _safe_float(val) -> float:
    """値を float に安全変換（全角数字対応・単位文字を除去）。"""
    if val is None:
        return 0.0
    try:
        if isinstance(val, str):
            s = _to_zenkaku_normalized(val)
            cleaned = re.sub(r"[^\d.\-]", "", s)
            return float(cleaned) if cleaned else 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val) -> int:
    """値を int に安全変換。"""
    if val is None:
        return 0
    try:
        if isinstance(val, str):
            s = _to_zenkaku_normalized(val)
            cleaned = re.sub(r"[^\d\-]", "", s)
            return int(cleaned) if cleaned else 0
        return int(val)
    except (ValueError, TypeError):
        return 0


# =====================================================================
# 個別バリデータ
# =====================================================================

def validate_module_output_w(value: float) -> tuple[float, str | None, str | None]:
    """モジュール出力Wを典型値にスナップする。

    TYPICAL_MODULE_OUTPUTS のうち最も近い値との差が10W以内ならスナップする。
    例: 661 → 660 （warning付き、confidence=medium）。

    Args:
        value: AIが読み取った出力W値

    Returns:
        (補正後出力W, 警告メッセージ or None, 新信頼度 or None)
    """
    v = _safe_float(value)
    if v <= 0:
        return v, None, "low"

    # 完全一致なら high
    if v in TYPICAL_MODULE_OUTPUTS:
        return v, None, "high"

    # 最も近い典型値を探す
    closest = min(TYPICAL_MODULE_OUTPUTS, key=lambda x: abs(x - v))
    diff = abs(closest - v)

    if diff <= 10:
        # スナップ
        warning = (
            f"モジュール出力 {v}W を典型値 {closest}W にスナップしました（誤差 {diff}W）。"
        )
        logger.info(warning)
        return float(closest), warning, "medium"

    # 典型値から大きく外れている → 補正せず low
    if v < 200 or v > 800:
        warning = (
            f"モジュール出力 {v}W は典型範囲(200〜800W)から外れています。"
            f"手書き文字の誤読の可能性があります。"
        )
        logger.warning(warning)
        return v, warning, "low"

    # 200〜800Wの範囲内だが典型値ではない → medium のまま
    warning = (
        f"モジュール出力 {v}W は典型値リストに存在しません（最近接値 {closest}W との差 {diff}W）。"
    )
    logger.info(warning)
    return v, warning, "medium"


def validate_pv_capacity_consistency(equipment: dict) -> tuple[float, str | None, str | None]:
    """PV容量と (出力W × 枚数 / 1000) の整合性をチェック。

    ±1%以内 → high
    ±5%以内 → medium
    それ以上 → 計算値で補正＋warning＋low

    Args:
        equipment: equipment フィールドの dict

    Returns:
        (補正後 pv_capacity_kw, 警告 or None, 新信頼度 or None)
    """
    module_w = _safe_float(equipment.get("module_output_w"))
    panels = _safe_int(equipment.get("planned_panels"))
    pv_kw = _safe_float(equipment.get("pv_capacity_kw"))

    # 算出材料が無い場合はそのまま返す
    if module_w <= 0 or panels <= 0:
        if pv_kw <= 0:
            return pv_kw, None, "low"
        return pv_kw, None, None

    calculated_kw = round(module_w * panels / 1000, 2)

    # PV容量が空 → 計算値で補完
    if pv_kw <= 0:
        warning = (
            f"PV容量が読み取れなかったため、計算値 {calculated_kw}kW "
            f"({module_w}W × {panels}枚 / 1000) で補完しました。"
        )
        logger.info(warning)
        return calculated_kw, warning, "medium"

    # 一致度判定
    deviation = abs(calculated_kw - pv_kw) / calculated_kw if calculated_kw > 0 else 1.0

    if deviation <= 0.01:
        return pv_kw, None, "high"
    if deviation <= 0.05:
        warning = (
            f"PV容量 {pv_kw}kW と計算値 {calculated_kw}kW の誤差は {deviation:.1%} です。"
        )
        logger.info(warning)
        return pv_kw, warning, "medium"

    # 5%超の乖離 → 計算値で補正
    warning = (
        f"PV容量 {pv_kw}kW と計算値 {calculated_kw}kW "
        f"({module_w}W × {panels}枚 / 1000) が {deviation:.0%} 乖離しています。"
        f"計算値で補正しました。"
    )
    logger.warning(warning)
    return calculated_kw, warning, "low"


def validate_postal_code(value: str) -> tuple[str, str | None, str | None]:
    """郵便番号を「XXX-XXXX」7桁形式に正規化する。

    Args:
        value: 郵便番号文字列（〒、半角・全角数字、空白を含む可能性あり）

    Returns:
        (整形後郵便番号, 警告 or None, 信頼度)
    """
    if value is None:
        return "", None, "low"
    if not isinstance(value, str):
        value = str(value)

    s = _to_zenkaku_normalized(value).replace("〒", "").strip()
    digits = re.sub(r"\D", "", s)

    if len(digits) == 7:
        formatted = f"{digits[:3]}-{digits[3:]}"
        if formatted == value.strip():
            return formatted, None, "high"
        return formatted, None, "high"

    if not digits:
        return "", None, "low"

    warning = (
        f"郵便番号「{value}」が7桁ではありません（{len(digits)}桁）。"
        f"手書き文字の誤読の可能性があります。"
    )
    logger.warning(warning)
    return s, warning, "low"


def validate_separation(value) -> tuple[float, str | None]:
    """離隔距離を mm 単位に正規化する。

    "3m" → 3000.0 / "300cm" → 3000.0 / 数値10未満 → m単位とみなしてmm化。

    Args:
        value: int / float / 文字列

    Returns:
        (mm単位の値, 警告 or None)
    """
    if value is None:
        return 0.0, None

    if isinstance(value, (int, float)):
        fval = float(value)
        if 0 < fval < 10:
            warning = f"離隔距離 {fval} を m単位と推定し {fval * 1000}mm に変換しました。"
            logger.info(warning)
            return fval * 1000, warning
        if 10 <= fval < 100:
            warning = f"離隔距離 {fval} を cm単位と推定し {fval * 10}mm に変換しました。"
            logger.info(warning)
            return fval * 10, warning
        return fval, None

    if isinstance(value, str):
        s = _to_zenkaku_normalized(value).strip().lower()
        m_match = re.match(r"([\d.]+)\s*m$", s)
        if m_match:
            mm = float(m_match.group(1)) * 1000
            warning = f"離隔距離 {value} を {mm}mm に変換しました。"
            logger.info(warning)
            return mm, warning
        cm_match = re.match(r"([\d.]+)\s*cm$", s)
        if cm_match:
            mm = float(cm_match.group(1)) * 10
            warning = f"離隔距離 {value} を {mm}mm に変換しました。"
            logger.info(warning)
            return mm, warning
        mm_match = re.match(r"([\d.]+)\s*mm$", s)
        if mm_match:
            return float(mm_match.group(1)), None
        # 単位なし数値文字列
        return _safe_float(value), None

    return _safe_float(value), None


def infer_module_maker(model: str) -> str | None:
    """型式からメーカー名を逆引きする。

    例: "CS7L-MS" → "Canadian Solar"

    Args:
        model: 型式文字列

    Returns:
        メーカー名 / 推定不能なら None
    """
    if not model or not isinstance(model, str):
        return None

    s = model.strip().upper()
    if not s:
        return None

    # 長いプレフィックスから優先的にマッチ（"NU-" が "N" にヒットしないように）
    sorted_prefixes = sorted(MODULE_MAKER_PREFIXES.items(), key=lambda x: -len(x[0]))
    for prefix, maker in sorted_prefixes:
        if s.startswith(prefix.upper()):
            logger.info(f"型式 '{model}' からメーカーを '{maker}' と推定しました。")
            return maker

    return None


def normalize_maker(name: str) -> str:
    """メーカー名の表記揺れを統一表記に正規化する。"""
    if not name or not isinstance(name, str):
        return name or ""
    key = name.strip().lower()
    if key in MAKER_NORMALIZATION:
        return MAKER_NORMALIZATION[key]
    # スペース・記号を除いて再判定
    key2 = re.sub(r"[\s・.\-]", "", key)
    for k, v in MAKER_NORMALIZATION.items():
        if re.sub(r"[\s・.\-]", "", k) == key2:
            return v
    return name.strip()


def correct_address(address: str, postal_code: str) -> tuple[str, str]:
    """住所と郵便番号を整合させる。

    - 住所先頭の「〒XXX-XXXX」を抽出して postal_code にも反映
    - 住所からは郵便番号部分を除去

    Args:
        address: 住所文字列
        postal_code: 既存の郵便番号

    Returns:
        (補正後住所, 補正後郵便番号)
    """
    addr = (address or "").strip()
    pc = (postal_code or "").strip()

    if not addr:
        return addr, pc

    # 住所内の〒XXX-XXXX or XXXXXXX を抽出
    normalized = _to_zenkaku_normalized(addr)
    zip_match = re.search(r"〒?\s*(\d{3})[\s\-]?(\d{4})", normalized)
    if zip_match:
        extracted_pc = f"{zip_match.group(1)}-{zip_match.group(2)}"
        # 既存 postal_code が空 or 形式不正なら抽出値で上書き
        if not pc or not re.match(r"^\d{3}-\d{4}$", pc):
            pc = extracted_pc
        # 住所から郵便番号部分を除去（元のテキストから検索）
        addr = re.sub(r"〒?\s*\d{3}[\s\-]?\d{4}\s*", "", normalized).strip()

    return addr, pc


def normalize_date(date_str: str) -> tuple[str, str | None]:
    """日付を YYYY/MM/DD 形式に正規化する。

    対応形式:
    - "2025/12/18", "2025-12-18", "2025.12.18"
    - "2025年9月29日"
    - "R7.12.18", "令和7年12月18日"
    - Excelシリアル番号（数値文字列、例: "46294"）

    Returns:
        (正規化済み日付, 警告 or None)
    """
    if not date_str or not str(date_str).strip():
        return "", None

    s = str(date_str).strip()
    s = _to_zenkaku_normalized(s)

    # 令和表記
    reiwa_match = re.match(
        r"(?:令和|R)\s*(\d{1,2})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})\s*日?",
        s,
    )
    if reiwa_match:
        year = 2018 + int(reiwa_match.group(1))
        month = int(reiwa_match.group(2))
        day = int(reiwa_match.group(3))
        return f"{year}/{month:02d}/{day:02d}", None

    # 年月日表記（漢字混じり）
    kanji_match = re.match(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", s)
    if kanji_match:
        return (
            f"{kanji_match.group(1)}/{int(kanji_match.group(2)):02d}/{int(kanji_match.group(3)):02d}",
            None,
        )

    # YYYY/MM/DD など
    full_match = re.match(r"(\d{4})\s*[./\-]\s*(\d{1,2})\s*[./\-]\s*(\d{1,2})", s)
    if full_match:
        return (
            f"{full_match.group(1)}/{int(full_match.group(2)):02d}/{int(full_match.group(3)):02d}",
            None,
        )

    # Excelシリアル番号（数値のみ、5桁前後）
    serial_match = re.match(r"^\d{4,6}$", s)
    if serial_match:
        try:
            serial = int(s)
            # Excelの基準日: 1899/12/30（うるう年バグ込みで合わせる）
            base = datetime(1899, 12, 30)
            dt = base + timedelta(days=serial)
            warning = f"Excelシリアル番号 {serial} を {dt:%Y/%m/%d} に変換しました。"
            logger.info(warning)
            return dt.strftime("%Y/%m/%d"), warning
        except (ValueError, OverflowError):
            pass

    return s, None


def normalize_design_status(value: str) -> tuple[str, str | None]:
    """設計確定度を許容値に正規化する。

    "確定" / "仮" / "未定" 以外は "未定" に倒す。
    """
    if not value:
        return "未定", None
    s = str(value).strip()
    if s in ALLOWED_DESIGN_STATUS:
        return s, None
    warning = f"設計確定度「{value}」は許容値外のため「未定」に補正しました。"
    logger.warning(warning)
    return "未定", warning


# =====================================================================
# メインエントリ: validate_and_correct
# =====================================================================

def validate_and_correct(raw: dict) -> tuple[dict, list[str], dict]:
    """抽出辞書全体を検証＆補正する。

    入力 dict はディープコピーされ、原データは変更されない。

    Args:
        raw: Claude API 返却の dict（project / equipment / high_voltage 等を含む）

    Returns:
        (corrected_dict, warnings, confidence_updates)
            - corrected_dict: 補正後の dict
            - warnings: 補正・検証ログのリスト
            - confidence_updates: フィールドパス → "high"/"medium"/"low"
    """
    if not isinstance(raw, dict):
        return raw, [], {}

    corrected = copy.deepcopy(raw)
    warnings: list[str] = []
    confidence_updates: dict[str, str] = {}

    # ---- project ----
    project = corrected.setdefault("project", {})
    if not isinstance(project, dict):
        project = {}
        corrected["project"] = project

    # 住所 ↔ 郵便番号の整合
    addr = project.get("address") or ""
    pc = project.get("postal_code") or ""
    new_addr, new_pc = correct_address(addr, pc)
    if new_addr != addr:
        warnings.append(f"住所から郵便番号を抽出して整理しました: '{addr}' → '{new_addr}'")
        project["address"] = new_addr
    if new_pc != pc:
        warnings.append(f"郵便番号を住所から補完しました: '{pc}' → '{new_pc}'")
        project["postal_code"] = new_pc

    # 郵便番号正規化
    if project.get("postal_code"):
        formatted, warn, conf = validate_postal_code(project["postal_code"])
        if formatted != project["postal_code"]:
            project["postal_code"] = formatted
        if warn:
            warnings.append(warn)
        if conf:
            confidence_updates["project.postal_code"] = conf
    else:
        confidence_updates["project.postal_code"] = "low"

    # 調査日
    if project.get("survey_date"):
        normalized_date, warn = normalize_date(project["survey_date"])
        if normalized_date != project["survey_date"]:
            warnings.append(
                f"調査日を正規化しました: '{project['survey_date']}' → '{normalized_date}'"
            )
            project["survey_date"] = normalized_date
        if warn:
            warnings.append(warn)

    # ---- equipment ----
    equipment = corrected.setdefault("equipment", {})
    if not isinstance(equipment, dict):
        equipment = {}
        corrected["equipment"] = equipment

    # メーカー名正規化 + 型式→メーカー逆引き
    maker = (equipment.get("module_maker") or "").strip()
    model = (equipment.get("module_model") or "").strip()
    if maker:
        normalized_maker = normalize_maker(maker)
        if normalized_maker != maker:
            warnings.append(f"メーカー名を正規化しました: '{maker}' → '{normalized_maker}'")
            equipment["module_maker"] = normalized_maker
            confidence_updates["equipment.module_maker"] = "high"
    elif model:
        inferred = infer_module_maker(model)
        if inferred:
            warnings.append(f"型式 '{model}' からメーカー '{inferred}' を推定しました。")
            equipment["module_maker"] = inferred
            confidence_updates["equipment.module_maker"] = "medium"
        else:
            confidence_updates["equipment.module_maker"] = "low"
    else:
        confidence_updates["equipment.module_maker"] = "low"

    # モジュール出力Wの典型値スナップ
    if equipment.get("module_output_w"):
        snapped, warn, conf = validate_module_output_w(equipment["module_output_w"])
        if snapped != _safe_float(equipment["module_output_w"]):
            equipment["module_output_w"] = snapped
        if warn:
            warnings.append(warn)
        if conf:
            confidence_updates["equipment.module_output_w"] = conf
    else:
        confidence_updates["equipment.module_output_w"] = "low"

    # PV容量の整合性
    pv_kw_corrected, warn, conf = validate_pv_capacity_consistency(equipment)
    if warn:
        warnings.append(warn)
    if pv_kw_corrected and pv_kw_corrected != _safe_float(equipment.get("pv_capacity_kw")):
        equipment["pv_capacity_kw"] = pv_kw_corrected
    if conf:
        confidence_updates["equipment.pv_capacity_kw"] = conf

    # 設計確定度
    if "design_status" in equipment:
        ds_corrected, warn = normalize_design_status(equipment["design_status"])
        if ds_corrected != equipment["design_status"]:
            equipment["design_status"] = ds_corrected
        if warn:
            warnings.append(warn)

    # ---- high_voltage ----
    hv = corrected.get("high_voltage")
    if isinstance(hv, dict):
        for sep_key in ("separation_ns_mm", "separation_ew_mm"):
            if sep_key in hv and hv[sep_key] is not None:
                mm, warn = validate_separation(hv[sep_key])
                if mm != _safe_float(hv[sep_key]):
                    hv[sep_key] = mm
                if warn:
                    warnings.append(f"{sep_key}: {warn}")

    # ---- confirmation ----
    confirmation = corrected.get("confirmation")
    if isinstance(confirmation, dict):
        for date_key in ("surveyor_date", "design_review_date", "works_review_date"):
            if confirmation.get(date_key):
                nd, warn = normalize_date(confirmation[date_key])
                if nd != confirmation[date_key]:
                    warnings.append(
                        f"{date_key} を正規化しました: '{confirmation[date_key]}' → '{nd}'"
                    )
                    confirmation[date_key] = nd
                if warn:
                    warnings.append(warn)

    return corrected, warnings, confidence_updates


# =====================================================================
# 動作確認用テスト
# =====================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("=" * 70)
    print("テストケース1: 典型的な現調シート（Canadian Solar 660W × 288枚）")
    print("=" * 70)
    case1 = {
        "project": {
            "project_name": "◯◯工業株式会社 太陽光発電設備",
            "address": "〒530-0001 大阪府大阪市北区梅田1-2-3",
            "postal_code": "",
            "survey_date": "R7.12.18",
        },
        "equipment": {
            "module_maker": "Canadian Solar Inc.",
            "module_model": "CS7L-MS",
            "module_output_w": 661,  # 誤読: 660に補正されるべき
            "planned_panels": 288,
            "pv_capacity_kw": 190.08,
            "design_status": "確定",
        },
        "high_voltage": {
            "separation_ns_mm": "3m",  # 単位付き → 3000mm
            "separation_ew_mm": 30,    # cm推定 → 300mm? (10〜100の範囲)
        },
    }
    corrected1, warnings1, conf1 = validate_and_correct(case1)
    print("補正後 module_output_w:", corrected1["equipment"]["module_output_w"])
    print("補正後 module_maker  :", corrected1["equipment"]["module_maker"])
    print("補正後 postal_code    :", corrected1["project"]["postal_code"])
    print("補正後 address        :", corrected1["project"]["address"])
    print("補正後 survey_date    :", corrected1["project"]["survey_date"])
    print("補正後 separation_ns  :", corrected1["high_voltage"]["separation_ns_mm"])
    print("warnings count:", len(warnings1))
    for w in warnings1:
        print("  -", w)
    print("confidence_updates:", conf1)

    assert corrected1["equipment"]["module_output_w"] == 660, "661→660 スナップ失敗"
    assert corrected1["equipment"]["module_maker"] == "Canadian Solar", "メーカー正規化失敗"
    assert corrected1["project"]["postal_code"] == "530-0001", "郵便番号抽出失敗"
    assert "〒" not in corrected1["project"]["address"], "住所から郵便番号除去失敗"
    assert corrected1["project"]["survey_date"] == "2025/12/18", "令和→西暦変換失敗"
    assert corrected1["high_voltage"]["separation_ns_mm"] == 3000, "離隔距離変換失敗"
    print("[OK] テストケース1 全アサート通過")

    print()
    print("=" * 70)
    print("テストケース2: メーカー空＋型式から推定（LONGi LR5-72HTH-580M）")
    print("=" * 70)
    case2 = {
        "project": {
            "address": "東京都千代田区丸の内1-1-1",
            "postal_code": "1000005",  # ハイフンなし → 100-0005
        },
        "equipment": {
            "module_maker": "",
            "module_model": "LR5-72HTH-580M",
            "module_output_w": 580,  # 典型値
            "planned_panels": 100,
            "pv_capacity_kw": 100.0,  # 計算値58.0kWと大きく乖離 → 補正されるべき
            "design_status": "未確定",  # 許容外 → "未定" に補正
        },
        "confirmation": {
            "surveyor_date": "2025年9月29日",
        },
    }
    corrected2, warnings2, conf2 = validate_and_correct(case2)
    print("補正後 module_maker  :", corrected2["equipment"]["module_maker"])
    print("補正後 pv_capacity_kw:", corrected2["equipment"]["pv_capacity_kw"])
    print("補正後 design_status :", corrected2["equipment"]["design_status"])
    print("補正後 postal_code   :", corrected2["project"]["postal_code"])
    print("補正後 surveyor_date :", corrected2["confirmation"]["surveyor_date"])
    print("warnings count:", len(warnings2))
    for w in warnings2:
        print("  -", w)
    print("confidence_updates:", conf2)

    assert corrected2["equipment"]["module_maker"] == "LONGi", "型式推定失敗"
    assert corrected2["equipment"]["pv_capacity_kw"] == 58.0, "PV容量整合補正失敗"
    assert corrected2["equipment"]["design_status"] == "未定", "design_status正規化失敗"
    assert corrected2["project"]["postal_code"] == "100-0005", "郵便番号整形失敗"
    assert corrected2["confirmation"]["surveyor_date"] == "2025/09/29", "和暦日付変換失敗"
    assert conf2.get("equipment.module_maker") == "medium", "推定信頼度設定失敗"
    assert conf2.get("equipment.pv_capacity_kw") == "low", "PV容量整合度low設定失敗"
    print("[OK] テストケース2 全アサート通過")

    print()
    print("=" * 70)
    print("テストケース3: 郵便番号不正＋PV容量空（計算値で補完）")
    print("=" * 70)
    case3 = {
        "project": {
            "address": "神奈川県横浜市西区高島2-19-12",
            "postal_code": "ABC1234",  # 数字3桁しかない → low
            "survey_date": "46294",  # Excelシリアル(2026/9/15相当)
        },
        "equipment": {
            "module_maker": "ジンコ",
            "module_model": "JKM440M-54HL4",
            "module_output_w": 440,
            "planned_panels": 200,
            "pv_capacity_kw": 0,  # 空 → 計算値88で補完
        },
    }
    corrected3, warnings3, conf3 = validate_and_correct(case3)
    print("補正後 module_maker  :", corrected3["equipment"]["module_maker"])
    print("補正後 pv_capacity_kw:", corrected3["equipment"]["pv_capacity_kw"])
    print("補正後 postal_code   :", corrected3["project"]["postal_code"])
    print("補正後 survey_date   :", corrected3["project"]["survey_date"])
    print("warnings count:", len(warnings3))
    for w in warnings3:
        print("  -", w)
    print("confidence_updates:", conf3)

    assert corrected3["equipment"]["module_maker"] == "Jinko", "ジンコ→Jinko正規化失敗"
    assert corrected3["equipment"]["pv_capacity_kw"] == 88.0, "PV容量計算補完失敗"
    assert conf3.get("project.postal_code") == "low", "郵便番号不正→low設定失敗"
    # シリアル番号変換の確認（厳密な日付値はテストせず、形式のみ確認）
    assert re.match(r"^\d{4}/\d{2}/\d{2}$", corrected3["project"]["survey_date"]), \
        "Excelシリアル変換失敗"
    print("[OK] テストケース3 全アサート通過")

    print()
    print("=" * 70)
    print("全テストケース完走")
    print("=" * 70)
