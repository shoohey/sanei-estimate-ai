"""客出し見積 v2 ビルダー（2026-08-15 顧客ルールブック【見積側】準拠）

大分類（固定・3条）:
    1. 共通仮設工事 / 2. 太陽光発電システム機器 / 3. 電材 / 4. 設置工事

設計原則:
- 図面側の設計確定情報を正とする（1条）。枚数・容量・PCS台数を再判断しない。
- 「支給品」は大分類ではなく明細の属性（4条）。支給品でも明細は機器カテゴリに
  残し、備考「御支給品」・金額0円とする。工事費は0円にしない。
- 機器（モジュール/PCS/GW等）は設計確定情報＋単価マスターから動的に拾い出す（5条）。
  単価マスターは「原価」、margin_rate（粗利率）を乗せて客出し単価にする（6条）。
- 不足情報は勝手に補完せず、手動入力＋「要確認」とする（7条）。
"""
from __future__ import annotations

import logging
from typing import Optional

from models.estimate_data import (
    CategorySection,
    CategoryType,
    EstimateData,
    LineItem,
    LineItemReasoning,
    PricingMethod,
)
from models.survey_data import SurveyData

logger = logging.getLogger(__name__)

UNCONFIRMED = "未確認"


def tax_amount(total_before_tax: int, rules: dict) -> int:
    """税額を v1 と同じ丸め設定（tax_rate / tax_rounding_method）で計算する。"""
    import math

    from config import TAX_RATE
    tax_raw = total_before_tax * float(rules.get("tax_rate", TAX_RATE) or TAX_RATE)
    method = rules.get("tax_rounding_method", "floor")
    if method == "round":
        return int(round(tax_raw))
    if method == "ceil":
        return int(math.ceil(tax_raw))
    return int(tax_raw)


def _parse_count(text: str, unit: str) -> int:
    """「8枚」「東4枚・西4枚（合計8枚）」等の数量文字列から確定数を取り出す。

    全数字の連結（東4枚・西4枚→448）を防ぐため単位付きで照合する。
    - 「合計/計 N<unit>」があればそれを採用
    - 「N<unit>」が1箇所だけならそれを採用
    - 単位なしで文字列全体が数字ならそれを採用
    - 内訳が複数あって合計表記が無い等の曖昧なケースは 0（不採用。
      勝手に解釈せず呼び出し側のフォールバックに委ねる）
    """
    import re

    s = str(text or "").strip()
    if not s:
        return 0
    m = re.search(rf"(?:合計|計)\s*(\d+)\s*{unit}", s)
    if m:
        return int(m.group(1))
    nums = re.findall(rf"(\d+)\s*{unit}", s)
    if len(nums) == 1:
        return int(nums[0])
    if not nums and s.isdigit():
        return int(s)
    return 0


def _sell_price(cost: Optional[int], margin_rate: float) -> Optional[int]:
    """原価 → 客出し単価（粗利率を乗せて100円単位に丸め）。"""
    if cost is None:
        return None
    if margin_rate <= 0:
        return int(cost)
    return int(round(cost * (1.0 + margin_rate) / 100.0) * 100)


def _master_price(model: str, description: str = "") -> tuple[Optional[int], str]:
    """単価マスター（原価）から型式で単価を探す（完全一致のみ）。

    金額に直結するため曖昧一致（fuzzy）は使わない。部分型式（例 SUN2000）で
    別型式の価格を拾う事故を防ぎ、見つからなければ手動入力行に落とす
    （Codexレビュー指摘）。
    """
    try:
        from product import price_master as pm
    except Exception:
        return None, ""
    q = (model or "").strip()
    if not q:
        return None, ""
    try:
        hits = pm.find_by_model(q, fuzzy=False)
    except Exception:
        hits = []
    for p in hits:
        try:
            v = p.get("unit_price")
            if v:
                return int(v), f"単価マスター: {p.get('model') or p.get('name', '')}"
        except (TypeError, ValueError):
            continue
    return None, ""


def _manual_item(no: int, description: str, remarks: str = "",
                 note: str = "") -> LineItem:
    return LineItem(
        no=no, description=description, remarks=remarks,
        quantity="1式", quantity_value=1, quantity_unit="式",
        unit_price=0, amount=0, is_manual_input=True,
        reasoning=LineItemReasoning(
            method=PricingMethod.MANUAL,
            formula="手動入力が必要です",
            source=note or "単価マスターに該当製品が見つかりません",
            note="金額を手動で入力してください",
        ),
    )


def _yaml_list_section(items_def: list, category: CategoryType,
                       category_number: int, margin_rate: float,
                       survey: SurveyData) -> CategorySection:
    """YAML定義リスト（setup/wiring/install）からセクションを構築する。

    v1 の材料費ビルダーと同じ仕様（fixed / is_manual / condition）を最小限
    サポートする。単価はYAMLの原価に margin_rate を乗せて客出しにする。
    """
    from pricing.pricing_engine import _evaluate_condition, _resolve_quantity

    section = CategorySection(category=category, category_number=category_number)
    for item_def in items_def or []:
        condition = item_def.get("condition", "")
        if condition and not _evaluate_condition(condition, survey):
            continue
        is_manual = bool(item_def.get("is_manual", False))
        quantity_str, quantity_val = _resolve_quantity(item_def, survey)
        cost = int(item_def.get("unit_price", 0) or 0)
        # 学習単価（learned_price タグ）は正規見積の客出し実績値のため、
        # 粗利率を重ね掛けしない（Codexレビュー指摘: 学習補正が上振れする）
        learned = bool(item_def.get("learned_price", False))
        price = cost if learned else (_sell_price(cost, margin_rate) or 0)
        amount = int((quantity_val or 0) * price) if not is_manual else 0
        if is_manual:
            reasoning = LineItemReasoning(
                method=PricingMethod.MANUAL,
                formula="手動入力が必要です",
                source=item_def.get("note", ""),
                note="金額を手動で入力してください",
            )
        else:
            if learned:
                margin_note = "学習済み単価（客出し実績値・粗利適用なし）"
            elif margin_rate > 0:
                margin_note = f"原価¥{cost:,} × 粗利{margin_rate:.0%}"
            else:
                margin_note = "原価ベース"
            reasoning = LineItemReasoning(
                method=PricingMethod.FIXED,
                formula=(f"{quantity_str}{item_def.get('quantity_unit', '')} × "
                         f"¥{price:,} = ¥{amount:,}") if price else "¥0（金額なし）",
                source=f"標準単価（{margin_note}）",
                note=item_def.get("note", ""),
            )
        unit = item_def.get("quantity_unit", "")
        section.items.append(LineItem(
            no=item_def.get("no", len(section.items) + 1),
            description=item_def.get("description", ""),
            remarks=item_def.get("remarks", ""),
            quantity=f"{quantity_str}{unit}" if quantity_str else "",
            quantity_value=quantity_val,
            quantity_unit=unit,
            unit_price=price,
            amount=amount,
            is_manual_input=is_manual,
            reasoning=reasoning,
        ))
    section.calculate_totals()
    return section


def _build_equipment_section(survey: SurveyData, handoff: dict,
                             margin_rate: float) -> CategorySection:
    """2. 太陽光発電システム機器 — 設計確定情報から動的に拾い出す（5条）。

    - モジュール: 型式 × 実配置枚数 × 単価マスター原価（＋粗利）
    - PCS: 型式 × 台数 × 単価マスター原価
    - 架台・Gateway・CT・計測機器: handoff/現調に情報があれば行を立てる
      （単価不明は手動入力＋要確認）
    - 図面側の枚数・容量は再判断しない（1条）
    """
    section = CategorySection(category=CategoryType.EQUIPMENT, category_number=2)
    handoff = handoff or {}
    no = 0

    def _known(key: str) -> str:
        v = str(handoff.get(key, "") or "").strip()
        return "" if v == UNCONFIRMED else v

    # --- モジュール ---
    eq = survey.equipment
    panels = int(eq.planned_panels or 0)
    model = (eq.module_model or "").strip()
    maker = (eq.module_maker or "").strip()
    # 設計確定情報があれば優先（図面側が正）
    if _known("モジュール型式"):
        model = _known("モジュール型式")
    if _known("モジュールメーカー"):
        maker = _known("モジュールメーカー")
    hp = _parse_count(_known("モジュール枚数"), "枚")
    if hp > 0:
        panels = hp
    # 図面側（設計確定情報あり）が枚数「未確認」の場合、現調の指示枚数を
    # 確定値として扱わない（1条: 図面側が正。Codexレビュー指摘）
    count_unconfirmed = bool(handoff) and str(
        handoff.get("モジュール枚数", "") or "").strip() == UNCONFIRMED
    if panels > 0 or model:
        no += 1
        desc = "太陽光モジュール"
        remarks = " ".join(x for x in (maker, model) if x)
        cost, source = (None, "") if count_unconfirmed \
            else _master_price(model, desc)
        price = _sell_price(cost, margin_rate)
        if count_unconfirmed:
            it = _manual_item(
                no, desc, remarks,
                note="図面側でモジュール枚数が未確認（設計確認のうえ入力してください）")
            it.quantity = f"{panels}枚（仮）" if panels else "1式"
            it.quantity_value = panels or 1
            it.quantity_unit = "枚" if panels else "式"
            section.items.append(it)
        elif price is not None:
            amount = panels * price if panels > 0 else 0
            section.items.append(LineItem(
                no=no, description=desc, remarks=remarks,
                quantity=f"{panels}枚" if panels else "",
                quantity_value=panels, quantity_unit="枚",
                unit_price=price, amount=amount,
                reasoning=LineItemReasoning(
                    method=PricingMethod.FIXED,
                    formula=f"{panels}枚 × ¥{price:,} = ¥{amount:,}",
                    source=source,
                    note="設計確定情報の枚数を使用（図面側が正）",
                ),
            ))
        else:
            it = _manual_item(no, desc, remarks,
                              note="単価マスター未登録（単価を入力してください）")
            it.quantity = f"{panels}枚" if panels else "1式"
            it.quantity_value = panels or 1
            it.quantity_unit = "枚" if panels else "式"
            section.items.append(it)

    # --- PCS ---
    pcs_model = _known("PCS型式")
    pcs_count = _parse_count(_known("PCS台数"), "台")
    if not pcs_model and (pcs_count > 0 or _known("PCS容量")
                          or _known("PCSメーカー")):
        # 型式は未確認だがPCSの存在は確定している → 手動入力行を立てる
        # （7条: 補完はしないが、明細の欠落もさせない）
        no += 1
        it = _manual_item(
            no, "パワーコンディショナ（PCS）",
            " ".join(x for x in (_known("PCSメーカー"), _known("PCS容量")) if x),
            note="PCS型式が未確認（型式・単価を確認してください）")
        qty = pcs_count or 1
        it.quantity = f"{qty}台"
        it.quantity_value = qty
        it.quantity_unit = "台"
        section.items.append(it)
    if pcs_model:
        no += 1
        # 台数が確定しているときだけ金額を計上する。台数未確認のまま
        # 「1台」で確定計上しない（未確認は補完しない — Codexレビュー指摘）
        cost, source = ((None, "") if pcs_count <= 0
                        else _master_price(pcs_model, "パワーコンディショナ"))
        price = _sell_price(cost, margin_rate)
        if pcs_count <= 0:
            it = _manual_item(
                no, "パワーコンディショナ（PCS）", pcs_model,
                note="PCS台数が未確認（台数・単価を確認のうえ入力してください）")
            it.quantity = "1台（仮）"
            it.quantity_value = 1
            it.quantity_unit = "台"
            section.items.append(it)
        elif price is not None:
            qty = pcs_count
            section.items.append(LineItem(
                no=no, description="パワーコンディショナ（PCS）",
                remarks=pcs_model,
                quantity=f"{qty}台", quantity_value=qty, quantity_unit="台",
                unit_price=price, amount=qty * price,
                reasoning=LineItemReasoning(
                    method=PricingMethod.FIXED,
                    formula=f"{qty}台 × ¥{price:,} = ¥{qty * price:,}",
                    source=source,
                    note="設計確定情報のPCSを使用（図面側が正）",
                ),
            ))
        else:
            it = _manual_item(no, "パワーコンディショナ（PCS）", pcs_model,
                              note="単価マスター未登録（単価を入力してください）")
            it.quantity = f"{pcs_count}台"
            it.quantity_value = pcs_count
            it.quantity_unit = "台"
            section.items.append(it)

    # PCSは太陽光システムの必須機器。設計確定情報が無い現調のみのフロー
    # （直接入力・PDF読取）でも明細を欠落させず、不明なら手動行を立てる
    # （Codexレビュー指摘: v2既定化で現調フローのPCS行が消えていた）
    if not any("パワーコンディショナ" in it.description
               for it in section.items):
        no += 1
        loc = ""
        try:
            if survey.high_voltage.pcs_space and survey.high_voltage.pcs_location:
                loc = f"設置場所: {survey.high_voltage.pcs_location.value}"
        except Exception:
            loc = ""
        it = _manual_item(
            no, "パワーコンディショナ（PCS）", loc,
            note="PCS型式・台数が未確認（確認のうえ入力してください）")
        it.quantity = "1台（仮）"
        it.quantity_value = 1
        it.quantity_unit = "台"
        section.items.append(it)

    # --- 架台 ---
    no += 1
    mount = _known("架台・固定方法")
    section.items.append(_manual_item(
        no, "架台", mount,
        note="架台・固定方法から選定（単価マスター参照または手動入力）"))

    # --- Gateway・CT・計測機器（現調注記に記載がある場合のみ行を立てる） ---
    others = _known("その他現調条件")
    gw_note = _known("PCS設置位置")
    if "GATEWAY" in (others + gw_note).upper() or "ゲートウェイ" in (others + gw_note):
        no += 1
        section.items.append(_manual_item(
            no, "Gateway・計測機器", "",
            note="現調注記より（型式・単価を確認してください）"))

    section.calculate_totals()
    return section


def _apply_handoff_supply(section: CategorySection, handoff: dict) -> None:
    """設計確定情報「支給品」の反映（4条: 属性として¥0＋備考「御支給品」）。

    図面側で支給品が確定している機器を、UIチェックを待たずに自動で¥0にする
    （Codexレビュー指摘: 引き継いだのに購入品として過大計上される）。
    呼び出し前に購入価格つきスナップショットを控えること（UIの復元用）。
    """
    import re

    supplied = str((handoff or {}).get("支給品", "") or "").strip()
    if not supplied or supplied in (UNCONFIRMED, "なし", "無し", "無"):
        return

    # 否定表現（「モジュール支給なし」「PCSは支給品ではない」等）を支給と
    # 誤読しないよう、文の区切りごとに機器名＋否定語の有無を見る（Codexレビュー
    # 指摘）。中黒（・）は「PCS・モジュールは支給品ではない」のような列挙で
    # 否定のスコープを共有するため、分割文字に含めない
    _NEGATIONS = ("なし", "無し", "無", "ではない", "しない", "せず",
                  "非支給", "除く", "除外", "対象外")
    _segments = [s for s in re.split(r"[、。,/／\n]", supplied) if s.strip()]

    def _component_supplied(*keywords: str) -> bool:
        hit = False
        for seg in _segments:
            seg_upper = seg.upper()
            if not any(k.upper() in seg_upper for k in keywords):
                continue
            if any(n in seg for n in _NEGATIONS):
                return False  # 明示的な否定が最優先
            hit = True
        return hit

    def _handoff_supplied(desc: str) -> bool:
        if "モジュール" in desc:
            return _component_supplied("モジュール", "パネル")
        if "パワーコンディショナ" in desc:
            return _component_supplied("PCS", "パワコン", "パワーコンディショナ")
        if desc == "架台":
            return _component_supplied("架台")
        if "Gateway" in desc:
            return _component_supplied("GATEWAY", "ゲートウェイ")
        return False

    for it in section.items:
        if _handoff_supplied(it.description):
            it.unit_price = 0
            it.amount = 0
            it.is_manual_input = False
            if "御支給品" not in (it.remarks or ""):
                it.remarks = (f"{it.remarks}\n御支給品" if it.remarks
                              else "御支給品")
            it.reasoning = LineItemReasoning(
                method=PricingMethod.SUPPLIED,
                formula="御支給品のため ¥0",
                source=f"設計確定情報「支給品」: {supplied}",
                note="機器は支給・設置工事は計上（工事費は0円にしない）",
            )
    section.calculate_totals()


def generate_estimate_v2(survey: SurveyData, rules: dict,
                         client_name: str = "",
                         handoff: Optional[dict] = None) -> EstimateData:
    """客出し見積 v2（4大分類）を生成する。

    v1 の generate_estimate と同じ EstimateData を返すため、
    プレビュー・PDF・CSV・履歴・学習は無改修で動作する。
    """
    from datetime import date, timedelta

    from config import COMPANY_INFO, generate_estimate_id

    margin_rate = float(rules.get("margin_rate", 0.0) or 0.0)
    handoff = dict(handoff or {})

    estimate = EstimateData()
    estimate.cover.estimate_id = generate_estimate_id()
    estimate.cover.issue_date = date.today().strftime("%Y/%m/%d")
    estimate.cover.validity_period = (
        date.today() + timedelta(days=30)).strftime("%Y/%m/%d")
    estimate.cover.client_name = client_name
    project_name = str(handoff.get("案件名", "") or "").strip()
    if not project_name or project_name == UNCONFIRMED:
        project_name = survey.project.project_name
    # 表紙のkWも設計確定情報を正とする（1条。図面側の実配置容量＞現調入力値）。
    # 図面側が容量を確定できなかった案件では現調値を流用せず「容量未確認」と
    # 明示する（Codexレビュー指摘）
    hv = str(handoff.get("PV容量", "") or "").strip()
    if hv == UNCONFIRMED:
        capacity_disp = "容量未確認"
    else:
        capacity_kw = survey.equipment.pv_capacity_kw
        if hv:
            try:
                capacity_kw = float(
                    hv.replace("kW", "").replace("ｋＷ", "").strip())
            except ValueError:
                pass
        capacity_disp = f"{capacity_kw}kW"
    estimate.cover.project_name = (
        f"{project_name}　太陽光設置工事 見積"
        f"（{capacity_disp}）")
    estimate.cover.project_location = survey.project.address
    estimate.cover.representative = COMPANY_INFO["default_representative"]

    equipment = _build_equipment_section(survey, handoff, margin_rate)
    # 支給品自動適用の前に購入価格つき原本を控える（UIでOFFに戻せるように。
    # Codexレビュー指摘: ¥0化後のスナップショットでは購入価格を復元できない）
    import copy as _copy
    estimate.equipment_purchase_snapshot = _copy.deepcopy(equipment.items)
    _apply_handoff_supply(equipment, handoff)

    categories = [
        _yaml_list_section(rules.get("setup_items"), CategoryType.SETUP,
                           1, margin_rate, survey),
        equipment,
        _yaml_list_section(rules.get("wiring_items"), CategoryType.WIRING,
                           3, margin_rate, survey),
        _yaml_list_section(rules.get("install_items"), CategoryType.INSTALL,
                           4, margin_rate, survey),
    ]

    estimate.summary.categories = categories
    estimate.summary.subtotal = sum(c.total for c in categories)

    # 値引き（v1 と同じ切り捨て方式）
    discount_method = rules.get("discount_method", "round_down_10000")
    subtotal = estimate.summary.subtotal
    if discount_method == "round_down_10000":
        total_before_tax = (subtotal // 10000) * 10000
    elif discount_method == "round_down_100000":
        total_before_tax = (subtotal // 100000) * 100000
    else:
        total_before_tax = subtotal
    estimate.summary.discount = total_before_tax - subtotal
    estimate.summary.total_before_tax = total_before_tax
    estimate.summary.tax = tax_amount(total_before_tax, rules)
    estimate.summary.total_with_tax = total_before_tax + estimate.summary.tax
    estimate.cover.total_before_tax = total_before_tax
    estimate.cover.tax = estimate.summary.tax
    estimate.cover.total_with_tax = estimate.summary.total_with_tax

    # 根拠一覧
    for cat in categories:
        for it in cat.items:
            if it.reasoning and it.reasoning.formula:
                estimate.reasoning_list.append(
                    f"[{cat.category.value}] {it.description}"
                    + (f" {it.remarks}" if it.remarks else "")
                    + f": {it.reasoning.formula}"
                    + (f" （{it.reasoning.source}）" if it.reasoning.source else ""))

    # 設計確定情報の未確認・設計確認事項を根拠に記録（7条: 要確認の明示）
    unconfirmed = [k for k, v in handoff.items()
                   if not str(k).startswith("_") and str(v) == UNCONFIRMED]
    if unconfirmed:
        estimate.reasoning_list.append(
            "【要確認】設計確定情報が未確認の項目: " + "、".join(unconfirmed))
    if handoff.get("_配置不可"):
        estimate.reasoning_list.append(
            "【設計確認】図面側が「指示枚数配置不可」の状態です。"
            "配置条件を確定してから見積を確定してください。")
    return estimate
