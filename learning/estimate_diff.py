"""AI見積と正規見積の差分抽出（見積の学習）

ParsedEstimate 同士（AI生成版 / 人が修正した正規版）を比較し、
EstimateDiffItem のリストを返す。

設計方針（docs/LEARNING_LOOP_DESIGN.md §10）:
- 学習可能な差分（単価変更・項目追加・項目削除）には store.add_rules へ
  そのまま渡せる proposed_rule を付ける。
- 数量差分は案件固有（PV容量・離隔距離で変わる）のため参考表示のみ
  （learnable=False, proposed_rule=None）。誤学習で全案件が壊れることを防ぐ。
- 支給品カテゴリの単価差分は学習しない（支給品は常に ¥0 計上のため）。
- 値引き行・小計行・合計行はパーサー側で items に入れない前提だが防御的に除外。
- 特記事項・カテゴリ不明の項目追加は反映先リストを特定できないため参考表示に落とす。
- 備考(remarks)違いの同名項目（例: pricing_rules.yaml 材料費「PVケーブル間」×5）は
  摘要キーの学習ルールが同名全項目に波及するため、備考まで完全一致でペアリング
  できた単価差分以外は参考表示に落とす。unit_price_override / item_suppress の
  ルールには match_remarks（正規化備考）と payload.old_unit_price を必ず含め、
  apply_estimate 側の複合照合（誤爆防止）に使う。
- 金額・単価がともに 0/None の行（クレーン費・外部足場等、条件不成立や手動入力
  前提の¥0行）の削除差分は学習せず参考表示（一括承認で恒久サプレスされる事故防止）。
- 単価欄が空で金額のみ記載の追加項目は 金額÷数量 で単価を補完する。
  補完もできない場合は参考表示（apply 側で¥0項目として登録される事故防止）。
"""
import difflib
import unicodedata
from collections import Counter
from datetime import datetime

from learning.models import ESTIMATE_CATEGORIES, EstimateDiffItem, ParsedEstimate

# fuzzy マッチとみなす類似度の下限（difflib.SequenceMatcher.ratio）
_FUZZY_THRESHOLD = 0.6
_EPS = 1e-6

# 明細差分の対象外とする行のキーワード（値引き・小計・合計など。防御的除外）
_EXCLUDED_KEYWORDS = ("値引", "小計", "合計", "消費税", "総計")

# item_add ルールの反映先を特定できるカテゴリ（特記事項は pricing_rules.yaml に
# 対応リストが無いため学習対象外。apply_estimate.CATEGORY_TO_LIST と対）
_ADDABLE_CATEGORIES = tuple(c for c in ESTIMATE_CATEGORIES if c != "特記事項")

# 項目構成（どの項目が載るか）を学習で変更しないカテゴリ（2026-08-13 顧客要望）。
# 例外案件の item_add / item_suppress がデフォルト化する事故を防ぐ。
# 単価学習（unit_price_override）は引き続き対象。
_FIXED_STRUCTURE_CATEGORIES = ("支給品", "材料費")

# 同一カテゴリ内に同名（正規化摘要が同一）項目が複数ある場合の注記
_DUP_NOTE = "（同名項目が複数あるため自動学習の対象外）"


# =============================================================
# 正規化
# =============================================================

def normalize_desc(s: str) -> str:
    """摘要文字列を比較用に正規化する。

    NFKC 正規化（全角英数→半角等）→ 小文字化 → 空白・記号
    （・、。()（）　等、英数字・かな・漢字以外の文字）を除去する。
    例: "ＰＶケーブル（間）" → "pvケーブル間"

    apply_estimate 側も同じ関数で pricing_rules.yaml の摘要を正規化して
    突き合わせるため、両者の表記揺れが吸収される。
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).lower()
    # isalnum() は漢字・かな・長音記号（ー）も True。空白・記号だけが落ちる
    return "".join(c for c in s if c.isalnum())


# =============================================================
# 内部ユーティリティ
# =============================================================

def _fmt_qty(v) -> str:
    """数量の表示用文字列（整数なら小数点を出さない）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _is_target_item(item) -> bool:
    """明細差分の対象行か（値引き・小計・合計・摘要空行は防御的に除外）。"""
    desc = item.description or ""
    if not normalize_desc(desc):
        return False
    return not any(kw in desc for kw in _EXCLUDED_KEYWORDS)


def _evidence(official: ParsedEstimate, summary: str) -> dict:
    """proposed_rule に付ける根拠情報（どの案件・ファイルから学んだか）。"""
    return {
        "project_name": official.project_name,
        "file_name": official.file_name,
        "summary": summary,
        "learned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def _make_rule(kind: str, category: str, match_description: str,
               display_description: str, payload: dict,
               summary: str, official: ParsedEstimate,
               match_remarks: str = "") -> dict:
    """store.add_rules にそのまま渡せる LearnedRule dict を組み立てる。

    match_remarks は正規化済み備考。備考違いの同名項目（材料費「PVケーブル間」×5
    等）を apply_estimate 側で1項目に特定するための複合照合キー。
    """
    return {
        "target": "estimate",
        "kind": kind,
        "category": category,
        "match_description": match_description,
        "match_remarks": match_remarks,
        "display_description": display_description,
        "payload": payload,
        "evidence": _evidence(official, summary),
        "enabled": True,
        "applied_count": 0,
    }


def _match_items(ai_items: list, of_items: list) -> tuple:
    """AI明細と正規明細の対応付け。

    備考違いの同名項目（材料費「PVケーブル間」×5等）でペアがずれて偽の
    単価差分が出ないよう、完全一致を3段階に分けて厳しい順に対応付ける:
    ①同カテゴリ内で (正規化摘要, 正規化備考) の完全一致
    ②同カテゴリ内で正規化摘要一致 + 単価一致（備考表記が揺れた場合のタイブレーク）
    ③同カテゴリ内で正規化摘要の完全一致
    ④同カテゴリ内 fuzzy（ratio >= 0.6、スコア降順の貪欲一意割当）
    ⑤カテゴリ不明("")の項目は全カテゴリ相手に同様（①〜④）。

    Returns:
        (pairs, rest_ai, rest_of)
        pairs は (ai_item, official_item, match_score) のリスト。
        rest_ai / rest_of は対応相手が見つからなかった明細のリスト。
    """
    ai_pool = list(range(len(ai_items)))
    of_pool = list(range(len(of_items)))
    ai_norm = [normalize_desc(it.description) for it in ai_items]
    of_norm = [normalize_desc(it.description) for it in of_items]
    ai_rem = [normalize_desc(it.remarks) for it in ai_items]
    of_rem = [normalize_desc(it.remarks) for it in of_items]
    pairs = []

    def _cat_ok(i: int, j: int, cross_category: bool) -> bool:
        if cross_category:
            return ai_items[i].category == "" or of_items[j].category == ""
        return ai_items[i].category == of_items[j].category

    def _exact_match(cross_category: bool, need_remarks: bool = False,
                     need_price: bool = False):
        """正規化摘要（+備考 / +単価）の完全一致で貪欲に対応付ける。"""
        for i in list(ai_pool):
            for j in of_pool:
                if not _cat_ok(i, j, cross_category):
                    continue
                if ai_norm[i] != of_norm[j]:
                    continue
                if need_remarks and ai_rem[i] != of_rem[j]:
                    continue
                if need_price and ai_items[i].unit_price != of_items[j].unit_price:
                    continue
                pairs.append((ai_items[i], of_items[j], 1.0))
                ai_pool.remove(i)
                of_pool.remove(j)
                break

    def _fuzzy_match(cross_category: bool):
        """類似度 >= 0.6 の候補をスコア降順で貪欲一意割当する。"""
        candidates = []
        for i in ai_pool:
            for j in of_pool:
                if not _cat_ok(i, j, cross_category):
                    continue
                ratio = difflib.SequenceMatcher(None, ai_norm[i], of_norm[j]).ratio()
                if ratio >= _FUZZY_THRESHOLD:
                    candidates.append((ratio, i, j))
        candidates.sort(key=lambda t: (-t[0], t[1], t[2]))
        used_ai, used_of = set(), set()
        for ratio, i, j in candidates:
            if i in used_ai or j in used_of:
                continue
            used_ai.add(i)
            used_of.add(j)
            pairs.append((ai_items[i], of_items[j], ratio))
        ai_pool[:] = [i for i in ai_pool if i not in used_ai]
        of_pool[:] = [j for j in of_pool if j not in used_of]

    # ① 同カテゴリ内 (摘要, 備考) 完全一致
    _exact_match(cross_category=False, need_remarks=True)
    # ② 同カテゴリ内 摘要一致 + 単価一致（タイブレーク）
    _exact_match(cross_category=False, need_price=True)
    # ③ 同カテゴリ内 摘要完全一致
    _exact_match(cross_category=False)
    # ④ 同カテゴリ内 fuzzy
    _fuzzy_match(cross_category=False)
    # ⑤ カテゴリ不明("")は全カテゴリ相手に（①〜④と同順）
    _exact_match(cross_category=True, need_remarks=True)
    _exact_match(cross_category=True, need_price=True)
    _exact_match(cross_category=True)
    _fuzzy_match(cross_category=True)

    rest_ai = [ai_items[i] for i in ai_pool]
    rest_of = [of_items[j] for j in of_pool]
    return pairs, rest_ai, rest_of


# =============================================================
# 公開関数
# =============================================================

def diff_estimates(ai: ParsedEstimate, official: ParsedEstimate) -> list:
    """AI見積と正規見積の差分を抽出する。

    Args:
        ai: AI生成版の ParsedEstimate（履歴/CSV/PDF由来）。
        official: 正規版（人が修正した最終版）の ParsedEstimate。

    Returns:
        list[EstimateDiffItem]。学習可能な差分には proposed_rule が付く。
    """
    ai_items = [it for it in (ai.items or []) if _is_target_item(it)]
    of_items = [it for it in (official.items or []) if _is_target_item(it)]

    # 同一カテゴリ内の同名項目（正規化摘要の重複）検出。摘要キーの学習ルールは
    # 同名全項目に波及するため（材料費「PVケーブル間」×5等）、重複に関わる
    # price_changed / item_removed は原則参考表示に落とす
    def _desc_key(it) -> tuple:
        return (it.category, normalize_desc(it.description))

    def _full_key(it) -> tuple:
        return (it.category, normalize_desc(it.description),
                normalize_desc(it.remarks))

    ai_desc_count = Counter(_desc_key(it) for it in ai_items)
    of_desc_count = Counter(_desc_key(it) for it in of_items)
    ai_full_count = Counter(_full_key(it) for it in ai_items)
    of_full_count = Counter(_full_key(it) for it in of_items)

    pairs, rest_ai, rest_of = _match_items(ai_items, of_items)
    diffs: list = []

    # 同一承認バッチ内で同キー (kind, category, match_description, match_remarks)
    # の提案が複数生まれると store 側の dedup で先勝ち以外が無警告に消えるため、
    # 2件目以降は参考表示に落とす（drawing_diff._face_rule と同じ発想）
    proposed_keys: dict = {}

    def _propose(kind: str, category: str, match_desc: str,
                 match_remarks: str, payload: dict):
        """ルール提案。同キー競合は参考表示（learnable=False）に落とす。"""
        key = (kind, category, match_desc, match_remarks)
        if key in proposed_keys:
            if proposed_keys[key] == payload:
                return False, "（同内容の学習提案が既にあるため省略）"
            return False, "（同種ルールの提案が競合するため参考表示。別々に学習してください）"
        proposed_keys[key] = payload
        return True, ""

    # --- マッチしたペア: 単価差分（学習可能）・数量差分（参考表示） ---
    for a, o, score in pairs:
        category = o.category or a.category
        # 学習ルールの照合先は pricing_rules.yaml（＝AI見積の生成元）のため、
        # match_description / match_remarks はAI側を正規化した値を使う
        desc = a.description or o.description
        match_desc = normalize_desc(desc)
        match_remarks = normalize_desc(a.remarks)

        # 単価差分（支給品は常に¥0のため学習対象外）
        #
        # 数量単位が異なるペア（例: AI「266枚×¥2,178」と正規「1式 ¥1,112,400」）は
        # 両者の「単価」の基準が違うため、正規側の単価をそのまま学習してはいけない
        # （枚数連動項目に式単価が入り 266枚×¥1,112,400=¥2.96億 の事故になる）。
        # 正規側の金額をAI側の数量で割り、AI側の単位あたり単価に換算して学習する。
        # 換算に必要な情報が無い場合は参考表示に落とす。
        a_unit = (a.quantity_unit or "").strip()
        o_unit = (o.quantity_unit or "").strip()
        unit_mismatch = bool(a_unit and o_unit and a_unit != o_unit)
        o_price = o.unit_price
        conv_note = ""
        if unit_mismatch:
            # 一式行は単価欄が空欄で金額のみのことが多いため、正規側の
            # 単価の有無にかかわらず金額があれば換算する。
            # 金額0以下は換算しない（案件固有の¥0行・値引行を恒久学習しない）
            if o.amount is not None and o.amount > 0 and a.quantity_value \
                    and a.quantity_value > 0:
                o_price = int(round(o.amount / a.quantity_value))
                o_qty_disp = (f"{_fmt_qty(o.quantity_value)}{o_unit} "
                              if o.quantity_value is not None else "")
                conv_note = (f"（正規 {o_qty_disp}"
                             f"¥{o.amount:,} ÷ {_fmt_qty(a.quantity_value)}{a_unit}"
                             f" で1{a_unit}単価に換算）")
            else:
                o_price = None  # 換算不能 → 下の参考表示分岐へ
        if category != "支給品" and unit_mismatch and o_price is None \
                and a.unit_price is not None and o.unit_price is not None \
                and a.unit_price != o.unit_price:
            diffs.append(EstimateDiffItem(
                diff_type="price_changed",
                category=category,
                description=desc,
                ai_item=a,
                official_item=o,
                match_score=score,
                summary=(f"単価 ¥{a.unit_price:,}/{a_unit} → "
                         f"¥{o.unit_price:,}/{o_unit}"
                         "（数量単位が異なり換算もできないため参考表示）"),
                learnable=False,
                proposed_rule=None,
            ))
        elif category != "支給品" \
                and a.unit_price is not None and o_price is not None \
                and a.unit_price != o_price:
            summary = f"単価 ¥{a.unit_price:,} → ¥{o_price:,}{conv_note}"
            # 単価の基準単位。AI側が単位を落とした場合は正規側で補う
            # （空文字を入れると apply 側の単位照合が正当な単価まで弾くため）
            payload = {"unit_price": o_price, "old_unit_price": a.unit_price,
                       "basis_quantity_unit": a_unit or o_unit}
            # 同名項目が複数ある場合、備考まで完全一致でペアリングでき、かつ
            # (カテゴリ, 摘要, 備考) が両側で一意なペアのみ学習可能とする
            has_dup = (ai_desc_count[_desc_key(a)] > 1
                       or of_desc_count[_desc_key(o)] > 1)
            remarks_matched = \
                normalize_desc(a.remarks) == normalize_desc(o.remarks)
            full_unique = (ai_full_count[_full_key(a)] == 1
                           and of_full_count[_full_key(o)] == 1)
            if has_dup and not (remarks_matched and full_unique):
                diffs.append(EstimateDiffItem(
                    diff_type="price_changed",
                    category=category,
                    description=desc,
                    ai_item=a,
                    official_item=o,
                    match_score=score,
                    summary=summary + _DUP_NOTE,
                    learnable=False,
                    proposed_rule=None,
                ))
            else:
                ok, note = _propose("unit_price_override", category,
                                    match_desc, match_remarks, payload)
                diffs.append(EstimateDiffItem(
                    diff_type="price_changed",
                    category=category,
                    description=desc,
                    ai_item=a,
                    official_item=o,
                    match_score=score,
                    summary=summary + note,
                    learnable=ok,
                    proposed_rule=_make_rule(
                        "unit_price_override", category, match_desc,
                        desc, payload, summary, official,
                        match_remarks=match_remarks) if ok else None,
                ))

        # 数量差分（案件固有 → 参考表示のみ）
        qa, qo = a.quantity_value, o.quantity_value
        if qa is not None and qo is not None and abs(qa - qo) > _EPS:
            # 単位は両側それぞれの表記で表示する（「266式 → 1式」の誤表示防止）
            unit = o.quantity_unit or a.quantity_unit
            ua = a.quantity_unit or unit
            uo = o.quantity_unit or unit
            summary = (f"数量 {_fmt_qty(qa)}{ua} → {_fmt_qty(qo)}{uo}"
                       f"（案件固有のため参考表示）")
            diffs.append(EstimateDiffItem(
                diff_type="quantity_changed",
                category=category,
                description=desc,
                ai_item=a,
                official_item=o,
                match_score=score,
                summary=summary,
                learnable=False,
                proposed_rule=None,
            ))

    # --- 正規のみに存在 → item_added（kind=item_add） ---
    for o in rest_of:
        category = o.category
        match_desc = normalize_desc(o.description)
        match_remarks = normalize_desc(o.remarks)
        qty_disp = f"{_fmt_qty(o.quantity_value)}{o.quantity_unit}" \
            if o.quantity_value is not None else ""
        price_disp = f"¥{o.unit_price:,}" if o.unit_price is not None else ""
        detail = "・".join(x for x in (qty_disp, price_disp) if x)
        summary = f"項目追加 「{o.description}」" + (f"（{detail}）" if detail else "")

        # 支給品・材料費は項目構成を学習で変えない（2026-08-13 顧客要望）。
        # 例外案件（材料が多い/支給品が無い等）の構成がデフォルト化する事故を防ぐ。
        # 項目の載せ替えは見積プレビューの「支給品の選択」で案件ごとに行い、
        # 学習は単価（unit_price_override）のみを対象とする。
        if category in _FIXED_STRUCTURE_CATEGORIES:
            diffs.append(EstimateDiffItem(
                diff_type="item_added",
                category=category,
                description=o.description,
                official_item=o,
                summary=summary + "（支給品・材料費の項目構成は学習で変更しません。"
                                  "単価のみ学習対象です）",
                learnable=False,
                proposed_rule=None,
            ))
            continue

        if category not in _ADDABLE_CATEGORIES:
            # 反映先リストを特定できない → 参考表示
            reason = "（特記事項は参考表示）" if category == "特記事項" \
                else "（カテゴリ不明のため参考表示）"
            diffs.append(EstimateDiffItem(
                diff_type="item_added",
                category=category,
                description=o.description,
                official_item=o,
                summary=summary + reason,
                learnable=False,
                proposed_rule=None,
            ))
            continue

        # 単価欄が空（金額のみ記載）の追加項目は 金額÷数量 で単価を補完。
        # 補完もできない場合は apply で¥0項目として登録される事故を防ぐため参考表示
        unit_price = o.unit_price
        completion_note = ""
        if unit_price is None:
            if o.amount:
                qty = o.quantity_value
                # 数量が正なら端数（0.5式等）もそのまま割る。数量不明は金額=単価とみなす
                unit_price = round(o.amount / qty) if qty and qty > 0 else o.amount
                completion_note = "（単価は金額÷数量で補完）"
            else:
                diffs.append(EstimateDiffItem(
                    diff_type="item_added",
                    category=category,
                    description=o.description,
                    official_item=o,
                    summary=summary + "（単価・金額が不明のため参考表示）",
                    learnable=False,
                    proposed_rule=None,
                ))
                continue

        payload = {
            "category": category,
            "description": o.description,
            "remarks": o.remarks,
            "quantity_value": o.quantity_value,
            "quantity_unit": o.quantity_unit,
            "unit_price": unit_price,
        }
        ok, note = _propose("item_add", category, match_desc, match_remarks, payload)
        diffs.append(EstimateDiffItem(
            diff_type="item_added",
            category=category,
            description=o.description,
            official_item=o,
            summary=summary + completion_note + note,
            learnable=ok,
            proposed_rule=_make_rule(
                "item_add", category, match_desc,
                o.description, payload, summary, official,
                match_remarks=match_remarks) if ok else None,
        ))

    # --- AIのみに存在 → item_removed（kind=item_suppress） ---
    for a in rest_ai:
        category = a.category
        match_desc = normalize_desc(a.description)
        match_remarks = normalize_desc(a.remarks)
        summary = f"項目削除 「{a.description}」（AI見積のみに存在）"

        # 支給品・材料費は項目構成を学習で変えない（2026-08-13 顧客要望。
        # 「支給品が全く無い」例外案件の学習で既定の支給品が消える事故を防ぐ）
        if category in _FIXED_STRUCTURE_CATEGORIES:
            diffs.append(EstimateDiffItem(
                diff_type="item_removed",
                category=category,
                description=a.description,
                ai_item=a,
                summary=summary + "（支給品・材料費の項目構成は学習で変更しません。"
                                  "単価のみ学習対象です）",
                learnable=False,
                proposed_rule=None,
            ))
            continue

        if category == "特記事項":
            # 特記事項は pricing_rules.yaml に対応リストが無い → 参考表示
            diffs.append(EstimateDiffItem(
                diff_type="item_removed",
                category=category,
                description=a.description,
                ai_item=a,
                summary=summary + "（特記事項は参考表示）",
                learnable=False,
                proposed_rule=None,
            ))
            continue

        # 金額・単価がともに0/Noneの行は条件不成立・手動入力前提の項目
        # （クレーン費・外部足場等）。恒久サプレスの誤学習を防ぐため参考表示
        if not a.amount and not a.unit_price:
            diffs.append(EstimateDiffItem(
                diff_type="item_removed",
                category=category,
                description=a.description,
                ai_item=a,
                summary=summary + "（¥0行（条件付き・手動入力項目）のため参考）",
                learnable=False,
                proposed_rule=None,
            ))
            continue

        # 同名項目が複数あると suppress が同名全項目に波及するため参考表示
        if (ai_desc_count[(category, match_desc)] > 1
                or of_desc_count[(category, match_desc)] > 1):
            diffs.append(EstimateDiffItem(
                diff_type="item_removed",
                category=category,
                description=a.description,
                ai_item=a,
                summary=summary + _DUP_NOTE,
                learnable=False,
                proposed_rule=None,
            ))
            continue

        payload = {"old_unit_price": a.unit_price}
        ok, note = _propose("item_suppress", category, match_desc,
                            match_remarks, payload)
        diffs.append(EstimateDiffItem(
            diff_type="item_removed",
            category=category,
            description=a.description,
            ai_item=a,
            summary=summary + note,
            learnable=ok,
            proposed_rule=_make_rule(
                "item_suppress", category, match_desc,
                a.description, payload, summary, official,
                match_remarks=match_remarks) if ok else None,
        ))

    return diffs
