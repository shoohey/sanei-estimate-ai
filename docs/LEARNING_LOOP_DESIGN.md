# v2.6 差分学習ループ 設計書

作成日: 2026-07-14

## 1. 目的

お客様（サンエー）自身が **AI見積もり** と **正規見積もり**（人が修正した最終版）をアップロードし、
差分を自動抽出 → 人が確認・承認 → 学習データとして蓄積 → 次回の見積もり生成・図面作成に自動反映する。
見積もり側・図面（簡易製図AI）側の両方に対応する。

## 2. 全体アーキテクチャ

```
[学習センター（新モード input_mode="learning"）]
  Step1: 学習タイプ選択 + アップロード
    ├─ 見積の学習: AI見積（履歴/CSV/PDF） + 正規見積PDF
    └─ 図面の学習: AI図面スペック（履歴/JSON） + 正規図面PDF
  Step2: 差分確認・承認（チェックボックスで選択承認）
  Step3: 学習完了 + 学習済みルール管理（有効/無効・削除）

[学習ストア]  knowledge/learned_estimate_rules.json
              knowledge/learned_drawing_rules.json
              knowledge/learning_history.json（学習セッションログ）

[自動反映]
  見積: pricing/knowledge_base.load_pricing_rules() 内で
        learning.apply_estimate.apply_learned_rules(rules) をマージ
        → 単価上書き / 項目追加 / 項目抑止 がエンジン無改修で全カテゴリに効く
  図面: drafting/app_pages._generate_drawing() 内で
        learning.apply_drawing.apply_learned_drawing_rules(spec) を place_panels 前に適用
        + spec_extractor の few-shot に学習済みゴールデン例を注入

[履歴自動保存（学習の材料）]
  data/estimate_history/  … PDF生成成功時に EstimateData をJSON保存
  data/drawing_history/   … 製図生成成功時に spec dict をJSON保存
```

## 3. 設計原則

1. **学習系の失敗は本体フローを止めない**: 本体（見積生成・製図）からの全フックは try/except で保護。
2. **数量差分は自動学習しない**: 数量は案件固有（PV容量・距離で変わる）のため「参考表示」のみ。
   誤学習で全案件の数量が壊れることを防ぐ。単価・項目有無・図面規約（gap/margin/向き）のみ学習。
3. **手動編集を尊重**: 図面ルールは「値が既定値のままの場合のみ」上書き（gap_long=25, gap_short=10, margin=500, orientation=AUTO）。
4. **承認なしに学習しない**: 差分は必ず人がStep2で承認したものだけ store に入る。
5. **既存UIパターン踏襲**: drafting モードと同じ「モードカード + 別系統ステップ + `learning_` プレフィクスのセッションキー」。

## 4. モジュール構成とファイル所有権

| ファイル | 内容 | 担当 |
|---|---|---|
| `learning/__init__.py` | パッケージ | 本体 |
| `learning/models.py` | ParsedEstimate / EstimateDiffItem / DrawingDiffItem 等 | 本体（契約） |
| `learning/store.py` | 学習ルールJSONストア（atomic write） | 本体（契約） |
| `learning/history.py` | 見積・図面の履歴保存/一覧/読込 | 本体（契約） |
| `learning/estimate_parser.py` | 見積PDF/CSV → ParsedEstimate | Agent B |
| `learning/estimate_diff.py` | 明細マッチング + 差分抽出 | Agent C |
| `learning/apply_estimate.py` | pricing rules への学習反映 | Agent C |
| `pricing/knowledge_base.py` | load_pricing_rules にフック追加（小改修） | Agent C |
| `learning/drawing_diff.py` | DraftingSpec 差分抽出 | Agent D |
| `learning/apply_drawing.py` | spec への学習反映 + few-shot提供 | Agent D |
| `drafting/spec_extractor.py` | 学習済みゴールデン例の few-shot 注入（小改修） | Agent D |
| `drafting/app_pages.py` | _generate_drawing フック + 履歴保存（小改修） | Agent D |
| `learning/app_pages.py` | 学習センターUI（モードカード + Step1-3） | Agent E |
| `app.py` | ルーター/インジケーター/カード/履歴保存フック/v2.6（小改修） | Agent E |
| `tests/test_learning_estimate.py` | 差分・適用のテスト（API不要・スクリプト式） | Agent C |
| `tests/test_learning_drawing.py` | 図面差分・適用のテスト（API不要） | Agent D |

## 5. データモデル（learning/models.py — 契約、変更禁止）

```python
class ParsedLineItem(BaseModel):
    category: str = ""            # "支給品"|"材料費"|"施工費"|"その他・諸経費等"|"付帯工事"|"特記事項"|""
    no: int = 0
    description: str = ""
    remarks: str = ""
    quantity_value: Optional[float] = None
    quantity_unit: str = ""
    unit_price: Optional[int] = None
    amount: Optional[int] = None

class ParsedEstimate(BaseModel):
    source: str = ""              # "ai" | "official"
    origin: str = ""              # "history" | "csv" | "pdf"
    file_name: str = ""
    estimate_id: str = ""
    client_name: str = ""
    project_name: str = ""
    issue_date: str = ""
    items: list[ParsedLineItem] = []
    subtotal: Optional[int] = None
    discount: Optional[int] = None
    total_before_tax: Optional[int] = None
    tax: Optional[int] = None
    total_with_tax: Optional[int] = None
    warnings: list[str] = []

class EstimateDiffItem(BaseModel):
    diff_type: str                # "price_changed"|"quantity_changed"|"item_added"|"item_removed"
    category: str = ""
    description: str = ""
    ai_item: Optional[ParsedLineItem] = None
    official_item: Optional[ParsedLineItem] = None
    match_score: float = 0.0
    summary: str = ""             # 例: "単価 ¥3,300 → ¥3,100"
    learnable: bool = True        # quantity_changed は False（参考表示のみ）
    proposed_rule: Optional[dict] = None   # 承認時そのまま store.add_rules へ渡す LearnedRule dict

class DrawingDiffItem(BaseModel):
    diff_type: str    # "gap_changed"|"margin_changed"|"orientation_changed"|"panel_count_changed"|
                      # "string_config_changed"|"mount_type_changed"|"panel_spec_changed"|"face_dimension_changed"
    target: str = ""              # 対象面名など
    ai_value: str = ""
    official_value: str = ""
    summary: str = ""
    learnable: bool = True        # panel_count / face_dimension は False（案件固有）
    proposed_rule: Optional[dict] = None
```

## 6. 学習ルール スキーマ（storeに保存する dict）

```json
{
  "id": "er-20260714120000-1",
  "target": "estimate",
  "kind": "unit_price_override",
  "category": "材料費",
  "match_description": "けーぶるはいせんこうじ",
  "display_description": "ケーブル配線工事",
  "payload": {"unit_price": 3100, "old_unit_price": 3300},
  "evidence": {"project_name": "○○店", "learned_at": "2026-07-14 12:00",
               "source_file": "正規見積.pdf", "note": "単価 ¥3,300 → ¥3,100"},
  "enabled": true,
  "applied_count": 0
}
```

kind と payload:

| target | kind | payload | 適用 |
|---|---|---|---|
| estimate | `unit_price_override` | `{unit_price, old_unit_price}` | 一致項目の unit_price 差替え |
| estimate | `item_add` | `{category, description, remarks, quantity_value, quantity_unit, unit_price}` | カテゴリ末尾に fixed 項目追加 |
| estimate | `item_suppress` | `{}` | 一致項目をルールから除去 |
| drawing | `gap_override` | `{gap_long_mm?, gap_short_mm?, roof_type}` | roof_type一致面のパネル間隔（roof_type="*"は全て） |
| drawing | `margin_override` | `{margin_mm, roof_type}` | roof_type一致面のマージン |
| drawing | `orientation_preference` | `{orientation, roof_type}` | AUTO面の向き既定 |
| drawing | `golden_example` | `{name, spec}` | spec_extractor few-shot に注入 |

一意キー: `(target, kind, category, match_description)`（drawingは `(target, kind, payload.roof_type)`、
golden_example は `(target, kind, payload.name)`）。同キーの再学習は**上書き更新**（evidence更新）。

## 7. ストアAPI（learning/store.py — 契約）

```python
ESTIMATE_RULES_PATH = KNOWLEDGE_DIR / "learned_estimate_rules.json"
DRAWING_RULES_PATH  = KNOWLEDGE_DIR / "learned_drawing_rules.json"
LEARNING_LOG_PATH   = KNOWLEDGE_DIR / "learning_history.json"

def load_rules(target: str) -> list[dict]            # 無ければ []。破損時は [] + 警告ログ
def add_rules(target: str, new_rules: list[dict]) -> list[dict]  # ID採番・同キー上書き・atomic保存
def set_rule_enabled(target: str, rule_id: str, enabled: bool) -> None
def delete_rule(target: str, rule_id: str) -> None
def enabled_rules(target: str) -> list[dict]
def append_learning_log(entry: dict) -> None
def load_learning_log() -> list[dict]
```

## 8. 履歴API（learning/history.py — 契約）

```python
ESTIMATE_HISTORY_DIR = BASE_DIR / "data" / "estimate_history"
DRAWING_HISTORY_DIR  = BASE_DIR / "data" / "drawing_history"

def save_estimate_history(estimate: EstimateData) -> Optional[Path]   # 例外を飲み None
def list_estimate_history() -> list[dict]   # 新しい順 [{path,estimate_id,client_name,project_name,saved_at,total_with_tax}]
def load_estimate_history(path: str | Path) -> Optional[EstimateData]
def estimate_to_parsed(estimate: EstimateData, file_name: str = "") -> ParsedEstimate  # source="ai", origin="history"
def save_drawing_history(spec_dict: dict) -> Optional[Path]
def list_drawing_history() -> list[dict]    # [{path,customer_name,drawing_type,total_panels,total_kw,saved_at}]
def load_drawing_history(path: str | Path) -> Optional[dict]
```

`.gitignore` に `data/` を追加（顧客データはgit管理外）。

## 9. 見積パーサー（learning/estimate_parser.py — Agent B）

```python
def parse_estimate_pdf(pdf_path: str | Path, source: str) -> ParsedEstimate
def parse_estimate_csv(csv_bytes: bytes, source: str, file_name: str = "") -> ParsedEstimate
```

- **PDF**: まず PyMuPDF `page.get_text()` でテキスト抽出。総文字数が十分（>300字）なら
  「テキスト → Claude で構造化JSON」（安価・高精度）。乏しければ（スキャンPDF）
  `extraction.pdf_reader.pdf_to_images`（`use_image_enhancement=False`）+ Vision にフォールバック。
- Claude 呼び出し: `config.CLAUDE_MODEL` / temperature=0 / max_tokens=8192 /
  リトライ3回（`extraction/survey_extractor.py` のパターン踏襲）。
- JSON修復は `extraction.survey_extractor._extract_json` / `_sanitize_json_str` を import して流用。
  金額・数量パースは `_safe_int` / `_safe_float` を流用。
- プロンプト: サンエー見積書フォーマット（6カテゴリ: 1.支給品/2.材料費/3.施工費/4.その他・諸経費等/
  5.付帯工事/6.特記事項）を明記し、カテゴリ名を上記6種に正規化させる。判別不能は "" のまま。
  値引き（お値引き/出精値引き等）は明細でなく `discount`（負値）へ。
  小計・税・税込合計も抽出し、明細合計との整合チェック → 不整合は warnings に追記。
- **CSV**: 本ツールの `export_estimate_to_csv_detailed`（`=== 見積明細 ===` セクション形式）と
  simple形式（ヘッダー `カテゴリ,No,摘要,備考,数量,単価,金額`）を自動判別。BOM/CRLF対応。API不要。

## 10. 差分エンジン（learning/estimate_diff.py — Agent C）

```python
def normalize_desc(s: str) -> str
def diff_estimates(ai: ParsedEstimate, official: ParsedEstimate) -> list[EstimateDiffItem]
```

- `normalize_desc`: NFKC → 小文字 → 空白/記号（・、。()（）等）除去。
- マッチング: ①同カテゴリ内で normalize_desc 完全一致 → ②同カテゴリ内 `difflib.SequenceMatcher.ratio() >= 0.6`
  の最良候補（スコア降順の貪欲一意割当）→ ③カテゴリ不明("")の項目は全カテゴリ相手に同様。
- マッチしたペア:
  - unit_price が両方 non-None かつ不一致 → `price_changed`（支給品カテゴリは除外）
  - quantity_value 不一致（>1e-6）→ `quantity_changed`（learnable=False, proposed_rule=None）
- 正規のみに存在 → `item_added`（proposed_rule: kind=item_add、正規側の単価・数量を payload に）
- AIのみに存在 → `item_removed`（proposed_rule: kind=item_suppress）
- 値引き行・小計行・合計行は明細差分の対象外（パーサー側で items に入れない前提だが防御的に除外）。
- proposed_rule の evidence には official の project_name / file_name / summary を格納。

## 11. 見積への学習反映（learning/apply_estimate.py — Agent C）

```python
def apply_learned_rules(rules: dict) -> dict      # pricing_rules.yaml ロード結果に適用して返す
def learned_rules_summary() -> dict               # UI表示用 {"total": n, "price": n, "add": n, "suppress": n}
```

- 対象リスト: `supplied_items` / `material_items` / `construction_items` / `overhead_items` / `additional_items`。
  カテゴリ名→リスト名のマップ: 支給品→supplied_items, 材料費→material_items, 施工費→construction_items,
  その他・諸経費等→overhead_items, 付帯工事→additional_items。
- `unit_price_override`: category のリスト内（category不明ならば全リスト）で
  normalize_desc(description) 一致項目の `unit_price` を差替え、`note` に "（学習補正: ¥X→¥Y）" 追記。
  `pricing_method` が `lump_formula` の項目は対象外（unit_price 不使用のため）。
- `item_suppress`: 一致項目をリストから除去。
- `item_add`: 対象リスト末尾に
  `{"no": 最大no+1, "description", "remarks", "quantity": 数量値, "quantity_unit", "unit_price",
    "pricing_method": "fixed", "note": "学習により追加（○○案件の正規見積より）"}` を追加。
- 入力 rules は **deepcopy してから変更**（YAMLロード結果の破壊防止）。
- `pricing/knowledge_base.load_pricing_rules()` の末尾にフック（try/except で全例外を握る）:

```python
    try:
        from learning.apply_estimate import apply_learned_rules
        rules = apply_learned_rules(rules)
    except Exception:
        pass  # 学習ストア破損時も見積生成は止めない
    return rules
```

## 12. 図面差分（learning/drawing_diff.py — Agent D)

```python
def diff_drawing_specs(ai_spec: dict, official_spec: dict) -> list[DrawingDiffItem]
```

- 入力は `spec_to_dict` 形式の dict（履歴 or `extract_drafting_spec` の結果）。
- 面の対応付け: name 完全一致 → 順序フォールバック。
- 比較項目:
  - panel.gap_long_mm / gap_short_mm → `gap_changed`（proposed_rule: gap_override, roof_type=面のroof_type。
    複数面で共通なら roof_type="*"）
  - face.margin_mm → `margin_changed`（margin_override）
  - face.orientation（配置結果ベース: 実際に置かれた向き）→ `orientation_changed`（orientation_preference）
  - face.panel_count / rows / cols → `panel_count_changed`（learnable=False）
  - face.width_mm / depth_mm（±2%超）→ `face_dimension_changed`（learnable=False）
  - mount_type → `mount_type_changed`（learnable=False, 参考）
  - strings[].config_text → `string_config_changed`（learnable=False, 参考）
  - panel.model / output_w → `panel_spec_changed`（learnable=False, 参考）
- 追加で必ず1件: `diff_type="golden_example"` 相当の提案として、official_spec 全体を
  few-shot お手本として登録する DrawingDiffItem（diff_type="golden_example", learnable=True,
  proposed_rule: kind=golden_example, payload={name: 顧客名+図面種別, spec: official_spec}）。

## 13. 図面への学習反映（learning/apply_drawing.py — Agent D）

```python
def apply_learned_drawing_rules(spec) -> tuple[spec, list[str]]   # DraftingSpec を受けて返す
def learned_golden_examples(limit: int = 2) -> list[dict]         # enabled の golden_example payload
```

- **既定値のままの場合のみ**上書き（手動編集尊重）:
  - `spec.panel.gap_long_mm == 25` → learned 値 / `gap_short_mm == 10` → learned 値
  - 各面 `margin_mm == 500` かつ roof_type 一致 → learned margin
  - 各面 `orientation == AUTO` かつ roof_type 一致 → learned orientation
- 適用した内容の日本語説明リストを返す（例: "折板屋根のマージン 500→300mm（学習値）"）。
- フック（drafting/app_pages.py `_generate_drawing`）: `spec_from_dict(d)` 直後に guarded 適用、
  適用内容があれば `st.caption("🧠 学習済みルール適用: ...")` 表示。
  render 成功後に `save_drawing_history(spec_to_dict(spec))` を guarded 実行。
- spec_extractor: `build_user_prompt` のゴールデン例ブロックの後に、guarded で
  `learned_golden_examples(2)` の JSON を「実案件の正解例」として追記（プロンプト肥大に注意し2件まで）。

## 14. UI（learning/app_pages.py — Agent E）

セッションキー（`learning_` プレフィクス）:
`learning_kind`("estimate"|"drawing") / `learning_ai_parsed` / `learning_official_parsed`（model_dumpのdict）/
`learning_diffs`（list[dict]）/ `learning_saved_count` / `learning_tmp_paths`

```python
def render_mode_card()          # 🧠 学習センター カード（drafting の render_mode_card と同型・key="start_learning_btn"）
def learning_step_names()       # ["入力方法", "アップロード", "差分確認・学習", "完了"]
def render_step1_upload()
def render_step2_review()
def render_step3_done()
def init_learning_session()
```

- **Step1**: `st.tabs(["📊 見積の学習", "📐 図面の学習"])`
  - 見積タブ: AI見積の入力方法を radio（「保存履歴から選択」/「CSVアップロード」/「PDFアップロード」）
    + 正規見積 PDF uploader。「差分を抽出」ボタン → parser 実行（spinner）→ diff_estimates → step2。
  - 図面タブ: AI図面（「保存履歴から選択」/「スペックJSONアップロード」）+ 正規図面 PDF uploader
    + 図面種別 radio。「差分を抽出」→ `extract_drafting_spec`（正規図面）→ diff_drawing_specs → step2。
  - ページ下部に「🗂 学習済みルールの管理」expander（一覧・有効/無効トグル・削除・学習ログ）。
- **Step2**: 差分をグループ表示（学習可能: checkbox付き・既定ON / 参考: checkboxなし）。
  各行: カテゴリ/対象・摘要・AI値 → 正規値・提案ルールの説明。全選択/全解除ボタン。
  「✅ 選択した差分を学習する」→ 承認された proposed_rule を `store.add_rules` + `append_learning_log`
  → step3。「← 戻る」でstep1。
- **Step3**: 学習完了サマリ（N件学習、内訳）+ 現在の学習済みルール一覧 + 「別のファイルで続けて学習」
  「モード選択へ戻る」。

app.py 側（Agent E）:
1. `import learning.app_pages as learning_pages`（app.py:40 付近）
2. Step0 のカード列を4枚に: `st.columns([2, 0.25, 2, 0.25, 2, 0.25, 2])`、4列目で
   `learning_pages.render_mode_card()`
3. ルーター（app.py:511-534）に drafting と同型の learning 分岐（step1=upload, 2=review, 3=done）
4. `_render_step_indicator` に learning 分岐（`learning_pages.learning_step_names()`）
5. PDF生成成功後（app.py:2221-2222 の `generate_pdf` 直後）に guarded で
   `learning.history.save_estimate_history(estimate)` + Step4 に「学習用に履歴保存済み」caption
6. ヘッダーバッジ v2.5 → v2.6
7. Step3（見積プレビュー）冒頭に、learned_rules_summary() の enabled 件数が1以上なら
   `st.caption("🧠 学習済みルール N件が単価・項目に反映されています")` を guarded 表示

## 15. テスト（API不要・スクリプト式 `python tests/test_learning_*.py` で実行、assert方式）

- test_learning_estimate.py: normalize_desc 表記揺れ / diff（単価変更・追加・削除・数量参考・
  fuzzy一致）/ apply_learned_rules（override・add・suppress・lump_formula除外・deepcopy非破壊）/
  store roundtrip（tmpdir に monkeypatch）
- test_learning_drawing.py: golden sample spec を改変して diff 検出 / apply（既定値のみ上書き・
  手動値尊重・roof_type条件）/ golden_example 提案

## 16. 将来拡張（今回スコープ外）

- 正規見積 Excel(xlsx) 直接対応（ground_truth.py のラベル検索方式を流用）
- 現調シート抽出（SurveyData）の差分学習（compare_survey_data 転用）
- 学習ルールの条件付き適用（案件規模・メーカー別）
- 図面PDF（AI生成側）の直接アップロード対応
